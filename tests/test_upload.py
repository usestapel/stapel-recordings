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
