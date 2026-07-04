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


def _storage_key(recording: Recording) -> str:
    prefix = recordings_settings.STORAGE_PREFIX.strip("/")
    return f"{prefix}/{recording.workspace_id}/{recording.id}/audio"


def create_upload_session(*, recording: Recording) -> UploadSession:
    """Create a single-PUT presigned upload session."""
    storage = get_storage()
    key = _storage_key(recording)
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
    *, recording: Recording, file_size_bytes: int, content_type: str | None = None
) -> tuple[UploadSession, list[dict], int]:
    """Initiate a multipart upload. Returns (session, parts, part_size)."""
    storage = get_storage()
    key = _storage_key(recording)
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
]
