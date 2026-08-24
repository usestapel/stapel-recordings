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

Run identity on the run events. ``recording.stage_completed`` /
``recording.completed`` / ``recording.failed`` all carry ``run_id`` and
``attempt`` (see ``pipeline.run_identity``). A recording can be put through
the pipeline again (``pipeline.reprocess_recording``) and every run costs the
host money, so the terminal event of run 2 must not be indistinguishable
from run 1's: without run identity a metering consumer can only build the
idempotency key ``recording:<id>``, its second debit short-circuits on the
first run's transaction, and every re-run is free. The key to build is
``recording:<id>:<run_id>``. ``recording.resummarized`` has carried the
equivalent since 0.17.0 — its ``job_id`` is minted per re-summary — and
``recording.uploaded`` needs none: it is emitted once, when the file lands.
- ``recording.resummarized`` (public) — a summary was regenerated on its
  own, outside the pipeline, because a user asked for it. Separate from
  ``recording.stage_completed(stage="merge")`` on purpose: that one says a
  pipeline run passed through summarization, this one says somebody spent a
  request on a new summary of an unchanged recording, which is the event a
  host meters or bills.
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
ACTION_RESUMMARIZED = "recording.resummarized"


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


def _run_fields(run_id, attempt) -> dict:
    """The run-identity half of a run event's payload.

    Omitted rather than sent as null when the caller has no run — the schemas
    keep both keys optional so a payload from before run identity existed
    still validates.
    """
    if not run_id:
        return {}
    return {"run_id": str(run_id), "attempt": int(attempt or 1)}


def emit_stage_completed(
    recording, stage: str, stage_index: int, *, run_id=None, attempt=None
) -> None:
    emit(
        ACTION_STAGE_COMPLETED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "stage": stage,
            "stage_index": int(stage_index),
            "status": recording.status,
            **_run_fields(run_id, attempt),
        },
        key=str(recording.id),
    )


def emit_completed(recording, *, run_id=None, attempt=None) -> None:
    """The terminal receipt of one pipeline run.

    ``run_id`` + ``attempt`` identify THAT run: they are what a consumer
    metering this event keys its idempotency on (``recording:<id>:<run_id>``),
    because the same recording can be run again and the rest of this payload
    would be identical.
    """
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
            **_run_fields(run_id, attempt),
        },
        key=str(recording.id),
    )


def emit_resummarized(recording, *, job_id, user_id=None) -> None:
    """A standalone re-summary finished and its summary is stored.

    ``job_id`` travels with it as the idempotency key: delivery is
    at-least-once, so a host that debits credits for this needs something
    that identifies THIS re-summary and not merely this recording, which can
    be re-summarized any number of times.
    """
    emit(
        ACTION_RESUMMARIZED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "user_id": str(user_id) if user_id is not None else None,
            "job_id": str(job_id),
        },
        key=str(recording.id),
    )


def emit_failed(
    recording, *, stage: str, reason: str, user_retryable: bool, run_id=None, attempt=None
) -> None:
    emit(
        ACTION_FAILED,
        {
            "recording_id": str(recording.id),
            "workspace_id": str(recording.workspace_id),
            "stage": stage,
            "reason": reason,
            "user_retryable": bool(user_retryable),
            **_run_fields(run_id, attempt),
        },
        key=str(recording.id),
    )


__all__ = [
    "ACTION_UPLOADED",
    "ACTION_STAGE",
    "ACTION_STAGE_COMPLETED",
    "ACTION_COMPLETED",
    "ACTION_FAILED",
    "ACTION_RESUMMARIZED",
    "emit_uploaded",
    "emit_stage",
    "emit_stage_completed",
    "emit_completed",
    "emit_failed",
    "emit_resummarized",
]
