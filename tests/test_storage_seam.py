"""Storage seam: default Django backend round-trip + backend swap."""
import pytest
from django.test import override_settings

from stapel_recordings import storage
from stapel_recordings.storage import DjangoStorageBackend, get_storage

pytestmark = pytest.mark.django_db


def test_default_backend_is_django_storage():
    assert isinstance(get_storage(), DjangoStorageBackend)


def test_default_backend_round_trip(tmp_path):
    backend = get_storage()
    key = "recordings/ws/rec/audio.bin"
    backend.put_bytes(key, b"hello-bytes", content_type="application/octet-stream")

    exists, size = backend.head_object(key)
    assert exists and size == len(b"hello-bytes")
    assert backend.get_bytes(key) == b"hello-bytes"

    dst = tmp_path / "out.bin"
    backend.download_to_file(key, str(dst))
    assert dst.read_bytes() == b"hello-bytes"

    backend.delete_object(key)
    assert backend.head_object(key) == (False, None)


def test_default_backend_upload_from_file_and_urls(tmp_path):
    backend = get_storage()
    src = tmp_path / "in.bin"
    src.write_bytes(b"file-content")
    key = "recordings/ws/rec/from_file.bin"
    backend.upload_from_file(key, str(src), content_type="audio/wav")
    assert backend.get_bytes(key) == b"file-content"
    # Presigned URLs degrade to the served URL on the Django backend.
    assert isinstance(backend.presigned_get_url(key), str)
    assert isinstance(backend.presigned_put_url(key), str)
    backend.delete_object(key)


def test_synthetic_multipart_on_default_backend():
    backend = get_storage()
    key = "recordings/ws/rec/multi.bin"
    upload_id = backend.create_multipart_upload(key)
    assert upload_id == key  # synthetic id
    url = backend.presigned_upload_part_url(key, upload_id, 1)
    assert isinstance(url, str)
    backend.complete_multipart_upload(key, upload_id, [])


def test_backend_swap_via_setting():
    with override_settings(STAPEL_RECORDINGS={"STORAGE": "stapel_recordings.tests.fakes.FakeStorage"}):
        storage.reset_storage_cache()
        backend = get_storage()
        from stapel_recordings.tests.fakes import FakeStorage

        assert isinstance(backend, FakeStorage)
        backend.put_bytes("k", b"x")
        assert backend.get_bytes("k") == b"x"
        assert backend.presigned_get_url("k").startswith("memory://get/")
