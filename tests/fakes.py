"""In-memory test doubles for the storage seam and pipeline stages."""
from __future__ import annotations

from stapel_recordings.stages import Stage
from stapel_recordings.storage import RecordingStorage

# Shared object store so every FakeStorage() instance (get_storage() builds a
# fresh one per resolved class) sees the same bytes.
_STORE: dict[str, bytes] = {}
_MULTIPART: dict[str, list] = {}


def reset_fake_storage() -> None:
    _STORE.clear()
    _MULTIPART.clear()


class FakeStorage(RecordingStorage):
    """Deterministic in-memory backend — no ffmpeg, no network."""

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        return f"memory://put/{key}"

    def presigned_get_url(self, key, *, expires_seconds=3600):
        return f"memory://get/{key}"

    def head_object(self, key):
        if key in _STORE:
            return True, len(_STORE[key])
        return False, None

    def download_to_file(self, key, dst_path):
        with open(dst_path, "wb") as fh:
            fh.write(_STORE[key])

    def upload_from_file(self, key, src_path, content_type=None):
        with open(src_path, "rb") as fh:
            _STORE[key] = fh.read()

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        _STORE[key] = bytes(data)

    def get_bytes(self, key):
        return _STORE[key]

    def delete_object(self, key):
        _STORE.pop(key, None)

    def create_multipart_upload(self, key, content_type=None):
        upload_id = f"upload-{key}"
        _MULTIPART[upload_id] = []
        return upload_id

    def presigned_upload_part_url(self, key, upload_id, part_number, *, expires_seconds=3600):
        return f"memory://part/{upload_id}/{part_number}"

    def complete_multipart_upload(self, key, upload_id, parts):
        _STORE.setdefault(key, b"assembled")
        _MULTIPART.pop(upload_id, None)

    def abort_multipart_upload(self, key, upload_id):
        _MULTIPART.pop(upload_id, None)
        _STORE.pop(key, None)


# ─── Custom stages used by pipeline extension-point tests ──────────────

STAGE_TRACE: list[str] = []


class RecordStage(Stage):
    """A trivial stage that appends its name to STAGE_TRACE."""

    name = "record"
    status = ""

    def run(self, recording, ctx):
        STAGE_TRACE.append("record")
        ctx = dict(ctx)
        ctx["record_ran"] = True
        return ctx


def redact_pii_stage(recording, ctx):
    """A callable stage — inserted before merge in a custom pipeline."""
    STAGE_TRACE.append("redact_pii")
    recording.metadata = {**(recording.metadata or {}), "redacted": True}
    recording.save(update_fields=["metadata", "updated_at"])
    return ctx


class SpyMergeStage(Stage):
    """Replacement for the built-in merge stage (swap-a-built-in test)."""

    name = "merge"
    status = ""

    def run(self, recording, ctx):
        STAGE_TRACE.append("spy_merge")
        recording.transcript_storage_key = "spy://transcript"
        recording.save(update_fields=["transcript_storage_key", "updated_at"])
        return ctx


def only_record_resolver(recording):
    """PIPELINE_RESOLVER seam double — a runtime/DB-sourced pipeline that
    ignores the PIPELINE setting and returns a single 'record' stage."""
    return ["record"]
