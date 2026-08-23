"""Subject-scoped erasure of everything this module owns.

stapel-gdpr 0.5.0 made the *subject* of an erasure a parameter: an account,
a workspace, a meeting or a recording all get the same machine — one
request, one receipt per data owner that claims the subject type, and a
refusal to certify a deletion on an owner's silence. This module is that
side of the protocol for recordings, and it is deliberately the ONLY place
that knows how to destroy a recording:

- :func:`recordings_for` turns a ``(subject_type, subject_key)`` pair into
  the rows this module owns about that subject;
- :func:`erase` destroys them — storage objects first, rows second — and
  returns what it removed, so a receipt can carry counts rather than a
  claim;
- :class:`GDPRErasureClient` is the other direction: how this module ASKS
  for an erasure (the scheduled purge in ``tasks.py``), through whatever
  gdpr client the host configured rather than by importing a service.

Idempotency is a requirement, not a property: delivery is at-least-once, so
a second erasure of the same subject must find nothing, remove nothing, and
still be able to receipt. That is what makes a redelivered
``gdpr.erasure.requested`` harmless.

Storage objects go through the module's own STORAGE seam
(``stapel_recordings.storage.get_storage``) — the same one uploads and the
pipeline use. There is no second object client here: a bucket reached by a
path this module does not otherwise use is a bucket erasure will one day
miss.
"""
from __future__ import annotations

import logging
import re
import uuid as uuid_lib
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

#: The owner name this module answers to in the gdpr protocol. Must equal
#: ``RecordingsGDPRProvider.section`` — the orchestrator matches receipts by
#: this string, and a module that erases under one name and receipts under
#: another looks exactly like an owner that never answered.
OWNER = "recordings"

SUBJECT_ACCOUNT = "account"
SUBJECT_WORKSPACE = "workspace"
SUBJECT_MEETING = "meeting"
SUBJECT_RECORDING = "recording"

#: The subject types this module claims, in the vocabulary of
#: ``STAPEL_GDPR["DATA_OWNERS"]``. Declared in one place because two
#: consumers read it: the erasure handler (what it will act on) and the
#: probe answer (what it tells the orchestrator it owns). If those two ever
#: disagree, ``gdpr.W006`` reports an owner that is alive for subjects it
#: silently ignores.
SUBJECT_TYPES = (
    SUBJECT_ACCOUNT,
    SUBJECT_WORKSPACE,
    SUBJECT_MEETING,
    SUBJECT_RECORDING,
)

#: Key under ``Recording.metadata`` a host uses to link a recording to its
#: own meeting entity. A recording IS the meeting for hosts that have no
#: separate object (``transcript_schema`` already numbers transcripts by
#: ``meeting_id = recording.id``), so the ``meeting`` subject matches BOTH:
#: the recording with that id, and every recording tagged with it here.
MEETING_METADATA_KEY = "meeting_id"


class GDPRStorageDeleteError(RuntimeError):
    """Raised when one or more storage objects could not be erased. The rows
    referencing them are kept, so the at-least-once retry paths (action
    redelivery / the GDPR orchestrator's timeout sweep) can re-drive the
    erasure instead of losing the only pointer to an orphaned object."""


class ErasureClient(ABC):
    """How this module ASKS for an erasure (the ``ERASURE_CLIENT`` seam).

    The scheduled purge does not delete anything itself: it opens an erasure
    request so a recording the user removed 30 days ago rides the same
    receipts path as an account closure and the product can show its state.
    Which mechanism opens that request is the host's deployment question —
    an in-process orchestrator in a monolith, an HTTP call to the gdpr
    service across the bus — so it is a seam, not an import.
    """

    @abstractmethod
    def available(self) -> bool:
        """Can this client open erasures right now? False makes the purge a
        reported no-op rather than an exception per aged row."""

    @abstractmethod
    def has_open_erasure(self, subject_type: str, subject_key: str) -> bool:
        """Is an erasure for this subject already in flight? The purge must
        not re-ask every day for a subject whose owners have not all
        answered yet."""

    @abstractmethod
    def request_erasure(
        self,
        subject_type: str,
        subject_key: str,
        *,
        workspace_id: str | None = None,
    ) -> str | None:
        """Open one erasure; returns its correlation id when the client
        knows it."""


class GDPRErasureClient(ErasureClient):
    """Default client: stapel-gdpr's orchestrator, in this process.

    Resolved lazily and by name — this package does not depend on
    stapel-gdpr, and a host that runs recordings without it gets an
    unavailable client (a logged, counted no-op) rather than an ImportError
    inside a scheduled task.
    """

    def _gdpr(self):
        """``(orchestrator, ErasureRequest)`` when stapel-gdpr runs here.

        A real import inside a guard, not a dotted-path string handed to the
        app registry: a literal naming another package's symbol has no
        remote form at all (it answers 503 the day the two run in separate
        processes), which is what the ``ERASURE_CLIENT`` seam exists to let
        a host replace. Two failure shapes, one meaning — "not in this
        process": ``ImportError`` when the package is absent, and
        ``RuntimeError`` when it is importable but missing from
        ``INSTALLED_APPS``, so its models have no app registry to belong to.
        """
        try:
            from stapel_gdpr.models import ErasureRequest
            from stapel_gdpr.orchestrator import gdpr_orchestrator
        except (ImportError, RuntimeError):
            return None
        return gdpr_orchestrator, ErasureRequest

    def available(self) -> bool:
        return self._gdpr() is not None

    def has_open_erasure(self, subject_type: str, subject_key: str) -> bool:
        resolved = self._gdpr()
        if resolved is None:  # pragma: no cover — callers check available()
            return False
        _, erasure_request = resolved
        return erasure_request.objects.filter(
            subject_type=subject_type,
            subject_key=str(subject_key),
            state__in=(erasure_request.STATE_QUEUED, erasure_request.STATE_ERASING),
        ).exists()

    def request_erasure(
        self,
        subject_type: str,
        subject_key: str,
        *,
        workspace_id: str | None = None,
    ) -> str | None:
        resolved = self._gdpr()
        if resolved is None:  # pragma: no cover — callers check available()
            return None
        orchestrator, _ = resolved
        request = orchestrator.request_erasure(
            subject_type,
            str(subject_key),
            workspace_id=workspace_id,
            # The user's own delete is what started this clock; the purge
            # only opens the request when the retention window closes.
            origin="user",
        )
        return getattr(request, "correlation_id", None)


def get_erasure_client() -> ErasureClient:
    """The configured ``ERASURE_CLIENT``, instantiated."""
    from .conf import recordings_settings

    return recordings_settings.ERASURE_CLIENT()


def _as_uuid(value) -> str | None:
    try:
        return str(uuid_lib.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def recordings_for(subject_type: str, subject_key, *, workspace_id=None):
    """The recordings this module owns about one subject.

    Returns an empty queryset — never raises — for a subject key that cannot
    address anything here (a malformed uuid, a user key of the wrong type).
    An owner that cannot find a subject owns nothing about it, and saying so
    with a zero-count receipt is the honest answer; raising would stall the
    whole request behind a redelivery loop that can never succeed.
    """
    from .models import Recording

    if subject_type == SUBJECT_ACCOUNT:
        try:
            return Recording.objects.filter(owner_id=subject_key)
        except (ValueError, TypeError):
            pass
    elif subject_type == SUBJECT_WORKSPACE:
        key = _as_uuid(subject_key)
        if key:
            return Recording.objects.filter(workspace_id=key)
    elif subject_type == SUBJECT_RECORDING:
        key = _as_uuid(subject_key)
        if key:
            return Recording.objects.filter(pk=key)
    elif subject_type == SUBJECT_MEETING:
        from django.db.models import Q

        criteria = Q(**{f"metadata__{MEETING_METADATA_KEY}": str(subject_key)})
        key = _as_uuid(subject_key)
        if key:
            criteria |= Q(pk=key)
        rows = Recording.objects.filter(criteria)
        # A host's meeting id is unique inside a workspace, not necessarily
        # across the platform, so narrow when the request states one.
        scope = _as_uuid(workspace_id)
        return rows.filter(workspace_id=scope) if scope else rows
    else:
        logger.warning("erasure: unclaimed subject type %r ignored", subject_type)
        return Recording.objects.none()

    logger.warning(
        "erasure: subject %s=%r does not address any recording", subject_type, subject_key
    )
    return Recording.objects.none()


def _count_key(label: str) -> str:
    """``"recordings.UploadSession"`` -> ``"upload_sessions"``."""
    name = label.split(".")[-1]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() + "s"


def erase(subject_type: str, subject_key, *, workspace_id=None) -> dict[str, int]:
    """Hard-delete every recording this module owns about one subject, with
    its storage objects, and return what was removed.

    Reliability contract (idempotent, at-least-once):

    - Rows are locked (``select_for_update``) before their keys are
      snapshotted, serializing with ``run_stage`` — a live stage cannot
      commit a *new* object key (normalized/transcript) for a row we are
      about to delete, so no orphan slips through the race window.
    - A row is deleted only after **all** of its objects were deleted.
      Failures are collected and re-raised at the end
      (:class:`GDPRStorageDeleteError`) *after* the clean rows' deletion
      committed — the caller's retry re-runs erasure for the kept rows only.
    - Children go with the parent by FK cascade: ``Speaker``, ``Segment``,
      ``UploadSession``, ``RecordingShare``, ``Job``, and — when the opt-in
      vector app is installed — ``SegmentEmbedding`` (through ``Segment``)
      and ``RecordingEmbedding``. The counts are Django's own per-model
      deletion tally, so an embedding table nobody remembered still shows up
      in the receipt instead of quietly surviving.
    - A second call finds nothing: zero counts, no error. That is what makes
      a redelivered ``gdpr.erasure.requested`` harmless.
    """
    from django.db import transaction

    from .models import Recording
    from .storage import get_storage

    storage = get_storage()
    counts: dict[str, int] = {}
    failed_keys: list[str] = []

    with transaction.atomic():
        rows = recordings_for(
            subject_type, subject_key, workspace_id=workspace_id
        ).select_for_update()
        deletable_ids = []
        for row in rows:
            row_ok = True
            for key in (
                row.file_storage_key,
                row.normalized_storage_key,
                row.transcript_storage_key,
            ):
                if not key:
                    continue
                try:
                    storage.delete_object(key)
                except Exception:
                    logger.warning(
                        "erasure: could not delete object %s for %s=%s "
                        "(row kept for retry)",
                        key, subject_type, subject_key, exc_info=True,
                    )
                    row_ok = False
                    failed_keys.append(key)
                else:
                    counts["storage_objects"] = counts.get("storage_objects", 0) + 1
            if row_ok:
                deletable_ids.append(row.pk)
        if deletable_ids:
            _, per_model = Recording.objects.filter(pk__in=deletable_ids).delete()
            for label, removed in per_model.items():
                if removed:
                    name = _count_key(label)
                    counts[name] = counts.get(name, 0) + removed

    if failed_keys:
        raise GDPRStorageDeleteError(
            f"could not delete {len(failed_keys)} storage object(s) for "
            f"{subject_type}={subject_key}; the referencing recording rows were "
            "kept — erasure will be retried"
        )
    return counts


__all__ = [
    "OWNER",
    "SUBJECT_TYPES",
    "SUBJECT_ACCOUNT",
    "SUBJECT_WORKSPACE",
    "SUBJECT_MEETING",
    "SUBJECT_RECORDING",
    "MEETING_METADATA_KEY",
    "GDPRStorageDeleteError",
    "ErasureClient",
    "GDPRErasureClient",
    "get_erasure_client",
    "recordings_for",
    "erase",
]
