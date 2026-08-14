"""Authorized media delivery (audit STORE-01).

The finding this closes: the bytes of a recording were reachable because the
bucket was reachable. Delivery was a *deployment* property — an anonymous
download policy on the bucket plus a public proxy in front of it — so every
authorization decision this module makes was advisory: anyone holding (or
guessing) an object key read the audio without passing any of them.

The rule here is the inverse, and it has two halves that only work together:

1. **Authorization first, URL second.** Nothing in this module hands out a
   media URL except through :func:`issue_media_url`, and every caller of it
   has already asked the object policy (owner path) or the share primitive
   (public path) whether *this* request may read *this* recording.
2. **The URL is a bearer credential, so it expires.** Once minted, it is
   the answer to "may I read these bytes" for as long as it is valid —
   nothing checks the policy again. That is why the TTL is short by default
   (``MEDIA_URL_TTL_SECONDS``) and why a backend that cannot bound the URL
   in time is refused rather than accommodated.

Refusing is the load-bearing part. ``RecordingStorage.presigned_get_url``
is a *seam*, and one shipped backend (:class:`DjangoStorageBackend`)
implements it as ``storage.url(key)`` — a permanent, unauthenticated URL.
Handing that to a client is the anonymous-bucket delivery this module is
supposed to stop depending on, dressed as a presigned URL. So the seam now
declares whether it signs (``RecordingStorage.signs_get_urls``), and a
backend that does not gets :class:`MediaDeliveryUnavailable` — a 503 the
operator can read — instead of a URL that works forever.

Consequence, stated plainly: with the default storage backend this module
serves no media at all. That is the intended failure mode. A host on
S3/MinIO uses :class:`~stapel_recordings.storage.S3Backend`; a host whose
django-storages backend genuinely signs its ``.url()`` says so with
``STAPEL_RECORDINGS["STORAGE_SIGNS_GET_URLS"] = True``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .conf import optional_flag, recordings_settings
from .storage import get_storage


class MediaUnavailable(Exception):
    """Base: the request was authorized, but no media URL can be issued."""


class MediaNotStored(MediaUnavailable):
    """The recording has no media object — never uploaded, or purged."""


class MediaDeliveryUnavailable(MediaUnavailable):
    """The configured storage backend cannot mint an expiring, credentialed
    GET URL. A deployment fault, not a caller fault: the caller was
    authorized and there is a stored object, but the only URL this backend
    could produce would not expire."""


@dataclass(frozen=True)
class MediaGrant:
    """A time-boxed permission to read one recording's bytes."""

    url: str
    expires_at: datetime
    ttl_seconds: int


def storage_signs_get_urls(storage=None) -> bool:
    """Whether presigned GETs from *storage* are credentialed and expiring.

    Tri-state: ``STORAGE_SIGNS_GET_URLS`` overrides the backend's own
    declaration when set, so a host can vouch for a Django storage backend
    that really does sign without subclassing anything. ``None`` (default)
    means "ask the backend", and a backend that says nothing means no.

    Read through :func:`~stapel_recordings.conf.optional_flag`, not
    ``bool()``: the setting arrives uncoerced, and ``bool("false")`` is True
    — which would turn a host writing the string ``"false"`` into a host
    VOUCHING that its backend signs, and hand out the permanent URL this
    whole module exists to stop handing out."""
    override = optional_flag("STORAGE_SIGNS_GET_URLS")
    if override is not None:
        return override
    return bool(getattr(storage if storage is not None else get_storage(), "signs_get_urls", False))


def media_storage_key(recording) -> str | None:
    """The object key playback should read, or ``None``.

    The uploaded original first, the pipeline's normalized copy as a
    fallback: the original is what the owner uploaded and what a share is
    understood to expose, but a host whose retention drops originals after
    conversion still has something to play."""
    return (
        getattr(recording, "file_storage_key", None)
        or getattr(recording, "normalized_storage_key", None)
        or None
    )


def issue_media_url(recording, *, ttl_seconds: int | None = None) -> MediaGrant:
    """Mint a short-lived media URL for an **already authorized** request.

    This function does not authorize — it cannot, because "may this caller
    read this recording" has two different answers (object policy for an
    account, share grant for a link) and both are decided by the caller.
    What it guarantees is the other half: the URL it returns is bounded in
    time, or there is no URL.

    Raises :class:`MediaNotStored` when the recording has no object and
    :class:`MediaDeliveryUnavailable` when the backend cannot sign.
    """
    key = media_storage_key(recording)
    if not key:
        raise MediaNotStored(f"recording {getattr(recording, 'id', '?')} has no stored media")

    storage = get_storage()
    if not storage_signs_get_urls(storage):
        raise MediaDeliveryUnavailable(
            f"{type(storage).__name__} cannot issue an expiring GET URL; "
            "configure STAPEL_RECORDINGS['STORAGE'] with a signing backend "
            "(storage.S3Backend) or set STORAGE_SIGNS_GET_URLS=True if the "
            "configured Django storage backend signs its url()"
        )

    ttl = int(ttl_seconds if ttl_seconds is not None else recordings_settings.MEDIA_URL_TTL_SECONDS)
    # A non-positive TTL would either be rejected by the signer or, worse,
    # silently mean "no expiry" on some backends. Clamp to a second: a
    # useless URL is a better failure than an eternal one.
    ttl = max(1, ttl)
    url = storage.presigned_get_url(key, expires_seconds=ttl)
    return MediaGrant(url=url, expires_at=timezone.now() + timedelta(seconds=ttl), ttl_seconds=ttl)


__all__ = [
    "MediaGrant",
    "MediaUnavailable",
    "MediaNotStored",
    "MediaDeliveryUnavailable",
    "issue_media_url",
    "media_storage_key",
    "storage_signs_get_urls",
]
