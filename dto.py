"""Dataclass DTOs — the API models of stapel-recordings (never ORM instances)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RecordingDTO:
    """A recording as seen by the API.

    ``is_processing`` / ``poll_after_seconds`` are the module's answer to
    "when do I ask again": this module serves no socket, so a client learns
    that a recording moved by re-reading it, and the two fields say whether
    that is worth doing and how soon. ``poll_after_seconds`` is ``None``
    exactly when ``is_processing`` is false — the status is terminal, or the
    client itself is holding the next move — which is how the payload says
    *stop* as explicitly as it says *ask again*. The same number travels as
    the ``Retry-After`` header for callers that read HTTP rather than the
    body.
    """

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
    is_processing: bool
    poll_after_seconds: Optional[int]


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
class JobDTO:
    """A background job this module accepted — the receipt for a 202.

    Deliberately not the whole Job row: a caller needs to know WHICH run was
    accepted (so it can poll, and so it can recognize its own retry landing
    on the same one) and what state it is in. ``recording_id`` is here
    because the job reference travels on its own once the client stores it.
    """

    id: str
    recording_id: Optional[str]
    workspace_id: str
    type: str
    status: str
    queued_at: str


@dataclass
class MediaURLDTO:
    """A short-lived, authorized URL to a recording's media object.

    The expiry travels *with* the URL because the client has to plan around
    it: the URL stops working, and a player that cached it has to come back
    here rather than retry a dead link forever."""

    url: str
    expires_at: str
    expires_in: int


@dataclass
class ShareUnlockDTO:
    """The token a client presents after passing a share's passcode."""

    unlock_token: str
    expires_in: int


@dataclass
class TranscriptSegmentDTO:
    """One speaker-attributed transcript segment.

    ONE shape for both readers — the owner's paginated transcript read and
    the projection inside a share link — because they are the same thing
    seen from two doors, and a client that renders a transcript must not
    have to write it twice. What differs between the doors is *whether* the
    segments are reachable at all (the share needs the ``transcript``
    grant), never their shape.
    """

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
    segments: list[TranscriptSegmentDTO]


def poll_after_seconds(status) -> Optional[int]:
    """How long a client should wait before re-reading a recording, or ``None``.

    ``None`` is an answer, not a missing one: it means nothing will change by
    asking again (see :meth:`~stapel_recordings.models.RecordingStatus.is_processing`).
    Single source for the payload field and the ``Retry-After`` header, so the
    two can never say different numbers.
    """
    from .conf import recordings_settings
    from .models import RecordingStatus

    if not RecordingStatus.is_processing(status):
        return None
    return int(recordings_settings.POLL_INTERVAL_SECONDS)


def segment_to_dto(segment) -> TranscriptSegmentDTO:
    """One ``Segment`` row → the wire shape, for either reader.

    The speaker is flattened to the name a reader can print: the human
    ``display_name`` when someone has named the voice, else the provider's
    own label (``speaker_0``). Callers should ``select_related("speaker")``.
    """
    speaker = segment.speaker
    return TranscriptSegmentDTO(
        sequence_num=segment.sequence_num,
        start_time=segment.start_time,
        end_time=segment.end_time,
        speaker=(speaker.display_name or speaker.label) if speaker else None,
        text=segment.text,
    )


def recording_to_dto(recording) -> RecordingDTO:
    from .resources import resource_key

    poll_after = poll_after_seconds(recording.status)
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
        is_processing=poll_after is not None,
        poll_after_seconds=poll_after,
    )


def job_to_dto(job) -> JobDTO:
    return JobDTO(
        id=str(job.id),
        recording_id=str(job.recording_id) if job.recording_id else None,
        workspace_id=str(job.workspace_id),
        type=job.type,
        status=job.status,
        queued_at=job.queued_at.isoformat(),
    )


def shared_recording_to_dto(access) -> SharedRecordingDTO:
    """Project a recording through a :class:`~stapel_recordings.shares.ShareAccess`.

    The projection is the enforcement point: a field the share does not
    grant is not fetched, not rendered and not reachable by asking again
    with a different query parameter."""
    from . import media, shares
    from .conf import recordings_settings

    recording = access.recording
    segments: list[TranscriptSegmentDTO] = []
    if access.has(shares.PERM_TRANSCRIPT):
        segments = [
            segment_to_dto(s)
            for s in recording.segments.select_related("speaker").all()
        ]

    media_url = None
    if access.has(shares.PERM_MEDIA):
        try:
            media_url = media.issue_media_url(
                recording,
                ttl_seconds=int(recordings_settings.SHARE_MEDIA_URL_TTL_SECONDS),
            ).url
        except media.MediaUnavailable:
            # No object, or a backend that can only produce a permanent URL.
            # The payload says "no media" rather than smuggling out a URL
            # that never expires — a share is the one caller where a leaked
            # forever-URL has no owner to notice it (audit STORE-01).
            media_url = None

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


def media_grant_to_dto(grant) -> MediaURLDTO:
    return MediaURLDTO(
        url=grant.url,
        expires_at=grant.expires_at.isoformat(),
        expires_in=int(grant.ttl_seconds),
    )


def upload_session_to_dto(session) -> UploadSessionDTO:
    return UploadSessionDTO(
        id=str(session.id),
        presigned_url=session.presigned_url,
        storage_key=session.storage_key,
        max_size_bytes=session.max_size_bytes,
        expires_at=session.expires_at.isoformat(),
    )
