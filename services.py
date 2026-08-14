"""Service-layer helpers: recording creation, upload sessions, finalize.

Object I/O goes through the STORAGE seam; the pipeline is kicked by
emitting ``recording.uploaded`` through the transactional outbox (no inline
publish → no publish-after-commit loss).

Upload invariants
-----------------
A presigned URL is an unattended write into the bucket: between issuing it
and finalizing, the only party in the loop is the client. Everything the
client says about the object (its name, its size, its type) is therefore a
*request*, and the server's own reading of the stored object is the only
fact. This module is written around that split:

- a declared size is checked **before** any storage state exists, and is
  what the session binds the object to — not a number recorded for
  display (``UploadSession.max_size_bytes`` is an enforced ceiling);
- the number of presigned part URLs one session mints is capped;
- one recording has at most one live upload session — a new one supersedes
  and aborts the old one instead of leaving orphan multiparts behind;
- ``finalize_upload`` accepts nothing on the client's word: the object must
  exist, its measured size must be within the session ceiling, and its
  leading bytes must pass the content policy. A failure aborts and cleans
  up, leaves the recording out of ``queued``, and **does not enqueue the
  pipeline** — no downstream work is started by an upload that never
  satisfied its invariants;
- and a check that cannot RUN counts as a failure, not as a pass: if the
  storage backend cannot serve the ranged read the content policy needs,
  finalize refuses (:class:`UploadContentUncheckable`) instead of accepting
  bytes nothing has looked at. Accepting them is a decision, and it is
  spelled ``UPLOAD_CONTENT_POLICY = "off"``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import events, media_types
from .conf import recordings_settings
from .models import Recording, RecordingStatus, UploadSession
from .storage import get_storage

logger = logging.getLogger(__name__)

#: Comm Function the workspaces module exposes to answer membership questions
#: by name (no import of that app). See stapel_workspaces.functions.
WORKSPACES_CHECK_MEMBERSHIP = "workspaces.check_membership"


class UploadTooLarge(ValueError):
    """A declared or measured upload size is outside the allowed range."""

    def __init__(self, size, limit):
        super().__init__(f"upload size {size!r} exceeds limit {limit}")
        self.size = size
        self.limit = limit


class UploadNotStored(ValueError):
    """Finalize was asked to complete an upload whose object is missing or
    empty in storage."""

    def __init__(self, key: str):
        super().__init__(f"no stored object for upload key {key!r}")
        self.key = key


class InvalidMultipartParts(ValueError):
    """The caller-supplied multipart part list is malformed or oversized."""


class UploadContentUncheckable(RuntimeError):
    """``UPLOAD_CONTENT_POLICY`` is on, but the storage backend cannot serve
    the ranged read the gate needs.

    A deployment fault, not a caller fault: the upload may well be fine, but
    nothing here can tell. A gate that cannot run refuses — the alternative
    is that the policy silently does not apply."""

    def __init__(self, key: str, backend: str, policy: str):
        super().__init__(
            f"{backend} cannot serve a ranged read, so UPLOAD_CONTENT_POLICY "
            f"{policy!r} could not be applied to {key!r}"
        )
        self.key = key
        self.backend = backend
        self.policy = policy


class UnsupportedUploadExtension(ValueError):
    """Raised when a caller-supplied upload filename is missing, has no
    extension, or one outside ``UPLOAD_EXTENSION_ALLOWLIST``."""

    def __init__(self, ext: str):
        super().__init__(f"unsupported upload extension: {ext!r}")
        self.ext = ext


def validated_upload_ext(filename: str) -> str:
    """Return the object-key suffix (``.mp3``) for *filename*. A missing
    filename, one with no extension, or one outside the allowlist raises
    :class:`UnsupportedUploadExtension`."""
    if not filename:
        raise UnsupportedUploadExtension(filename)
    _, dot, ext = filename.rpartition(".")
    ext = ext.strip().lower()
    if not dot or not ext:
        raise UnsupportedUploadExtension(filename)
    allowlist = {e.lower() for e in (recordings_settings.UPLOAD_EXTENSION_ALLOWLIST or [])}
    if ext not in allowlist:
        raise UnsupportedUploadExtension(ext)
    return f".{ext}"


def _storage_key(recording: Recording, *, filename: str) -> str:
    prefix = recordings_settings.STORAGE_PREFIX.strip("/")
    base = f"{prefix}/{recording.workspace_id}/{recording.id}/audio"
    return f"{base}{validated_upload_ext(filename)}"


def check_workspace_membership(*, user_id, workspace_id) -> bool:
    """True iff *user_id* is an accepted member of *workspace_id*.

    Asks the workspaces module by comm name (``workspaces.check_membership``)
    — no import of that app, the transport is deployment config. **Fails
    closed**: any wiring/provider failure (workspaces not deployed, route not
    configured, provider error) denies access rather than leaking another
    member's recordings."""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    if user_id is None or workspace_id is None:
        return False
    try:
        result = call(
            WORKSPACES_CHECK_MEMBERSHIP,
            {"workspace_id": str(workspace_id), "user_id": str(user_id)},
        )
    except CommError:
        return False
    return bool(isinstance(result, dict) and result.get("is_member"))


def _checked_declared_size(declared: int | None, *, required: bool) -> int:
    """Validate a caller-declared upload size against ``MAX_UPLOAD_BYTES``.

    Runs before any storage state is created: an oversized request must not
    leave a session row, a multipart upload id or a signed URL behind."""
    limit = int(recordings_settings.MAX_UPLOAD_BYTES)
    if declared is None:
        if required:
            raise UploadTooLarge(declared, limit)
        return limit
    try:
        size = int(declared)
    except (TypeError, ValueError) as exc:
        raise UploadTooLarge(declared, limit) from exc
    if size <= 0 or size > limit:
        raise UploadTooLarge(size, limit)
    return size


def _supersede_open_sessions(recording: Recording) -> None:
    """Close any not-yet-finalized session of *recording*.

    One recording, one live upload session. Without this, every retry of the
    client's "start upload" call mints another presigned URL and (for
    multipart) another server-side upload id that nothing ever aborts —
    unbounded orphan state in the bucket, paid for by the tenant."""
    for stale in recording.upload_sessions.filter(finalized_at__isnull=True):
        try:
            abort_multipart_upload_session(session=stale)
        except Exception:  # cleanup must not block the new session
            logger.warning(
                "upload: could not abort superseded session %s for recording %s",
                stale.pk, recording.pk, exc_info=True,
            )
            stale.delete()


def create_upload_session(
    *,
    recording: Recording,
    filename: str,
    declared_size_bytes: int | None = None,
    content_type: str | None = None,
) -> UploadSession:
    """Create a single-PUT presigned upload session.

    The *filename* extension is validated against
    ``UPLOAD_EXTENSION_ALLOWLIST`` and appended to the object key
    (``…/audio.mp3``).

    *declared_size_bytes*, when given, is validated against
    ``MAX_UPLOAD_BYTES`` up front and becomes the session's enforced
    ceiling — finalize rejects an object measured above it. Without it the
    ceiling is ``MAX_UPLOAD_BYTES``. *content_type* is bound into the
    presigned URL where the backend supports it, so the signature only
    admits an upload declaring that type."""
    max_size = _checked_declared_size(declared_size_bytes, required=False)
    storage = get_storage()
    key = _storage_key(recording, filename=filename)
    ttl = int(recordings_settings.UPLOAD_SESSION_TTL_SECONDS)
    _supersede_open_sessions(recording)
    presigned_url = storage.presigned_put_url(
        key, expires_seconds=ttl, content_type=content_type
    )
    session = UploadSession.objects.create(
        recording=recording,
        presigned_url=presigned_url,
        storage_key=key,
        max_size_bytes=max_size,
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    if recording.status == RecordingStatus.CREATED:
        recording.status = RecordingStatus.UPLOADING
        recording.save(update_fields=["status", "updated_at"])
    return session


def start_multipart_upload(
    *,
    recording: Recording,
    file_size_bytes: int,
    content_type: str | None = None,
    filename: str,
) -> tuple[UploadSession, list[dict], int]:
    """Initiate a multipart upload. Returns (session, parts, part_size).

    *filename* behaves as in :func:`create_upload_session` (validated
    extension appended to the object key). *file_size_bytes* is required,
    must be positive and within ``MAX_UPLOAD_BYTES``, and becomes the
    session's enforced ceiling; the derived part count is additionally
    capped by ``MAX_MULTIPART_PARTS``."""
    max_size = _checked_declared_size(file_size_bytes, required=True)
    part_size = int(recordings_settings.MULTIPART_PART_SIZE)
    num_parts = max(1, (max_size + part_size - 1) // part_size)
    part_cap = int(recordings_settings.MAX_MULTIPART_PARTS)
    if num_parts > part_cap:
        raise InvalidMultipartParts(
            f"{num_parts} parts exceeds MAX_MULTIPART_PARTS ({part_cap}) — "
            "raise MULTIPART_PART_SIZE or lower MAX_UPLOAD_BYTES"
        )

    storage = get_storage()
    key = _storage_key(recording, filename=filename)
    ttl = int(recordings_settings.MULTIPART_SESSION_TTL_SECONDS)
    _supersede_open_sessions(recording)

    upload_id = storage.create_multipart_upload(key, content_type=content_type)
    parts = [
        {
            "part_number": n,
            "presigned_url": storage.presigned_upload_part_url(
                key, upload_id, n, expires_seconds=ttl
            ),
        }
        for n in range(1, num_parts + 1)
    ]
    session = UploadSession.objects.create(
        recording=recording,
        presigned_url="",
        storage_key=key,
        max_size_bytes=max_size,
        expires_at=timezone.now() + timedelta(seconds=ttl),
        is_multipart=True,
        multipart_upload_id=upload_id,
    )
    if recording.status == RecordingStatus.CREATED:
        recording.status = RecordingStatus.UPLOADING
        recording.save(update_fields=["status", "updated_at"])
    return session, parts, part_size


def abort_multipart_upload_session(*, session: UploadSession) -> None:
    if session.multipart_upload_id:
        get_storage().abort_multipart_upload(session.storage_key, session.multipart_upload_id)
    session.delete()


def _validated_parts(session: UploadSession, parts: list[dict] | None) -> list[dict]:
    """Check the caller's part list before it is sent to storage.

    Validation happens *before* ``complete_multipart_upload`` because a
    completed multipart is a materialized object: rejecting afterwards means
    the bad object already exists and has to be cleaned up."""
    items = list(parts or [])
    cap = int(recordings_settings.MAX_MULTIPART_PARTS)
    if len(items) > cap:
        raise InvalidMultipartParts(f"{len(items)} parts exceeds MAX_MULTIPART_PARTS ({cap})")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise InvalidMultipartParts(f"part entry is not an object: {item!r}")
        number = item.get("PartNumber", item.get("part_number"))
        try:
            number = int(number)
        except (TypeError, ValueError) as exc:
            raise InvalidMultipartParts(f"part entry without a part number: {item!r}") from exc
        if number < 1 or number > cap:
            raise InvalidMultipartParts(f"part number out of range: {number}")
        if number in seen:
            raise InvalidMultipartParts(f"duplicate part number: {number}")
        seen.add(number)
    return items


def _verify_stored_object(session: UploadSession) -> int:
    """Measure the stored object and enforce every invariant on it.

    Returns the measured size. Raises :class:`UploadNotStored`,
    :class:`UploadTooLarge`,
    :class:`~stapel_recordings.media_types.UnsupportedUploadContent`, or
    :class:`UploadContentUncheckable` when the content policy is on and the
    backend cannot serve the ranged read it needs.

    The caller-declared size is deliberately NOT a fallback here: a HEAD
    that finds nothing, or finds a zero-byte object, means the presigned
    write never landed, and believing the client at that point is how an
    empty recording enters the pipeline with a size someone will be billed
    for."""
    storage = get_storage()
    exists, actual_size = storage.head_object(session.storage_key)
    if not exists or not actual_size:
        raise UploadNotStored(session.storage_key)
    actual_size = int(actual_size)
    if actual_size > int(session.max_size_bytes):
        raise UploadTooLarge(actual_size, int(session.max_size_bytes))

    policy = str(recordings_settings.UPLOAD_CONTENT_POLICY)
    if policy != media_types.POLICY_OFF:
        try:
            prefix = storage.read_prefix(session.storage_key, media_types.PREFIX_BYTES)
        except NotImplementedError as exc:
            # A gate that cannot run must refuse, not wave the object
            # through. Falling through here (the old behaviour) meant
            # UPLOAD_CONTENT_POLICY silently did not apply on any backend
            # without ranged reads — including a host's OWN backend, which
            # inherits that NotImplementedError from RecordingStorage by
            # doing nothing, so the gate switched itself off by omission and
            # an executable or HTML polyglot landed under an audio.mp3 key.
            # Turning the gate off is a decision, so it has to be stated:
            # UPLOAD_CONTENT_POLICY = "off".
            logger.error(
                "upload: storage backend %s cannot read a prefix — content "
                "policy %r could not be applied to %s; refusing the upload "
                "(implement RecordingStorage.read_prefix, or set "
                "UPLOAD_CONTENT_POLICY='off' to accept unchecked bytes)",
                type(storage).__name__, policy, session.storage_key,
            )
            raise UploadContentUncheckable(
                session.storage_key, type(storage).__name__, policy
            ) from exc
        media_types.check_prefix(prefix, policy=policy)
    return actual_size


def _cleanup_failed_upload(session: UploadSession) -> None:
    """Remove the object and session left by an upload that failed
    validation, so a rejected upload costs the tenant nothing."""
    storage = get_storage()
    try:
        storage.delete_object(session.storage_key)
    except Exception:
        logger.warning(
            "upload: could not delete rejected object %s", session.storage_key, exc_info=True
        )
    UploadSession.objects.filter(pk=session.pk).delete()


def finalize_upload(
    *, session: UploadSession, file_size_bytes: int | None = None, parts: list[dict] | None = None
) -> Recording:
    """Finalize an upload and enqueue the pipeline.

    Idempotent: if the recording already has a ``file_storage_key`` (a
    concurrent finalize won), returns it without re-emitting. Emits
    ``recording.uploaded`` through the outbox — the event leaves iff this
    transaction commits, and it is only reached once every invariant in
    :func:`_verify_stored_object` holds.

    *file_size_bytes* is a client claim, kept for API compatibility. It is
    checked against the session ceiling and otherwise ignored: the recorded
    size is what storage reports.

    Raises :class:`UploadNotStored`, :class:`UploadTooLarge`,
    :class:`InvalidMultipartParts`,
    :class:`~stapel_recordings.media_types.UnsupportedUploadContent` or
    :class:`UploadContentUncheckable` when the upload is not acceptable — or,
    for the last one, cannot be shown to be. In every one of those cases the
    object and the session are cleaned up, the recording stays out of
    ``queued`` and no ``recording.uploaded`` event is emitted.
    """
    try:
        return _finalize_upload_locked(
            session=session, file_size_bytes=file_size_bytes, parts=parts
        )
    except (
        UploadNotStored,
        UploadTooLarge,
        UploadContentUncheckable,
        media_types.UnsupportedUploadContent,
    ):
        # Cleanup runs OUTSIDE the finalize transaction on purpose: that
        # transaction has just been unwound by this exception, so a delete
        # issued inside it would roll back with it and leave the rejected
        # object and its session alive.
        _cleanup_failed_upload(session)
        raise


@transaction.atomic
def _finalize_upload_locked(
    *, session: UploadSession, file_size_bytes: int | None = None, parts: list[dict] | None = None
) -> Recording:
    recording = Recording.objects.select_for_update().get(pk=session.recording_id)
    if recording.file_storage_key:
        return recording  # already finalized

    if file_size_bytes is not None and int(file_size_bytes) > int(session.max_size_bytes):
        raise UploadTooLarge(int(file_size_bytes), int(session.max_size_bytes))

    storage = get_storage()
    if session.is_multipart and session.multipart_upload_id:
        checked = _validated_parts(session, parts)
        storage.complete_multipart_upload(
            session.storage_key, session.multipart_upload_id, checked
        )

    actual_size = _verify_stored_object(session)

    session.finalized_at = timezone.now()
    session.save(update_fields=["finalized_at"])

    recording.file_storage_key = session.storage_key
    recording.file_size_bytes = actual_size
    recording.status = RecordingStatus.QUEUED
    recording.save(update_fields=["file_storage_key", "file_size_bytes", "status", "updated_at"])

    events.emit_uploaded(recording)
    return recording


__all__ = [
    "create_upload_session",
    "start_multipart_upload",
    "abort_multipart_upload_session",
    "finalize_upload",
    "validated_upload_ext",
    "UnsupportedUploadExtension",
    "UploadTooLarge",
    "UploadNotStored",
    "UploadContentUncheckable",
    "InvalidMultipartParts",
    "check_workspace_membership",
    "WORKSPACES_CHECK_MEMBERSHIP",
]
