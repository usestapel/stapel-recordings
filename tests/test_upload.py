"""Upload sessions: single-PUT, multipart, finalize idempotency."""
import pytest

from stapel_recordings import events, services
from stapel_recordings.models import RecordingStatus, UploadSession

pytestmark = pytest.mark.django_db


def test_create_upload_session(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r)
    assert session.presigned_url.startswith("memory://put/")
    assert session.storage_key.endswith(f"{r.id}/audio")
    r.refresh_from_db()
    assert r.status == RecordingStatus.UPLOADING


# ── G5: filename / extension in the upload key ────────────────────────────


def test_create_upload_session_without_filename_keeps_legacy_key(use_fakes, make_recording):
    """Backward compatibility: no filename → the extension-less …/audio key."""
    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r)
    assert session.storage_key.endswith(f"{r.id}/audio")


def test_create_upload_session_appends_validated_extension(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r, filename="Team Sync.MP3")
    assert session.storage_key.endswith(f"{r.id}/audio.mp3")  # lower-cased


def test_create_upload_session_rejects_disallowed_extension(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(services.UnsupportedUploadExtension):
        services.create_upload_session(recording=r, filename="malware.exe")


def test_create_upload_session_rejects_extensionless_filename(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(services.UnsupportedUploadExtension):
        services.create_upload_session(recording=r, filename="noext")


def test_extension_allowlist_is_settings_extensible(use_fakes, make_recording):
    from django.test import override_settings

    r = make_recording(status=RecordingStatus.CREATED)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "UPLOAD_EXTENSION_ALLOWLIST": ["mp3", "xyz"],
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        session = services.create_upload_session(recording=r, filename="clip.xyz")
    assert session.storage_key.endswith(f"{r.id}/audio.xyz")


def test_multipart_upload_honours_filename_extension(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _parts, _sz = services.start_multipart_upload(
        recording=r, file_size_bytes=1024, filename="talk.wav"
    )
    assert session.storage_key.endswith(f"{r.id}/audio.wav")


def test_create_recording_api_rejects_bad_filename(use_fakes, api_client, user):
    import uuid

    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/recordings",
        {"workspace_id": str(uuid.uuid4()), "title": "x", "filename": "bad.exe"},
        format="json",
    )
    assert resp.status_code == 400


def test_create_recording_api_accepts_good_filename(use_fakes, api_client, user):
    import uuid

    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/recordings",
        {"workspace_id": str(uuid.uuid4()), "title": "x", "filename": "meeting.m4a"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["upload"]["storage_key"].endswith("audio.m4a")


def test_start_multipart_upload_splits_parts(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, parts, part_size = services.start_multipart_upload(
        recording=r, file_size_bytes=25 * 1024 * 1024
    )
    assert session.is_multipart is True
    assert session.multipart_upload_id
    assert part_size == 10 * 1024 * 1024
    assert len(parts) == 3
    assert parts[0]["part_number"] == 1


def test_finalize_is_idempotent_and_emits_once(use_fakes, make_recording):
    from stapel_recordings.storage import get_storage

    r = make_recording(status=RecordingStatus.UPLOADING)
    session = services.create_upload_session(recording=r)
    get_storage().put_bytes(session.storage_key, b"data")

    services.finalize_upload(session=session, file_size_bytes=4)
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED
    assert r.file_storage_key == session.storage_key

    # Second finalize (concurrent-worker replay) is a no-op — no 2nd event.
    services.finalize_upload(session=session, file_size_bytes=4)

    from stapel_core.django.outbox.models import OutboxEvent

    assert OutboxEvent.objects.filter(topic=events.ACTION_UPLOADED).count() == 1


def test_abort_multipart_removes_session(use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session, _parts, _sz = services.start_multipart_upload(recording=r, file_size_bytes=1024)
    services.abort_multipart_upload_session(session=session)
    assert not UploadSession.objects.filter(pk=session.pk).exists()
