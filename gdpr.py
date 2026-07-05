"""GDPR data handler for the recordings a user owns.

Recordings hold user data (owner, titles, transcripts, audio objects), so
this module is a GDPR data holder: it registers a provider (monolith
mode) and an ``@on_action("user.deleted")`` consumer (``actions.py``).
Storage objects are erased through the STORAGE seam, not a hardcoded S3
client.
"""
from __future__ import annotations

import logging

from stapel_core.gdpr import GDPRProvider

logger = logging.getLogger(__name__)


class GDPRStorageDeleteError(RuntimeError):
    """Raised when one or more storage objects could not be erased. The rows
    referencing them are kept, so the at-least-once retry paths (``user.deleted``
    redelivery / the GDPR orchestrator) can re-drive the erasure."""


class RecordingsGDPRProvider(GDPRProvider):
    section = "recordings"

    def export(self, user_id) -> dict:
        from .models import Recording

        rows = Recording.objects.filter(owner_id=user_id, deleted_at__isnull=True)
        return {
            "recordings": [
                {
                    "id": str(r.id),
                    "workspace_id": str(r.workspace_id),
                    "title": r.title,
                    "status": r.status,
                    "language": r.language,
                    "duration_seconds": r.duration_seconds,
                    "provider_used": r.provider_used,
                    "summary": r.summary,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }

    def delete(self, user_id) -> None:
        """Hard-delete the user's recordings and every storage object they
        reference (raw, normalized, transcript). Cascade removes Speaker/
        Segment/UploadSession/Job rows.

        Reliability contract (idempotent, at-least-once):

        - Rows are locked (``select_for_update``) before their keys are
          snapshotted, serializing with ``run_stage`` — a live stage cannot
          commit a *new* object key (normalized/transcript) for a row we are
          about to delete, so no orphan slips through the race window.
        - A row is deleted only after **all** of its objects were deleted.
          Failures are collected and re-raised at the end
          (:class:`GDPRStorageDeleteError`) *after* the clean rows' deletion
          committed — the caller's retry (``user.deleted`` redelivery or the
          GDPR orchestrator) re-runs erasure for the kept rows only.
        """
        from django.db import transaction

        from .models import Recording
        from .storage import get_storage

        storage = get_storage()
        failed_keys: list[str] = []
        with transaction.atomic():
            rows = Recording.objects.select_for_update().filter(owner_id=user_id)
            deletable_ids = []
            for r in rows:
                row_ok = True
                for key in (r.file_storage_key, r.normalized_storage_key, r.transcript_storage_key):
                    if not key:
                        continue
                    try:
                        storage.delete_object(key)
                    except Exception:
                        logger.warning(
                            "gdpr: could not delete object %s for user %s (row kept for retry)",
                            key, user_id, exc_info=True,
                        )
                        row_ok = False
                        failed_keys.append(key)
                if row_ok:
                    deletable_ids.append(r.pk)
            Recording.objects.filter(pk__in=deletable_ids).delete()
        if failed_keys:
            raise GDPRStorageDeleteError(
                f"could not delete {len(failed_keys)} storage object(s) for user {user_id}; "
                "the referencing recording rows were kept — erasure will be retried"
            )

    def anonymize(self, user_id) -> None:
        # Recordings are hard-deleted (they are private user artifacts), so
        # there is no retained-but-anonymized content to scrub.
        pass


__all__ = ["RecordingsGDPRProvider", "GDPRStorageDeleteError"]
