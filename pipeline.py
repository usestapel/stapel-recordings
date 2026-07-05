"""Generic, data-driven pipeline driver.

The pipeline is not a hardcoded chain of consumers — it is an ordered list
of stage names run by one generic driver over the open stage registry
(``stages.py``). This is the flagship extension point: reorder / subset /
insert / replace stages purely through settings, a resolver, or the
runtime ``register_stage`` API — no forking.

Flow (each step outbox-backed, at-least-once, idempotent):

    finalize_upload -> emit recording.uploaded
        -> start_pipeline: emit recording.stage(index=0)
            -> run_stage(0): run stage handler, on success
               emit recording.stage(index=1) ... until the list is
               exhausted -> status=completed, emit recording.completed.

Progress cursor — **names, not positions**. The pipeline may be edited at
runtime (``PIPELINE_RESOLVER``), so a positional index alone cannot be
trusted across deliveries. ``run_stage`` persists, in the same transaction
as each successful stage, the *names* of the completed stages
(``metadata.pipeline.completed``) plus the position of the last completion
(``completed_index``). On every delivery it:

- drops the event if ``stage_index <= completed_index`` (a duplicate of an
  already-completed stage — no re-run, **no re-emit** of public events);
- otherwise runs the *first stage in the currently resolved pipeline whose
  name has not completed* (the event index is only a dedup hint). Editing
  the list under a live recording is therefore safe: each named stage runs
  at most once, removed stages are skipped (with a warning when the
  removed stage was pending), inserted stages run, and an empty/exhausted
  list finalizes only when every listed stage has completed. A resolver
  that returns an **empty list** is treated as a misconfiguration → DLQ
  (never a silent ``completed``). Stage names within one pipeline must be
  unique — the completed-set treats a repeated name as already done.

``run_stage`` locks the recording (``select_for_update``) for the whole
stage and classifies stage errors into retry vs DLQ. ``error`` is terminal
for deliveries: a DLQ'd recording is only revived through the explicit
:func:`retry_recording` transition, never by a redelivered event.

The stage list comes from ``PIPELINE_RESOLVER`` (default: the ``PIPELINE``
setting) — point that seam at a DB/per-workspace source to edit pipelines
at runtime. Resolver failures are parked as retryable (bounded by
``MAX_STAGE_RETRIES``, then DLQ) instead of crash-looping the delivery.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from . import events
from .conf import recordings_settings
from .models import Recording, RecordingStatus
from .stages import StageFatal, StageRetryable, get_stage

logger = logging.getLogger(__name__)

# ERROR is terminal for event deliveries: after DLQ the only way back into
# the pipeline is the explicit retry_recording() transition below. Without
# this, a broker redelivery could "resurrect" a recording whose
# recording.failed event already reached consumers (refunds, notifications).
_TERMINAL = {RecordingStatus.COMPLETED, RecordingStatus.ERROR, RecordingStatus.DELETED}


# ─── Resolver seam ─────────────────────────────────────────────────────


def default_pipeline_resolver(recording) -> list[str]:
    """Default resolver: the ``PIPELINE`` setting. A host swaps this for a
    DB/per-recording/per-workspace source via ``PIPELINE_RESOLVER``."""
    return list(recordings_settings.PIPELINE)


def resolve_pipeline(recording) -> list[str]:
    resolver = recordings_settings.PIPELINE_RESOLVER  # import_strings resolves it
    return list(resolver(recording))


# ─── Entry ─────────────────────────────────────────────────────────────


def start_pipeline(recording_id: str) -> None:
    """Kick the pipeline for a freshly uploaded recording. Idempotent under
    concurrent duplicate deliveries: the row is locked and a
    ``metadata.pipeline`` marker is written in the same transaction as the
    ``recording.stage(0)`` emit, so a second delivery (or a concurrent one —
    it serializes on the lock) sees the marker and skips."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("start_pipeline: recording %s not found", recording_id)
            return
        if recording.status in _TERMINAL:
            return
        if (recording.metadata or {}).get("pipeline"):
            return  # already started
        metadata = dict(recording.metadata or {})
        metadata["pipeline"] = {"started_at": timezone.now().isoformat()}
        recording.metadata = metadata
        recording.save(update_fields=["metadata", "updated_at"])
        events.emit_stage(recording.id, 0)


# ─── Driver ────────────────────────────────────────────────────────────


def run_stage(recording_id: str, stage_index: int) -> None:
    """Run the next pending stage of the recording's resolved pipeline and,
    on success, emit the next stage event (or finalize). All within one
    locked, atomic unit.

    *stage_index* (from the event payload) is a dedup hint only: an index
    at or below the persisted ``completed_index`` is a duplicate of an
    already-completed stage and is dropped without re-running or
    re-emitting anything. Which stage actually runs is decided **by name**
    against the currently resolved pipeline (first listed stage whose name
    has not completed), so a pipeline edited under a live recording never
    causes a wrong stage to run at a stale position."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("run_stage: recording %s not found", recording_id)
            return

        if recording.status in _TERMINAL:
            return

        try:
            pipeline = resolve_pipeline(recording)
        except Exception as exc:
            # A broken resolver (missing per-workspace row, DB glitch) must
            # not crash the delivery into an unbounded outbox retry loop:
            # park it as a retryable stage failure — bounded by
            # MAX_STAGE_RETRIES, then DLQ.
            logger.exception("run_stage: pipeline resolver failed for %s", recording_id)
            _handle_retry(recording, "<pipeline_resolver>", "pipeline_resolver_error", str(exc))
            return

        if not pipeline:
            # An empty pipeline for a recording that still has work queued is
            # a misconfiguration; completing it silently would publish a lie.
            _dlq(recording, stage="<pipeline>", reason="empty_pipeline")
            return

        if stage_index <= _completed_index(recording):
            return  # duplicate delivery of an already-completed stage — no-op

        completed = set(_completed_stages(recording))
        next_index = next((i for i, name in enumerate(pipeline) if name not in completed), None)
        if next_index is None:
            _finalize(recording)  # every listed stage has completed
            return
        stage_name = pipeline[next_index]

        started = (recording.metadata or {}).get("pipeline", {}).get("stage")
        if started and started != stage_name and started not in pipeline and started not in completed:
            logger.warning(
                "pipeline: recording %s pending stage %r was removed from the "
                "resolved pipeline — skipping to %r",
                recording.id, started, stage_name,
            )

        try:
            stage = get_stage(stage_name)
        except (KeyError, TypeError, ImportError) as exc:
            _dlq(recording, stage=stage_name, reason=f"unresolvable_stage: {exc}")
            return

        _set_current(recording, next_index, stage_name)
        if stage.status:
            recording.status = stage.status
        recording.save(update_fields=["status", "metadata", "updated_at"])

        ctx = (recording.metadata or {}).get("pipeline", {}).get("ctx") or {}
        try:
            new_ctx = stage.run(recording, ctx) or {}
        except StageFatal as exc:
            _dlq(recording, stage=stage_name, reason=exc.reason, detail=exc.detail)
            return
        except StageRetryable as exc:
            _handle_retry(recording, stage_name, exc.reason, exc.detail)
            return
        except Exception as exc:  # unexpected — reconcile can re-drive
            logger.exception("run_stage: unexpected error in %s for %s", stage_name, recording_id)
            _handle_retry(recording, stage_name, "unexpected", str(exc))
            return

        recording.retry_count = 0
        _store_ctx(recording, new_ctx)
        # Persist "stage N completed" in the same transaction as the success
        # events: a redelivery of a completed stage is now distinguishable
        # from crash recovery (crash before this commit re-runs the stage;
        # after it, the duplicate is dropped by the completed_index guard
        # above and public events are never re-emitted with fresh event_ids).
        _mark_completed(recording, next_index, stage_name)
        recording.save(update_fields=["retry_count", "metadata", "updated_at"])

        events.emit_stage_completed(recording, stage_name, next_index)
        events.emit_stage(recording.id, next_index + 1)


def retry_recording(recording_id: str) -> bool:
    """Explicit ``error -> queued`` transition: re-enter the pipeline after a
    DLQ. Returns True if the recording was requeued.

    This is the *only* way back from ``error`` — event redeliveries never
    resurrect a DLQ'd recording. Completed stages are kept (the cursor is
    the persisted completed-set), so the retry resumes at the first
    not-yet-completed stage of the currently resolved pipeline. Expose this
    from an app-layer endpoint/admin action as needed."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("retry_recording: recording %s not found", recording_id)
            return False
        if recording.status != RecordingStatus.ERROR:
            return False
        recording.status = RecordingStatus.QUEUED
        recording.retry_count = 0
        recording.save(update_fields=["status", "retry_count", "updated_at"])
        events.emit_stage(recording.id, _completed_index(recording) + 1)
    logger.info("pipeline: recording %s requeued for retry", recording_id)
    return True


# ─── terminal transitions ──────────────────────────────────────────────


def _finalize(recording: Recording) -> None:
    if recording.status == RecordingStatus.COMPLETED:
        return
    recording.status = RecordingStatus.COMPLETED
    # Save + terminal emit as one unit. run_stage() already holds the outer
    # transaction.atomic(); this nests as a savepoint (joins the outer txn,
    # events still leave only on outer commit) and keeps the pair atomic even
    # if a future caller invokes _finalize() outside run_stage.
    with mutate_and_emit():
        recording.save(update_fields=["status", "updated_at"])
        events.emit_completed(recording)
    logger.info("pipeline: recording %s completed", recording.id)


def _handle_retry(recording: Recording, stage_name: str, reason: str, detail=None) -> None:
    recording.retry_count = (recording.retry_count or 0) + 1
    _set_last_error(recording, stage_name, reason, detail)
    if recording.retry_count > int(recordings_settings.MAX_STAGE_RETRIES):
        _dlq(recording, stage=stage_name, reason=f"retries_exhausted: {reason}", already_errored=True)
        return
    # Park it: reconcile re-emits recording.stage(current) after the stuck
    # threshold. Avoids a tight in-process retry loop.
    recording.status = RecordingStatus.QUEUED
    recording.save(update_fields=["retry_count", "status", "metadata", "updated_at"])
    logger.info("pipeline: %s stage %s retryable (%s), attempt %d — parked",
                recording.id, stage_name, reason, recording.retry_count)


def _dlq(recording: Recording, *, stage: str, reason: str, detail=None, already_errored=False) -> None:
    recording.status = RecordingStatus.ERROR
    if not already_errored:
        _set_last_error(recording, stage, reason, detail)
    # Save + terminal DLQ emit as one unit. run_stage() already holds the outer
    # transaction.atomic(); this nests as a savepoint (joins the outer txn,
    # events still leave only on outer commit) and keeps the pair atomic even
    # if a future caller invokes _dlq() outside run_stage.
    with mutate_and_emit():
        recording.save(update_fields=["status", "metadata", "updated_at"])
        events.emit_failed(recording, stage=stage, reason=reason, user_retryable=True)
    logger.warning("pipeline: recording %s DLQ at stage %s (%s)", recording.id, stage, reason)


# ─── metadata helpers ──────────────────────────────────────────────────


def _pipeline_meta(recording: Recording) -> dict:
    return (recording.metadata or {}).get("pipeline") or {}


def _completed_index(recording: Recording) -> int:
    """Pipeline position of the last completed stage (-1 = none). Used as a
    cheap duplicate-delivery guard; the completed *names* are authoritative
    for what still has to run."""
    return int(_pipeline_meta(recording).get("completed_index", -1))


def _completed_stages(recording: Recording) -> list[str]:
    return list(_pipeline_meta(recording).get("completed") or [])


def _mark_completed(recording: Recording, stage_index: int, stage_name: str) -> None:
    metadata = dict(recording.metadata or {})
    pl = dict(metadata.get("pipeline") or {})
    done = list(pl.get("completed") or [])
    if stage_name not in done:
        done.append(stage_name)
    pl["completed"] = done
    pl["completed_index"] = stage_index
    metadata["pipeline"] = pl
    recording.metadata = metadata


def _set_current(recording: Recording, stage_index: int, stage_name: str) -> None:
    metadata = dict(recording.metadata or {})
    pl = dict(metadata.get("pipeline") or {})
    pl["stage_index"] = stage_index
    pl["stage"] = stage_name
    pl["updated_at"] = timezone.now().isoformat()
    metadata["pipeline"] = pl
    recording.metadata = metadata


def _store_ctx(recording: Recording, ctx: dict) -> None:
    metadata = dict(recording.metadata or {})
    pl = dict(metadata.get("pipeline") or {})
    pl["ctx"] = ctx
    metadata["pipeline"] = pl
    recording.metadata = metadata


def _set_last_error(recording: Recording, stage: str, reason: str, detail=None) -> None:
    metadata = dict(recording.metadata or {})
    metadata["last_error"] = {
        "stage": stage,
        "reason": reason,
        "detail": (str(detail)[:500] if detail else None),
        "at": timezone.now().isoformat(),
    }
    recording.metadata = metadata


__all__ = [
    "default_pipeline_resolver",
    "resolve_pipeline",
    "start_pipeline",
    "run_stage",
    "retry_recording",
]
