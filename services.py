"""Service-layer helpers: recording creation, upload sessions, finalize.

Object I/O goes through the STORAGE seam; the pipeline is kicked by
emitting ``recording.uploaded`` through the transactional outbox (no inline
publish → no publish-after-commit loss).
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import events
from .conf import recordings_settings
from .models import Recording, RecordingStatus, UploadSession
from .storage import get_storage

#: Comm Function the workspaces module exposes to answer membership questions
#: by name (no import of that app). See stapel_workspaces.functions.
WORKSPACES_CHECK_MEMBERSHIP = "workspaces.check_membership"


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


def create_upload_session(*, recording: Recording, filename: str) -> UploadSession:
    """Create a single-PUT presigned upload session.

    The *filename* extension is validated against
    ``UPLOAD_EXTENSION_ALLOWLIST`` and appended to the object key
    (``…/audio.mp3``)."""
    storage = get_storage()
    key = _storage_key(recording, filename=filename)
    ttl = int(recordings_settings.UPLOAD_SESSION_TTL_SECONDS)
    presigned_url = storage.presigned_put_url(key, expires_seconds=ttl)
    session = UploadSession.objects.create(
        recording=recording,
        presigned_url=presigned_url,
        storage_key=key,
        max_size_bytes=int(recordings_settings.MAX_UPLOAD_BYTES),
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
    extension appended to the object key)."""
    storage = get_storage()
    key = _storage_key(recording, filename=filename)
    part_size = int(recordings_settings.MULTIPART_PART_SIZE)
    num_parts = max(1, (file_size_bytes + part_size - 1) // part_size)
    ttl = int(recordings_settings.MULTIPART_SESSION_TTL_SECONDS)

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
        max_size_bytes=int(recordings_settings.MAX_UPLOAD_BYTES),
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


@transaction.atomic
def finalize_upload(
    *, session: UploadSession, file_size_bytes: int | None = None, parts: list[dict] | None = None
) -> Recording:
    """Finalize an upload and enqueue the pipeline.

    Idempotent: if the recording already has a ``file_storage_key`` (a
    concurrent finalize won), returns it without re-emitting. Emits
    ``recording.uploaded`` through the outbox — the event leaves iff this
    transaction commits.
    """
    recording = Recording.objects.select_for_update().get(pk=session.recording_id)
    if recording.file_storage_key:
        return recording  # already finalized

    storage = get_storage()
    if session.is_multipart and session.multipart_upload_id:
        storage.complete_multipart_upload(session.storage_key, session.multipart_upload_id, parts or [])

    session.finalized_at = timezone.now()
    session.save(update_fields=["finalized_at"])

    recording.file_storage_key = session.storage_key
    exists, actual_size = storage.head_object(session.storage_key)
    if exists and actual_size:
        recording.file_size_bytes = actual_size
    elif file_size_bytes is not None:
        recording.file_size_bytes = file_size_bytes
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
    "check_workspace_membership",
    "WORKSPACES_CHECK_MEMBERSHIP",
]
