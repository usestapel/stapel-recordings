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


def test_delete_failure_keeps_rows_and_raises_then_retry_succeeds(
    use_fakes, make_recording, user, monkeypatch
):
    """A failed delete_object must not swallow the error and drop the row —
    the row (with its keys) is kept and the exception propagates so the
    at-least-once retry paths (redelivery / GDPR orchestrator) can re-drive
    the erasure. The retry is idempotent."""
    from stapel_recordings.gdpr import GDPRStorageDeleteError
    from stapel_recordings.storage import get_storage
    from stapel_recordings.tests.fakes import FakeStorage

    r = _seed(make_recording)
    storage = get_storage()
    original = FakeStorage.delete_object

    def failing(self, key):
        if key == r.transcript_storage_key:
            raise RuntimeError("object store down")
        original(self, key)

    monkeypatch.setattr(FakeStorage, "delete_object", failing)
    with pytest.raises(GDPRStorageDeleteError):
        RecordingsGDPRProvider().delete(user.id)

    # Row kept (so the key is not lost), object still tracked in storage.
    assert Recording.objects.filter(owner_id=user.id).exists()
    assert storage.head_object(r.transcript_storage_key)[0] is True

    # Storage healed -> the retry erases object + row.
    monkeypatch.setattr(FakeStorage, "delete_object", original)
    RecordingsGDPRProvider().delete(user.id)
    assert not Recording.objects.filter(owner_id=user.id).exists()
    assert storage.head_object(r.transcript_storage_key)[0] is False


def test_delete_partial_failure_still_erases_clean_rows(use_fakes, make_recording, user, monkeypatch):
    """Rows whose objects all deleted are erased even when another row's
    object fails — only the failed row is kept for retry."""
    from stapel_recordings.gdpr import GDPRStorageDeleteError
    from stapel_recordings.storage import get_storage
    from stapel_recordings.tests.fakes import FakeStorage

    bad = _seed(make_recording)
    good = make_recording(
        status=RecordingStatus.COMPLETED,
        file_storage_key="recordings/ws/r2/audio",
        transcript_storage_key="recordings/ws/r2/transcript.json",
    )
    storage = get_storage()
    for key in (good.file_storage_key, good.transcript_storage_key):
        storage.put_bytes(key, b"x")

    original = FakeStorage.delete_object

    def failing(self, key):
        if key == bad.file_storage_key:
            raise RuntimeError("object store down")
        original(self, key)

    monkeypatch.setattr(FakeStorage, "delete_object", failing)
    with pytest.raises(GDPRStorageDeleteError):
        RecordingsGDPRProvider().delete(user.id)

    remaining = list(Recording.objects.filter(owner_id=user.id))
    assert [r.pk for r in remaining] == [bad.pk]
    assert storage.head_object(good.transcript_storage_key)[0] is False
