"""Transcribe stage boundary + §7.21 split producer/consumer tests.

STT lives in stapel-agent; recordings only CALLS ``llm.transcribe`` and
persists the returned transcript. The pipeline is split into synchronous
halves: a producer that emits an Action to the outbox, and a consumer that
drains the outbox and runs the work.
"""

import pytest

from stapel_recordings import events, services
from stapel_recordings.models import Recording, RecordingStatus, Segment
from stapel_recordings.stages import TranscribeStage

pytestmark = pytest.mark.django_db


def _seed_uploaded(make_recording):
    from stapel_recordings.storage import get_storage

    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"audio", content_type="audio/mpeg")
    return r, session


def test_producer_finalize_emits_uploaded_without_running(use_fakes, make_recording):
    """Producer half: finalize writes recording.uploaded to the outbox but
    the pipeline has NOT run yet (event undelivered)."""
    r, session = _seed_uploaded(make_recording)
    services.finalize_upload(session=session, file_size_bytes=5)

    from stapel_core.django.outbox.models import OutboxEvent

    row = OutboxEvent.objects.filter(topic=events.ACTION_UPLOADED, dispatched_at__isnull=True).first()
    assert row is not None
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED
    assert r.file_storage_key == session.storage_key
    assert Segment.objects.filter(recording=r).count() == 0


def test_consumer_drains_and_calls_llm_transcribe(use_fakes, make_recording, stub_transcribe, stub_summarize, drain):
    """Consumer half: draining the outbox walks the pipeline; transcribe
    calls llm.transcribe with the presigned audio URL and stores segments."""
    r, session = _seed_uploaded(make_recording)
    services.finalize_upload(session=session, file_size_bytes=5)

    drain()

    assert len(stub_transcribe.calls) == 1
    payload = stub_transcribe.calls[0]
    assert payload["audio_url"].startswith("https://fake.invalid/get/")
    # The URL the provider fetches with is minted for a CONFIGURED lifetime
    # (TRANSCRIBE_AUDIO_URL_TTL_SECONDS) — with a private bucket it is the
    # only way in, so its deadline is an operator's decision, not a literal.
    assert "expires_in=3600" in payload["audio_url"]
    assert payload["diarization"] is True
    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    assert Segment.objects.filter(recording=r).count() == 2


def test_transcribe_persists_words_from_word_level_only(make_recording, stub_transcribe):
    """A provider returning word-level output only is grouped into
    utterances by speaker."""
    stub_transcribe.result = {
        "status": "ok",
        "provider_used": "words-asr",
        "fallback_used": True,
        "transcript": {
            "provider": "words-asr",
            "language": "en",
            "duration_seconds": 3.0,
            "words": [
                {"text": "a", "start": 0.0, "end": 0.5, "speaker": "s0"},
                {"text": "b", "start": 0.5, "end": 1.0, "speaker": "s0"},
                {"text": "c", "start": 1.0, "end": 1.5, "speaker": "s1"},
            ],
            "utterances": [],
            "speakers_detected": ["s0", "s1"],
            "raw": {},
        },
    }
    r = make_recording(status=RecordingStatus.TRANSCRIBING, normalized_storage_key="k")
    with pytest.MonkeyPatch().context() as mp:
        # storage.presigned_get_url is only used to build the URL; the stub
        # ignores it, so any backend works here.
        from stapel_recordings import storage

        mp.setattr(storage, "get_storage", lambda: _UrlOnly())
        TranscribeStage().run(r, {})

    assert Segment.objects.filter(recording=r).count() == 2  # grouped s0, s1
    r.refresh_from_db()
    assert r.fallback_used is True
    assert r.provider_used == "words-asr"


def test_transcribe_failure_parks_for_retry(ready_recording, stub_transcribe, drain):
    """A failure result from llm.transcribe is retryable — the recording is
    parked (QUEUED) with an incremented retry_count, not DLQ'd immediately."""
    stub_transcribe.result = {"status": "failure", "reason": "provider down"}
    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.QUEUED
    assert r.retry_count == 1
    assert r.workflow_state["last_error"]["stage"] == "transcribe"


class _UrlOnly:
    def presigned_get_url(self, key, *, expires_seconds=3600):
        return f"memory://get/{key}"


# ─── An empty transcript over real audio is not a finished stage ──────────
#
# The incident (a client host, 2026-09-21): a 10-minute meeting came back
# from llm.transcribe as ``status: ok`` with zero utterances and zero words
# (the agent served a checkpointed empty answer). This stage persisted
# nothing, merge skipped the summary, and the recording was declared
# ``completed`` with segments_count=0. Every gate was green.
#
# Ordinary django_db tests: the assertion is about the stage OUTCOME (fatal
# vs completed), which is decided in Python before anything commits, so
# pytest-django's transaction wrapping cannot make them pass for the wrong
# reason — the pre-change code completes the recording inside the very same
# wrapped transaction, and that is what the first test fails on.


def _empty_transcript_result(duration=None):
    return {
        "status": "ok",
        "provider_used": "elevenlabs",
        "fallback_used": False,
        "cached": True,
        "transcript": {
            "provider": "elevenlabs",
            "language": "spa",
            "duration_seconds": duration,
            "words": [],
            "utterances": [],
            "speakers_detected": [],
            "raw": {},
        },
    }


def test_an_empty_transcript_over_ten_minutes_does_not_complete_the_recording(
    ready_recording, stub_transcribe, drain
):
    from stapel_core.django.outbox.models import OutboxEvent

    ready_recording.duration_seconds = 600.0185
    ready_recording.save(update_fields=["duration_seconds"])
    stub_transcribe.result = _empty_transcript_result()

    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.ERROR
    assert r.segments_count == 0
    assert r.workflow_state["last_error"]["stage"] == "transcribe"
    assert r.workflow_state["last_error"]["reason"] == "empty_transcript"
    assert "600s of audio" in r.workflow_state["last_error"]["detail"]
    assert "checkpoint" in r.workflow_state["last_error"]["detail"]
    topics = list(OutboxEvent.objects.values_list("topic", flat=True))
    assert "recording.failed" in topics
    assert "recording.completed" not in topics


def test_an_empty_transcript_is_fatal_not_retried(make_recording, stub_transcribe):
    """A retry re-reads the same checkpointed nothing; the stage says so once."""
    from stapel_recordings.stages import StageFatal, TranscribeStage

    r = make_recording(
        status=RecordingStatus.TRANSCRIBING, normalized_storage_key="k", duration_seconds=600.0
    )
    with pytest.raises(StageFatal) as excinfo:
        TranscribeStage().resume(r, {}, _empty_transcript_result())
    assert excinfo.value.reason == "empty_transcript"
    assert Segment.objects.filter(recording=r).count() == 0


def test_a_short_silent_clip_is_a_result(ready_recording, stub_transcribe, drain):
    ready_recording.duration_seconds = 2.0
    ready_recording.save(update_fields=["duration_seconds"])
    stub_transcribe.result = _empty_transcript_result(duration=2.0)

    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert r.segments_count == 0


def test_unmeasured_audio_may_be_silent(make_recording, stub_transcribe):
    """No duration on the row and none in the transcript: nothing to weigh
    the emptiness against, so the stage keeps its old answer."""
    from stapel_recordings.stages import TranscribeStage

    r = make_recording(status=RecordingStatus.TRANSCRIBING, normalized_storage_key="k")
    TranscribeStage().resume(r, {}, _empty_transcript_result())
    r.refresh_from_db()
    assert r.segments_count == 0


def test_the_floor_is_configuration(make_recording, stub_transcribe, settings):
    from stapel_recordings.stages import TranscribeStage

    settings.STAPEL_RECORDINGS = {
        **getattr(settings, "STAPEL_RECORDINGS", {}),
        "EMPTY_TRANSCRIPT_MIN_AUDIO_SECONDS": 0,
    }
    r = make_recording(
        status=RecordingStatus.TRANSCRIBING, normalized_storage_key="k", duration_seconds=600.0
    )
    TranscribeStage().resume(r, {}, _empty_transcript_result())
    r.refresh_from_db()
    assert r.segments_count == 0


def test_an_empty_stranded_handoff_is_dropped_so_a_retry_transcribes(
    use_fakes, make_recording, stub_transcribe
):
    """The handoff object is re-adopted on every pass while it exists; an
    empty one would be adopted, refused, adopted, refused, for ever."""
    import json

    from stapel_recordings.stages import StageFatal, TranscribeStage, handoff_key
    from stapel_recordings.storage import get_storage

    r = make_recording(
        status=RecordingStatus.TRANSCRIBING, normalized_storage_key="k", duration_seconds=600.0
    )
    key = handoff_key(r)
    get_storage().put_bytes(
        key, json.dumps(_empty_transcript_result()["transcript"]).encode(),
        content_type="application/json",
    )

    with pytest.raises(StageFatal) as excinfo:
        TranscribeStage().run(r, {})

    assert excinfo.value.reason == "empty_transcript"
    assert "stranded handoff" in excinfo.value.detail
    assert stub_transcribe.calls == []  # adopted, not re-bought
    try:
        leftover = get_storage().get_bytes(key)
    except KeyError:  # the fake raises for a missing object
        leftover = None
    assert not leftover
