"""comm Action names + emit helpers for the recordings pipeline.

All side effects leave through the transactional outbox (``emit`` writes
the event with the caller's DB transaction; delivery happens after
commit). No inline publish, so there is no publish-after-commit loss —
the prior raw-bus dual-write bug is gone.

Action surface:

- ``recording.uploaded`` (public, entry) — a file has landed; the driver
  starts the pipeline. Emitted by ``services.finalize_upload``.
- ``recording.stage`` (internal) — "run stage N for this recording". The
  generic driver both emits and consumes this to walk the resolved
  stage-list. Idempotent: re-delivery re-runs an idempotent stage.
- ``recording.stage_completed`` (public) — informational; observers can
  react to a specific stage finishing (e.g. billing on "transcribe").
- ``recording.completed`` (public, terminal) — pipeline exhausted.
- ``recording.failed`` (public, terminal / DLQ) — a stage gave up.

Every name has a JSON schema under ``schemas/emits/`` validated in tests.
"""
from __future__ import annotations

from stapel_core.comm import emit

ACTION_UPLOADED = "recording.uploaded"
ACTION_STAGE = "recording.stage"
ACTION_STAGE_COMPLETED = "recording.stage_completed"
ACTION_COMPLETED = "recording.completed"
ACTION_FAILED = "recording.failed"


def emit_uploaded(recording) -> None:
    emit(
        ACTION_UPLOADED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "owner_id": str(recording.owner_id) if recording.owner_id else None,
            "storage_key": recording.file_storage_key,
            "file_size_bytes": recording.file_size_bytes,
            "source_type": recording.source_type,
        },
        key=str(recording.id),
    )


def emit_stage(recording_id: str, stage_index: int) -> None:
    emit(
        ACTION_STAGE,
        {"recording_id": str(recording_id), "stage_index": int(stage_index)},
        key=str(recording_id),
    )


def emit_stage_completed(recording, stage: str, stage_index: int) -> None:
    emit(
        ACTION_STAGE_COMPLETED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "stage": stage,
            "stage_index": int(stage_index),
            "status": recording.status,
        },
        key=str(recording.id),
    )


def emit_completed(recording) -> None:
    emit(
        ACTION_COMPLETED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "owner_id": str(recording.owner_id) if recording.owner_id else None,
            "duration_seconds": recording.duration_seconds,
            "segments_count": recording.segments_count,
            "speakers_count": recording.speakers_count,
            "word_count": recording.word_count,
            "provider_used": recording.provider_used,
        },
        key=str(recording.id),
    )


def emit_failed(recording, *, stage: str, reason: str, user_retryable: bool) -> None:
    emit(
        ACTION_FAILED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "stage": stage,
            "reason": reason,
            "user_retryable": bool(user_retryable),
        },
        key=str(recording.id),
    )


__all__ = [
    "ACTION_UPLOADED",
    "ACTION_STAGE",
    "ACTION_STAGE_COMPLETED",
    "ACTION_COMPLETED",
    "ACTION_FAILED",
    "emit_uploaded",
    "emit_stage",
    "emit_stage_completed",
    "emit_completed",
    "emit_failed",
]
