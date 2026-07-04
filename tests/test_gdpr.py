"""GDPR: provider export/delete + the user.deleted consumer."""
import pytest

from stapel_recordings.gdpr import RecordingsGDPRProvider
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def _seed(make_recording):
    from stapel_recordings.storage import get_storage

    r = make_recording(
        status=RecordingStatus.COMPLETED,
        file_storage_key="recordings/ws/r/audio",
        normalized_storage_key="recordings/ws/r/audio.normalized.wav",
        transcript_storage_key="recordings/ws/r/transcript.json",
        summary="hi",
    )
    storage = get_storage()
    for key in (r.file_storage_key, r.normalized_storage_key, r.transcript_storage_key):
        storage.put_bytes(key, b"x")
    return r


def test_provider_export(use_fakes, make_recording, user):
    _seed(make_recording)
    data = RecordingsGDPRProvider().export(user.id)
    assert len(data["recordings"]) == 1
    assert data["recordings"][0]["summary"] == "hi"


def test_provider_delete_removes_rows_and_objects(use_fakes, make_recording, user):
    from stapel_recordings.storage import get_storage

    r = _seed(make_recording)
    storage = get_storage()
    assert storage.head_object(r.transcript_storage_key)[0] is True

    RecordingsGDPRProvider().delete(user.id)

    assert not Recording.objects.filter(owner_id=user.id).exists()
    assert storage.head_object(r.transcript_storage_key)[0] is False


def test_user_deleted_consumer_erases(use_fakes, make_recording, user, drain):
    from stapel_core.comm import emit

    _seed(make_recording)
    emit("user.deleted", {"user_id": str(user.id)})
    drain()

    assert not Recording.objects.filter(owner_id=user.id).exists()


def test_provider_registered_in_registry():
    from stapel_core.gdpr import gdpr_registry

    assert "recordings" in gdpr_registry.sections
