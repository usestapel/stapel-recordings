"""Summarization via the stubbed llm.summarize comm Function (merge stage)."""
import pytest
from django.test import override_settings

from stapel_recordings import events
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def test_merge_calls_summarize_and_stores(ready_recording, stub_transcribe, stub_summarize, drain):
    events.emit_stage(ready_recording.id, 0)
    drain()
    assert len(stub_summarize.calls) == 1
    assert "text" in stub_summarize.calls[0]  # rendered markdown
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.summary == "A short summary."


def test_summarize_disabled(ready_recording, stub_transcribe, stub_summarize, drain):
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "SUMMARIZE_ENABLED": False}):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    assert stub_summarize.calls == []
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.summary is None
    assert r.status == RecordingStatus.COMPLETED


def test_summarize_failure_does_not_fail_recording(ready_recording, stub_transcribe, stub_summarize, drain):
    stub_summarize.result = {"status": "failure", "reason": "llm down"}
    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED  # transcript is the primary artifact
    assert r.summary is None
    assert r.transcript_storage_key
