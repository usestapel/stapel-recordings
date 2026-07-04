"""Shared fixtures for the stapel-recordings test suite."""
import uuid

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _reset_fakes():
    from stapel_recordings.tests import fakes

    fakes.reset_fake_storage()
    fakes.STAGE_TRACE.clear()
    yield
    fakes.reset_fake_storage()
    fakes.STAGE_TRACE.clear()


@pytest.fixture
def fake_storage_settings():
    """Swap in the in-memory storage backend + passthrough normalizer so
    the pipeline runs without ffmpeg or a real object store."""
    return override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
        }
    )


@pytest.fixture
def use_fakes(fake_storage_settings):
    with fake_storage_settings:
        from stapel_recordings import storage

        storage.reset_storage_cache()
        yield


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create(username=f"u-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def make_recording(db, user):
    from stapel_recordings.models import Recording, RecordingStatus

    def _make(**kwargs):
        defaults = dict(
            workspace_id=uuid.uuid4(),
            owner=user,
            title="Test recording",
            status=RecordingStatus.QUEUED,
            file_storage_key=None,
            diarization_enabled=True,
        )
        defaults.update(kwargs)
        return Recording.objects.create(**defaults)

    return _make


@pytest.fixture
def ready_recording(use_fakes, make_recording):
    """A queued recording whose raw audio object is present in FakeStorage —
    ready to enter the pipeline at stage 0 (convert)."""
    from stapel_recordings.storage import get_storage

    r = make_recording(status="queued")
    key = f"recordings/{r.workspace_id}/{r.id}/audio"
    r.file_storage_key = key
    r.save(update_fields=["file_storage_key"])
    get_storage().put_bytes(key, b"raw-audio-bytes", content_type="audio/mpeg")
    return r


@pytest.fixture
def drain():
    """Deliver all pending outbox events, walking the whole pipeline. Mirrors
    the production relay (dispatch_outbox)."""
    from stapel_core.django.outbox.relay import dispatch_pending

    def _drain(max_passes: int = 50) -> int:
        total = 0
        for _ in range(max_passes):
            delivered, _failed = dispatch_pending(limit=200)
            total += delivered
            if delivered == 0:
                break
        return total

    return _drain


@pytest.fixture
def stub_transcribe():
    """Register a stub ``llm.transcribe`` comm Function; returns a recorder
    the test can inspect (calls + configurable result)."""
    from stapel_core.comm import register_function

    class Recorder:
        def __init__(self):
            self.calls = []
            self.result = {
                "status": "ok",
                "provider_used": "stub-asr",
                "fallback_used": False,
                "transcript": {
                    "provider": "stub-asr",
                    "language": "en",
                    "duration_seconds": 12.0,
                    "words": [],
                    "utterances": [
                        {"text": "hello world", "start": 0.0, "end": 2.0, "speaker": "speaker_0", "confidence": 0.9, "word_indexes": []},
                        {"text": "goodbye", "start": 2.0, "end": 4.0, "speaker": "speaker_1", "confidence": 0.8, "word_indexes": []},
                    ],
                    "speakers_detected": ["speaker_0", "speaker_1"],
                    "raw": {},
                },
            }

        def __call__(self, payload):
            self.calls.append(payload)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    recorder = Recorder()
    register_function("llm.transcribe", recorder)
    return recorder


@pytest.fixture
def stub_summarize():
    from stapel_core.comm import register_function

    class Recorder:
        def __init__(self):
            self.calls = []
            self.result = {"status": "ok", "summary": "A short summary.", "usage": {}}

        def __call__(self, payload):
            self.calls.append(payload)
            return self.result

    recorder = Recorder()
    register_function("llm.summarize", recorder)
    return recorder
