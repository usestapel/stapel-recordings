"""Dataclass DTOs — the API models of stapel-recordings (never ORM instances)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RecordingDTO:
    """A recording as seen by the API."""

    id: str
    resource_key: str
    workspace_id: str
    title: str
    status: str
    source_type: str
    language: Optional[str]
    duration_seconds: Optional[float]
    segments_count: int
    speakers_count: int
    word_count: int
    provider_used: Optional[str]
    transcript_storage_key: Optional[str]
    summary: Optional[str]
    created_at: str


@dataclass
class UploadSessionDTO:
    """A single-PUT upload session."""

    id: str
    presigned_url: str
    storage_key: str
    max_size_bytes: int
    expires_at: str


@dataclass
class CreateRecordingResponse:  # noqa: R004
    recording: RecordingDTO
    upload: UploadSessionDTO


@dataclass
class SharedSegmentDTO:
    """One transcript segment as seen through a share link."""

    sequence_num: int
    start_time: float
    end_time: float
    speaker: Optional[str]
    text: str


@dataclass
class SharedRecordingDTO:
    """A recording as seen through a public share link.

    Field presence follows the share's granted permissions, not the
    caller's request: ``summary``, ``segments`` and ``media_url`` stay empty
    unless the link grants them. The recording's internal identifiers
    (workspace, storage keys, provider) are not part of this payload at
    all — a public link is not a window into the tenant.
    """

    id: str
    title: str
    status: str
    language: Optional[str]
    duration_seconds: Optional[float]
    created_at: str
    permissions: list[str]
    summary: Optional[str]
    media_url: Optional[str]
    segments: list[SharedSegmentDTO]



def recording_to_dto(recording) -> RecordingDTO:
    from .resources import resource_key

    return RecordingDTO(
        id=str(recording.id),
        resource_key=resource_key(recording),
        workspace_id=str(recording.workspace_id),
        title=recording.title,
        status=recording.status,
        source_type=recording.source_type,
        language=recording.language,
        duration_seconds=recording.duration_seconds,
        segments_count=recording.segments_count,
        speakers_count=recording.speakers_count,
        word_count=recording.word_count,
        provider_used=recording.provider_used,
        transcript_storage_key=recording.transcript_storage_key,
        summary=recording.summary,
        created_at=recording.created_at.isoformat(),
    )


def shared_recording_to_dto(access) -> SharedRecordingDTO:
    """Project a recording through a :class:`~stapel_recordings.shares.ShareAccess`.

    The projection is the enforcement point: a field the share does not
    grant is not fetched, not rendered and not reachable by asking again
    with a different query parameter."""
    from . import shares
    from .conf import recordings_settings
    from .storage import get_storage

    recording = access.recording
    segments: list[SharedSegmentDTO] = []
    if access.has(shares.PERM_TRANSCRIPT):
        segments = [
            SharedSegmentDTO(
                sequence_num=s.sequence_num,
                start_time=s.start_time,
                end_time=s.end_time,
                speaker=(s.speaker.display_name or s.speaker.label) if s.speaker else None,
                text=s.text,
            )
            for s in recording.segments.select_related("speaker").all()
        ]

    media_url = None
    if access.has(shares.PERM_MEDIA) and recording.file_storage_key:
        media_url = get_storage().presigned_get_url(
            recording.file_storage_key,
            expires_seconds=int(recordings_settings.SHARE_MEDIA_URL_TTL_SECONDS),
        )

    return SharedRecordingDTO(
        id=str(recording.id),
        title=recording.title,
        status=recording.status,
        language=recording.language,
        duration_seconds=recording.duration_seconds,
        created_at=recording.created_at.isoformat(),
        permissions=list(access.permissions),
        summary=recording.summary if access.has(shares.PERM_SUMMARY) else None,
        media_url=media_url,
        segments=segments,
    )


def upload_session_to_dto(session) -> UploadSessionDTO:
    return UploadSessionDTO(
        id=str(session.id),
        presigned_url=session.presigned_url,
        storage_key=session.storage_key,
        max_size_bytes=session.max_size_bytes,
        expires_at=session.expires_at.isoformat(),
    )
