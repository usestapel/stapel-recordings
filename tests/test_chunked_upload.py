"""Verified, resumable multipart uploads (fingerprint v1, chunked.py)."""
import base64
import hashlib
import uuid
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_recordings import chunked, events, services
from stapel_recordings.models import RecordingStatus, UploadSession
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db

PART = 4
DATA = b"0123456789"  # 3 parts of a 4-byte part size: 4, 4, 2


@pytest.fixture
def small_parts():
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "MULTIPART_PART_SIZE": PART,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        yield
        storage.reset_storage_cache()


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _chunks(data=DATA):
    return [data[i:i + PART] for i in range(0, len(data), PART)]


def _start(recording, data=DATA, fingerprint=None):
    fp = fingerprint or chunked.fingerprint_bytes(data, PART)[0]
    session, parts, part_size = services.start_multipart_upload(
        recording=recording, file_size_bytes=len(data), filename="take.mp3", fingerprint=fp
    )
    return session, parts, part_size


def _send_all(session, data=DATA, skip=()):
    for n, chunk in enumerate(_chunks(data), start=1):
        if n not in skip:
            fakes.put_part(session.multipart_upload_id, n, chunk, sha256_b64=_b64(chunk))


# ── fingerprint v1 test vectors (shared with the client) ──────────────────


def test_fingerprint_vector_abc():
    fp, hashes = chunked.fingerprint_bytes(b"abc", 2)
    assert fp == "8c4efddc2b77fce99702c6c9d161847e1fee33254038bd4f2239c0d3b2789d83"
    assert hashes[0].startswith("fb8e20fc") and hashes[0].endswith("0603")
    assert hashes[1].startswith("2e7d2c03") and hashes[1].endswith("efc6")


def test_fingerprint_vector_two_mib():
    data = bytes(((i * 7 + 3) & 0xFF) for i in range(2 * 1024 * 1024 + 5))
    fp, hashes = chunked.fingerprint_bytes(data, 1024 * 1024)
    assert fp == "9fa7f4afce224ad288277d5ded529f672341fc3e360d5d555cdf4f9c5df1eaba"
    assert [h[:8] + h[-4:] for h in hashes] == [
        "172c15dc28fd", "172c15dc28fd", "c0a7188b4646",
    ]


def test_fingerprint_vector_empty():
    fp, hashes = chunked.fingerprint_bytes(b"", 4)
    assert fp == "0bfd9fff69fdcfb5ac58acfb5054f1c485487dab1004940357d2036b9b713956"
    assert len(hashes) == 1


def test_fingerprint_of_matches_bytes_form():
    fp, hashes = chunked.fingerprint_bytes(DATA, PART)
    assert chunked.fingerprint_of(len(DATA), PART, hashes) == fp


def test_expected_part_size():
    assert [chunked.expected_part_size(n, 10, 4) for n in (1, 2, 3)] == [4, 4, 2]
    assert chunked.expected_part_size(1, 8, 4) == 4
    assert chunked.expected_part_size(2, 8, 4) == 4
    with pytest.raises(ValueError):
        chunked.expected_part_size(3, 8, 4)


# ── start ────────────────────────────────────────────────────────────────


def test_start_with_fingerprint_is_verified_and_mints_nothing(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, parts, part_size = _start(r)
    assert parts == []
    assert part_size == PART
    assert session.fingerprint == chunked.fingerprint_bytes(DATA, PART)[0]
    assert (session.part_size_bytes, session.total_parts) == (PART, 3)
    assert fakes._MULTIPART[session.multipart_upload_id]["verified"] is True


def test_legacy_start_is_unchanged(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, parts, _ = services.start_multipart_upload(
        recording=r, file_size_bytes=len(DATA), filename="take.mp3"
    )
    assert [p["part_number"] for p in parts] == [1, 2, 3]
    assert "?sha256" not in parts[0]["presigned_url"]
    assert session.fingerprint is None and session.total_parts is None
    assert fakes._MULTIPART[session.multipart_upload_id]["verified"] is False


def test_start_refuses_a_malformed_fingerprint(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(services.InvalidMultipartParts):
        _start(r, fingerprint="ABC")
    assert not UploadSession.objects.filter(recording=r).exists()


# ── mint ─────────────────────────────────────────────────────────────────


def test_mint_binds_each_url_to_its_hash(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    digest = hashlib.sha256(b"0123").hexdigest()
    minted = chunked.mint_part_urls(
        session, [{"part_number": 1, "sha256": digest}], expires_seconds=60
    )
    b64 = base64.b64encode(bytes.fromhex(digest)).decode()
    assert minted == [
        {
            "part_number": 1,
            "presigned_url": f"memory://part/{session.multipart_upload_id}/1?sha256={b64}",
            "headers": {"x-amz-checksum-sha256": b64, "x-amz-sdk-checksum-algorithm": "SHA256"},
        }
    ]


def test_mint_moves_the_session_deadline(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    UploadSession.objects.filter(pk=session.pk).update(
        expires_at=timezone.now() + timedelta(seconds=30)
    )
    session.refresh_from_db()
    chunked.mint_part_urls(
        session, [{"part_number": 1, "sha256": "a" * 64}], expires_seconds=60
    )
    session.refresh_from_db()
    assert session.expires_at > timezone.now() + timedelta(hours=1)


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [{"part_number": 0, "sha256": "a" * 64}],
        [{"part_number": 4, "sha256": "a" * 64}],
        [{"part_number": 1, "sha256": "A" * 64}],
        [{"part_number": 1, "sha256": "a" * 63}],
        [{"part_number": 1, "sha256": "a" * 64}, {"part_number": 1, "sha256": "b" * 64}],
        [{"part_number": 1, "sha256": "a" * 64}] * 101,
    ],
)
def test_mint_validates_the_part_list(small_parts, make_recording, parts):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    with pytest.raises(services.InvalidMultipartParts):
        chunked.mint_part_urls(session, parts, expires_seconds=60)


def test_mint_refuses_a_legacy_or_expired_session(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    legacy, _, _ = services.start_multipart_upload(
        recording=r, file_size_bytes=len(DATA), filename="take.mp3"
    )
    with pytest.raises(chunked.NotAVerifiedUpload):
        chunked.mint_part_urls(legacy, [{"part_number": 1, "sha256": "a" * 64}], expires_seconds=60)

    session, _, _ = _start(r)
    UploadSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    session.refresh_from_db()
    with pytest.raises(chunked.UploadSessionExpired) as exc:
        chunked.mint_part_urls(session, [{"part_number": 1, "sha256": "a" * 64}], expires_seconds=60)
    assert exc.value.error_key == "error.409.recording_upload_expired"


# ── manifest ─────────────────────────────────────────────────────────────


def test_manifest_reports_what_the_store_holds(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session, skip=(2,))
    listed = chunked.manifest(session)
    assert listed["total_parts"] == 3
    assert listed["part_size_bytes"] == PART
    assert [p["part_number"] for p in listed["uploaded_parts"]] == [1, 3]
    assert listed["uploaded_parts"][0]["sha256"] == hashlib.sha256(b"0123").hexdigest()
    assert listed["missing"] == [2]


def test_manifest_counts_a_wrong_sized_part_as_missing(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session)
    fakes.put_part(session.multipart_upload_id, 3, b"8", sha256_b64=_b64(b"8"))
    assert chunked.manifest(session)["missing"] == [3]


def test_fake_store_refuses_an_unbound_part_in_verified_mode(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    with pytest.raises(ValueError, match="MissingChecksum"):
        fakes.put_part(session.multipart_upload_id, 1, b"0123")
    with pytest.raises(ValueError, match="XAmzContentChecksumMismatch"):
        fakes.put_part(session.multipart_upload_id, 1, b"0123", sha256_b64=_b64(b"xxxx"))


# ── verify at complete ───────────────────────────────────────────────────


def test_complete_with_a_missing_part_is_409_and_keeps_the_session(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session, skip=(2,))
    with pytest.raises(chunked.UploadPartsMissing) as exc:
        services.finalize_upload(session=session, parts=[])
    assert exc.value.http_status == 409
    assert exc.value.error_key == "error.409.recording_upload_parts_missing"
    assert exc.value.error_params == {"count": 1, "missing": "2"}
    assert UploadSession.objects.filter(pk=session.pk, finalized_at__isnull=True).exists()
    r.refresh_from_db()
    assert r.status == RecordingStatus.UPLOADING
    assert fakes.COMPLETED == []

    # Resume: send the missing part, complete again.
    fakes.put_part(session.multipart_upload_id, 2, b"4567", sha256_b64=_b64(b"4567"))
    services.finalize_upload(session=session, parts=[])
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED


def test_missing_list_names_at_most_fifty(small_parts, make_recording):
    data = bytes(range(256)) * 1  # 64 parts of 4 bytes
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r, data=data)
    with pytest.raises(chunked.UploadPartsMissing) as exc:
        services.finalize_upload(session=session, parts=[])
    assert exc.value.error_params["count"] == 64
    assert exc.value.error_params["missing"] == ",".join(str(n) for n in range(1, 51))


def test_a_declared_hash_the_store_disagrees_with_is_part_mismatch(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session)
    declared = [
        {"PartNumber": 1, "ETag": "x", "sha256": hashlib.sha256(b"0123").hexdigest()},
        {"PartNumber": 2, "ETag": "x", "sha256": "f" * 64},
    ]
    with pytest.raises(chunked.UploadPartMismatch) as exc:
        services.finalize_upload(session=session, parts=declared)
    assert exc.value.error_key == "error.409.recording_upload_part_mismatch"
    assert exc.value.error_params == {"part_number": 2}
    assert fakes.COMPLETED == []


def test_parts_that_do_not_make_the_fingerprint_are_part_zero(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    other = b"abcdefghij"
    session, _, _ = _start(r, fingerprint=chunked.fingerprint_bytes(other, PART)[0])
    _send_all(session, data=DATA)  # same sizes, different bytes
    with pytest.raises(chunked.UploadPartMismatch) as exc:
        services.finalize_upload(session=session, parts=[])
    assert exc.value.error_params == {"part_number": 0}


def test_complete_uses_the_stores_etags_and_checksums(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session)
    services.finalize_upload(
        session=session, parts=[{"PartNumber": n, "ETag": "client-said"} for n in (1, 2, 3)]
    )
    sent = fakes.COMPLETED[0]["parts"]
    assert [p["PartNumber"] for p in sent] == [1, 2, 3]
    assert sent[0]["ETag"] == hashlib.md5(b"0123").hexdigest()
    assert sent[2]["ChecksumSHA256"] == _b64(b"89")
    assert fakes._STORE[session.storage_key] == DATA
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED
    assert r.file_size_bytes == len(DATA)


def test_finalize_is_idempotent_for_a_verified_upload(small_parts, make_recording):
    from stapel_core.django.outbox.models import OutboxEvent

    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    _send_all(session)
    services.finalize_upload(session=session, parts=[])
    session.refresh_from_db()
    # The store no longer lists anything; a replay must not re-verify.
    again = services.finalize_upload(session=session, parts=[])
    assert again.pk == r.pk
    assert len(fakes.COMPLETED) == 1
    assert OutboxEvent.objects.filter(topic=events.ACTION_UPLOADED).count() == 1


def test_legacy_complete_passes_the_client_list_through(small_parts, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = services.start_multipart_upload(
        recording=r, file_size_bytes=len(DATA), filename="take.mp3"
    )
    parts = [{"PartNumber": 1, "ETag": "e1"}]
    services.finalize_upload(session=session, parts=parts)
    assert fakes.COMPLETED[0]["parts"] == parts


# ── lookup ───────────────────────────────────────────────────────────────


def test_lookup_states(small_parts, make_recording):
    fp = chunked.fingerprint_bytes(DATA, PART)[0]
    r = make_recording(status=RecordingStatus.CREATED)
    assert chunked.lookup(r.workspace_id, fp) is None

    session, _, _ = _start(r)
    found = chunked.lookup(r.workspace_id, fp)
    assert found["state"] == "in_progress" and found["session"].pk == session.pk

    assert chunked.lookup(uuid.uuid4(), fp) is None  # another workspace

    _send_all(session)
    services.finalize_upload(session=session, parts=[])
    found = chunked.lookup(r.workspace_id, fp)
    assert found["state"] == "complete" and found["recording"].pk == r.pk


def test_lookup_ignores_expired_and_deleted(small_parts, make_recording):
    fp = chunked.fingerprint_bytes(DATA, PART)[0]
    r = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(r)
    UploadSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert chunked.lookup(r.workspace_id, fp) is None

    r2 = make_recording(status=RecordingStatus.CREATED, workspace_id=r.workspace_id)
    _start(r2)
    r2.deleted_at = timezone.now()
    r2.save(update_fields=["deleted_at"])
    assert chunked.lookup(r.workspace_id, fp) is None


def test_lookup_is_narrowed_by_the_callers_queryset(small_parts, make_recording):
    from stapel_recordings.models import Recording

    fp = chunked.fingerprint_bytes(DATA, PART)[0]
    r = make_recording(status=RecordingStatus.CREATED)
    _start(r)
    assert chunked.lookup(r.workspace_id, fp, queryset=Recording.objects.none()) is None


# ── HTTP ─────────────────────────────────────────────────────────────────

BASE = "/recordings/api/v1/recordings"


def test_http_verified_flow(small_parts, api_client, user, make_recording, stub_membership):
    r = make_recording(status=RecordingStatus.CREATED)
    stub_membership.grant(r.workspace_id, user.pk)
    api_client.force_authenticate(user=user)
    fp, hashes = chunked.fingerprint_bytes(DATA, PART)

    look = api_client.get(f"{BASE}/uploads/lookup", {"fingerprint": fp, "workspace_id": str(r.workspace_id)})
    assert look.status_code == 200, look.content
    assert look.json()["found"] is False

    start = api_client.post(
        f"{BASE}/{r.id}/multipart",
        {"file_size_bytes": len(DATA), "content_type": "audio/mpeg", "fingerprint": fp},
        format="json",
    )
    assert start.status_code == 201, start.content
    body = start.json()
    assert body["parts"] == [] and body["total_parts"] == 3 and body["part_size_bytes"] == PART
    upload_id = body["upload_id"]

    mint = api_client.post(
        f"{BASE}/{r.id}/multipart/{upload_id}/parts",
        {"parts": [{"part_number": n, "sha256": h} for n, h in enumerate(hashes, start=1)]},
        format="json",
    )
    assert mint.status_code == 200, mint.content
    minted = mint.json()["parts"]
    assert minted[0]["headers"]["x-amz-checksum-sha256"] == _b64(b"0123")

    session = UploadSession.objects.get(pk=upload_id)
    _send_all(session, skip=(3,))

    look = api_client.get(f"{BASE}/uploads/lookup", {"fingerprint": fp, "workspace_id": str(r.workspace_id)}).json()
    assert look["found"] is True and look["state"] == "in_progress"
    assert look["upload_id"] == upload_id and look["missing"] == [3]
    assert [p["part_number"] for p in look["uploaded_parts"]] == [1, 2]

    manifest = api_client.get(f"{BASE}/{r.id}/multipart/{upload_id}/parts", {"mint": "none"})
    assert manifest.status_code == 200
    assert manifest.json()["missing"] == [3] and manifest.json()["parts"] == []

    complete_body = {
        "parts": [{"part_number": n, "etag": "e", "sha256": h} for n, h in enumerate(hashes, start=1)]
    }
    missing = api_client.post(f"{BASE}/{r.id}/multipart/{upload_id}/complete", complete_body, format="json")
    assert missing.status_code == 409
    assert "recording_upload_parts_missing" in missing.content.decode()

    fakes.put_part(session.multipart_upload_id, 3, b"89", sha256_b64=_b64(b"89"))
    done = api_client.post(f"{BASE}/{r.id}/multipart/{upload_id}/complete", complete_body, format="json")
    assert done.status_code == 200, done.content
    assert done.json()["status"] == RecordingStatus.QUEUED

    again = api_client.post(f"{BASE}/{r.id}/multipart/{upload_id}/complete", complete_body, format="json")
    assert again.status_code == 200

    look = api_client.get(f"{BASE}/uploads/lookup", {"fingerprint": fp, "workspace_id": str(r.workspace_id)}).json()
    assert look["state"] == "complete" and look["recording_id"] == str(r.id) and look["upload_id"] is None


def test_http_lookup_requires_membership(small_parts, api_client, user, make_recording, stub_membership):
    r = make_recording(status=RecordingStatus.CREATED)
    api_client.force_authenticate(user=user)
    resp = api_client.get(
        f"{BASE}/uploads/lookup", {"fingerprint": "a" * 64, "workspace_id": str(r.workspace_id)}
    )
    assert resp.status_code == 403


def test_http_legacy_flow_is_unchanged(small_parts, api_client, user, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    api_client.force_authenticate(user=user)
    start = api_client.post(f"{BASE}/{r.id}/multipart", {"file_size_bytes": len(DATA), "filename": "take.mp3"}, format="json")
    assert start.status_code == 201, start.content
    body = start.json()
    assert [p["part_number"] for p in body["parts"]] == [1, 2, 3]
    assert body["parts"][0]["headers"] == {}
    upload_id = body["upload_id"]

    listed = api_client.get(f"{BASE}/{r.id}/multipart/{upload_id}/parts", {"mint": "2,3"}).json()
    assert [p["part_number"] for p in listed["parts"]] == [2, 3]

    mint = api_client.post(
        f"{BASE}/{r.id}/multipart/{upload_id}/parts",
        {"parts": [{"part_number": 1, "sha256": "a" * 64}]},
        format="json",
    )
    assert mint.status_code == 409

    done = api_client.post(
        f"{BASE}/{r.id}/multipart/{upload_id}/complete",
        {"parts": [{"part_number": n, "etag": f"e{n}"} for n in (1, 2, 3)]},
        format="json",
    )
    assert done.status_code == 200, done.content
    assert fakes.COMPLETED[0]["parts"][0] == {"PartNumber": 1, "ETag": "e1"}


def test_http_abort_and_foreign_recording(small_parts, api_client, user, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    api_client.force_authenticate(user=user)
    upload_id = api_client.post(
        f"{BASE}/{r.id}/multipart", {"file_size_bytes": len(DATA), "content_type": "audio/mpeg"}, format="json"
    ).json()["upload_id"]
    assert api_client.delete(f"{BASE}/{r.id}/multipart/{upload_id}/abort").status_code == 204
    assert not UploadSession.objects.filter(pk=upload_id).exists()
    assert api_client.get(f"{BASE}/{uuid.uuid4()}/multipart/{upload_id}/parts").status_code == 404
