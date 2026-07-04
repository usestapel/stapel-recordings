"""Retry parking and DLQ exhaustion."""
import pytest
from django.test import override_settings

from stapel_recordings import events
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def test_dlq_after_max_retries_emits_failed(ready_recording, stub_transcribe, drain):
    stub_transcribe.result = {"status": "failure", "reason": "provider down"}
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "MAX_STAGE_RETRIES": 1}):
        from stapel_recordings import storage

        storage.reset_storage_cache()

        # First pass: convert ok, transcribe fails -> parked (retry_count 1).
        events.emit_stage(ready_recording.id, 0)
        drain()
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.QUEUED
        assert r.retry_count == 1

        # Reconcile re-drives the transcribe stage -> retry_count 2 > max -> DLQ.
        events.emit_stage(ready_recording.id, 1)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.ERROR
    assert r.metadata["last_error"]["stage"] == "transcribe"

    from stapel_core.django.outbox.models import OutboxEvent

    failed = OutboxEvent.objects.filter(topic=events.ACTION_FAILED)
    assert failed.exists()


def test_fatal_stage_error_goes_straight_to_dlq(make_recording, drain):
    """A StageFatal (bad input) DLQs without consuming retries."""
    from stapel_recordings import stages

    def boom(recording, ctx):
        raise stages.StageFatal("bad_input", "unreadable")

    stages.register_stage("boom", boom)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["boom"]}):
        events.emit_stage(r.id, 0)
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert r.retry_count == 0  # fatal — no retry consumed
    assert r.metadata["last_error"]["reason"] == "bad_input"
