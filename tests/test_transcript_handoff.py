"""The transcribe stage asks for a key and reads the transcript back.

The defect, measured on the owner's stand (2026-09-09): a 2h28m meeting's
transcript is 8 647 617 bytes and the NATS broker between this module and
stapel-agent carries 8 388 608. The pipeline dropped the recording at the
transcribe stage — twice for the same meeting.

The audio already travelled to the agent as a presigned GET. These tests
cover the mirror image: a presigned PUT goes out with the payload, and the
transcript comes back as a key this module reads from its own storage.
"""
import json

import pytest
from django.test import override_settings

from stapel_recordings import stages
from stapel_recordings.stages import StageRetryable, TranscribeStage


TRANSCRIPT = {
    "provider": "elevenlabs",
    "language": "ru",
    "duration_seconds": 8898.005333,
    "words": [{"text": "hello", "start": 0.0, "end": 0.4, "speaker": "speaker_0"}],
    "utterances": [{"text": "hello", "start": 0.0, "end": 0.4,
                    "speaker": "speaker_0", "word_indexes": [0]}],
    "speakers_detected": ["speaker_0"],
}
BODY = json.dumps(TRANSCRIPT).encode()


class FakeStorage:
    signs_put_urls = True
    signs_get_urls = True

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_urls = []

    def presigned_get_url(self, key, *, expires_seconds=3600):
        return f"https://minio/get/{key}?sig=x"

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        self.put_urls.append((key, expires_seconds, content_type))
        return f"https://minio/put/{key}?sig=x"

    def get_bytes(self, key):
        return self.objects[key]


class UnsignedStorage(FakeStorage):
    signs_put_urls = False


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(stages, "get_storage", lambda: fake)
    return fake


pytestmark = pytest.mark.django_db


class TestThePayloadAsksForAKey:
    def test_it_carries_a_presigned_put_and_the_key(self, storage, make_recording):
        recording = make_recording(normalized_storage_key='recordings/x/y/audio.opus')
        payload = TranscribeStage().build_payload(recording)

        key = f"recordings/{recording.workspace_id}/{recording.id}/transcript.raw.json"
        assert payload["transcript_key"] == key
        assert payload["transcript_put_url"] == f"https://minio/put/{key}?sig=x"
        assert storage.put_urls[0][2] == "application/json"

    def test_the_audio_url_is_still_there(self, storage, make_recording):
        recording = make_recording(normalized_storage_key='recordings/x/y/audio.opus')
        payload = TranscribeStage().build_payload(recording)
        assert payload["audio_url"].startswith("https://minio/get/")

    def test_a_backend_that_cannot_sign_a_put_is_not_asked_to(
        self, monkeypatch, make_recording
    ):
        """Not a size test — a deployment fact, read once from the backend."""
        recording = make_recording(normalized_storage_key='recordings/x/y/audio.opus')
        monkeypatch.setattr(stages, "get_storage", lambda: UnsignedStorage())

        payload = TranscribeStage().build_payload(recording)

        assert "transcript_put_url" not in payload
        assert "transcript_key" not in payload

    @override_settings(STAPEL_RECORDINGS={"TRANSCRIPT_HANDOFF": False})
    def test_a_host_can_turn_it_off(self, storage, make_recording):
        recording = make_recording(normalized_storage_key='recordings/x/y/audio.opus')
        assert "transcript_put_url" not in TranscribeStage().build_payload(recording)

    @override_settings(STAPEL_RECORDINGS={"TRANSCRIPT_HANDOFF": True})
    def test_a_host_can_force_it_on(self, monkeypatch, make_recording):
        recording = make_recording(normalized_storage_key='recordings/x/y/audio.opus')
        monkeypatch.setattr(stages, "get_storage", lambda: UnsignedStorage())
        assert "transcript_put_url" in TranscribeStage().build_payload(recording)


class TestReadingTheAnswer:
    def test_a_reference_is_fetched_from_storage(self, monkeypatch):
        key = "recordings/ws/rec/transcript.raw.json"
        fake = FakeStorage({key: BODY})
        monkeypatch.setattr(stages, "get_storage", lambda: fake)

        out = stages.transcript_from_result({
            "status": "ok",
            "transcript_ref": {"key": key, "bytes": len(BODY), "sha256": "x"},
            "provider_used": "elevenlabs",
        })

        assert out == TRANSCRIPT

    def test_an_inline_transcript_still_works(self, storage):
        out = stages.transcript_from_result({
            "status": "ok", "transcript": TRANSCRIPT,
        })
        assert out == TRANSCRIPT

    def test_a_short_read_is_refused_rather_than_persisted(self, monkeypatch):
        """The failure this shape could hide: a recording missing its tail."""
        key = "k"
        monkeypatch.setattr(stages, "get_storage",
                            lambda: FakeStorage({key: BODY[:20]}))

        with pytest.raises(StageRetryable) as exc:
            stages.transcript_from_result({
                "transcript_ref": {"key": key, "bytes": len(BODY)},
            })
        assert exc.value.reason == "transcript_truncated"

    def test_an_unreadable_object_is_retryable(self, monkeypatch):
        monkeypatch.setattr(stages, "get_storage",
                            lambda: FakeStorage({"k": b"not json"}))

        with pytest.raises(StageRetryable) as exc:
            stages.transcript_from_result({"transcript_ref": {"key": "k"}})
        assert exc.value.reason == "transcript_unreadable"

    def test_a_missing_object_is_retryable(self, monkeypatch):
        monkeypatch.setattr(stages, "get_storage", lambda: FakeStorage({}))

        with pytest.raises(StageRetryable) as exc:
            stages.transcript_from_result({"transcript_ref": {"key": "gone"}})
        assert exc.value.reason == "transcript_fetch_failed"

    def test_a_reference_with_no_key_is_retryable(self, storage):
        with pytest.raises(StageRetryable) as exc:
            stages.transcript_from_result({"transcript_ref": {"bytes": 5}})
        assert exc.value.reason == "transcript_ref_incomplete"


def test_resume_persists_segments_from_a_reference(monkeypatch, make_recording):
    recording = make_recording()
    key = "recordings/ws/rec/transcript.raw.json"
    monkeypatch.setattr(stages, "get_storage", lambda: FakeStorage({key: BODY}))

    TranscribeStage().resume(recording, {}, {
        "status": "ok",
        "transcript_ref": {"key": key, "bytes": len(BODY)},
        "provider_used": "elevenlabs",
        "fallback_used": False,
    })

    recording.refresh_from_db()
    assert recording.segments.count() == 1
    assert recording.provider_used == "elevenlabs"
    assert recording.segments.first().text == "hello"


def test_the_whole_pipeline_runs_through_a_reference(
    ready_recording, stub_summarize, drain
):
    """End to end on the module's own fakes: the stub agent writes the
    transcript where the payload told it to and answers with the key, and
    the recording completes with segments — nothing but counts on the wire.
    """
    from stapel_core.comm import register_function, tasks as task_primitive

    from stapel_recordings import events
    from stapel_recordings.models import Recording, RecordingStatus
    from stapel_recordings.storage import get_storage

    seen = {}

    def agent(payload):
        seen["payload"] = payload
        key = payload["transcript_key"]
        body = json.dumps(TRANSCRIPT).encode()
        get_storage().put_bytes(key, body, content_type="application/json")
        return {
            "status": "ok",
            "transcript_ref": {"key": key, "bytes": len(body), "sha256": "x"},
            "transcript_meta": {"words": 1, "utterances": 1},
            "provider_used": "elevenlabs",
            "fallback_used": False,
        }

    register_function("llm.transcribe", agent)
    task_primitive._handlers.pop("llm.transcribe", None)
    task_primitive.register_task("llm.transcribe", agent)

    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert r.segments.count() == 1
    assert r.transcript_storage_key  # merge still writes the unified schema
    assert seen["payload"]["transcript_put_url"].startswith("memory://put/")
    # The two artifacts are different files with different schemas, and each
    # has exactly one writer — see the release notes.
    assert seen["payload"]["transcript_key"] != r.transcript_storage_key
