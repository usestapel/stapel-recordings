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
class CreateRecordingResponse:
    recording: RecordingDTO
    upload: UploadSessionDTO


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


def upload_session_to_dto(session) -> UploadSessionDTO:
    return UploadSessionDTO(
        id=str(session.id),
        presigned_url=session.presigned_url,
        storage_key=session.storage_key,
        max_size_bytes=session.max_size_bytes,
        expires_at=session.expires_at.isoformat(),
    )
