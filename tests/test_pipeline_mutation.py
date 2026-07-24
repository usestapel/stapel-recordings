"""Pipeline edited under live recordings + duplicate-delivery idempotency.

The progress cursor is the persisted set of *completed stage names* (plus a
``completed_index`` dedup guard), not a bare positional index. These tests
pin the contract: each named stage runs at most once; removed stages are
skipped (warning when the removed stage was pending); inserted stages run;
an empty resolved pipeline DLQs instead of silently completing; duplicates
of completed stages re-run nothing and re-emit nothing.
"""
import json
import logging

import pytest
from django.test import override_settings

from stapel_recordings import events, pipeline, stages
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db


def _trace(name):
    def _stage(recording, ctx):
        fakes.STAGE_TRACE.append(name)
        return ctx

    return _stage


@pytest.fixture
def abc_stages():
    names = ("st_a", "st_b", "st_c")
    for n in names:
        stages.register_stage(n, _trace(n))
    yield names
    for n in names:
        stages.unregister_stage(n)


def _pl(*names):
    return override_settings(STAPEL_RECORDINGS={"PIPELINE": list(names)})


def _outbox(topic):
    from stapel_core.django.outbox.models import OutboxEvent

    return OutboxEvent.objects.filter(topic=topic)


def _stage_completed_names():
    return [json.loads(row.event_json)["payload"]["stage"] for row in _outbox(events.ACTION_STAGE_COMPLETED)]


# ─── H1: pipeline edited under a live recording ────────────────────────


def test_removed_earlier_stage_does_not_skip_pending_stage(make_recording, abc_stages):
    """[a,b,c] -> [b,c] with an in-flight stage(1): b must run, not c."""
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl("st_a", "st_b", "st_c"):
        pipeline.run_stage(str(r.id), 0)
    assert fakes.STAGE_TRACE == ["st_a"]

    # Operator removes the already-completed 'st_a'; the in-flight event
    # (positionally pointing at what is now 'st_c') is delivered.
    with _pl("st_b", "st_c"):
        pipeline.run_stage(str(r.id), 1)
    assert fakes.STAGE_TRACE == ["st_a", "st_b"]  # name cursor: b, never skipped


def test_shortened_pipeline_finalizes_only_when_listed_work_done(make_recording, abc_stages, drain):
    """[a,b,c] -> [a] after a completed: remaining removed stages don't run,
    the recording finalizes because every *listed* stage has completed."""
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl("st_a", "st_b", "st_c"):
        pipeline.run_stage(str(r.id), 0)

    with _pl("st_a"):
        pipeline.run_stage(str(r.id), 1)  # stale in-flight index

    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    assert fakes.STAGE_TRACE == ["st_a"]
    assert _outbox(events.ACTION_COMPLETED).count() == 1


def test_reordered_pipeline_runs_each_stage_exactly_once(make_recording, abc_stages, drain):
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl("st_a", "st_b", "st_c"):
        pipeline.run_stage(str(r.id), 0)

    with _pl("st_b", "st_a", "st_c"):
        drain()

    assert sorted(fakes.STAGE_TRACE) == ["st_a", "st_b", "st_c"]
    assert len(fakes.STAGE_TRACE) == 3  # no stage ran twice
    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    completed_names = _stage_completed_names()
    assert sorted(completed_names) == ["st_a", "st_b", "st_c"]


def test_inserted_stage_before_cursor_runs(make_recording, abc_stages, drain):
    """[a,c] -> [a,b,c] after a completed: the inserted b runs, a is not
    re-run — the pipeline is declarative, each listed stage runs once."""
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl("st_a", "st_c"):
        pipeline.run_stage(str(r.id), 0)

    with _pl("st_a", "st_b", "st_c"):
        drain()

    assert fakes.STAGE_TRACE == ["st_a", "st_b", "st_c"]
    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED


def test_pending_stage_removed_skips_with_warning(make_recording, abc_stages, caplog, drain):
    """A parked (started-but-not-completed) stage removed from the list is
    skipped with an explicit warning — the edit is treated as operator
    intent, not silently and not a DLQ."""

    def failing_b(recording, ctx):
        raise stages.StageRetryable("net_down")

    stages.register_stage("st_b", failing_b)
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl("st_a", "st_b", "st_c"):
        pipeline.run_stage(str(r.id), 0)
        pipeline.run_stage(str(r.id), 1)  # b fails -> parked, metadata.stage == st_b
    r.refresh_from_db()
    assert r.retry_count == 1

    with _pl("st_a", "st_c"), caplog.at_level(logging.WARNING, logger="stapel_recordings.pipeline"):
        pipeline.run_stage(str(r.id), 1)  # reconcile re-drive after the edit
        drain()

    assert "removed from the resolved pipeline" in caplog.text
    assert fakes.STAGE_TRACE == ["st_a", "st_c"]
    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED


def test_empty_pipeline_dlqs_instead_of_silent_completed(make_recording):
    """A resolver returning [] (missing per-workspace config) must not
    publish a lying recording.completed."""
    r = make_recording(status=RecordingStatus.QUEUED)
    with _pl():
        pipeline.run_stage(str(r.id), 0)

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert r.metadata["last_error"]["reason"] == "empty_pipeline"
    assert _outbox(events.ACTION_FAILED).count() == 1
    assert not _outbox(events.ACTION_COMPLETED).exists()


# ─── H2: duplicate deliveries of completed stages ──────────────────────


def test_duplicate_stage_event_does_not_duplicate_public_events(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    """S1 repro: a duplicate recording.stage(0) (reconcile racing the live
    worker / broker redelivery) must not re-emit stage_completed with fresh
    event_ids for every subsequent stage."""
    events.emit_stage(ready_recording.id, 0)
    events.emit_stage(ready_recording.id, 0)  # the duplicate
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    names = _stage_completed_names()
    assert sorted(names) == ["convert", "diarize", "embed", "merge", "transcribe"]  # exactly once each
    assert _outbox(events.ACTION_COMPLETED).count() == 1
    assert len(stub_transcribe.calls) == 1


def test_duplicate_of_completed_stage_is_total_noop(ready_recording, stub_transcribe, stub_summarize, drain):
    """Mid-pipeline: once 'stage 0 completed' is persisted, a redelivery of
    stage 0 runs nothing and emits nothing."""
    from stapel_core.django.outbox.models import OutboxEvent

    pipeline.run_stage(str(ready_recording.id), 0)  # convert completes
    before = OutboxEvent.objects.count()

    pipeline.run_stage(str(ready_recording.id), 0)  # duplicate delivery
    assert OutboxEvent.objects.count() == before  # no new events at all

    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert sorted(_stage_completed_names()) == ["convert", "diarize", "embed", "merge", "transcribe"]


def test_completion_cursor_is_persisted_in_success_txn(ready_recording, stub_transcribe, stub_summarize):
    pipeline.run_stage(str(ready_recording.id), 0)
    r = Recording.objects.get(pk=ready_recording.id)
    pl = r.metadata["pipeline"]
    assert pl["completed"] == ["convert"]
    assert pl["completed_index"] == 0


# ─── start_pipeline idempotency marker ─────────────────────────────────


def test_duplicate_start_pipeline_emits_single_stage0(make_recording):
    r = make_recording(status=RecordingStatus.QUEUED)
    pipeline.start_pipeline(str(r.id))
    pipeline.start_pipeline(str(r.id))  # duplicate recording.uploaded

    assert _outbox(events.ACTION_STAGE).count() == 1
    r.refresh_from_db()
    assert r.metadata["pipeline"]["started_at"]
