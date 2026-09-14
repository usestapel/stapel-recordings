"""``needs_payment`` — the pipeline parks on an empty wallet, it does not fail.

An account that cannot pay for the work left on a recording is not a broken
recording: nothing failed, a retry cannot help, and the only thing that moves
it is money. These tests hold that line at every seam it crosses — the stage
signal, the driver's park, the event, the redelivery guard and the explicit
transition back.
"""
import json
import uuid

import pytest
from django.test import override_settings
from stapel_core.django.outbox.models import OutboxEvent

from stapel_recordings import events, pipeline, stages
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def _payload(topic):
    """The one emitted payload for *topic*, out of the outbox row."""
    return json.loads(OutboxEvent.objects.get(topic=topic).event_json)["payload"]


def _broke_stage(reason="insufficient_credits", detail="balance 0"):
    def _stage(recording, ctx):
        raise stages.StageNeedsPayment(reason, detail)

    return _stage


def test_needs_payment_is_not_a_processing_status():
    """A client must STOP polling: the next move is the user's, not ours."""
    assert RecordingStatus.is_processing(RecordingStatus.NEEDS_PAYMENT) is False
    assert RecordingStatus.is_processing("needs_payment") is False
    assert RecordingStatus.NEEDS_PAYMENT not in __import__(
        "stapel_recordings.models", fromlist=["PROCESSING_STATUSES"]
    ).PROCESSING_STATUSES


def test_stage_needs_payment_parks_the_recording(make_recording, drain):
    """StageNeedsPayment -> status needs_payment + a needs_payment block, and
    recording.needs_payment instead of recording.failed."""
    stages.register_stage("broke", _broke_stage())
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["broke"]}):
        events.emit_stage(r.id, 0)
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.NEEDS_PAYMENT
    block = r.workflow_state["needs_payment"]
    assert block["stage"] == "broke"
    assert block["reason"] == "insufficient_credits"
    assert block["detail"] == "balance 0"
    assert block["at"]
    # Not an error, and not written where a UI reads errors from.
    assert "last_error" not in r.workflow_state
    assert r.retry_count == 0

    assert OutboxEvent.objects.filter(topic=events.ACTION_NEEDS_PAYMENT).count() == 1
    assert not OutboxEvent.objects.filter(topic=events.ACTION_FAILED).exists()
    assert not OutboxEvent.objects.filter(topic=events.ACTION_COMPLETED).exists()


def test_needs_payment_event_carries_reason_and_run_identity(make_recording, drain):
    stages.register_stage("broke_evt", _broke_stage("free_minutes_exhausted", "cap 3m"))
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["broke_evt"]}):
        events.emit_stage(r.id, 0)
        drain()

    r.refresh_from_db()
    payload = _payload(events.ACTION_NEEDS_PAYMENT)
    assert payload["recording_id"] == str(r.id)
    assert payload["workspace_id"] == str(r.workspace_id)
    assert payload["owner_id"] == str(r.owner_id)
    assert payload["stage"] == "broke_evt"
    assert payload["reason"] == "free_minutes_exhausted"
    assert payload["run_id"] == pipeline.run_identity(r)["run_id"]
    assert payload["attempt"] == 1
    # The detail can carry balance internals — it stays server-side.
    assert "detail" not in payload


def test_redelivery_does_not_resurrect_a_parked_recording(make_recording, drain):
    """Terminal for deliveries: re-driving it would spend money the account
    does not have."""
    calls = []

    def broke(recording, ctx):
        calls.append(1)
        raise stages.StageNeedsPayment("insufficient_credits")

    stages.register_stage("broke_redeliver", broke)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["broke_redeliver"]}):
        events.emit_stage(r.id, 0)
        drain()
        r.refresh_from_db()
        assert r.status == RecordingStatus.NEEDS_PAYMENT
        assert len(calls) == 1

        # Broker redelivery, a stale reconcile pass, and a duplicate
        # recording.uploaded.
        pipeline.run_stage(str(r.id), 0)
        pipeline.start_pipeline(str(r.id))
        drain()

    r.refresh_from_db()
    assert r.status == RecordingStatus.NEEDS_PAYMENT
    assert len(calls) == 1
    assert OutboxEvent.objects.filter(topic=events.ACTION_NEEDS_PAYMENT).count() == 1


def test_reconcile_does_not_sweep_a_parked_recording(make_recording):
    """The stuck-recording watchdog must not re-drive a parked recording —
    it is waiting for a top-up, not hung."""
    from datetime import timedelta

    from django.utils import timezone

    from stapel_recordings.management.commands.recordings_reconcile import Command

    r = make_recording(status=RecordingStatus.NEEDS_PAYMENT)
    Recording.objects.filter(pk=r.pk).update(
        updated_at=timezone.now() - timedelta(days=1)
    )
    assert Command().reconcile_once() == 0
    assert not OutboxEvent.objects.filter(topic=events.ACTION_STAGE).exists()


def test_resume_after_payment_requeues_from_first_incomplete_stage(make_recording, drain):
    """needs_payment -> queued, resuming at the first stage whose name has not
    completed, on the SAME run."""
    ran = []
    broke = {"on": True}

    def ok(recording, ctx):
        ran.append("ok")
        return ctx

    def gate(recording, ctx):
        if broke["on"]:
            raise stages.StageNeedsPayment("insufficient_credits", "balance 0")
        ran.append("gate")
        return ctx

    stages.register_stage("ok_paid", ok)
    stages.register_stage("gate_paid", gate)
    r = make_recording(status=RecordingStatus.QUEUED)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["ok_paid", "gate_paid"]}):
        events.emit_stage(r.id, 0)
        drain()
        r.refresh_from_db()
        assert r.status == RecordingStatus.NEEDS_PAYMENT
        assert ran == ["ok"]
        parked_identity = pipeline.run_identity(r)

        broke["on"] = False
        assert pipeline.resume_after_payment(str(r.id)) is True
        r.refresh_from_db()
        assert r.status == RecordingStatus.QUEUED
        # The block is gone from where a client reads current state, and the
        # reason survives for diagnostics.
        assert "needs_payment" not in r.workflow_state
        assert r.workflow_state["paid_for"]["reason"] == "insufficient_credits"
        assert r.workflow_state["paid_for"]["paid_at"]
        assert r.retry_count == 0

        drain()

    r.refresh_from_db()
    # The completed stage did not run twice; the parked one finished.
    assert ran == ["ok", "gate"]
    assert r.status == RecordingStatus.COMPLETED
    # Same run: a metering consumer keyed on recording_id + run_id charges once.
    assert pipeline.run_identity(r) == parked_identity
    completed = _payload(events.ACTION_COMPLETED)
    assert completed["run_id"] == parked_identity["run_id"]
    assert completed["attempt"] == parked_identity["attempt"] == 1


@pytest.mark.parametrize(
    "status",
    [
        RecordingStatus.CREATED,
        RecordingStatus.UPLOADING,
        RecordingStatus.QUEUED,
        RecordingStatus.TRANSCRIBING,
        RecordingStatus.COMPLETED,
        RecordingStatus.ERROR,
        RecordingStatus.DELETED,
    ],
)
def test_resume_after_payment_refuses_every_other_status(make_recording, status):
    """Only needs_payment is a payable park — and no side effects otherwise."""
    r = make_recording(status=status, workflow_state={"pipeline": {"run_id": "r1"}})
    assert pipeline.resume_after_payment(str(r.id)) is False
    r.refresh_from_db()
    assert r.status == status
    assert r.workflow_state == {"pipeline": {"run_id": "r1"}}
    assert not OutboxEvent.objects.filter(topic=events.ACTION_STAGE).exists()


def test_resume_after_payment_on_missing_recording_is_false(db):
    assert pipeline.resume_after_payment(str(uuid.uuid4())) is False


def test_retry_recording_refuses_a_parked_recording(make_recording):
    """A retry is the wrong tool: the stage did not fail, the wallet is empty,
    and re-running it would hit the same gate."""
    r = make_recording(
        status=RecordingStatus.NEEDS_PAYMENT,
        workflow_state={"needs_payment": {"reason": "insufficient_credits"}},
    )
    assert pipeline.retry_recording(str(r.id)) is False
    r.refresh_from_db()
    assert r.status == RecordingStatus.NEEDS_PAYMENT
    assert r.workflow_state["needs_payment"]["reason"] == "insufficient_credits"


def test_normalizer_payment_gate_leaves_convert_as_needs_payment(ready_recording, drain):
    """A host's affordability gate speaks through the NORMALIZER seam, and the
    convert stage must not flatten it into a StageFatal / DLQ."""
    with override_settings(
        STAPEL_RECORDINGS={
            **_FAKE,
            "NORMALIZER": "stapel_recordings.tests.fakes.unaffordable_normalize",
            "PIPELINE": ["convert"],
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.NEEDS_PAYMENT
    assert r.workflow_state["needs_payment"]["reason"] == "insufficient_credits"
    assert r.workflow_state["needs_payment"]["stage"] == "convert"
    assert OutboxEvent.objects.filter(topic=events.ACTION_NEEDS_PAYMENT).exists()
    assert not OutboxEvent.objects.filter(topic=events.ACTION_FAILED).exists()


def test_payment_required_is_caught_before_the_fatal_handler():
    """Ordering is load-bearing: NormalizePaymentRequired IS a NormalizeFatal,
    so an `except NormalizeFatal` reached first would swallow it into a DLQ."""
    from stapel_recordings.normalize import NormalizeFatal, NormalizePaymentRequired

    assert issubclass(NormalizePaymentRequired, NormalizeFatal)
    assert issubclass(stages.StageNeedsPayment, stages.StageError)
    assert not issubclass(stages.StageNeedsPayment, stages.StageFatal)

    convert = stages.get_stage("convert")
    recording = Recording(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        title="t",
        file_storage_key="k",
        status=RecordingStatus.QUEUED,
    )
    with override_settings(
        STAPEL_RECORDINGS={
            **_FAKE,
            "NORMALIZER": "stapel_recordings.tests.fakes.unaffordable_normalize",
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        from stapel_recordings.storage import get_storage

        get_storage().put_bytes("k", b"bytes", content_type="audio/mpeg")
        with pytest.raises(stages.StageNeedsPayment) as caught:
            convert.run(recording, {})
    assert caught.value.reason == "insufficient_credits"


def test_dto_surfaces_the_status_and_the_reason_but_not_the_detail(make_recording):
    """A host UI renders "needs payment" with the reason; the detail carries
    balance internals and stays where errors' details stay."""
    from stapel_recordings.dto import recording_to_dto

    r = make_recording(
        status=RecordingStatus.NEEDS_PAYMENT,
        workflow_state={
            "needs_payment": {
                "stage": "convert",
                "reason": "insufficient_credits",
                "detail": "balance 0 of 1200 credits",
            }
        },
    )
    dto = recording_to_dto(r)
    assert dto.status == "needs_payment"
    assert dto.needs_payment_reason == "insufficient_credits"
    assert dto.is_processing is False
    assert dto.poll_after_seconds is None
    assert "balance 0" not in str(dto.__dict__)


def test_dto_reason_is_absent_once_the_recording_moved_on(make_recording):
    """A stale reason on a paid, finished recording would be read as current."""
    from stapel_recordings.dto import recording_to_dto

    r = make_recording(
        status=RecordingStatus.COMPLETED,
        workflow_state={"paid_for": {"reason": "insufficient_credits"}},
    )
    assert recording_to_dto(r).needs_payment_reason is None
