"""GDPR data handler for the recordings a user owns.

Recordings hold user data (owner, titles, transcripts, audio objects), so
this module is a GDPR data holder: it registers a provider (monolith mode)
and the erasure/probe subscribers in ``actions.py`` (any transport).

This file owns the *account* view of that data — export, and the in-process
provider interface, which is account-scoped by construction. Destruction
itself lives in :mod:`stapel_recordings.erasure`, which is subject-scoped:
:meth:`RecordingsGDPRProvider.delete` is a thin call into
``erase("account", user_id)``, the same function the comm subscriber runs
for every other subject. One deletion path, four subjects — a provider that
destroyed rows its own way would be the second implementation that drifts.

Storage objects are erased through the STORAGE seam, not a hardcoded S3
client.
"""
from __future__ import annotations

import logging

from stapel_core.gdpr import GDPRProvider

from .erasure import SUBJECT_ACCOUNT, GDPRStorageDeleteError, erase

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
        reference — :func:`stapel_recordings.erasure.erase` for the
        ``account`` subject.

        Idempotent and at-least-once safe; a storage failure keeps the
        referencing rows and raises :class:`GDPRStorageDeleteError` so the
        caller's retry can re-drive it. See ``erasure.erase`` for the full
        contract (row locking against a live pipeline stage, per-row
        all-or-nothing object deletion, cascade counts).
        """
        erase(SUBJECT_ACCOUNT, user_id)

    def anonymize(self, user_id) -> None:
        # Recordings are hard-deleted (they are private user artifacts), so
        # there is no retained-but-anonymized content to scrub.
        pass


__all__ = ["RecordingsGDPRProvider", "GDPRStorageDeleteError"]
