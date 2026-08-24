"""Run identity on the pipeline's public events (the billing seam).

A recording can go through the pipeline more than once
(``pipeline.reprocess_recording``) and each run costs the host real money.
Before run identity, the terminal ``recording.completed`` of run 2 was
byte-identical to run 1's, so a consumer that meters post-hoc could only key
its idempotency on ``recording:<id>`` — its second debit short-circuited on
the first run's transaction and every re-run was free. These tests pin what
makes ``recording:<id>:<run_id>`` buildable: one run_id per run, a DIFFERENT
one per reprocess, a monotonic attempt, and the same run_id across a retry
of a failed run.
"""
import json
import uuid

import pytest
from django.test import override_settings

from stapel_recordings import events, pipeline, stages
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def _payloads(topic):
    from stapel_core.django.outbox.models import OutboxEvent

    return [
        json.loads(row.event_json)["payload"]
        for row in OutboxEvent.objects.filter(topic=topic).order_by("id")
    ]


def _trace(name):
    def _stage(recording, ctx):
        fakes.STAGE_TRACE.append(name)
        return ctx

    return _stage


@pytest.fixture
def two_stages():
    for n in ("ri_one", "ri_two"):
        stages.register_stage(n, _trace(n))
    yield ("ri_one", "ri_two")
    for n in ("ri_one", "ri_two"):
        stages.unregister_stage(n)


@pytest.fixture
def run_pipeline(two_stages):
    """Run the whole 2-stage pipeline for a recording, under fake storage."""

    def _run(recording, drain):
        with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": list(two_stages)}):
            events.emit_stage(recording.id, 0)
            drain()

    return _run


# ─── the run's identity ────────────────────────────────────────────────


def test_initial_run_emits_a_run_id(make_recording, run_pipeline, drain):
    r = make_recording(status=RecordingStatus.QUEUED)
    run_pipeline(r, drain)

    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED
    completed = _payloads(events.ACTION_COMPLETED)
    assert len(completed) == 1
    run_id = completed[0]["run_id"]
    uuid.UUID(run_id)  # a real uuid, not a placeholder
    assert completed[0]["attempt"] == 1
    # And it is the run recorded on the recording itself.
    assert pipeline.run_identity(r) == {"run_id": run_id, "attempt": 1}


def test_reprocess_emits_a_different_run_id_and_increments_attempt(
    make_recording, run_pipeline, drain
):
    """THE BILLING BUG. Two runs, two identities — so a consumer keying on
    recording_id + run_id charges twice, once per run."""
    r = make_recording(status=RecordingStatus.QUEUED)
    run_pipeline(r, drain)
    first = _payloads(events.ACTION_COMPLETED)[0]

    assert pipeline.reprocess_recording(str(r.id)) is True
    r.refresh_from_db()
    run_pipeline(r, drain)

    completed = _payloads(events.ACTION_COMPLETED)
    assert len(completed) == 2
    second = completed[1]
    assert second["run_id"] != first["run_id"]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    # The keys a metering consumer builds are distinct — the whole point.
    assert len({f"{p['recording_id']}:{p['run_id']}" for p in completed}) == 2


def test_reprocess_stamps_the_new_run_before_any_stage_runs(make_recording, run_pipeline, drain):
    """The identity is minted by the transition itself, not by the first
    stage: a run that dies before stage 0 still has a run_id to refund."""
    r = make_recording(status=RecordingStatus.QUEUED)
    run_pipeline(r, drain)
    r.refresh_from_db()
    before = pipeline.run_identity(r)

    assert pipeline.reprocess_recording(str(r.id)) is True
    r.refresh_from_db()
    after = pipeline.run_identity(r)
    assert after["run_id"] != before["run_id"]
    assert after["attempt"] == before["attempt"] + 1


def test_stage_completed_carries_the_run_it_belongs_to(make_recording, run_pipeline, drain):
    r = make_recording(status=RecordingStatus.QUEUED)
    run_pipeline(r, drain)
    r.refresh_from_db()
    run_id = pipeline.run_identity(r)["run_id"]

    per_stage = _payloads(events.ACTION_STAGE_COMPLETED)
    assert [p["stage"] for p in per_stage] == ["ri_one", "ri_two"]
    assert {p["run_id"] for p in per_stage} == {run_id}
    assert {p["attempt"] for p in per_stage} == {1}


def test_a_retry_of_a_failed_run_is_the_same_run(make_recording, drain):
    """retry_recording resumes a DLQ'd run — same run_id, same attempt. A
    run that needed a retry to finish must be billed once, not twice."""
    boom = {"fail": True}

    def flaky(recording, ctx):
        if boom["fail"]:
            raise stages.StageFatal("boom")
        return ctx

    stages.register_stage("ri_flaky", flaky)
    try:
        r = make_recording(status=RecordingStatus.QUEUED)
        with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["ri_flaky"]}):
            events.emit_stage(r.id, 0)
            drain()
            r.refresh_from_db()
            assert r.status == RecordingStatus.ERROR
            failed = _payloads(events.ACTION_FAILED)
            assert len(failed) == 1
            run_id = failed[0]["run_id"]
            assert failed[0]["attempt"] == 1

            boom["fail"] = False
            assert pipeline.retry_recording(str(r.id)) is True
            drain()

        r.refresh_from_db()
        assert r.status == RecordingStatus.COMPLETED
        completed = _payloads(events.ACTION_COMPLETED)
        assert len(completed) == 1
        assert completed[0]["run_id"] == run_id  # same run, resumed
        assert completed[0]["attempt"] == 1
    finally:
        stages.unregister_stage("ri_flaky")


def test_a_run_that_predates_run_ids_still_gets_one(make_recording, run_pipeline, drain):
    """Backfill: a recording mid-flight when this version shipped has a
    pipeline marker but no run_id. Its terminal event must still be
    billable, so the driver mints one on its next write."""
    r = make_recording(status=RecordingStatus.QUEUED)
    r.workflow_state = {"pipeline": {"started_at": "2026-01-01T00:00:00+00:00"}}
    r.save(update_fields=["workflow_state"])

    run_pipeline(r, drain)

    completed = _payloads(events.ACTION_COMPLETED)
    assert len(completed) == 1
    uuid.UUID(completed[0]["run_id"])
    assert completed[0]["attempt"] == 1


def test_run_identity_never_mints(make_recording):
    """The public reader is read-only: it reports the absence rather than
    silently starting a run nobody asked for."""
    r = make_recording(status=RecordingStatus.CREATED)
    assert pipeline.run_identity(r) == {"run_id": None, "attempt": 1}
    r.refresh_from_db()
    assert (r.workflow_state or {}).get("pipeline") is None


def test_start_pipeline_mints_once_under_duplicate_delivery(make_recording):
    r = make_recording(status=RecordingStatus.QUEUED)
    pipeline.start_pipeline(str(r.id))
    r.refresh_from_db()
    first = pipeline.run_identity(r)
    assert first["run_id"] and first["attempt"] == 1

    pipeline.start_pipeline(str(r.id))  # duplicate delivery
    r.refresh_from_db()
    assert pipeline.run_identity(r) == first


# ─── the committed payload schemas accept it ───────────────────────────


def test_run_fields_validate_against_the_emit_schemas(make_recording):
    """emit() validates against schemas/emits/*.json — these calls raise if
    the committed schema does not know the new fields."""
    from stapel_core.comm import emit

    r = make_recording(status=RecordingStatus.COMPLETED)
    run_id = str(uuid.uuid4())
    emit(
        events.ACTION_COMPLETED,
        {
            "recording_id": str(r.id),
            "workspace_id": str(r.workspace_id),
            "owner_id": None,
            "duration_seconds": None,
            "segments_count": 0,
            "speakers_count": 0,
            "word_count": 0,
            "provider_used": None,
            "run_id": run_id,
            "attempt": 2,
        },
    )
    emit(
        events.ACTION_STAGE_COMPLETED,
        {
            "recording_id": str(r.id),
            "workspace_id": str(r.workspace_id),
            "stage": "transcribe",
            "stage_index": 1,
            "status": "transcribing",
            "run_id": run_id,
            "attempt": 2,
        },
    )
    emit(
        events.ACTION_FAILED,
        {
            "recording_id": str(r.id),
            "workspace_id": str(r.workspace_id),
            "stage": "transcribe",
            "reason": "boom",
            "user_retryable": True,
            "run_id": run_id,
            "attempt": 2,
        },
    )


def test_schemas_stay_additive_for_payloads_without_run_identity():
    """The fields are optional: a payload emitted by the previous version
    still validates, so this is a widening of the contract, not a break."""
    from stapel_core.comm import emit

    emit(
        events.ACTION_COMPLETED,
        {
            "recording_id": "r1",
            "workspace_id": "w1",
            "segments_count": 0,
            "speakers_count": 0,
            "word_count": 0,
        },
    )


def test_attempt_must_be_positive():
    from stapel_core.comm import emit
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        emit(
            events.ACTION_COMPLETED,
            {
                "recording_id": "r1",
                "workspace_id": "w1",
                "segments_count": 0,
                "speakers_count": 0,
                "word_count": 0,
                "run_id": "run-1",
                "attempt": 0,
            },
        )


# ─── the neighbouring billable event was audited too ───────────────────


def test_resummarized_already_identifies_its_own_run(
    ready_recording, stub_transcribe, stub_summarize, drain, use_fakes
):
    """``recording.resummarized`` had no gap to close: job_id is minted per
    re-summary, so two re-summaries of one recording are already two keys."""
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED

    stages.start_resummarize(r, user=r.owner)
    stages.start_resummarize(r, user=r.owner)
    drain()

    job_ids = [p["job_id"] for p in _payloads(events.ACTION_RESUMMARIZED)]
    assert len(job_ids) == 2
    assert len(set(job_ids)) == 2
