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

``run_stage`` locks the recording (``select_for_update``), guards on
status + a recorded stage index (stale redeliveries are dropped), records
the current index on ``metadata['pipeline']`` so ``reconcile`` can re-drive
a stuck recording, and classifies stage errors into retry vs DLQ.

The stage list comes from ``PIPELINE_RESOLVER`` (default: the ``PIPELINE``
setting) — point that seam at a DB/per-workspace source to edit pipelines
at runtime.
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

_TERMINAL = {RecordingStatus.COMPLETED, RecordingStatus.DELETED}


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
    """Kick the pipeline for a freshly uploaded recording. Idempotent: a
    recording whose pipeline already started (metadata set) is skipped."""
    try:
        recording = Recording.objects.get(pk=recording_id)
    except Recording.DoesNotExist:
        logger.warning("start_pipeline: recording %s not found", recording_id)
        return
    if recording.status in _TERMINAL:
        return
    if (recording.metadata or {}).get("pipeline"):
        return  # already started
    events.emit_stage(recording.id, 0)


# ─── Driver ────────────────────────────────────────────────────────────


def run_stage(recording_id: str, stage_index: int) -> None:
    """Run stage *stage_index* of the recording's resolved pipeline and, on
    success, emit the next stage (or finalize). All within one locked,
    atomic unit."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("run_stage: recording %s not found", recording_id)
            return

        if recording.status in _TERMINAL:
            return

        pipeline = resolve_pipeline(recording)
        current = _current_index(recording)
        if stage_index < current:
            return  # stale redelivery — already progressed past this stage

        if stage_index >= len(pipeline):
            _finalize(recording)
            return

        stage_name = pipeline[stage_index]
        try:
            stage = get_stage(stage_name)
        except (KeyError, TypeError) as exc:
            _dlq(recording, stage=stage_name, reason=f"unresolvable_stage: {exc}")
            return

        _set_current(recording, stage_index, stage_name)
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
        recording.save(update_fields=["retry_count", "metadata", "updated_at"])

        events.emit_stage_completed(recording, stage_name, stage_index)
        events.emit_stage(recording.id, stage_index + 1)


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


def _current_index(recording: Recording) -> int:
    return (recording.metadata or {}).get("pipeline", {}).get("stage_index", -1)


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
]
