"""Public share links and passcode unlock (audit SHARE-01).

A share link is an authorization decision made for an anonymous request, so
it is a *primitive*, not a view detail. This module publishes the whole
thing — mint, resolve, unlock, verify, rotate, revoke — so no consumer has
to invent it, and the safe behaviour is what a caller gets by default.

The shape of the mechanism
--------------------------
Two secrets, deliberately different in kind:

**Link token** — 32 random bytes, urlsafe-encoded, returned exactly once at
creation and stored only as a SHA-256 digest. High entropy, so a fast digest
is the right hash and lookup-by-digest is the right comparison: the database
never holds a working link, and no code path compares the secret itself.

**Passcode** — human-chosen and therefore low entropy, so it goes through
Django's configured password hasher (a slow KDF) and is guarded by a
counter: :data:`SHARE_UNLOCK_MAX_ATTEMPTS` failures lock unlocking for
``SHARE_UNLOCK_LOCKOUT_SECONDS``. Without that pair, a four-digit passcode
is an afternoon's work.

**Unlock token** — what the client presents after passing the passcode. It
is a signed, purpose-salted, time-limited token carrying the share id, the
token version and the granted permissions
(:func:`issue_unlock_token`). Signed rather than stored because the
verification already reads the share row: a stored random token would add a
write per unlock and buy nothing. It is bound in four ways, and every one of
them matters — the salt binds it to *this* purpose, the share id to *this*
share (a token from another share is not a key here), the version to the
current passcode/revocation generation, and ``max_age`` to a lifetime. The
failure this replaces is a token that is merely *nonempty*.

Verification is total: :func:`access_share` is the single call a view needs,
and every refusal it can make — unknown link, revoked, expired, deleted
recording, passcode required, wrong passcode, throttled — is a typed
exception carrying the i18n error key to answer with. A caller cannot
accidentally skip one of those checks, because there is no partial entry
point.

Permission enforcement is the caller's, but the vocabulary is not: a share
grants a subset of :data:`SHARE_PERMISSIONS` and :func:`require_permission`
is how a view asks. The default grant is the minimum (``view``) — a share
created without an explicit permission list exposes metadata, not the
transcript.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .conf import recordings_settings
from .errors import (
    ERR_401_SHARE_PASSCODE_REQUIRED,
    ERR_403_SHARE_PERMISSION_DENIED,
    ERR_404_SHARE_NOT_FOUND,
    ERR_429_SHARE_THROTTLED,
)
from .models import Recording, RecordingShare, RecordingStatus

#: Signing salt — binds an unlock token to this purpose. A signature valid
#: for some other salt in the same project is not valid here.
_UNLOCK_SALT = "stapel_recordings.share_unlock"

#: Recording metadata: title, status, duration, counters. Always granted.
PERM_VIEW = "view"
#: The transcript itself (segments/speakers).
PERM_TRANSCRIPT = "transcript"
#: The generated summary.
PERM_SUMMARY = "summary"
#: A time-limited URL to the media object.
PERM_MEDIA = "media"

#: The vocabulary a share may grant. Consumers add their own only by
#: extending this tuple in a subclassing host — an unknown permission is
#: rejected at creation rather than silently granting nothing.
SHARE_PERMISSIONS = (PERM_VIEW, PERM_TRANSCRIPT, PERM_SUMMARY, PERM_MEDIA)

#: What a share grants when the caller does not say. Least privilege.
DEFAULT_PERMISSIONS = (PERM_VIEW,)


class ShareError(Exception):
    """Base for every share refusal. Carries the i18n error key and the HTTP
    status a view should answer with, so the mapping lives with the rule
    rather than being re-derived in each consumer."""

    error_key = ERR_404_SHARE_NOT_FOUND
    status_code = 404


class ShareNotFound(ShareError):
    """Unknown, revoked, expired link, or a recording that is gone.

    All four collapse into one refusal on purpose: distinguishing them tells
    a probing client which of its guessed tokens exist."""


class SharePasscodeRequired(ShareError):
    """The share is passcode-protected and no valid unlock token was
    presented."""

    error_key = ERR_401_SHARE_PASSCODE_REQUIRED
    status_code = 401


class ShareThrottled(ShareError):
    """Too many failed unlock attempts; unlocking is locked out."""

    error_key = ERR_429_SHARE_THROTTLED
    status_code = 429

    def __init__(self, retry_after_seconds: int = 0):
        super().__init__(f"unlock locked out for {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class SharePermissionDenied(ShareError):
    """The share does not grant the permission the caller asked for."""

    error_key = ERR_403_SHARE_PERMISSION_DENIED
    status_code = 403


@dataclass
class ShareAccess:
    """The outcome of an authorized public share request."""

    share: RecordingShare
    recording: Recording
    permissions: tuple = field(default_factory=tuple)

    def has(self, permission: str) -> bool:
        return permission in self.permissions


# ─── token helpers ─────────────────────────────────────────────────────


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _setting(name: str, default):
    try:
        return getattr(recordings_settings, name)
    except AttributeError:  # pragma: no cover - defensive
        return default


def _normalized_permissions(permissions) -> list[str]:
    if permissions is None:
        return list(DEFAULT_PERMISSIONS)
    wanted = [str(p) for p in permissions]
    unknown = [p for p in wanted if p not in SHARE_PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown share permissions: {unknown}")
    # PERM_VIEW is implied by holding any grant at all.
    ordered = [p for p in SHARE_PERMISSIONS if p in wanted or p == PERM_VIEW]
    return ordered


# ─── lifecycle ─────────────────────────────────────────────────────────


def create_share(
    *,
    recording: Recording,
    permissions=None,
    passcode: str | None = None,
    expires_at=None,
    created_by=None,
) -> tuple[RecordingShare, str]:
    """Mint a share link. Returns ``(share, link_token)``.

    The link token is returned **once** — only its digest is stored, so a
    lost link is re-created, never recovered."""
    token = secrets.token_urlsafe(32)
    share = RecordingShare.objects.create(
        recording=recording,
        created_by=created_by,
        link_token_hash=_digest(token),
        passcode_hash=make_password(passcode) if passcode else "",
        permissions=_normalized_permissions(permissions),
        expires_at=expires_at,
    )
    return share, token


def set_share_passcode(share: RecordingShare, passcode: str | None) -> RecordingShare:
    """Set/clear the passcode and **rotate** the token generation.

    The rotation is the point: a passcode changed because it leaked has to
    invalidate the unlock tokens issued under the old one, and clearing the
    counter re-opens unlocking for the legitimate owner."""
    share.passcode_hash = make_password(passcode) if passcode else ""
    share.token_version = F("token_version") + 1
    share.failed_unlock_count = 0
    share.locked_until = None
    share.save(
        update_fields=[
            "passcode_hash", "token_version", "failed_unlock_count",
            "locked_until", "updated_at",
        ]
    )
    share.refresh_from_db(fields=["token_version"])
    return share


def revoke_share(share: RecordingShare) -> RecordingShare:
    """Revoke a share. Bumps the token generation too, so an unlock token
    minted a second earlier stops working with it."""
    share.revoked_at = timezone.now()
    share.token_version = F("token_version") + 1
    share.save(update_fields=["revoked_at", "token_version", "updated_at"])
    share.refresh_from_db(fields=["token_version"])
    return share


# ─── resolution + verification ─────────────────────────────────────────


def resolve_share(link_token: str) -> Optional[RecordingShare]:
    """Return the share a link token references, or ``None``.

    Lookup is by digest: the stored column is a hash, so an attacker with
    read access to the table still has no working link, and the comparison
    never touches the secret."""
    if not link_token:
        return None
    return (
        RecordingShare.objects.select_related("recording")
        .filter(link_token_hash=_digest(str(link_token)))
        .first()
    )


def _assert_live(share: RecordingShare, *, now=None) -> None:
    now = now or timezone.now()
    if share.revoked_at is not None:
        raise ShareNotFound("share revoked")
    if share.expires_at is not None and share.expires_at <= now:
        raise ShareNotFound("share expired")
    recording = share.recording
    if recording.deleted_at is not None or recording.status == RecordingStatus.DELETED:
        # The recording's lifecycle outranks the link: a deletion that a
        # share link can still read is not a deletion.
        raise ShareNotFound("recording unavailable")


def issue_unlock_token(share: RecordingShare) -> str:
    """Mint a signed unlock token for *share* (see the module docstring for
    what it is bound to)."""
    return signing.dumps(
        {
            "share": str(share.id),
            "v": int(share.token_version),
            "perms": list(share.permissions or []),
        },
        salt=_UNLOCK_SALT,
    )


def verify_unlock_token(share: RecordingShare, token: str) -> bool:
    """True iff *token* is a currently valid unlock token for *share*.

    Every clause is load-bearing: signature (not forged), ``max_age`` (not
    expired), share id (not another share's token), version (not minted
    before the last passcode change or revocation)."""
    if not token:
        return False
    max_age = int(_setting("SHARE_UNLOCK_TOKEN_TTL_SECONDS", 3600))
    try:
        payload = signing.loads(str(token), salt=_UNLOCK_SALT, max_age=max_age)
    except signing.BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("share")) != str(share.id):
        return False
    return int(payload.get("v", -1)) == int(share.token_version)


def unlock_share(share: RecordingShare, passcode: str, *, now=None) -> str:
    """Check *passcode* and return a fresh unlock token.

    Raises :class:`ShareThrottled` while locked out and
    :class:`SharePasscodeRequired` on a wrong passcode. Both the counter and
    the lockout are persisted on the row, so the bound holds across
    processes — a per-process counter is not a bound at all.
    """
    now = now or timezone.now()
    _assert_live(share, now=now)
    if not share.passcode_hash:
        # Nothing to unlock; hand back a token anyway so a client that always
        # unlocks first works against both kinds of share.
        return issue_unlock_token(share)

    if share.locked_until and share.locked_until > now:
        raise ShareThrottled(int((share.locked_until - now).total_seconds()))

    if check_password(passcode or "", share.passcode_hash):
        RecordingShare.objects.filter(pk=share.pk).update(
            failed_unlock_count=0, locked_until=None
        )
        share.failed_unlock_count = 0
        share.locked_until = None
        return issue_unlock_token(share)

    _register_failed_unlock(share, now=now)
    raise SharePasscodeRequired("invalid passcode")


def _register_failed_unlock(share: RecordingShare, *, now) -> None:
    max_attempts = int(_setting("SHARE_UNLOCK_MAX_ATTEMPTS", 5))
    lockout = int(_setting("SHARE_UNLOCK_LOCKOUT_SECONDS", 300))
    with transaction.atomic():
        locked = RecordingShare.objects.select_for_update().get(pk=share.pk)
        locked.failed_unlock_count = (locked.failed_unlock_count or 0) + 1
        if locked.failed_unlock_count >= max_attempts:
            locked.locked_until = now + timedelta(seconds=lockout)
            locked.failed_unlock_count = 0
        locked.save(update_fields=["failed_unlock_count", "locked_until", "updated_at"])
    share.refresh_from_db(fields=["failed_unlock_count", "locked_until"])


def access_share(link_token: str, *, unlock_token: str | None = None, now=None) -> ShareAccess:
    """Authorize a public share request end to end.

    The one call a view makes. Resolves the link, enforces revocation,
    expiry and the recording's own lifecycle, requires a **verified** unlock
    token when a passcode is set, counts the access atomically, and returns
    what the caller is allowed to render.
    """
    now = now or timezone.now()
    share = resolve_share(link_token)
    if share is None:
        raise ShareNotFound("unknown share token")
    _assert_live(share, now=now)

    if share.passcode_hash and not verify_unlock_token(share, unlock_token or ""):
        raise SharePasscodeRequired("passcode required")

    # F() so concurrent reads of the same link cannot lose counts against
    # each other (read-modify-write on a public endpoint is a lost update by
    # construction).
    RecordingShare.objects.filter(pk=share.pk).update(
        access_count=F("access_count") + 1, last_accessed_at=now
    )
    return ShareAccess(
        share=share,
        recording=share.recording,
        permissions=tuple(share.permissions or DEFAULT_PERMISSIONS),
    )


def require_permission(access: ShareAccess, permission: str) -> None:
    """Raise :class:`SharePermissionDenied` unless *access* grants
    *permission*."""
    if not access.has(permission):
        raise SharePermissionDenied(f"share does not grant {permission!r}")


__all__ = [
    "PERM_VIEW",
    "PERM_TRANSCRIPT",
    "PERM_SUMMARY",
    "PERM_MEDIA",
    "SHARE_PERMISSIONS",
    "DEFAULT_PERMISSIONS",
    "ShareAccess",
    "ShareError",
    "ShareNotFound",
    "SharePasscodeRequired",
    "SharePermissionDenied",
    "ShareThrottled",
    "create_share",
    "revoke_share",
    "set_share_passcode",
    "resolve_share",
    "access_share",
    "unlock_share",
    "issue_unlock_token",
    "verify_unlock_token",
    "require_permission",
]
