"""Generic pipeline driver: end-to-end run, status progression, idempotency."""
import pytest

from stapel_recordings import events, pipeline
from stapel_recordings.models import Recording, RecordingStatus, Segment, Speaker

pytestmark = pytest.mark.django_db


def test_full_pipeline_completes(ready_recording, stub_transcribe, stub_summarize, drain):
    """convert -> transcribe -> diarize -> merge -> embed -> completed,
    driven purely by the outbox (the production reliability path). embed is
    a no-op here (vector app not installed)."""
    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert r.segments_count == 2
    assert r.speakers_count == 2
    assert r.provider_used == "stub-asr"
    assert r.normalized_storage_key
    assert r.transcript_storage_key
    assert r.summary == "A short summary."
    assert Segment.objects.filter(recording=r).count() == 2
    assert Speaker.objects.filter(recording=r).count() == 2
    # Terminal public event was emitted and delivered.
    from stapel_core.django.outbox.models import OutboxEvent

    assert OutboxEvent.objects.filter(topic=events.ACTION_COMPLETED).exists()


def test_status_progression_is_a_state_machine(ready_recording, stub_transcribe, stub_summarize, drain):
    """Running one stage at a time surfaces the created→…→completed machine."""
    seen = []

    from stapel_core.comm import on_action

    @on_action(events.ACTION_STAGE_COMPLETED)
    def _spy(event):
        seen.append((event.payload["stage"], event.payload["status"]))

    try:
        events.emit_stage(ready_recording.id, 0)
        drain()
    finally:
        from stapel_core.comm.registry import action_registry

        action_registry._subscribers[events.ACTION_STAGE_COMPLETED].remove(_spy)

    stages_run = [s for s, _ in seen]
    assert stages_run == ["convert", "transcribe", "diarize", "merge", "embed"]
    # convert ran while status was NORMALIZING, transcribe while TRANSCRIBING…
    status_by_stage = dict(seen)
    assert status_by_stage["convert"] == RecordingStatus.NORMALIZING
    assert status_by_stage["transcribe"] == RecordingStatus.TRANSCRIBING
    assert status_by_stage["merge"] == RecordingStatus.MERGING


def test_redelivery_does_not_double_process(ready_recording, stub_transcribe, stub_summarize, drain):
    """Re-running an already-completed stage index is a cheap no-op (no
    duplicate segments) — the at-least-once idempotency contract."""
    events.emit_stage(ready_recording.id, 0)
    drain()
    assert Segment.objects.filter(recording=ready_recording).count() == 2

    # Re-deliver the transcribe stage (index 1) after completion.
    pipeline.run_stage(str(ready_recording.id), 1)
    drain()
    assert Segment.objects.filter(recording=ready_recording).count() == 2
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED


def test_transcribe_is_idempotent_on_replay(ready_recording, stub_transcribe, drain):
    """Replaying the transcribe stage when segments already exist does not
    call the STT function again nor duplicate rows."""
    from stapel_recordings.stages import TranscribeStage

    # First run creates segments.
    ready_recording.normalized_storage_key = "recordings/x/y/audio.normalized.wav"
    ready_recording.save(update_fields=["normalized_storage_key"])
    TranscribeStage().run(ready_recording, {})
    assert Segment.objects.filter(recording=ready_recording).count() == 2
    assert len(stub_transcribe.calls) == 1

    # Replay — guarded by segments.exists().
    TranscribeStage().run(ready_recording, {})
    assert len(stub_transcribe.calls) == 1
    assert Segment.objects.filter(recording=ready_recording).count() == 2
