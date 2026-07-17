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
    assert payload["audio_url"].startswith("memory://get/")
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
    assert r.metadata["last_error"]["stage"] == "transcribe"


class _UrlOnly:
    def presigned_get_url(self, key, *, expires_seconds=3600):
        return f"memory://get/{key}"
