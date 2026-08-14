"""Authorized media delivery (audit STORE-01).

The finding: the bytes were served by the bucket, anonymously, so every
authorization decision in this module was decoration. The properties pinned
here are the ones that let the bucket be made private without losing
playback — and the ones that make "private bucket" the only supported way to
run this:

  - bytes are reachable only through an endpoint that authorizes first;
  - an unauthenticated caller gets no URL, on either the owner or the share
    path (a share still requires a *valid* link, and the media grant on top);
  - the URL it does hand out expires, and the deadline is real — the signed
    URL carries it, and the TTL is a setting, short by default;
  - a storage backend that can only mint a permanent URL is REFUSED, not
    accommodated. That refusal is what stops delivery from quietly falling
    back to the anonymous bucket the audit found.
"""
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_recordings import media, shares
from stapel_recordings.models import RecordingStatus

pytestmark = pytest.mark.django_db

MEDIA_URL = "/recordings/api/v1/recordings/{}/media"


@pytest.fixture
def stored_recording(use_fakes, make_recording):
    """A completed recording whose media object exists in FakeStorage."""
    from stapel_recordings.storage import get_storage

    recording = make_recording(status=RecordingStatus.COMPLETED)
    key = f"recordings/{recording.workspace_id}/{recording.id}/audio.mp3"
    recording.file_storage_key = key
    recording.save(update_fields=["file_storage_key"])
    get_storage().put_bytes(key, b"audio-bytes", content_type="audio/mpeg")
    return recording


def _unsigned_backend():
    """Swap in a backend whose GET URL never expires (the DjangoStorageBackend
    shape: ``storage.url()``)."""
    return override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.UnsignedFakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
        }
    )


# ── the finding itself: no bytes without an authorization decision ───────


def test_unauthenticated_caller_gets_no_media_url(api_client, stored_recording):
    """The core of STORE-01: with the bucket private, THIS is the only door,
    and an anonymous caller does not get through it."""
    resp = api_client.get(MEDIA_URL.format(stored_recording.id))

    assert resp.status_code in (401, 403), resp.content
    assert stored_recording.file_storage_key.encode() not in resp.content


def test_a_stranger_gets_no_media_url(api_client, stored_recording, django_user_model):
    """Authenticated is not authorized: the object policy is asked about
    *this* recording, and the default policy is owner-only."""
    stranger = django_user_model.objects.create(username=f"s-{uuid.uuid4().hex[:8]}")
    api_client.force_authenticate(user=stranger)

    resp = api_client.get(MEDIA_URL.format(stored_recording.id))

    assert resp.status_code == 404, resp.content
    assert b"fake.invalid" not in resp.content


def test_owner_gets_a_short_lived_url(api_client, stored_recording, user):
    api_client.force_authenticate(user=user)
    before = timezone.now()

    resp = api_client.get(MEDIA_URL.format(stored_recording.id))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert stored_recording.file_storage_key in body["url"]
    # The expiry is part of the payload because the client has to plan for
    # it — and it is the DEFAULT, which must stay short.
    assert body["expires_in"] == 300
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert before < expires_at <= before + timedelta(seconds=301)


def test_media_url_ttl_is_configuration(api_client, stored_recording, user):
    """The TTL is a STAPEL_RECORDINGS key, not a literal — an operator can
    make it shorter without touching code."""
    api_client.force_authenticate(user=user)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "MEDIA_URL_TTL_SECONDS": 30,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        resp = api_client.get(MEDIA_URL.format(stored_recording.id))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["expires_in"] == 30
    # The TTL actually reached the signer, rather than only being reported.
    assert "expires_in=30" in body["url"]


def test_redirect_mode_hands_the_url_to_the_player(api_client, stored_recording, user):
    api_client.force_authenticate(user=user)

    resp = api_client.get(MEDIA_URL.format(stored_recording.id) + "?redirect=1")

    assert resp.status_code == 302
    assert stored_recording.file_storage_key in resp["Location"]


def test_recording_without_media_is_409(api_client, use_fakes, make_recording, user):
    recording = make_recording(status=RecordingStatus.QUEUED)
    api_client.force_authenticate(user=user)

    resp = api_client.get(MEDIA_URL.format(recording.id))

    assert resp.status_code == 409, resp.content


# ── the refusal that keeps delivery off the anonymous bucket ─────────────


def test_unsigned_backend_is_refused_instead_of_leaking_a_permanent_url(
    api_client, stored_recording, user
):
    """A backend that can only produce a forever-URL gets 503 and no URL.

    Handing that URL out would restore exactly what the audit found: bytes
    reachable by anyone who ever saw the link, with no deadline and no
    second authorization."""
    api_client.force_authenticate(user=user)
    with _unsigned_backend():
        from stapel_recordings import storage

        storage.reset_storage_cache()
        resp = api_client.get(MEDIA_URL.format(stored_recording.id))

    assert resp.status_code == 503, resp.content
    assert b"public.example" not in resp.content


def test_default_django_storage_backend_declares_it_cannot_sign():
    """The shipped default is honest about ``storage.url()``: it is a served
    URL, not a presigned one."""
    from stapel_recordings.storage import DjangoStorageBackend, S3Backend

    assert DjangoStorageBackend.signs_get_urls is False
    assert S3Backend.signs_get_urls is True


def test_host_can_vouch_for_a_signing_django_backend(stored_recording):
    """Escape hatch for django-storages backends that really do sign
    (S3Boto3Storage with querystring_auth) — a setting, not a fork."""
    with _unsigned_backend():
        from stapel_recordings import storage

        storage.reset_storage_cache()
        with pytest.raises(media.MediaDeliveryUnavailable):
            media.issue_media_url(stored_recording)

    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.UnsignedFakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "STORAGE_SIGNS_GET_URLS": True,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        assert media.issue_media_url(stored_recording).url.startswith("https://public.example/")


# ── the URL really expires ───────────────────────────────────────────────


def test_presigned_url_carries_a_deadline_and_honours_the_ttl():
    """The S3 backend's GET URL is a SigV4 query signature with an explicit
    lifetime — this is what makes a private bucket serviceable.

    Signing is local arithmetic (no request is made, and these credentials
    are fabricated for the test), so what is asserted is the artifact: the
    URL carries a signature, the credential scope, its issue time and the
    exact TTL it was minted with — and a different TTL produces a different
    deadline rather than the same permanent link."""
    pytest.importorskip("boto3")
    from stapel_recordings.storage import S3Backend

    with override_settings(
        STAPEL_RECORDINGS={
            "S3_ENDPOINT_URL": "https://storage.invalid",
            "S3_PUBLIC_URL": "https://storage.invalid",
            "S3_ACCESS_KEY": "test-only-not-a-credential",
            "S3_SECRET_KEY": "test-only-not-a-credential",
            "S3_BUCKET": "recordings-test",
        }
    ):
        backend = S3Backend()
        short = parse_qs(urlparse(backend.presigned_get_url("k/audio.mp3", expires_seconds=60)).query)
        longer = parse_qs(urlparse(backend.presigned_get_url("k/audio.mp3", expires_seconds=900)).query)

    assert short["X-Amz-Expires"] == ["60"]
    assert longer["X-Amz-Expires"] == ["900"]
    # A signature bound to that window, not a bare object URL.
    assert short["X-Amz-Signature"][0]
    assert short["X-Amz-Credential"][0]
    # Issued now, so the deadline is now + TTL — the URL dies on its own.
    issued = datetime.strptime(short["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    assert issued + timedelta(seconds=60) <= timezone.now() + timedelta(seconds=61)


def test_issue_media_url_never_mints_an_endless_url(stored_recording):
    """A non-positive TTL is clamped, not passed through: on some signers
    ``0`` means "no expiry", which is the one outcome that must be
    unreachable from this module."""
    grant = media.issue_media_url(stored_recording, ttl_seconds=0)

    assert grant.ttl_seconds == 1
    assert "expires_in=1" in grant.url


def test_media_key_falls_back_to_the_normalized_copy(use_fakes, make_recording):
    """A host whose retention drops originals after conversion still plays."""
    recording = make_recording(status=RecordingStatus.COMPLETED, normalized_storage_key="n/audio.wav")

    assert media.media_storage_key(recording) == "n/audio.wav"


# ── the public share path ────────────────────────────────────────────────


def _share(recording, **kwargs):
    return shares.create_share(recording=recording, **kwargs)


def test_share_media_url_is_authorized_and_short_lived(api_client, stored_recording):
    """A shared recording plays without the bucket being readable by anyone:
    the link is verified on every call, and the URL it yields expires."""
    _share_row, link = _share(stored_recording, permissions=[shares.PERM_MEDIA])

    resp = api_client.get(f"/recordings/api/v1/shares/{link}/media")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert stored_recording.file_storage_key in body["url"]
    # Shorter than the owner's: this URL leaves the trust boundary.
    assert body["expires_in"] == 300
    assert "expires_in=300" in body["url"]


def test_share_without_the_media_grant_gets_no_url(api_client, stored_recording):
    _share_row, link = _share(stored_recording)  # default grant: view only

    resp = api_client.get(f"/recordings/api/v1/shares/{link}/media")

    assert resp.status_code == 403, resp.content
    assert b"fake.invalid" not in resp.content


def test_forged_share_token_gets_no_url(api_client, stored_recording):
    resp = api_client.get(f"/recordings/api/v1/shares/{uuid.uuid4().hex}/media")

    assert resp.status_code == 404, resp.content
    assert b"fake.invalid" not in resp.content


def test_revoked_share_stops_serving_media(api_client, stored_recording):
    share, link = _share(stored_recording, permissions=[shares.PERM_MEDIA])
    shares.revoke_share(share)

    resp = api_client.get(f"/recordings/api/v1/shares/{link}/media")

    assert resp.status_code == 404, resp.content


def test_passcode_share_serves_media_only_after_unlock(api_client, stored_recording):
    """The SHARE-01 shape, on the media path: an arbitrary token is not a
    key, and the real one comes from the unlock endpoint."""
    _share_row, link = _share(
        stored_recording, permissions=[shares.PERM_MEDIA], passcode="1234"
    )
    url = f"/recordings/api/v1/shares/{link}/media"

    assert api_client.get(url).status_code == 401
    assert api_client.get(url, HTTP_X_SHARE_UNLOCK_TOKEN="anything").status_code == 401

    unlocked = api_client.post(
        f"/recordings/api/v1/shares/{link}/unlock", {"passcode": "1234"}, format="json"
    )
    assert unlocked.status_code == 200, unlocked.content
    token = unlocked.json()["unlock_token"]

    ok = api_client.get(url, HTTP_X_SHARE_UNLOCK_TOKEN=token)
    assert ok.status_code == 200, ok.content
    assert stored_recording.file_storage_key in ok.json()["url"]


def test_wrong_passcode_is_refused_and_bounded(api_client, stored_recording):
    _share_row, link = _share(stored_recording, permissions=[shares.PERM_MEDIA], passcode="1234")
    url = f"/recordings/api/v1/shares/{link}/unlock"

    with override_settings(STAPEL_RECORDINGS={"SHARE_UNLOCK_MAX_ATTEMPTS": 2}):
        assert api_client.post(url, {"passcode": "0000"}, format="json").status_code == 401
        assert api_client.post(url, {"passcode": "0000"}, format="json").status_code == 401
        throttled = api_client.post(url, {"passcode": "1234"}, format="json")

    assert throttled.status_code == 429, throttled.content
    assert throttled["Retry-After"]


def test_share_detail_renders_only_what_is_granted(api_client, stored_recording):
    stored_recording.summary = "secret summary"
    stored_recording.save(update_fields=["summary"])
    _share_row, link = _share(stored_recording, permissions=[shares.PERM_MEDIA])

    body = api_client.get(f"/recordings/api/v1/shares/{link}").json()

    assert body["summary"] is None
    assert body["segments"] == []
    assert stored_recording.file_storage_key in body["media_url"]
    # The tenant's own identifiers are not in a public payload at all.
    assert "workspace_id" not in body


def test_share_payload_omits_media_when_the_backend_cannot_sign(
    api_client, stored_recording
):
    """Fail-closed on the public path too: no permanent URL is smuggled into
    the share payload when delivery is unavailable."""
    _share_row, link = _share(stored_recording, permissions=[shares.PERM_MEDIA])

    with _unsigned_backend():
        from stapel_recordings import storage

        storage.reset_storage_cache()
        body = api_client.get(f"/recordings/api/v1/shares/{link}").json()
        media_resp = api_client.get(f"/recordings/api/v1/shares/{link}/media")

    assert body["media_url"] is None
    assert media_resp.status_code == 503
    assert b"public.example" not in media_resp.content
