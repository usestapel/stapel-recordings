"""G10: the explicit ``completed -> queued`` reprocess transition.

Distinct from ``retry_recording`` (``error -> queued``, resumes at the first
not-yet-completed stage): reprocess re-runs the whole pipeline from stage 0
for a finished recording and is a forbidden no-op from every other status.
"""
import pytest
from django.test import override_settings

from stapel_recordings import events, pipeline, stages
from stapel_recordings.models import RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def test_reprocess_from_completed_reruns_the_whole_pipeline(make_recording, drain):
    ran = []

    def s_one(recording, ctx):
        ran.append("one")
        return ctx

    def s_two(recording, ctx):
        ran.append("two")
        return ctx

    stages.register_stage("s_one", s_one)
    stages.register_stage("s_two", s_two)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["s_one", "s_two"]}):
        events.emit_stage(r.id, 0)
        drain()
        r.refresh_from_db()
        assert r.status == RecordingStatus.COMPLETED
        assert ran == ["one", "two"]

        # Reprocess: completed -> queued, cursor cleared, both stages re-run.
        assert pipeline.reprocess_recording(str(r.id)) is True
        r.refresh_from_db()
        assert r.status == RecordingStatus.QUEUED
        assert r.metadata["pipeline"].get("completed") in (None, [])
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    assert ran == ["one", "two", "one", "two"]  # full re-run, not a resume


@pytest.mark.parametrize(
    "status",
    [
        RecordingStatus.CREATED,
        RecordingStatus.UPLOADING,
        RecordingStatus.QUEUED,
        RecordingStatus.TRANSCRIBING,
        RecordingStatus.ERROR,
        RecordingStatus.DELETED,
    ],
)
def test_reprocess_forbidden_from_non_completed(make_recording, status):
    r = make_recording(status=status)
    assert pipeline.reprocess_recording(str(r.id)) is False
    r.refresh_from_db()
    assert r.status == status  # unchanged, no side effects


def test_reprocess_missing_recording_is_false(db):
    import uuid

    assert pipeline.reprocess_recording(str(uuid.uuid4())) is False


def test_reprocess_emits_stage_zero(make_recording):
    import json

    from stapel_core.django.outbox.models import OutboxEvent

    r = make_recording(status=RecordingStatus.COMPLETED)
    assert pipeline.reprocess_recording(str(r.id)) is True
    ev = OutboxEvent.objects.filter(topic=events.ACTION_STAGE).order_by("-id").first()
    assert ev is not None
    payload = json.loads(ev.event_json)["payload"]
    assert payload["stage_index"] == 0
    assert payload["recording_id"] == str(r.id)


def test_retry_and_reprocess_are_distinct_transitions(make_recording):
    """retry is error->queued; reprocess is completed->queued. Neither fires
    from the other's source state."""
    err = make_recording(status=RecordingStatus.ERROR)
    assert pipeline.reprocess_recording(str(err.id)) is False  # reprocess not for error
    assert pipeline.retry_recording(str(err.id)) is True

    done = make_recording(status=RecordingStatus.COMPLETED)
    assert pipeline.retry_recording(str(done.id)) is False  # retry not for completed
    assert pipeline.reprocess_recording(str(done.id)) is True
