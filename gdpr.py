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
        Segment/UploadSession/Job rows."""
        from .models import Recording
        from .storage import get_storage

        storage = get_storage()
        rows = Recording.objects.filter(owner_id=user_id)
        for r in rows:
            for key in (r.file_storage_key, r.normalized_storage_key, r.transcript_storage_key):
                if key:
                    try:
                        storage.delete_object(key)
                    except Exception:
                        logger.warning("gdpr: could not delete object %s for user %s", key, user_id)
        rows.delete()

    def anonymize(self, user_id) -> None:
        # Recordings are hard-deleted (they are private user artifacts), so
        # there is no retained-but-anonymized content to scrub.
        pass


__all__ = ["RecordingsGDPRProvider"]
