"""A stage result is reused only while its INPUT has not changed.

The production defect these were written against: a recording processed
under a trim, then re-queued in full after the customer paid for the whole
meeting, completed in 55 seconds without running a single stage. Every
stage found the artifact of the trimmed run — a normalized object, segment
rows, a transcript, a summary — and returned early on its existence. The
customer paid for two hours of transcription and kept ten minutes.

The mirror-image failure costs money in the other direction and is just as
easy to ship: a retry over an UNCHANGED input that re-buys the priced call
it already has an answer for. Both are asserted here, because a fix for one
that breaks the other is not a fix.
"""
import pytest

from stapel_recordings import checkpoints, events, pipeline
from stapel_recordings.models import Recording, Segment
from stapel_recordings.stages import TranscribeStage, stage_input_dedupe_key

pytestmark = pytest.mark.django_db


def _longer_transcript():
    """What a re-run over the FULL source comes back with: more of the
    meeting, and a duration that says so."""
    return {
        "status": "ok",
        "provider_used": "stub-asr",
        "fallback_used": False,
        "transcript": {
            "provider": "stub-asr",
            "language": "en",
            "duration_seconds": 7115.86,
            "words": [],
            "utterances": [
                {"text": f"turn {i}", "start": float(i), "end": float(i) + 1,
                 "speaker": "speaker_0", "confidence": 0.9, "word_indexes": []}
                for i in range(5)
            ],
            "speakers_detected": ["speaker_0"],
            "raw": {},
        },
    }


def _unlock(recording, new_source: bytes):
    """What an app layer does when the customer pays for the full meeting.

    The source object changes (the trim is gone), the checkpoints of the
    trimmed run are declared stale, and only then is the pipeline requeued.
    """
    from stapel_recordings.storage import get_storage

    get_storage().put_bytes(recording.file_storage_key, new_source)
    assert pipeline.invalidate_from(str(recording.id), "convert")
    assert pipeline.reprocess_recording(str(recording.id))


def test_a_rerun_over_a_changed_input_reruns_every_stage_exactly_once(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == "completed"
    assert len(stub_transcribe.calls) == 1
    first_summary_calls = len(stub_summarize.calls)

    stub_transcribe.result = _longer_transcript()
    _unlock(r, b"the-whole-meeting-this-time")
    drain()

    r.refresh_from_db()
    assert r.status == "completed"
    # The paid call was made again — ONCE — because the audio is different.
    assert len(stub_transcribe.calls) == 2
    assert stub_transcribe.calls[0]["audio_content_hash"] != \
        stub_transcribe.calls[1]["audio_content_hash"]
    # And every stage's artifact is the new run's, not the old one's.
    assert r.duration_seconds == pytest.approx(7115.86)
    assert r.segments_count == 5
    assert len(stub_summarize.calls) == first_summary_calls + 1


def test_the_old_transcript_is_replaced_and_never_mixed_into_the_new_one(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    """Segments of two runs of the same meeting are not a longer meeting."""
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert Segment.objects.filter(recording=r).count() == 2

    stub_transcribe.result = _longer_transcript()
    _unlock(r, b"the-whole-meeting-this-time")
    drain()

    r.refresh_from_db()
    texts = list(
        Segment.objects.filter(recording=r).order_by("sequence_num").values_list("text", flat=True)
    )
    assert texts == ["turn 0", "turn 1", "turn 2", "turn 3", "turn 4"]
    assert r.segments_count == len(texts)
    assert r.speakers.count() == 1  # the two speakers of the first run are gone


def test_the_summary_and_the_stored_transcript_follow_the_new_segments(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)

    stub_transcribe.result = _longer_transcript()
    stub_summarize.result = {"status": "ok", "summary": "The whole meeting.", "usage": {}}
    _unlock(r, b"the-whole-meeting-this-time")
    drain()

    r.refresh_from_db()
    assert r.summary == "The whole meeting."
    from stapel_recordings.storage import get_storage

    stored = get_storage().get_bytes(r.transcript_storage_key).decode()
    assert "turn 4" in stored
    assert "hello world" not in stored


def test_a_retry_of_a_failed_stage_over_an_unchanged_input_does_not_repay(
    ready_recording, stub_transcribe, stub_summarize, drain, monkeypatch
):
    """The other half of the rule: same input, same answer, no second bill.

    The summary fails the run after transcription is already paid for. The
    retry must resume at merge and leave the transcription alone — which is
    exactly the reuse the fingerprint is there to PERMIT.
    """
    from stapel_recordings import stages

    boom = {"status": "error", "reason": "summarizer on fire"}
    stub_summarize.result = boom
    monkeypatch.setattr(
        stages.MergeStage, "resume",
        lambda self, rec, ctx, result: (_ for _ in ()).throw(
            stages.StageRetryable("summarize_failed", "on fire")
        ),
    )
    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == "queued", "the merge stage was supposed to park as retryable"
    assert r.retry_count == 1
    assert len(stub_transcribe.calls) == 1

    monkeypatch.undo()
    stub_summarize.result = {"status": "ok", "summary": "Recovered.", "usage": {}}
    # What reconcile does for a parked stage: re-deliver it. Same input,
    # same run — the transcription above is the checkpoint it resumes from.
    events.emit_stage(r.id, (r.workflow_state["pipeline"]["completed_index"]) + 1)
    drain()

    r.refresh_from_db()
    assert r.status == "completed"
    assert r.summary == "Recovered."
    assert len(stub_transcribe.calls) == 1, "the retry re-bought the transcription"


def test_an_artifact_of_unknown_provenance_is_trusted_until_it_is_declared_stale(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    """Upgrading must not re-bill history.

    Recordings finished before fingerprints were recorded carry artifacts
    whose provenance nobody knows. Unknown is not stale: a reprocess over
    one reuses them, exactly as it did before, and only an explicit
    declaration spends money on them again.
    """
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)

    # Forget what produced them — the state of every pre-upgrade row.
    state = dict(r.workflow_state or {})
    state["pipeline"] = {
        k: v for k, v in (state.get("pipeline") or {}).items()
        if k != checkpoints.FINGERPRINTS_KEY
    }
    r.workflow_state = state
    r.save(update_fields=["workflow_state"])

    assert pipeline.reprocess_recording(str(r.id))
    drain()
    r.refresh_from_db()
    assert r.status == "completed"
    assert len(stub_transcribe.calls) == 1, "a reprocess re-bought an artifact it could reuse"

    assert pipeline.invalidate_from(str(r.id), "convert")
    assert pipeline.reprocess_recording(str(r.id))
    drain()
    assert len(stub_transcribe.calls) == 2


def test_a_declaration_alone_changes_no_artifact(ready_recording, stub_transcribe,
                                                  stub_summarize, drain):
    """invalidate_from declares; it does not requeue and does not delete.

    The user keeps seeing the finished result until the re-run replaces it —
    there is no window in which the meeting they paid for shows as empty.
    """
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)

    assert pipeline.invalidate_from(str(r.id), "convert")
    r.refresh_from_db()
    assert r.status == "completed"
    assert r.segments_count == 2
    assert Segment.objects.filter(recording=r).count() == 2
    assert r.summary
    assert len(stub_transcribe.calls) == 1


def test_a_second_click_on_the_same_rerun_is_the_same_unit_of_work(ready_recording):
    """The idempotency key is the recording AND the input.

    Two submissions for the same audio coalesce (core returns the first
    task's id while it is in flight), and the re-converted meeting does
    not — which the storage key alone could not tell apart, because the
    normalized object is written back to the same path.
    """
    stage = TranscribeStage()
    ready_recording.normalized_storage_key = "recordings/ws/rec/audio.wav"
    ready_recording.workflow_state = {"audio_content_hash": "sha256:aaa"}

    first = stage_input_dedupe_key(ready_recording, stage)
    assert stage_input_dedupe_key(ready_recording, stage) == first

    ready_recording.workflow_state = {"audio_content_hash": "sha256:bbb"}
    assert stage_input_dedupe_key(ready_recording, stage) != first


class TestTheComparisonRefusesToGuess:
    """``checkpoints.is_stale`` is the one place the rule is spelled out."""

    def test_a_recorded_fingerprint_that_differs_is_stale(self, ready_recording):
        checkpoints.record_fingerprint(ready_recording, "transcribe", "audio=aaa")
        assert checkpoints.is_stale(ready_recording, "transcribe", "audio=bbb")

    def test_the_same_fingerprint_is_not(self, ready_recording):
        checkpoints.record_fingerprint(ready_recording, "transcribe", "audio=aaa")
        assert not checkpoints.is_stale(ready_recording, "transcribe", "audio=aaa")

    def test_neither_unknown_is_stale(self, ready_recording):
        assert not checkpoints.is_stale(ready_recording, "transcribe", "audio=aaa")
        checkpoints.record_fingerprint(ready_recording, "transcribe", "audio=aaa")
        assert not checkpoints.is_stale(ready_recording, "transcribe", None)

    def test_a_declaration_outranks_both(self, ready_recording):
        checkpoints.record_fingerprint(ready_recording, "transcribe", "audio=aaa")
        checkpoints.declare_invalid(ready_recording, ["transcribe"])
        assert checkpoints.is_stale(ready_recording, "transcribe", "audio=aaa")

    def test_and_is_spent_by_the_rerun_it_asked_for(self, ready_recording):
        checkpoints.declare_invalid(ready_recording, ["transcribe"])
        checkpoints.clear_invalidation(ready_recording, "transcribe")
        assert not checkpoints.is_stale(ready_recording, "transcribe", "audio=aaa")
