"""The flagship extension point: pipeline = data + an open stage registry.

Reorder / subset / insert / replace stages and swap the resolver — all
fork-free.
"""
import pytest
from django.test import override_settings

from stapel_recordings import events, stages
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db


def test_custom_stage_runs_in_order(ready_recording, stub_transcribe, stub_summarize, drain):
    """A custom 'redact_pii' stage inserted before merge runs in position."""
    stages.register_stage("redact_pii", fakes.redact_pii_stage)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "PIPELINE": ["convert", "transcribe", "redact_pii", "merge"],
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert r.metadata.get("redacted") is True
    assert r.transcript_storage_key  # merge still ran, after redact
    assert "redact_pii" in fakes.STAGE_TRACE


def test_subset_pipeline_skips_diarize(ready_recording, stub_transcribe, stub_summarize, drain):
    """Dropping 'diarize' from the list is a pure config change."""
    seen = []
    from stapel_core.comm import on_action
    from stapel_core.comm.registry import action_registry

    @on_action(events.ACTION_STAGE_COMPLETED)
    def _spy(event):
        seen.append(event.payload["stage"])

    try:
        with override_settings(
            STAPEL_RECORDINGS={
                "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
                "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
                "PIPELINE": ["convert", "transcribe", "merge"],
            }
        ):
            from stapel_recordings import storage

            storage.reset_storage_cache()
            events.emit_stage(ready_recording.id, 0)
            drain()
    finally:
        action_registry._subscribers[events.ACTION_STAGE_COMPLETED].remove(_spy)

    assert seen == ["convert", "transcribe", "merge"]
    assert Recording.objects.get(pk=ready_recording.id).status == RecordingStatus.COMPLETED


def test_swap_a_builtin_stage(ready_recording, stub_transcribe, drain):
    """Replace the built-in merge with a custom handler via STAGES overlay."""
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "STAGES": {"merge": "stapel_recordings.tests.fakes.SpyMergeStage"},
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.transcript_storage_key == "spy://transcript"
    assert "spy_merge" in fakes.STAGE_TRACE


def test_remove_a_builtin_stage_via_none():
    """A STAGES overlay value of None removes a built-in (merge-semantics)."""
    with override_settings(STAPEL_RECORDINGS={"STAGES": {"diarize": None}}):
        resolved = stages.resolve_stages()
    assert "diarize" not in resolved
    assert "convert" in resolved


def test_runtime_register_and_unregister():
    stages.register_stage("record", fakes.RecordStage)
    assert "record" in stages.resolve_stages()
    stages.unregister_stage("record")
    assert "record" not in stages.resolve_stages()


def test_resolver_failure_parks_then_dlqs_bounded(make_recording, drain):
    """A crashing PIPELINE_RESOLVER (missing per-workspace row, DB glitch)
    must not crash-loop the delivery forever in the outbox: it is parked as
    a retryable failure, bounded by MAX_STAGE_RETRIES, then DLQ'd."""
    from stapel_recordings import pipeline

    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(
        STAPEL_RECORDINGS={
            "PIPELINE_RESOLVER": "stapel_recordings.tests.fakes.broken_resolver",
            "MAX_STAGE_RETRIES": 1,
        }
    ):
        pipeline.run_stage(str(r.id), 0)
        r.refresh_from_db()
        assert r.status == RecordingStatus.QUEUED  # parked, not crash-looping
        assert r.retry_count == 1
        assert r.metadata["last_error"]["stage"] == "<pipeline_resolver>"

        pipeline.run_stage(str(r.id), 0)  # reconcile re-drive, retries exhausted

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    from stapel_core.django.outbox.models import OutboxEvent

    assert OutboxEvent.objects.filter(topic=events.ACTION_FAILED).count() == 1


def test_broken_overlay_entry_does_not_break_unrelated_pipelines(make_recording, drain):
    """get_stage is lazy: a broken dotted-path in the STAGES overlay only
    affects pipelines that actually include that stage."""
    stages.register_stage("record", fakes.RecordStage)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(
        STAPEL_RECORDINGS={
            "STAGES": {"unrelated": "no.such.module.Stage"},
            "PIPELINE": ["record"],
        }
    ):
        events.emit_stage(r.id, 0)
        drain()

    assert fakes.STAGE_TRACE == ["record"]
    assert Recording.objects.get(pk=r.id).status == RecordingStatus.COMPLETED


def test_broken_overlay_stage_in_pipeline_dlqs_that_recording(make_recording, drain):
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(
        STAPEL_RECORDINGS={
            "STAGES": {"broken": "no.such.module.Stage"},
            "PIPELINE": ["broken"],
        }
    ):
        events.emit_stage(r.id, 0)
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert r.metadata["last_error"]["reason"].startswith("unresolvable_stage")


def test_pipeline_resolver_seam(make_recording, drain):
    """A custom PIPELINE_RESOLVER sources a non-default pipeline (e.g. a
    per-recording / DB-stored definition edited at runtime)."""
    stages.register_stage("record", fakes.RecordStage)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(
        STAPEL_RECORDINGS={
            "PIPELINE_RESOLVER": "stapel_recordings.tests.fakes.only_record_resolver",
            # PIPELINE below is intentionally different — the resolver wins.
            "PIPELINE": ["convert", "transcribe", "diarize", "merge"],
        }
    ):
        events.emit_stage(r.id, 0)
        drain()

    assert fakes.STAGE_TRACE == ["record"]
    assert Recording.objects.get(pk=r.id).status == RecordingStatus.COMPLETED
