"""The error marker must clear once the pipeline recovers.

Before 2026-08-08, ``workflow_state["last_error"]`` was set on stage failure and
never cleared: not on a successful retry, not on requeue, not even once the
recording reached ``completed``. A fully transcribed, summarized, embedded
recording kept carrying the reason for a long-past failure — and that field
drove decisions.

The reason isn't discarded, it moves to ``recovered_error``: the diagnosis
stays available for ops but stops posing as current state.
"""
import pytest
from django.test import override_settings

from stapel_recordings import events, stages
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.stages import Stage, StageRetryable

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


class FlakyStage(Stage):
    """Fails on the first run, passes on the second — like a flaky network or quota."""

    name = "flaky"
    status = RecordingStatus.MERGING

    failures = 0

    def run(self, recording, ctx):
        type(self).failures += 1
        if type(self).failures == 1:
            raise StageRetryable(reason="embedder rejected the batch", detail="413")
        return ctx


@pytest.fixture
def flaky():
    FlakyStage.failures = 0
    stages.register_stage("flaky", FlakyStage())
    yield FlakyStage


def _run_pipeline(recording_id, drain, index=0):
    """One pass of the ``convert -> flaky`` pipeline.

    *index* mirrors what reconcile puts in the event when re-driving a
    parked recording: it targets the CURRENT stage, not stage zero (a
    duplicate re-emit of an already-completed stage is dropped by the
    dedup guard).
    """
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["convert", "flaky"]}):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(recording_id, index)
        drain()


def test_marker_set_on_failure(ready_recording, flaky, drain):
    _run_pipeline(ready_recording.id, drain)

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.workflow_state["last_error"]["reason"] == "embedder rejected the batch"
    assert "recovered_error" not in r.workflow_state


def test_marker_cleared_when_stage_succeeds(ready_recording, flaky, drain):
    _run_pipeline(ready_recording.id, drain)  # failure, recording parked
    _run_pipeline(ready_recording.id, drain, 1)  # retry — stage passes

    r = Recording.objects.get(pk=ready_recording.id)
    assert "last_error" not in r.workflow_state, (
        "recording completed but the field still claims it's broken"
    )
    assert r.status == RecordingStatus.COMPLETED


def test_reason_moves_to_recovered_error(ready_recording, flaky, drain):
    _run_pipeline(ready_recording.id, drain)
    _run_pipeline(ready_recording.id, drain, 1)

    r = Recording.objects.get(pk=ready_recording.id)
    recovered = r.workflow_state["recovered_error"]
    assert recovered["reason"] == "embedder rejected the batch"
    assert recovered["stage"] == "flaky"
    assert recovered["recovered_at"], "needs a timestamp for WHEN it recovered"


def test_no_fields_added_on_clean_success(ready_recording, flaky, drain):
    """Success with no prior failure must not add empty fields."""
    FlakyStage.failures = 1  # first run already succeeds
    _run_pipeline(ready_recording.id, drain)

    r = Recording.objects.get(pk=ready_recording.id)
    assert "last_error" not in r.workflow_state
    assert "recovered_error" not in r.workflow_state


def test_field_persists_while_recording_broken(ready_recording, flaky, drain):
    """The flip side: a recording that's truly stuck must keep the reason."""
    with override_settings(
        STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["convert", "flaky"], "MAX_STAGE_RETRIES": 0}
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.ERROR
    assert r.workflow_state["last_error"]["stage"] == "flaky"
    assert "recovered_error" not in r.workflow_state
