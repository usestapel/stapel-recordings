"""Storage seam for verified multipart: checksum binding and list_parts."""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings

from stapel_recordings.storage import DjangoStorageBackend, RecordingStorage, S3Backend


class _StubS3:
    def __init__(self, pages=None):
        self.calls = []
        self.pages = list(pages or [])

    def create_multipart_upload(self, **kw):
        self.calls.append(("create", kw))
        return {"UploadId": "u-1"}

    def list_parts(self, **kw):
        self.calls.append(("list", kw))
        return self.pages.pop(0)

    def complete_multipart_upload(self, **kw):
        self.calls.append(("complete", kw))


def _backend(stub):
    b = S3Backend()
    b._client = lambda public: stub  # instance attribute shadows the cached method
    b._bucket = lambda: "bkt"
    return b


def test_create_passes_the_checksum_algorithm_only_when_asked():
    stub = _StubS3()
    b = _backend(stub)
    b.create_multipart_upload("k", content_type="audio/mpeg")
    b.create_multipart_upload("k", checksum_algorithm="SHA256")
    assert "ChecksumAlgorithm" not in stub.calls[0][1]
    assert stub.calls[1][1]["ChecksumAlgorithm"] == "SHA256"


def test_list_parts_paginates_and_converts_the_digest():
    digest = hashlib.sha256(b"x").digest()
    stub = _StubS3(
        pages=[
            {
                "Parts": [{"PartNumber": 2, "ETag": '"e2"', "Size": 4,
                           "ChecksumSHA256": base64.b64encode(digest).decode()}],
                "IsTruncated": True,
                "NextPartNumberMarker": 2,
            },
            {"Parts": [{"PartNumber": 1, "ETag": '"e1"', "Size": 4}], "IsTruncated": False},
        ]
    )
    parts = _backend(stub).list_parts("k", "u-1")
    assert parts == [
        {"part_number": 1, "etag": "e1", "size": 4, "sha256": None},
        {"part_number": 2, "etag": "e2", "size": 4, "sha256": digest.hex()},
    ]
    assert stub.calls[1][1]["PartNumberMarker"] == 2


def test_complete_passes_part_checksums_through():
    stub = _StubS3()
    parts = [{"PartNumber": 2, "ETag": "b", "ChecksumSHA256": "y"}, {"PartNumber": 1, "ETag": "a"}]
    _backend(stub).complete_multipart_upload("k", "u-1", parts)
    sent = stub.calls[0][1]["MultipartUpload"]["Parts"]
    assert [p["PartNumber"] for p in sent] == [1, 2]
    assert sent[1]["ChecksumSHA256"] == "y"


def test_part_checksum_headers():
    assert S3Backend().part_checksum_headers("abc=") == {
        "x-amz-checksum-sha256": "abc=",
        "x-amz-sdk-checksum-algorithm": "SHA256",
    }


def test_presigned_part_url_signs_the_checksum_headers():
    pytest.importorskip("boto3")
    with override_settings(
        STAPEL_RECORDINGS={
            "S3_ENDPOINT_URL": "http://store.invalid:9000",
            "S3_ACCESS_KEY": "k",
            "S3_SECRET_KEY": "s",
        }
    ):
        b = S3Backend()
        b64 = base64.b64encode(hashlib.sha256(b"part").digest()).decode()
        bound = urlparse(b.presigned_upload_part_url("key", "u", 1, expires_seconds=60, sha256_b64=b64))
        plain = urlparse(b.presigned_upload_part_url("key", "u", 1, expires_seconds=60))
    assert parse_qs(bound.query)["X-Amz-SignedHeaders"] == [
        "host;x-amz-checksum-sha256;x-amz-sdk-checksum-algorithm"
    ]
    assert parse_qs(plain.query)["X-Amz-SignedHeaders"] == ["host"]


def test_base_backend_cannot_list_and_binds_nothing():
    class Minimal(RecordingStorage):
        presigned_put_url = presigned_get_url = head_object = None
        download_to_file = upload_from_file = put_bytes = get_bytes = None
        delete_object = create_multipart_upload = presigned_upload_part_url = None
        complete_multipart_upload = abort_multipart_upload = None

    m = Minimal()
    assert m.part_checksum_headers("x") == {}
    with pytest.raises(NotImplementedError):
        m.list_parts("k", "u")


def test_django_backend_lists_its_single_synthetic_part(tmp_path):
    with override_settings(
        STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                              "OPTIONS": {"location": str(tmp_path)}}}
    ):
        b = DjangoStorageBackend()
        key = "recordings/ws/rec/audio.mp3"
        assert b.list_parts(key, key) == []
        b.put_bytes(key, b"abcd")
        assert b.list_parts(key, key) == [{"part_number": 1, "etag": "", "size": 4, "sha256": None}]
