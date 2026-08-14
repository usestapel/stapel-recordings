"""Upload invariants (audit REC-02).

Everything a client says about an upload is a request; the stored object is
the fact. These tests pin that split: declared sizes are bounded before any
storage state exists, part counts are capped, and finalize refuses an object
that is missing, empty, oversized or not media — without enqueueing the
pipeline.
"""
import pytest
from django.test import override_settings

from stapel_recordings import events, media_types, services
from stapel_recordings.models import Recording, RecordingStatus, UploadSession
from stapel_recordings.storage import get_storage

pytestmark = pytest.mark.django_db


def _outbox_uploaded_count():
    from stapel_core.django.outbox.models import OutboxEvent

    return OutboxEvent.objects.filter(topic=events.ACTION_UPLOADED).count()


# ── declared size is bounded before storage state exists ─────────────────


def test_create_session_rejects_declared_size_over_max(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(services.UploadTooLarge):
        services.create_upload_session(
            recording=r, filename="take.mp3", declared_size_bytes=3 * 1024 * 1024 * 1024
        )
    assert not UploadSession.objects.filter(recording=r).exists()


def test_declared_size_becomes_the_enforced_ceiling(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(
        recording=r, filename="take.mp3", declared_size_bytes=8
    )
    assert session.max_size_bytes == 8
    get_storage().put_bytes(session.storage_key, b"0123456789")  # 10 > 8
    with pytest.raises(services.UploadTooLarge):
        services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status != RecordingStatus.QUEUED
    assert _outbox_uploaded_count() == 0


def test_multipart_requires_positive_size(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    for bad in (0, -1, None):
        with pytest.raises(services.UploadTooLarge):
            services.start_multipart_upload(
                recording=r, file_size_bytes=bad, filename="take.mp3"
            )
    assert not UploadSession.objects.filter(recording=r).exists()


def test_multipart_part_count_is_capped(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "MULTIPART_PART_SIZE": 1024,
            "MAX_MULTIPART_PARTS": 4,
            "MAX_UPLOAD_BYTES": 10 * 1024 * 1024,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        with pytest.raises(services.InvalidMultipartParts):
            services.start_multipart_upload(
                recording=r, file_size_bytes=1024 * 1024, filename="take.mp3"
            )
    assert not UploadSession.objects.filter(recording=r).exists()


# ── one live session per recording ───────────────────────────────────────


def test_new_session_supersedes_the_open_one(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    first, _parts, _sz = services.start_multipart_upload(
        recording=r, file_size_bytes=1024, filename="take.mp3"
    )
    second = services.create_upload_session(recording=r, filename="take.mp3")
    open_sessions = list(UploadSession.objects.filter(recording=r))
    assert [s.pk for s in open_sessions] == [second.pk]
    assert not UploadSession.objects.filter(pk=first.pk).exists()


# ── finalize verifies the stored object ──────────────────────────────────


def test_finalize_rejects_missing_object(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    with pytest.raises(services.UploadNotStored):
        services.finalize_upload(session=session, file_size_bytes=1234)
    r.refresh_from_db()
    assert r.file_storage_key is None
    assert r.file_size_bytes is None
    assert r.status != RecordingStatus.QUEUED
    assert _outbox_uploaded_count() == 0
    assert not UploadSession.objects.filter(pk=session.pk).exists()


def test_finalize_rejects_zero_byte_object(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"")
    with pytest.raises(services.UploadNotStored):
        services.finalize_upload(session=session, file_size_bytes=999)
    r.refresh_from_db()
    assert r.status != RecordingStatus.QUEUED
    assert _outbox_uploaded_count() == 0


def test_finalize_records_measured_size_not_declared(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"1234567890")
    services.finalize_upload(session=session, file_size_bytes=3)
    r.refresh_from_db()
    assert r.file_size_bytes == 10


def test_finalize_rejects_declared_size_over_ceiling(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(
        recording=r, filename="take.mp3", declared_size_bytes=16
    )
    get_storage().put_bytes(session.storage_key, b"1234")
    with pytest.raises(services.UploadTooLarge):
        services.finalize_upload(session=session, file_size_bytes=64)


def test_finalize_rejects_multipart_with_duplicate_parts(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session, _parts, _sz = services.start_multipart_upload(
        recording=r, file_size_bytes=1024, filename="take.mp3"
    )
    with pytest.raises(services.InvalidMultipartParts):
        services.finalize_upload(
            session=session,
            parts=[{"part_number": 1, "etag": "a"}, {"part_number": 1, "etag": "b"}],
        )
    # Rejected BEFORE the multipart was completed — no object materialized.
    exists, _size = get_storage().head_object(session.storage_key)
    assert exists is False


# ── content policy ───────────────────────────────────────────────────────


def test_finalize_rejects_executable_content(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"MZ\x90\x00" + b"\x00" * 64)
    with pytest.raises(media_types.UnsupportedUploadContent):
        services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status != RecordingStatus.QUEUED
    assert _outbox_uploaded_count() == 0
    exists, _size = get_storage().head_object(session.storage_key)
    assert exists is False  # rejected object is cleaned up


def test_finalize_rejects_html_polyglot(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"\n  <html><script>alert(1)</script>")
    with pytest.raises(media_types.UnsupportedUploadContent):
        services.finalize_upload(session=session)


def test_default_policy_accepts_unrecognized_bytes(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"raw pcm-ish bytes")
    services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED


def test_strict_policy_requires_known_media(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"raw pcm-ish bytes")
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "UPLOAD_CONTENT_POLICY": media_types.POLICY_REQUIRE_KNOWN_MEDIA,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        with pytest.raises(media_types.UnsupportedUploadContent):
            services.finalize_upload(session=session)


def test_strict_policy_accepts_a_real_container(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"ID3\x03\x00\x00\x00\x00\x00\x00audio")
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "UPLOAD_CONTENT_POLICY": media_types.POLICY_REQUIRE_KNOWN_MEDIA,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED


# ── a gate that cannot run refuses ───────────────────────────────────────
#
# ``RecordingStorage.read_prefix`` raises NotImplementedError by default, so
# a host's own backend fails the content gate open by INHERITING — writing no
# code switches the policy off. Finalize must refuse instead.

_NO_RANGED_READ = {
    "STORAGE": "stapel_recordings.tests.fakes.NoRangedReadStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def _with_no_ranged_reads(**extra):
    return override_settings(STAPEL_RECORDINGS={**_NO_RANGED_READ, **extra})


def test_finalize_refuses_when_the_content_gate_cannot_run(use_fakes, make_recording):
    from stapel_recordings import storage

    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    # A Windows executable parked under an audio.mp3 key — exactly what the
    # policy exists to stop, and what a backend without ranged reads used to
    # wave through with a log line.
    get_storage().put_bytes(session.storage_key, b"MZ\x90\x00" + b"\x00" * 64)
    with _with_no_ranged_reads():
        storage.reset_storage_cache()
        with pytest.raises(services.UploadContentUncheckable):
            services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status != RecordingStatus.QUEUED
    assert r.file_storage_key in (None, "")
    assert _outbox_uploaded_count() == 0
    storage.reset_storage_cache()
    exists, _size = get_storage().head_object(session.storage_key)
    assert exists is False  # unverifiable object is cleaned up like a rejected one


def test_content_policy_off_accepts_a_backend_without_ranged_reads(
    use_fakes, make_recording
):
    """The opt-out is the policy switch that already exists, and it is a
    stated decision rather than a property of the backend."""
    from stapel_recordings import storage

    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"raw pcm-ish bytes")
    with _with_no_ranged_reads(UPLOAD_CONTENT_POLICY=media_types.POLICY_OFF):
        storage.reset_storage_cache()
        services.finalize_upload(session=session)
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED


def test_classifier_prefers_dangerous_over_media():
    # A zip whose central directory is appended to an mp3 header is still a
    # zip to anything that reads it from the end.
    verdict, _label = media_types.classify_prefix(b"PK\x03\x04ID3")
    assert verdict == media_types.DANGEROUS


# ── API surface ──────────────────────────────────────────────────────────


def test_finalize_api_answers_415_for_rejected_content(use_fakes, api_client, user, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING, owner=user)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"%PDF-1.4 not audio")
    api_client.force_authenticate(user=user)
    resp = api_client.post(f"/recordings/api/v1/recordings/{r.id}/finalize", {}, format="json")
    assert resp.status_code == 415, resp.content


def test_finalize_api_answers_503_when_the_content_gate_cannot_run(
    use_fakes, api_client, user, make_recording
):
    """A deployment fault reads as one: 503, not a 415 that blames the file."""
    from stapel_recordings import storage

    r = make_recording(status=RecordingStatus.UPLOADING, owner=user)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    get_storage().put_bytes(session.storage_key, b"MZ\x90\x00" + b"\x00" * 64)
    api_client.force_authenticate(user=user)
    with _with_no_ranged_reads():
        storage.reset_storage_cache()
        resp = api_client.post(
            f"/recordings/api/v1/recordings/{r.id}/finalize", {}, format="json"
        )
    assert resp.status_code == 503, resp.content
    assert resp.json()["localizable_error"] == "error.503.recording_upload_unverifiable"
    assert Recording.objects.get(pk=r.id).status != RecordingStatus.QUEUED


def test_finalize_api_answers_409_when_nothing_was_stored(use_fakes, api_client, user, make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING, owner=user)
    services.create_upload_session(recording=r, filename="take.mp3")
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        f"/recordings/api/v1/recordings/{r.id}/finalize", {"file_size_bytes": 10}, format="json"
    )
    assert resp.status_code == 409, resp.content
    assert Recording.objects.get(pk=r.id).status != RecordingStatus.QUEUED
