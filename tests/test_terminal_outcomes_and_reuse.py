"""Outcomes no retry can change, and work already paid for.

* An all-empty STT chain (stapel-agent >= 0.37.0 classifies it
  ``failure_class: "input"``) is fatal: a client host re-drove a ten-minute
  file of non-speech four times and paid both providers on every pass.
* :func:`pipeline.close_recording` takes a recording out of the retry ladder
  when a person decided it cannot be served.
* A run whose charge the duplicate guard waived reuses the original's
  transcript instead of paying the provider to hear the same bytes again.
* ``confidence_avg`` and ``processing_latency_ms`` are written (they were
  model fields nothing filled).
"""
import pytest
from django.utils import timezone

from stapel_recordings import duplicates, pipeline, services
from stapel_recordings.models import Recording, RecordingStatus, Segment, Speaker
from stapel_recordings.stages import (
    StageFatal,
    StageRetryable,
    TranscribeStage,
    raise_for_transcribe_failure,
)

pytestmark = pytest.mark.django_db

ALL_EMPTY = {
    "status": "failure",
    "reason": "all STT providers failed: a (empty): ...; b (empty): ...",
    "failure_class": "input",
    "reason_code": "empty",
    "provider_attempts": [
        {"provider": "a", "reason": "empty"},
        {"provider": "b", "reason": "empty"},
    ],
}


class TestTranscribeFailureVerdict:
    def test_an_all_empty_chain_is_fatal(self):
        with pytest.raises(StageFatal) as caught:
            raise_for_transcribe_failure(ALL_EMPTY)
        assert caught.value.reason == "empty_transcript"

    def test_another_input_refusal_is_fatal_too(self):
        with pytest.raises(StageFatal) as caught:
            raise_for_transcribe_failure({"status": "failure", "reason": "x", "failure_class": "input"})
        assert caught.value.reason == "transcribe_refused"

    @pytest.mark.parametrize("result", [
        {"status": "failure", "reason": "timeout"},
        {"status": "failure", "reason": "quota", "failure_class": "provider"},
        None,
    ])
    def test_everything_else_stays_retryable(self, result):
        with pytest.raises(StageRetryable):
            raise_for_transcribe_failure(result)

    def test_ok_is_not_a_failure(self):
        raise_for_transcribe_failure({"status": "ok"})

    def test_the_pipeline_does_not_re_drive_it(self, ready_recording, use_fakes, stub_transcribe):
        stub_transcribe.result = ALL_EMPTY
        rid = str(ready_recording.id)
        pipeline.run_stage(rid, 0)
        pipeline.run_stage(rid, 1)

        r = Recording.objects.get(pk=rid)
        assert r.status == RecordingStatus.ERROR
        assert r.workflow_state["last_error"]["reason"] == "empty_transcript"
        assert r.retry_count == 0
        assert len(stub_transcribe.calls) == 1


class TestCloseRecording:
    def test_a_queued_recording_in_the_retry_ladder_is_closed(self, make_recording):
        r = make_recording(status=RecordingStatus.QUEUED, retry_count=2)

        assert pipeline.close_recording(str(r.id), reason="no_speech", detail="staff") is True

        r.refresh_from_db()
        assert r.status == RecordingStatus.ERROR
        assert r.workflow_state["last_error"]["reason"] == "closed:no_speech"
        # Terminal: a re-drive is now a no-op.
        pipeline.run_stage(str(r.id), 1)
        r.refresh_from_db()
        assert r.status == RecordingStatus.ERROR

    @pytest.mark.parametrize("status", [
        RecordingStatus.COMPLETED, RecordingStatus.ERROR, RecordingStatus.NEEDS_PAYMENT,
        RecordingStatus.CREATED, RecordingStatus.UPLOADING,
    ])
    def test_terminal_and_client_held_recordings_are_left_alone(self, make_recording, status):
        r = make_recording(status=status)
        assert pipeline.close_recording(str(r.id), reason="no_speech") is False
        r.refresh_from_db()
        assert r.status == status

    def test_a_late_task_result_is_ignored(self, make_recording):
        r = make_recording(status=RecordingStatus.TRANSCRIBING)
        r.workflow_state = {"pipeline": {"stage": "transcribe", "awaiting": {
            "task_id": "t-1", "stage": "transcribe", "index": 1}}}
        r.save(update_fields=["workflow_state"])

        pipeline.close_recording(str(r.id), reason="source_lost")
        pipeline.resume_stage(str(r.id), "t-1", {"status": "ok"})

        r.refresh_from_db()
        assert r.status == RecordingStatus.ERROR
        assert "awaiting" not in r.workflow_state["pipeline"]


def _original(make_recording, user, **kwargs):
    original = make_recording(
        status=RecordingStatus.COMPLETED, owner=user, language="en",
        language_mode="manual", duration_seconds=60.0, provider_used="stub-asr",
        **kwargs,
    )
    sp = Speaker.objects.create(recording=original, label="speaker_0", display_name="Alice (renamed)")
    Segment.objects.create(
        recording=original, speaker=sp, sequence_num=0, start_time=0, end_time=2,
        text="edited by the owner", original_text="machine words", is_edited=True,
        confidence=0.5, word_count=2,
    )
    Segment.objects.create(
        recording=original, speaker=sp, sequence_num=1, start_time=2, end_time=4,
        text="second", confidence=0.7, word_count=1,
    )
    original.segments_count = 2
    original.save(update_fields=["segments_count"])
    return original


def _waived_repeat(make_recording, user, original, *, charge=duplicates.CHARGE_WAIVED, **kwargs):
    fields = dict(
        status=RecordingStatus.QUEUED, owner=user, language="en", language_mode="manual",
        duration_seconds=60.0, normalized_storage_key="k/audio",
    )
    fields.update(kwargs)
    r = make_recording(**fields)
    r.workflow_state = {"pipeline": {"run_id": "run-1", "attempt": 1}}
    r.metadata = {duplicates.METADATA_KEY: {
        "of": str(original.pk), "match": duplicates.MATCH_CONTENT, "charge": charge,
        "run_id": "run-1", "at": timezone.now().isoformat(),
    }}
    r.save(update_fields=["workflow_state", "metadata"])
    return r


class TestWaivedDuplicateReusesTheTranscript:
    def test_the_transcript_is_copied_and_no_provider_is_called(
        self, make_recording, user, use_fakes, stub_transcribe
    ):
        original = _original(make_recording, user)
        r = _waived_repeat(make_recording, user, original)

        TranscribeStage().run(r, {})

        assert stub_transcribe.calls == []
        # workflow_state is the driver's to save; read it off the instance.
        assert r.workflow_state["transcript_reused_from"] == {"of": str(original.pk), "run_id": "run-1"}
        meter = r.workflow_state["metering"]["transcribe"]
        assert meter["cost_usd"] == 0 and meter["cost_basis"] == "cached" and meter["calls"] == 0
        r.refresh_from_db()
        texts = list(r.segments.order_by("sequence_num").values_list("text", flat=True))
        # The machine transcript, not what the owner typed into the source.
        assert texts == ["machine words", "second"]
        (speaker,) = r.speakers.all()
        assert speaker.label == "speaker_0" and speaker.display_name is None
        assert r.segments_count == 2 and r.speakers_count == 1
        assert r.provider_used == "stub-asr"
        assert r.confidence_avg == pytest.approx(0.6)
        # The source is untouched.
        assert original.segments.get(sequence_num=0).text == "edited by the owner"

    @pytest.mark.parametrize("change", [
        {"charge": duplicates.CHARGE_CHARGED},
        {"language_mode": "auto"},
        {"provider_override": "other-asr"},
        {"diarization_enabled": False},
    ])
    def test_no_reuse_unless_waived_and_same_parameters(
        self, make_recording, user, use_fakes, stub_transcribe, change
    ):
        original = _original(make_recording, user)
        r = _waived_repeat(make_recording, user, original, **change)

        TranscribeStage().run(r, {})

        assert len(stub_transcribe.calls) == 1

    def test_no_reuse_for_another_run_or_a_deleted_original(
        self, make_recording, user, use_fakes, stub_transcribe
    ):
        original = _original(make_recording, user)
        r = _waived_repeat(make_recording, user, original)
        r.workflow_state = {"pipeline": {"run_id": "run-2", "attempt": 1}}
        r.save(update_fields=["workflow_state"])
        TranscribeStage().run(r, {})
        assert len(stub_transcribe.calls) == 1

        other = _waived_repeat(make_recording, user, original)
        original.deleted_at = timezone.now()
        original.save(update_fields=["deleted_at"])
        TranscribeStage().run(other, {})
        assert len(stub_transcribe.calls) == 2


class TestFieldsNothingWrote:
    def test_confidence_avg_is_the_mean_of_reported_confidences(
        self, make_recording, use_fakes, stub_transcribe
    ):
        r = make_recording(status=RecordingStatus.QUEUED, normalized_storage_key="k/audio")
        TranscribeStage().run(r, {})
        r.refresh_from_db()
        assert r.confidence_avg == pytest.approx(0.85)

    def test_processing_latency_is_written_on_completion(
        self, use_fakes, make_recording, stub_transcribe, stub_summarize, drain
    ):
        from stapel_recordings.storage import get_storage

        r = make_recording(status=RecordingStatus.CREATED)
        session = services.create_upload_session(recording=r, filename="take.mp3")
        get_storage().put_bytes(session.storage_key, b"audio", content_type="audio/mpeg")
        services.finalize_upload(session=session, file_size_bytes=5)
        drain()

        r.refresh_from_db()
        assert r.status == RecordingStatus.COMPLETED
        assert r.processing_latency_ms is not None and r.processing_latency_ms >= 0
