"""``task.completed`` / ``task.failed`` reach the driver only for recordings tasks.

The subscription is process-wide: a host's export renderer correlates by its
own job id, and every one of its completions used to land in
``resume_stage`` as "recording … not found".
"""
import uuid

import pytest

from stapel_recordings import actions

pytestmark = pytest.mark.django_db


class _Event:
    def __init__(self, **payload):
        self.payload = payload


@pytest.fixture
def driver(monkeypatch):
    calls = []
    monkeypatch.setattr("stapel_recordings.pipeline.resume_stage",
                        lambda *a: calls.append(("resume", a)))
    monkeypatch.setattr("stapel_recordings.pipeline.fail_stage",
                        lambda *a: calls.append(("fail", a)))
    monkeypatch.setattr("stapel_recordings.stages.resume_resummarize", lambda *a: False)
    monkeypatch.setattr("stapel_recordings.stages.fail_resummarize", lambda *a: False)
    monkeypatch.setattr("stapel_core.comm.status", lambda task_id: type("S", (), {"result": {}})())
    return calls


def test_a_foreign_kind_for_a_foreign_id_is_ignored(driver, caplog):
    job_id = str(uuid.uuid4())
    actions.handle_task_completed(_Event(task_id="t1", kind="host.export.render",
                                         correlation_id=job_id))
    actions.handle_task_failed(_Event(task_id="t1", kind="host.export.render",
                                      correlation_id=job_id, error="x"))
    assert driver == []
    assert "not found" not in caplog.text


@pytest.mark.parametrize("kind", ["llm.transcribe", "llm.summarize", None])
def test_pipeline_kinds_are_routed(driver, kind):
    payload = dict(task_id="t1", correlation_id=str(uuid.uuid4()))
    if kind:
        payload["kind"] = kind
    actions.handle_task_completed(_Event(**payload))
    assert [c[0] for c in driver] == ["resume"]


def test_a_host_kind_for_a_real_recording_is_still_routed(driver, make_recording):
    r = make_recording()
    actions.handle_task_completed(_Event(task_id="t1", kind="host.biased_transcribe",
                                         correlation_id=str(r.id)))
    assert [c[0] for c in driver] == ["resume"]


def test_a_non_uuid_correlation_id_is_not_a_recording(driver):
    actions.handle_task_completed(_Event(task_id="t1", kind="host.export.render",
                                         correlation_id="export-42"))
    assert driver == []
