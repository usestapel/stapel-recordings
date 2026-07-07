"""Object-storage seam for recordings.

Recordings never talks to a specific S3 client directly. All object I/O
goes through a ``RecordingStorage`` implementation resolved from the
``STAPEL_RECORDINGS["STORAGE"]`` dotted path (single-strategy replace
seam). Two backends ship:

- :class:`DjangoStorageBackend` (default) — rides on Django's configured
  ``default_storage`` (local filesystem in dev, any django-storages
  backend in prod). Presigned URLs degrade to ``storage.url(key)``; a
  synthetic multipart shim keeps the API total for backends without a
  native multipart protocol.
- :class:`S3Backend` — boto3 presigned/multipart
  helpers. ``boto3`` is an optional dependency (``pip install
  stapel-recordings[s3]``); imported lazily with a clear error.

Contract (all keys are storage-relative strings):

    presigned_put_url(key, *, expires_seconds, content_type=None) -> str
    presigned_get_url(key, *, expires_seconds) -> str
    head_object(key) -> (exists: bool, size: int | None)
    download_to_file(key, dst_path) -> None
    upload_from_file(key, src_path, content_type=None) -> None
    put_bytes(key, data, content_type=...) -> None
    get_bytes(key) -> bytes
    delete_object(key) -> None
    create_multipart_upload(key, content_type=None) -> str   # upload_id
    presigned_upload_part_url(key, upload_id, part_number, *, expires_seconds) -> str
    complete_multipart_upload(key, upload_id, parts) -> None
    abort_multipart_upload(key, upload_id) -> None
"""
from __future__ import annotations

import io
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional


class RecordingStorage(ABC):
    """Interface every storage backend implements. Methods raise on hard
    failure so pipeline stages can classify transient vs fatal I/O."""

    # ── URLs ─────────────────────────────────────────────────────────
    @abstractmethod
    def presigned_put_url(self, key: str, *, expires_seconds: int = 900, content_type: Optional[str] = None) -> str: ...

    @abstractmethod
    def presigned_get_url(self, key: str, *, expires_seconds: int = 3600) -> str: ...

    # ── Objects ──────────────────────────────────────────────────────
    @abstractmethod
    def head_object(self, key: str) -> tuple[bool, Optional[int]]: ...

    @abstractmethod
    def download_to_file(self, key: str, dst_path: str) -> None: ...

    @abstractmethod
    def upload_from_file(self, key: str, src_path: str, content_type: Optional[str] = None) -> None: ...

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def delete_object(self, key: str) -> None: ...

    # ── Multipart ────────────────────────────────────────────────────
    @abstractmethod
    def create_multipart_upload(self, key: str, content_type: Optional[str] = None) -> str: ...

    @abstractmethod
    def presigned_upload_part_url(self, key: str, upload_id: str, part_number: int, *, expires_seconds: int = 3600) -> str: ...

    @abstractmethod
    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None: ...

    @abstractmethod
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


# ─────────────────────────────────────────────────────────────────────
# Default: Django default_storage backend
# ─────────────────────────────────────────────────────────────────────


class DjangoStorageBackend(RecordingStorage):
    """Backend over Django's ``default_storage``.

    Works out of the box with the filesystem backend and any
    django-storages provider. Presigned URLs fall back to ``storage.url``
    (public/served URL); native multipart is emulated with a single-part
    shim so the client-facing flow is uniform in dev.
    """

    def _storage(self):
        from django.core.files.storage import default_storage

        return default_storage

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        # No native presigned PUT — host uploads via the finalize endpoint
        # (server-side put_bytes/upload_from_file). Return the served URL
        # so clients still have a stable reference.
        try:
            return self._storage().url(key)
        except Exception:
            return key

    def presigned_get_url(self, key, *, expires_seconds=3600):
        try:
            return self._storage().url(key)
        except Exception:
            return key

    def head_object(self, key):
        storage = self._storage()
        if not storage.exists(key):
            return False, None
        try:
            return True, storage.size(key)
        except Exception:
            return True, None

    def download_to_file(self, key, dst_path):
        storage = self._storage()
        with storage.open(key, "rb") as src, open(dst_path, "wb") as dst:
            for chunk in src.chunks():
                dst.write(chunk)

    def upload_from_file(self, key, src_path, content_type=None):
        from django.core.files import File

        with open(src_path, "rb") as fh:
            self._save(key, File(fh))

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        from django.core.files.base import ContentFile

        self._save(key, ContentFile(data))

    def _save(self, key, content):
        storage = self._storage()
        if storage.exists(key):
            storage.delete(key)
        storage.save(key, content)

    def get_bytes(self, key):
        with self._storage().open(key, "rb") as fh:
            return fh.read()

    def delete_object(self, key):
        storage = self._storage()
        if storage.exists(key):
            storage.delete(key)

    # Synthetic multipart: one part is a plain PUT. ``upload_id`` == key.
    def create_multipart_upload(self, key, content_type=None):
        return key

    def presigned_upload_part_url(self, key, upload_id, part_number, *, expires_seconds=3600):
        return self.presigned_put_url(key, expires_seconds=expires_seconds)

    def complete_multipart_upload(self, key, upload_id, parts):
        return None

    def abort_multipart_upload(self, key, upload_id):
        # Best-effort cleanup of a never-finalized object.
        try:
            self.delete_object(key)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Optional: S3 / MinIO backend (boto3)
# ─────────────────────────────────────────────────────────────────────


class S3Backend(RecordingStorage):
    """S3-compatible (AWS S3 / MinIO) backend with native presigned URLs
    and multipart. Configured from ``STAPEL_RECORDINGS`` keys:

        S3_ENDPOINT_URL, S3_PUBLIC_URL, S3_ACCESS_KEY, S3_SECRET_KEY,
        S3_REGION, S3_BUCKET.
    """

    MULTIPART_PART_SIZE = 10 * 1024 * 1024

    def _conf(self, key, default=None):
        from .conf import recordings_settings

        try:
            return getattr(recordings_settings, key)
        except AttributeError:
            return default

    def _bucket(self) -> str:
        return self._conf("S3_BUCKET", "stapel-recordings")

    @lru_cache(maxsize=2)  # noqa: B019 — instance is a process-wide singleton
    def _client(self, public: bool):
        try:
            import boto3
            from botocore.client import Config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "S3Backend requires boto3 — install stapel-recordings[s3]"
            ) from exc
        endpoint = self._conf("S3_PUBLIC_URL") if public else self._conf("S3_ENDPOINT_URL")
        return boto3.client(
            "s3",
            endpoint_url=endpoint or self._conf("S3_ENDPOINT_URL"),
            aws_access_key_id=self._conf("S3_ACCESS_KEY"),
            aws_secret_access_key=self._conf("S3_SECRET_KEY"),
            region_name=self._conf("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        params = {"Bucket": self._bucket(), "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client(True).generate_presigned_url(
            "put_object", Params=params, ExpiresIn=expires_seconds
        )

    def presigned_get_url(self, key, *, expires_seconds=3600):
        return self._client(True).generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket(), "Key": key},
            ExpiresIn=expires_seconds,
        )

    def head_object(self, key):
        try:
            resp = self._client(False).head_object(Bucket=self._bucket(), Key=key)
        except Exception:
            return False, None
        return True, int(resp.get("ContentLength", 0))

    def download_to_file(self, key, dst_path):
        self._client(False).download_file(self._bucket(), key, dst_path)

    def upload_from_file(self, key, src_path, content_type=None):
        extra = {"ContentType": content_type} if content_type else None
        self._client(False).upload_file(src_path, self._bucket(), key, ExtraArgs=extra)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._client(False).upload_fileobj(
            io.BytesIO(data), self._bucket(), key, ExtraArgs={"ContentType": content_type}
        )

    def get_bytes(self, key):
        buf = io.BytesIO()
        self._client(False).download_fileobj(self._bucket(), key, buf)
        return buf.getvalue()

    def delete_object(self, key):
        self._client(False).delete_object(Bucket=self._bucket(), Key=key)

    def create_multipart_upload(self, key, content_type=None):
        params = {"Bucket": self._bucket(), "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client(False).create_multipart_upload(**params)["UploadId"]

    def presigned_upload_part_url(self, key, upload_id, part_number, *, expires_seconds=3600):
        return self._client(True).generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self._bucket(),
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires_seconds,
        )

    def complete_multipart_upload(self, key, upload_id, parts):
        self._client(False).complete_multipart_upload(
            Bucket=self._bucket(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])},
        )

    def abort_multipart_upload(self, key, upload_id):
        try:
            self._client(False).abort_multipart_upload(
                Bucket=self._bucket(), Key=key, UploadId=upload_id
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────

_backend_instance: Optional[RecordingStorage] = None
_backend_class = None


def get_storage() -> RecordingStorage:
    """Return the configured storage backend (cached per resolved class)."""
    global _backend_instance, _backend_class
    from .conf import recordings_settings

    cls = recordings_settings.STORAGE  # import_strings resolves the dotted path
    if _backend_instance is None or _backend_class is not cls:
        _backend_instance = cls()
        _backend_class = cls
    return _backend_instance


def reset_storage_cache() -> None:
    """Tests / settings-change hook."""
    global _backend_instance, _backend_class
    _backend_instance = None
    _backend_class = None


__all__ = [
    "RecordingStorage",
    "DjangoStorageBackend",
    "S3Backend",
    "get_storage",
    "reset_storage_cache",
]
