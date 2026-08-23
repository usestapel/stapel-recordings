"""Scheduled work of stapel-recordings — the purge that closes the loop.

A recording the user deletes is soft-deleted: ``deleted_at`` is stamped, the
row leaves every listing, and the audio, the transcript and the embeddings
stay exactly where they were. Nothing ever removed them. "Deleted from your
account" was a UI state, and the only thing that could still erase the bytes
was closing the whole account.

:func:`purge_soft_deleted_recordings` is the missing half. Once a recording
has been soft-deleted for ``PURGE_AFTER_DAYS``, it does not delete it
directly — it opens a gdpr **erasure request** for it. That matters:

- the erasure is receipted by every data owner that claims the ``recording``
  subject (this module for the rows and objects, media/cdn for derived
  files, the agent for prompts about it), so a single-recording delete is
  proven the same way an account closure is;
- the product can show the state ("pending deletion until X") from
  ``GET /gdpr/api/v1/erasures/{id}`` instead of guessing;
- this module's own erasure subscriber (``actions.py``) is what finally
  destroys the rows — one destruction path, not two.

Celery is OPTIONAL. :func:`purge_soft_deleted_recordings` is a plain
callable any scheduler can invoke; when celery is installed it is also
registered as a shared task under the stable name
:data:`PURGE_TASK_NAME`. Wire it into a host's beat schedule:

    from stapel_recordings.tasks import get_recordings_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_recordings_beat_schedule(),
        ...
    }

The cadence is configuration (``STAPEL_RECORDINGS["PURGE_SCHEDULE"]``, a
crontab kwargs dict), not a literal, and ``checks.py`` warns
(``stapel_recordings.W010``) when a host drives a beat schedule that runs
nothing here — a retention job nobody scheduled is a promise, not a
mechanism.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The name a beat schedule must reference (stable across refactors).
PURGE_TASK_NAME = "stapel_recordings.tasks.purge_soft_deleted_recordings"


def purge_soft_deleted_recordings(limit: int | None = None) -> dict:
    """Open an erasure for every recording soft-deleted longer than
    ``PURGE_AFTER_DAYS`` ago. Returns what it did, and logs it.

    Skips a recording that already has an erasure in flight: the request is
    open until every owner receipts (or the orchestrator times it out), and
    asking again daily would mint a queue of duplicates for the one subject
    whose owners are already the problem.

    Returns ``{"aged", "requested", "already_open", "skipped"}``. ``skipped``
    is non-zero only when no erasure client is available (no stapel-gdpr in
    this deployment) — the aged rows are then reported and left alone rather
    than destroyed outside the receipts path.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .conf import recordings_settings
    from .erasure import SUBJECT_RECORDING, get_erasure_client
    from .models import Recording

    days = int(recordings_settings.PURGE_AFTER_DAYS)
    cutoff = timezone.now() - timedelta(days=days)
    aged = Recording.objects.filter(
        deleted_at__isnull=False, deleted_at__lt=cutoff
    ).order_by("deleted_at")
    if limit:
        aged = aged[:limit]

    result = {"aged": 0, "requested": 0, "already_open": 0, "skipped": 0}
    client = get_erasure_client()
    if not client.available():
        result["aged"] = result["skipped"] = aged.count()
        if result["aged"]:
            logger.error(
                "recordings purge: %d recording(s) soft-deleted more than %d days "
                "ago, but no erasure client is available (is stapel_gdpr "
                "installed?) — nothing was erased",
                result["aged"], days,
            )
        return result

    for recording in aged:
        result["aged"] += 1
        key = str(recording.id)
        if client.has_open_erasure(SUBJECT_RECORDING, key):
            result["already_open"] += 1
            continue
        client.request_erasure(
            SUBJECT_RECORDING, key, workspace_id=str(recording.workspace_id)
        )
        result["requested"] += 1

    if result["aged"]:
        logger.info(
            "recordings purge: %d aged, %d erasure(s) requested, %d already open",
            result["aged"], result["requested"], result["already_open"],
        )
    return result


def get_recordings_beat_schedule() -> dict:
    """Beat entry for the retention purge, on the configured cadence."""
    from celery.schedules import crontab

    from .conf import recordings_settings

    schedule = dict(recordings_settings.PURGE_SCHEDULE or {})
    return {
        "recordings-soft-delete-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**schedule),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_soft_deleted_recordings = shared_task(name=PURGE_TASK_NAME)(
        purge_soft_deleted_recordings
    )


__all__ = [
    "PURGE_TASK_NAME",
    "purge_soft_deleted_recordings",
    "get_recordings_beat_schedule",
]
