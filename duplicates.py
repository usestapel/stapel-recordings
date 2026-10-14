"""The same file uploaded twice by the same owner.

Two questions, asked at two moments:

* **At upload** (:func:`refuse_duplicate`) — does another live recording of
  this owner already hold a file with this fingerprint v1
  (:mod:`.chunked`)? Then the upload is refused with
  ``409 error.409.recording_upload_duplicate`` naming that recording, unless
  the caller explicitly asked to process it again (``allow_duplicate``). The
  match is owner-wide, across workspaces: the person paying is the owner,
  not the workspace. Only finished uploads count (an open session is
  resumed through :func:`.chunked.lookup`, not refused).

* **Before paid work** (:func:`judge_run`) — is this run a repeat of a
  completed recording the owner already has? Matched by a server-verified
  fingerprint (both uploads multipart-verified) with the earlier run
  covering at least this run's length, or by a content match the HOST
  verified server-side (``judge_run(verified_original=...)``, e.g. a hash of
  the stored audio). Only those two waive the charge. For uploads without
  either, the same stored byte size and a duration within
  ``DUPLICATE_DURATION_TOLERANCE_SECONDS`` is recorded as a suspected repeat
  and still charged: size and duration are attacker-choosable, so they may
  flag but never waive. The verdict is written to the library-reserved
  ``metadata["duplicate_run"]`` and travels on ``recording.completed`` as
  ``duplicate_of`` + ``charge_waived``, so a metering consumer can decline
  to bill the same file twice. A run the owner explicitly asked for
  (``allow_duplicate``) is marked but not waived.

The host decides when to call :func:`judge_run` (after the duration is
measured, before the paid stage); this module only answers.
"""
from __future__ import annotations

from typing import Optional

from django.utils import timezone

from stapel_core.django.api.errors import StapelServiceError

from .conf import recordings_settings
from .errors import ERR_409_UPLOAD_DUPLICATE
from .models import Recording, RecordingStatus, UploadSession

#: ``metadata`` key holding the duplicate verdict. Library-reserved.
METADATA_KEY = "duplicate_run"

MATCH_FINGERPRINT = "fingerprint"
MATCH_CONTENT = "content"
MATCH_SIZE_DURATION = "size_duration"

#: Matches that prove the same bytes server-side and so may waive a charge.
WAIVING_MATCHES = frozenset({MATCH_FINGERPRINT, MATCH_CONTENT})

CHARGE_WAIVED = "waived"
CHARGE_CHARGED = "charged"

#: How many candidate sessions/recordings one question reads at most.
_SCAN = 50


class UploadDuplicate(StapelServiceError, ValueError):
    """The owner already has a recording holding this exact file.

    HTTP: ``409 error.409.recording_upload_duplicate`` with
    ``{recording_id}``. Repeat the request with ``allow_duplicate: true`` to
    process the file again."""

    def __init__(self, recording_id):
        super().__init__(
            409, ERR_409_UPLOAD_DUPLICATE, params={"recording_id": str(recording_id)}
        )
        self.args = (f"file already uploaded as recording {recording_id}",)
        self.recording_id = str(recording_id)


def _live(qs):
    return qs.filter(deleted_at__isnull=True).exclude(status=RecordingStatus.DELETED)


def _holds_file(session: UploadSession) -> bool:
    recording = session.recording
    return bool(
        session.finalized_at
        or recording.file_storage_key
        or recording.normalized_storage_key
    )


def find_upload_duplicate(
    owner_id, fingerprint: Optional[str], *, exclude_recording_id=None
) -> Optional[Recording]:
    """The newest live recording of *owner_id* whose finished upload carries
    *fingerprint*, else ``None``. Any workspace."""
    from .chunked import is_sha256_hex

    if not owner_id or not fingerprint or not is_sha256_hex(fingerprint):
        return None
    sessions = (
        UploadSession.objects.filter(
            fingerprint=fingerprint,
            recording__owner_id=owner_id,
            recording__deleted_at__isnull=True,
        )
        .exclude(recording__status=RecordingStatus.DELETED)
        .select_related("recording")
        .order_by("-created_at")
    )
    if exclude_recording_id is not None:
        sessions = sessions.exclude(recording_id=exclude_recording_id)
    for session in sessions[:_SCAN]:
        if _holds_file(session):
            return session.recording
    return None


def refuse_duplicate(
    *,
    owner_id,
    fingerprint: Optional[str],
    allow_duplicate: bool = False,
    recording: Optional[Recording] = None,
) -> Optional[Recording]:
    """Raise :class:`UploadDuplicate` when the owner already holds this file.

    With ``allow_duplicate`` nothing is raised; when a duplicate exists and
    *recording* (the upload's target) is given, the explicit request is
    recorded on it (``metadata["duplicate_run"]["requested_by_user"]``) so
    :func:`judge_run` charges that run. A *recording* that was created
    with the request already recorded stays allowed for its own upload, so
    a client that said ``allow_duplicate`` at create need not repeat it at
    every upload call. Returns the existing recording or ``None``."""
    existing = find_upload_duplicate(
        owner_id,
        fingerprint,
        exclude_recording_id=recording.pk if recording is not None else None,
    )
    if existing is None:
        return None
    if not allow_duplicate and not (recording is not None and was_requested(recording)):
        raise UploadDuplicate(existing.pk)
    if recording is not None:
        note_requested(recording, existing)
    return existing


def was_requested(recording: Recording) -> bool:
    """Did the owner knowingly ask to process this recording's file again?"""
    block = (recording.metadata or {}).get(METADATA_KEY)
    return isinstance(block, dict) and block.get("requested_by_user") is True


def note_requested(recording: Recording, existing: Recording) -> None:
    """Record that the owner asked to process *existing*'s file again."""
    metadata = dict(recording.metadata or {})
    metadata[METADATA_KEY] = {
        "of": str(existing.pk),
        "match": MATCH_FINGERPRINT,
        "requested_by_user": True,
        "at": timezone.now().isoformat(),
    }
    recording.metadata = metadata
    recording.save(update_fields=["metadata", "updated_at"])


def _verified_fingerprints(recording: Recording) -> set[str]:
    return set(
        UploadSession.objects.filter(
            recording=recording,
            fingerprint__isnull=False,
            finalized_at__isnull=False,
        )
        .exclude(multipart_upload_id__isnull=True)
        .exclude(multipart_upload_id="")
        .values_list("fingerprint", flat=True)
    )


def tolerance_seconds() -> float:
    return float(recordings_settings.DUPLICATE_DURATION_TOLERANCE_SECONDS)


def find_prior_copy(recording: Recording) -> Optional[tuple[Recording, str]]:
    """A completed live recording of the same owner holding the same file.

    ``(original, match)`` or ``None``. Needs this run's measured
    ``duration_seconds``; without it nothing matches (the run is billed as
    usual and the absence is the caller's to log). Matches, in order:

    * ``fingerprint`` — a server-verified (multipart) fingerprint shared
      with the original, and the original's processed duration covers this
      one (a trimmed preview does not cover a full run);
    * ``size_duration`` — the same stored ``file_size_bytes`` and a duration
      within :func:`tolerance_seconds`.
    """
    duration = recording.duration_seconds
    if not recording.owner_id or duration is None:
        return None
    tol = tolerance_seconds()
    candidates = (
        _live(Recording.objects.filter(owner_id=recording.owner_id, status=RecordingStatus.COMPLETED))
        .exclude(pk=recording.pk)
        .filter(duration_seconds__isnull=False)
    )
    fingerprints = _verified_fingerprints(recording)
    if fingerprints:
        verified = (
            UploadSession.objects.filter(
                fingerprint__in=fingerprints,
                finalized_at__isnull=False,
                recording__in=candidates,
            )
            .exclude(multipart_upload_id__isnull=True)
            .exclude(multipart_upload_id="")
            .select_related("recording")
            .order_by("-created_at")
        )
        for session in verified[:_SCAN]:
            original = session.recording
            if float(original.duration_seconds) >= float(duration) - tol:
                return original, MATCH_FINGERPRINT
    if recording.file_size_bytes:
        same = candidates.filter(
            file_size_bytes=recording.file_size_bytes,
            duration_seconds__gte=float(duration) - tol,
            duration_seconds__lte=float(duration) + tol,
        ).order_by("-created_at")
        original = same.first()
        if original is not None:
            return original, MATCH_SIZE_DURATION
    return None


def judge_run(
    recording: Recording,
    *,
    run_id: Optional[str],
    verified_original: Optional[Recording] = None,
) -> Optional[dict]:
    """Decide whether this run repeats a completed recording, and record it.

    Writes ``metadata["duplicate_run"] = {of, match, charge, run_id, at}``
    (and keeps ``requested_by_user``) when this run repeats one. ``charge``
    is ``waived`` only for a server-verified match (:data:`WAIVING_MATCHES`)
    the owner did not explicitly ask to re-run; a size+duration match and a
    requested re-run are ``charged``. *verified_original* is a completed
    recording of the same owner the HOST proved holds the same content
    (for example by a hash of the stored audio it computed itself); it is
    used when the library's own verified fingerprint finds nothing. A run
    that repeats nothing clears a stale verdict of an earlier run. Returns
    the stored block or ``None``.
    """
    metadata = dict(recording.metadata or {})
    previous = metadata.get(METADATA_KEY) if isinstance(metadata.get(METADATA_KEY), dict) else {}
    requested = bool(previous.get("requested_by_user"))
    found = find_prior_copy(recording)
    if (
        verified_original is not None
        and (found is None or found[1] not in WAIVING_MATCHES)
        and verified_original.pk != recording.pk
        and verified_original.owner_id == recording.owner_id
    ):
        found = (verified_original, MATCH_CONTENT)
    if found is None:
        if requested:
            block = {**previous, "charge": CHARGE_CHARGED, "run_id": run_id}
            metadata[METADATA_KEY] = block
        elif METADATA_KEY in metadata:
            metadata.pop(METADATA_KEY)
            block = None
        else:
            return None
    else:
        original, match = found
        block = {
            "of": str(original.pk),
            "match": match,
            "charge": (
                CHARGE_WAIVED
                if match in WAIVING_MATCHES and not requested
                else CHARGE_CHARGED
            ),
            "run_id": run_id,
            "at": timezone.now().isoformat(),
        }
        if requested:
            block["requested_by_user"] = True
        metadata[METADATA_KEY] = block
    recording.metadata = metadata
    recording.save(update_fields=["metadata", "updated_at"])
    return block


def completed_fields(recording: Recording, run_id) -> dict:
    """``{duplicate_of, charge_waived}`` for this run's ``recording.completed``.

    Only a verdict written for THIS run counts; an earlier run's verdict
    says nothing about the run that just finished."""
    block = (recording.metadata or {}).get(METADATA_KEY)
    if not isinstance(block, dict) or not block.get("of"):
        return {}
    if not run_id or str(block.get("run_id") or "") != str(run_id):
        return {}
    return {
        "duplicate_of": str(block["of"]),
        "charge_waived": block.get("charge") == CHARGE_WAIVED,
    }


__all__ = [
    "METADATA_KEY",
    "MATCH_FINGERPRINT",
    "MATCH_CONTENT",
    "MATCH_SIZE_DURATION",
    "WAIVING_MATCHES",
    "CHARGE_WAIVED",
    "CHARGE_CHARGED",
    "UploadDuplicate",
    "find_upload_duplicate",
    "refuse_duplicate",
    "note_requested",
    "was_requested",
    "find_prior_copy",
    "judge_run",
    "completed_fields",
    "tolerance_seconds",
]
