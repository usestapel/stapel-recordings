"""Verified, resumable multipart uploads keyed by a file fingerprint.

A large upload is a list of parts, each hashed by the client before it is
sent. The part URL is signed over that hash, so the store refuses any other
bytes; the store then reports the digest it computed for every part it
holds. That report — never the client's memory — is what a resumed upload
continues from (:func:`manifest`) and what completion is checked against
(:func:`verify_manifest`).

Fingerprint v1 (identical on client and server)::

    P = part size, S = total size, n = max(1, ceil(S / P))
    h_i = SHA-256(bytes[(i-1)P : min(iP, S)])            # raw 32 bytes
    fingerprint = hex(SHA-256(b"stapel-upload-v1\\n" + str(S) + "\\n"
                              + str(P) + "\\n" + h_1 || ... || h_n))

The same file therefore has the same fingerprint wherever it is hashed, and
:func:`lookup` recognises it: already uploaded, or in progress and
resumable from whatever the store holds.

Sessions without a fingerprint (legacy multipart) never reach this module.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta

from django.utils import timezone
from stapel_core.django.api.errors import StapelServiceError

from .conf import recordings_settings
from .errors import (
    ERR_409_INVALID_STATE,
    ERR_409_UPLOAD_EXPIRED,
    ERR_409_UPLOAD_PART_MISMATCH,
    ERR_409_UPLOAD_PARTS_MISSING,
    ERR_503_UPLOAD_UNVERIFIABLE,
)
from .models import UploadSession
from .storage import get_storage

FINGERPRINT_PREFIX = b"stapel-upload-v1\n"
#: Storage checksum algorithm a verified upload is created with.
CHECKSUM_ALGORITHM = "SHA256"
#: Part URLs minted per call.
MAX_MINT_PER_CALL = 100
#: Missing part numbers named in a 409 (the count is always exact).
MISSING_IN_ERROR = 50

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ── Refusals ─────────────────────────────────────────────────────────


class UploadPartsMissing(StapelServiceError, ValueError):
    """Complete was asked before every part arrived with its expected size.

    HTTP: ``409 error.409.recording_upload_parts_missing`` with
    ``{count, missing}`` (``missing`` = comma list of the first 50)."""

    def __init__(self, missing: list[int]):
        missing = sorted(int(n) for n in missing)
        super().__init__(
            409,
            ERR_409_UPLOAD_PARTS_MISSING,
            params={
                "count": len(missing),
                "missing": ",".join(str(n) for n in missing[:MISSING_IN_ERROR]),
            },
        )
        self.args = (f"{len(missing)} parts missing",)
        self.missing = missing


class UploadPartMismatch(StapelServiceError, ValueError):
    """A stored part's digest is not the declared one; ``part_number`` 0
    means the file as a whole does not hash to the session's fingerprint.

    HTTP: ``409 error.409.recording_upload_part_mismatch`` with
    ``{part_number}``."""

    def __init__(self, part_number: int):
        super().__init__(
            409, ERR_409_UPLOAD_PART_MISMATCH, params={"part_number": int(part_number)}
        )
        self.args = (f"part {part_number} does not match",)
        self.part_number = int(part_number)


class UploadSessionExpired(StapelServiceError, ValueError):
    """HTTP: ``409 error.409.recording_upload_expired``."""

    def __init__(self, session_id):
        super().__init__(409, ERR_409_UPLOAD_EXPIRED)
        self.args = (f"upload session {session_id} expired",)


class NotAVerifiedUpload(StapelServiceError, ValueError):
    """A fingerprint-only verb on a session without a fingerprint, or on one
    already finalized. HTTP: ``409 error.409.recording_invalid_state``."""

    def __init__(self, detail: str):
        super().__init__(409, ERR_409_INVALID_STATE)
        self.args = (detail,)


class PartsUnlistable(StapelServiceError, RuntimeError):
    """The storage backend cannot list parts, so a verified upload cannot be
    checked. HTTP: ``503 error.503.recording_upload_unverifiable``."""

    def __init__(self, backend: str):
        super().__init__(503, ERR_503_UPLOAD_UNVERIFIABLE)
        self.args = (f"{backend} cannot list multipart parts",)


# ── Fingerprint v1 ───────────────────────────────────────────────────


def total_parts_for(size: int, part_size: int) -> int:
    size, part_size = int(size), int(part_size)
    if part_size <= 0:
        raise ValueError("part size must be positive")
    return max(1, -(-size // part_size))


def expected_part_size(n: int, size: int, part_size: int) -> int:
    """Byte length of 1-based part *n* of a *size*-byte file."""
    n, size, part_size = int(n), int(size), int(part_size)
    total = total_parts_for(size, part_size)
    if n < 1 or n > total:
        raise ValueError(f"part {n} outside 1..{total}")
    return min(n * part_size, size) - (n - 1) * part_size


def fingerprint_of(size: int, part_size: int, part_hashes_hex: list[str]) -> str:
    """Fingerprint v1 from the per-part SHA-256 digests (hex)."""
    h = hashlib.sha256()
    h.update(FINGERPRINT_PREFIX)
    h.update(f"{int(size)}\n{int(part_size)}\n".encode())
    for digest in part_hashes_hex:
        h.update(bytes.fromhex(digest))
    return h.hexdigest()


def fingerprint_bytes(data: bytes, part_size: int) -> tuple[str, list[str]]:
    """Fingerprint v1 of in-memory *data*, with its part digests (hex)."""
    size = len(data)
    n = total_parts_for(size, part_size)
    hashes = [
        hashlib.sha256(data[i * part_size:(i + 1) * part_size]).hexdigest()
        for i in range(n)
    ]
    return fingerprint_of(size, part_size, hashes), hashes


def is_sha256_hex(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def hex_to_b64(digest_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(digest_hex)).decode("ascii")


# ── Session verbs ────────────────────────────────────────────────────


def _require_open_verified(session: UploadSession) -> None:
    if not session.fingerprint:
        raise NotAVerifiedUpload(f"session {session.pk} has no fingerprint")
    if session.finalized_at:
        raise NotAVerifiedUpload(f"session {session.pk} is already finalized")
    if session.expires_at <= timezone.now():
        raise UploadSessionExpired(session.pk)


def _stored_parts(session: UploadSession) -> list[dict]:
    storage = get_storage()
    try:
        return list(storage.list_parts(session.storage_key, session.multipart_upload_id))
    except NotImplementedError as exc:
        raise PartsUnlistable(type(storage).__name__) from exc


def _good_parts(session: UploadSession, stored: list[dict]) -> dict[int, dict]:
    """Stored parts inside 1..total whose size is the expected one."""
    size, part_size, total = (
        int(session.max_size_bytes), int(session.part_size_bytes), int(session.total_parts)
    )
    good: dict[int, dict] = {}
    for part in stored:
        n = int(part["part_number"])
        if 1 <= n <= total and int(part.get("size") or 0) == expected_part_size(n, size, part_size):
            sha = part.get("sha256")
            good[n] = {
                "part_number": n,
                "etag": str(part.get("etag") or ""),
                "size": int(part.get("size") or 0),
                "sha256": sha.lower() if isinstance(sha, str) else None,
            }
    return good


def mint_part_urls(session: UploadSession, parts: list[dict], *, expires_seconds: int) -> list[dict]:
    """Part URLs bound to the client's per-part SHA-256.

    ``parts = [{part_number, sha256}]`` (≤ 100, hex digests). Each answer is
    ``{part_number, presigned_url, headers}``: the PUT must send exactly
    ``headers``, and the store refuses bytes with another digest. Minting is
    proof the upload is alive, so it also moves the session deadline."""
    from .services import InvalidMultipartParts

    _require_open_verified(session)
    items = list(parts or [])
    if not items or len(items) > MAX_MINT_PER_CALL:
        raise InvalidMultipartParts(
            f"mint takes 1..{MAX_MINT_PER_CALL} parts, got {len(items)}",
            max_parts=MAX_MINT_PER_CALL,
        )
    total = int(session.total_parts)
    seen: set[int] = set()
    wanted: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidMultipartParts(f"part entry is not an object: {item!r}")
        try:
            n = int(item.get("part_number"))
        except (TypeError, ValueError) as exc:
            raise InvalidMultipartParts(f"part entry without a part number: {item!r}") from exc
        if n < 1 or n > total:
            raise InvalidMultipartParts(f"part number out of range 1..{total}: {n}")
        if n in seen:
            raise InvalidMultipartParts(f"duplicate part number: {n}")
        digest = item.get("sha256")
        if not is_sha256_hex(digest):
            raise InvalidMultipartParts(f"part {n}: sha256 must be 64 lower-case hex chars")
        seen.add(n)
        wanted.append((n, digest))

    storage = get_storage()
    minted = []
    for n, digest in wanted:
        b64 = hex_to_b64(digest)
        minted.append(
            {
                "part_number": n,
                "presigned_url": storage.presigned_upload_part_url(
                    session.storage_key,
                    session.multipart_upload_id,
                    n,
                    expires_seconds=int(expires_seconds),
                    sha256_b64=b64,
                ),
                "headers": dict(storage.part_checksum_headers(b64)),
            }
        )
    session.expires_at = timezone.now() + timedelta(
        seconds=int(recordings_settings.MULTIPART_SESSION_TTL_SECONDS)
    )
    session.save(update_fields=["expires_at"])
    return minted


def manifest(session: UploadSession) -> dict:
    """What the STORE holds for a verified session, and what is still missing.

    ``uploaded_parts`` lists only parts with the expected size; a part of
    any other size is reported missing, so a resume re-sends it."""
    if not session.fingerprint:
        raise NotAVerifiedUpload(f"session {session.pk} has no fingerprint")
    good = _good_parts(session, _stored_parts(session))
    total = int(session.total_parts)
    return {
        "part_size_bytes": int(session.part_size_bytes),
        "total_parts": total,
        "uploaded_parts": [good[n] for n in sorted(good)],
        "missing": [n for n in range(1, total + 1) if n not in good],
    }


def lookup(workspace_id, fingerprint: str, *, queryset=None) -> dict | None:
    """The newest recording in *workspace_id* carrying *fingerprint*.

    ``{state: "complete", recording, session}`` when that upload finished,
    ``{state: "in_progress", recording, session}`` while its session is
    open and unexpired, ``None`` otherwise (expired open sessions are
    ignored). *queryset* narrows the recordings considered — pass the
    caller's visible set so a lookup never names a recording the caller
    could not open."""
    if not is_sha256_hex(fingerprint):
        return None
    sessions = (
        UploadSession.objects.filter(
            fingerprint=fingerprint,
            recording__workspace_id=workspace_id,
            recording__deleted_at__isnull=True,
        )
        .select_related("recording")
        .order_by("-created_at")
    )
    if queryset is not None:
        sessions = sessions.filter(recording__in=queryset)
    now = timezone.now()
    for session in sessions[:20]:
        recording = session.recording
        if session.finalized_at or recording.file_storage_key or recording.normalized_storage_key:
            return {"state": "complete", "recording": recording, "session": session}
        if session.expires_at > now:
            return {"state": "in_progress", "recording": recording, "session": session}
    return None


def _declared_digests(declared_parts) -> dict[int, str]:
    from .services import InvalidMultipartParts

    out: dict[int, str] = {}
    for item in declared_parts or []:
        if not isinstance(item, dict):
            raise InvalidMultipartParts(f"part entry is not an object: {item!r}")
        number = item.get("part_number", item.get("PartNumber"))
        try:
            number = int(number)
        except (TypeError, ValueError) as exc:
            raise InvalidMultipartParts(f"part entry without a part number: {item!r}") from exc
        digest = item.get("sha256")
        if digest in (None, ""):
            continue
        if not is_sha256_hex(digest):
            raise InvalidMultipartParts(f"part {number}: sha256 must be 64 lower-case hex chars")
        out[number] = digest
    return out


def verify_manifest(session: UploadSession, declared_parts) -> list[dict]:
    """Check a verified upload against the store before completing it.

    Every part 1..total must be stored with its expected size, else
    :class:`UploadPartsMissing`. Where the store reports a digest it must
    equal the declared one (when declared), else
    :class:`UploadPartMismatch` for that part; when it reports a digest for
    every part, their fingerprint must equal the session's, else
    :class:`UploadPartMismatch` ``(0)``.

    Returns the part list to complete with — the STORE's ETags and digests,
    not the client's."""
    declared = _declared_digests(declared_parts)
    good = _good_parts(session, _stored_parts(session))
    total = int(session.total_parts)
    missing = [n for n in range(1, total + 1) if n not in good]
    if missing:
        raise UploadPartsMissing(missing)

    ordered = [good[n] for n in range(1, total + 1)]
    for part in ordered:
        want = declared.get(part["part_number"])
        if part["sha256"] and want and part["sha256"] != want:
            raise UploadPartMismatch(part["part_number"])
    if all(p["sha256"] for p in ordered):
        recomputed = fingerprint_of(
            int(session.max_size_bytes),
            int(session.part_size_bytes),
            [p["sha256"] for p in ordered],
        )
        if recomputed != session.fingerprint:
            raise UploadPartMismatch(0)

    out = []
    for part in ordered:
        entry = {"PartNumber": part["part_number"], "ETag": part["etag"]}
        if part["sha256"]:
            entry["ChecksumSHA256"] = hex_to_b64(part["sha256"])
        out.append(entry)
    return out


__all__ = [
    "FINGERPRINT_PREFIX",
    "CHECKSUM_ALGORITHM",
    "MAX_MINT_PER_CALL",
    "total_parts_for",
    "expected_part_size",
    "fingerprint_of",
    "fingerprint_bytes",
    "is_sha256_hex",
    "hex_to_b64",
    "mint_part_urls",
    "manifest",
    "lookup",
    "verify_manifest",
    "UploadPartsMissing",
    "UploadPartMismatch",
    "UploadSessionExpired",
    "NotAVerifiedUpload",
    "PartsUnlistable",
]
