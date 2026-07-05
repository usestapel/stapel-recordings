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


def test_redelivery_does_not_resurrect_dlqed_recording(make_recording, drain):
    """ERROR is terminal for deliveries: after DLQ (recording.failed already
    published) a redelivered recording.stage must not re-run the stage nor
    emit a contradicting recording.completed."""
    from stapel_core.django.outbox.models import OutboxEvent

    from stapel_recordings import pipeline, stages

    calls = []

    def boom(recording, ctx):
        calls.append(1)
        raise stages.StageFatal("bad_input")

    stages.register_stage("boom_dlq", boom)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["boom_dlq"]}):
        events.emit_stage(r.id, 0)
        drain()
        r.refresh_from_db()
        assert r.status == RecordingStatus.ERROR
        assert len(calls) == 1

        # Broker redelivery / stale reconcile duplicate of the same stage.
        pipeline.run_stage(str(r.id), 0)
        # Duplicate recording.uploaded must not restart it either.
        pipeline.start_pipeline(str(r.id))
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert len(calls) == 1  # stage never re-ran
    assert OutboxEvent.objects.filter(topic=events.ACTION_FAILED).count() == 1
    assert not OutboxEvent.objects.filter(topic=events.ACTION_COMPLETED).exists()


def test_retry_recording_is_the_explicit_error_to_queued_transition(make_recording, drain):
    """retry_recording(): error -> queued, resumes at the first
    not-yet-completed stage, and is a no-op for non-error recordings."""
    from stapel_recordings import pipeline, stages

    fail = {"on": True}
    ran = []

    def flaky(recording, ctx):
        if fail["on"]:
            raise stages.StageFatal("bad_input")
        ran.append("flaky")
        return ctx

    def ok(recording, ctx):
        ran.append("ok")
        return ctx

    stages.register_stage("ok_stage", ok)
    stages.register_stage("flaky_stage", flaky)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["ok_stage", "flaky_stage"]}):
        events.emit_stage(r.id, 0)
        drain()
        r.refresh_from_db()
        assert r.status == RecordingStatus.ERROR
        assert ran == ["ok"]

        fail["on"] = False
        assert pipeline.retry_recording(str(r.id)) is True
        r.refresh_from_db()
        assert r.status == RecordingStatus.QUEUED
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    assert ran == ["ok", "flaky"]  # completed stage was not re-run on retry
    assert pipeline.retry_recording(str(r.id)) is False  # not in error anymore
