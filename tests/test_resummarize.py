"""Summarize-only re-run: POST /recordings/{id}/resummarize.

The cheap regenerate — same ``llm.summarize`` call as the ``merge`` stage,
no STT, no diarization, no pipeline. What is asserted here is the whole
contract a host bills against: who may ask (the object policy), when it is
refused (409 nothing to summarize / 503 summaries off), what an acceptance
returns (202 + a Job), that a second ask joins the first job instead of
paying twice, that the staleness receipt is re-pinned, and that exactly one
``recording.resummarized`` leaves per produced summary.
"""
import json
import uuid

import pytest
from django.test import override_settings
from stapel_core.django.outbox.models import OutboxEvent

from stapel_recordings import events, stages
from stapel_recordings.models import Job, JobStatus, JobType, Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


def _url(recording_id):
    return f"/recordings/api/v1/recordings/{recording_id}/resummarize"


@pytest.fixture
def transcribed(ready_recording, stub_transcribe, stub_summarize, drain):
    """A completed recording with a transcript, a summary and a version key."""
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert r.transcript_storage_key
    return r


def _resummarized_events(recording_id):
    return [
        json.loads(e.event_json)["payload"]
        for e in OutboxEvent.objects.filter(
            topic=events.ACTION_RESUMMARIZED
        ).order_by("id")
        if json.loads(e.event_json)["payload"]["recording_id"] == str(recording_id)
    ]


# ─── authority ─────────────────────────────────────────────────────────


def test_owner_gets_202_and_a_job(api_client, transcribed, stub_summarize, user):
    stub_summarize.result = {"status": "ok", "summary": "A newer summary.", "usage": {}}
    api_client.force_authenticate(user=user)

    response = api_client.post(_url(transcribed.id))

    assert response.status_code == 202
    body = response.data
    assert body["recording_id"] == str(transcribed.id)
    assert body["type"] == JobType.SUMMARIZE
    job = Job.objects.get(pk=body["id"])
    assert job.recording_id == transcribed.id
    assert job.owner_id == user.pk
    # Inline task dispatch: the work already happened inside the request.
    assert job.status == JobStatus.COMPLETED
    assert Recording.objects.get(pk=transcribed.id).summary == "A newer summary."


def test_a_stranger_gets_404_not_403(api_client, transcribed, stub_summarize, db):
    """Owner-only, and the refusal does not confirm the recording exists."""
    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    api_client.force_authenticate(user=other)
    calls = len(stub_summarize.calls)

    response = api_client.post(_url(transcribed.id))

    assert response.status_code == 404
    assert response.json()["localizable_error"] == "error.404.recording_not_found"
    assert len(stub_summarize.calls) == calls  # nothing delegated, nothing billed


def test_anonymous_is_refused(api_client, transcribed, stub_summarize):
    calls = len(stub_summarize.calls)

    response = api_client.post(_url(transcribed.id))

    assert response.status_code in (401, 403)
    assert len(stub_summarize.calls) == calls


def test_policy_verb_defaults_to_the_reprocess_answer(transcribed, user):
    """can_resummarize is not a second rule — it follows can_reprocess unless
    a host splits them deliberately."""
    from stapel_recordings.policy import OwnerOnlyPolicy, RecordingPolicy

    owner_only = OwnerOnlyPolicy()
    assert owner_only.can_resummarize(user, transcribed) is True
    assert RecordingPolicy().can_resummarize(user, transcribed) is False  # fail-closed

    class SummaryOnly(OwnerOnlyPolicy):
        def can_reprocess(self, user, recording):
            return False

    # The narrowed reprocess answer is inherited without touching this verb...
    assert SummaryOnly().can_resummarize(user, transcribed) is False

    class Split(SummaryOnly):
        def can_resummarize(self, user, recording):
            return self.can_read(user, recording)

    # ...and one method splits them when the host wants that.
    assert Split().can_resummarize(user, transcribed) is True
    assert Split().can_reprocess(user, transcribed) is False


def test_a_host_policy_denying_resummarize_gets_404(
    api_client, transcribed, stub_summarize, user
):
    """The pre-0.18 shape: a verb answering with a bare ``bool`` still gets
    this module's fail-closed 404 for every denial."""
    api_client.force_authenticate(user=user)
    calls = len(stub_summarize.calls)
    with override_settings(
        STAPEL_RECORDINGS={
            **_FAKE,
            "RECORDING_POLICY": "stapel_recordings.tests.test_resummarize.NoResummaryPolicy",
        }
    ):
        response = api_client.post(_url(transcribed.id))

    assert response.status_code == 404
    assert len(stub_summarize.calls) == calls


class NoResummaryPolicy:
    """Reads everything, re-summarizes nothing (host policy under test)."""

    def visible_queryset(self, user, qs=None):
        from stapel_recordings.models import Recording as _R

        return (qs if qs is not None else _R.objects.all()).filter(
            deleted_at__isnull=True
        )

    def can_read(self, user, recording):
        return True

    def can_resummarize(self, user, recording):
        return False


# ─── a refusal carries its reason (0.18.0) ─────────────────────────────
#
# The bool above renders 404 for every denial, which is a lie for the denial
# a metered host actually has to make: "you are out of credits". A policy
# that answers with a PolicyDecision names the status and the key itself.


class OutOfCreditsPolicy(NoResummaryPolicy):
    """A metered host: everyone reads, an empty balance is 402 + a host key."""

    def can_resummarize(self, user, recording):
        from stapel_recordings.policy import PolicyDecision

        return PolicyDecision.deny("error.402.myapp_out_of_credits", status=402)


class PaymentRequiredWithoutAKeyPolicy(NoResummaryPolicy):
    """Names the status, leaves the key out — the module fills it in."""

    def can_resummarize(self, user, recording):
        from stapel_recordings.policy import PolicyDecision

        return PolicyDecision.deny(status=402)


class AllowedByDecisionPolicy(NoResummaryPolicy):
    """The allow arm of the new shape — must behave exactly like True."""

    def can_resummarize(self, user, recording):
        from stapel_recordings.policy import PolicyDecision

        return PolicyDecision.allow()


def _with_policy(dotted):
    return override_settings(
        STAPEL_RECORDINGS={**_FAKE, "RECORDING_POLICY": dotted}
    )


def test_a_decision_denial_renders_its_own_status_and_code(
    api_client, transcribed, stub_summarize, user
):
    api_client.force_authenticate(user=user)
    calls = len(stub_summarize.calls)
    with _with_policy(
        "stapel_recordings.tests.test_resummarize.OutOfCreditsPolicy"
    ):
        response = api_client.post(_url(transcribed.id))

    assert response.status_code == 402
    # The host's OWN key, untouched: this is what the UI branches on to offer
    # a top-up instead of rendering "this recording does not exist".
    assert response.json()["localizable_error"] == "error.402.myapp_out_of_credits"
    assert len(stub_summarize.calls) == calls  # refused before anything is billed
    assert not Job.objects.filter(recording=transcribed).exists()


def test_a_denial_that_names_only_a_status_gets_this_modules_key(
    api_client, transcribed, stub_summarize, user
):
    api_client.force_authenticate(user=user)
    with _with_policy(
        "stapel_recordings.tests.test_resummarize.PaymentRequiredWithoutAKeyPolicy"
    ):
        response = api_client.post(_url(transcribed.id))

    assert response.status_code == 402
    assert response.json()["localizable_error"] == "error.402.recording_payment_required"


def test_a_decision_denial_precedes_the_state_refusals(
    api_client, make_recording, stub_summarize, user
):
    """Authority is still answered BEFORE state: a caller who may not ask
    gets the policy's answer, not a 409 that describes the recording."""
    r = make_recording(status=RecordingStatus.TRANSCRIBING)  # nothing to summarize
    api_client.force_authenticate(user=user)
    with _with_policy(
        "stapel_recordings.tests.test_resummarize.OutOfCreditsPolicy"
    ):
        response = api_client.post(_url(r.id))

    assert response.status_code == 402
    assert stub_summarize.calls == []


def test_an_allowed_decision_is_the_same_202_as_true(
    api_client, transcribed, stub_summarize, user
):
    """The allow arm changes nothing: same 202, same job, same summary."""
    stub_summarize.result = {"status": "ok", "summary": "Decided.", "usage": {}}
    api_client.force_authenticate(user=user)
    with _with_policy(
        "stapel_recordings.tests.test_resummarize.AllowedByDecisionPolicy"
    ):
        response = api_client.post(_url(transcribed.id))

    assert response.status_code == 202
    assert response.data["type"] == JobType.SUMMARIZE
    assert Job.objects.get(pk=response.data["id"]).recording_id == transcribed.id
    assert Recording.objects.get(pk=transcribed.id).summary == "Decided."


# ─── refusals ──────────────────────────────────────────────────────────


def test_409_when_there_is_no_transcript_yet(
    api_client, make_recording, stub_summarize, user
):
    r = make_recording(status=RecordingStatus.TRANSCRIBING)
    api_client.force_authenticate(user=user)

    response = api_client.post(_url(r.id))

    assert response.status_code == 409
    assert response.json()["localizable_error"] == "error.409.recording_no_transcript"
    assert stub_summarize.calls == []
    assert not Job.objects.filter(recording=r).exists()  # no receipt for a refusal


def test_409_when_the_transcript_key_exists_but_holds_no_segments(
    api_client, make_recording, stub_summarize, user
):
    """A stored transcript with nothing in it is still nothing to summarize —
    and summarizing it would bill for an empty conversation."""
    r = make_recording(status=RecordingStatus.COMPLETED)
    r.transcript_storage_key = f"recordings/{r.workspace_id}/{r.id}/transcript.json"
    r.save(update_fields=["transcript_storage_key"])
    api_client.force_authenticate(user=user)

    response = api_client.post(_url(r.id))

    assert response.status_code == 409
    assert stub_summarize.calls == []


def test_503_when_the_deployment_has_summaries_off(
    api_client, transcribed, stub_summarize, user
):
    api_client.force_authenticate(user=user)
    calls = len(stub_summarize.calls)
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "SUMMARIZE_ENABLED": False}):
        response = api_client.post(_url(transcribed.id))

    assert response.status_code == 503
    assert response.json()["localizable_error"] == (
        "error.503.recording_summarize_unavailable"
    )
    assert len(stub_summarize.calls) == calls


def test_the_recording_status_is_never_moved(api_client, transcribed, stub_summarize, user):
    api_client.force_authenticate(user=user)

    api_client.post(_url(transcribed.id))

    assert Recording.objects.get(pk=transcribed.id).status == RecordingStatus.COMPLETED


# ─── idempotency ───────────────────────────────────────────────────────


def test_a_second_request_joins_the_job_in_flight(
    api_client, transcribed, stub_summarize, user
):
    """The double-clicked button costs one summary, not two."""
    inflight = Job.objects.create(
        workspace_id=transcribed.workspace_id,
        owner=user,
        recording=transcribed,
        type=JobType.SUMMARIZE,
        status=JobStatus.PROCESSING,
        options={"task_id": "task-in-flight"},
    )
    api_client.force_authenticate(user=user)
    calls = len(stub_summarize.calls)

    first = api_client.post(_url(transcribed.id))
    second = api_client.post(_url(transcribed.id))

    assert first.status_code == second.status_code == 202
    assert first.data["id"] == second.data["id"] == str(inflight.id)
    assert len(stub_summarize.calls) == calls  # no second delegated call
    assert Job.objects.filter(recording=transcribed, type=JobType.SUMMARIZE).count() == 1


def test_a_finished_job_does_not_block_a_later_re_summary(
    transcribed, stub_summarize, user
):
    """Idempotency is scoped to work IN FLIGHT — a summary regenerated
    yesterday must not make today's request a no-op."""
    stages.start_resummarize(transcribed, user=user)
    stub_summarize.result = {"status": "ok", "summary": "Third pass.", "usage": {}}

    job, started = stages.start_resummarize(transcribed, user=user)

    assert started is True
    assert job.status == JobStatus.COMPLETED
    assert Recording.objects.get(pk=transcribed.id).summary == "Third pass."


# ─── the summary is stored the way the pipeline stores it ──────────────


def test_staleness_is_cleared_and_the_version_key_re_pinned(
    transcribed, stub_summarize, user
):
    from stapel_recordings.transcript_schema import from_db_segments, transcript_hash

    # An edit marked the summary stale and left the old version key behind.
    r = Recording.objects.get(pk=transcribed.id)
    r.metadata = {
        "staleness": {"summary": True},
        "derived": {"summary": {"transcript_hash": "hash-of-an-older-transcript"}},
        "note": "the client's own key, untouched",
    }
    r.save(update_fields=["metadata"])
    stub_summarize.result = {"status": "ok", "summary": "Rebuilt.", "usage": {}}

    stages.start_resummarize(r, user=user)

    r.refresh_from_db()
    assert r.summary == "Rebuilt."
    assert "staleness" not in r.metadata
    assert r.metadata["derived"]["summary"]["transcript_hash"] == transcript_hash(
        from_db_segments(r)
    )
    assert r.metadata["note"] == "the client's own key, untouched"


def test_the_merge_stage_pins_the_same_key(transcribed):
    """One writer for both ways in: a pipeline-produced summary carries the
    same receipt, so the first edit after it is what makes it stale."""
    from stapel_recordings.transcript_schema import from_db_segments, transcript_hash

    assert transcribed.metadata["derived"]["summary"]["transcript_hash"] == (
        transcript_hash(from_db_segments(transcribed))
    )


def test_a_client_can_neither_forge_nor_drop_the_receipt(transcribed):
    from stapel_recordings.metadata import ReservedMetadataKey, set_user_metadata

    with pytest.raises(ReservedMetadataKey):
        set_user_metadata(transcribed, {"derived": {"summary": {"transcript_hash": "x"}}})
    with pytest.raises(ReservedMetadataKey):
        set_user_metadata(transcribed, {"nested": {"staleness": {"summary": False}}})

    set_user_metadata(transcribed, {"colour": "blue"})
    transcribed.refresh_from_db()
    assert transcribed.metadata["colour"] == "blue"
    assert transcribed.metadata["derived"]["summary"]["transcript_hash"]  # survived


def test_a_failed_summary_leaves_the_old_one_and_emits_nothing(
    transcribed, stub_summarize, user
):
    stub_summarize.result = {"status": "failure", "reason": "llm down"}
    before = transcribed.summary

    job, started = stages.start_resummarize(transcribed, user=user)

    assert started is True
    assert job.status == JobStatus.FAILED
    assert job.error["reason"] == "summary_not_produced"
    assert Recording.objects.get(pk=transcribed.id).summary == before
    assert _resummarized_events(transcribed.id) == []


# ─── the receipt a host bills against ──────────────────────────────────


def test_exactly_one_event_per_produced_summary(
    api_client, transcribed, stub_summarize, user
):
    api_client.force_authenticate(user=user)

    response = api_client.post(_url(transcribed.id))

    payloads = _resummarized_events(transcribed.id)
    assert len(payloads) == 1
    assert payloads[0] == {
        "recording_id": str(transcribed.id),
        "workspace_id": str(transcribed.workspace_id),
        "user_id": str(user.pk),
        "job_id": response.data["id"],
    }


def test_the_event_validates_against_its_schema():
    """Nullable user_id (a re-summary can be driven by a host job)."""
    events.emit(
        events.ACTION_RESUMMARIZED,
        {
            "recording_id": "r1",
            "workspace_id": "w1",
            "user_id": None,
            "job_id": "j1",
        },
        key="r1",
    )


def test_a_joined_job_does_not_emit_a_second_event(
    api_client, transcribed, stub_summarize, user
):
    """One event per summary, not per request — otherwise the retry that was
    supposed to be free gets billed."""
    api_client.force_authenticate(user=user)
    api_client.post(_url(transcribed.id))

    Job.objects.filter(recording=transcribed, type=JobType.SUMMARIZE).update(
        status=JobStatus.PROCESSING, options={"task_id": "still-running"}
    )
    api_client.post(_url(transcribed.id))

    assert len(_resummarized_events(transcribed.id)) == 1


# ─── the queued path (a broker, not inline dispatch) ───────────────────


def test_an_awaiting_job_is_completed_by_its_task_result(
    transcribed, stub_summarize, user, monkeypatch
):
    """With a real broker the request returns before the model does; the
    result arrives later on task.completed and finishes the same Job."""

    def _awaiting(kind, payload, *, recording, deadline_seconds=None, max_attempts=3):
        raise stages.StageAwaiting("task-42", kind)

    monkeypatch.setattr(stages, "submit_task", _awaiting)

    job, started = stages.start_resummarize(transcribed, user=user)
    assert started is True
    assert job.status == JobStatus.PROCESSING
    assert job.options["task_id"] == "task-42"
    assert _resummarized_events(transcribed.id) == []

    handled = stages.resume_resummarize(
        transcribed.id, "task-42", {"status": "ok", "summary": "Late but real."}
    )

    assert handled is True
    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETED
    assert Recording.objects.get(pk=transcribed.id).summary == "Late but real."
    assert len(_resummarized_events(transcribed.id)) == 1


def test_a_foreign_task_result_is_not_claimed(transcribed, user):
    """The routing in actions.py is an ordering, not a fork: a result this
    runner does not own must fall through to the pipeline driver."""
    assert stages.resume_resummarize(transcribed.id, "some-other-task", {}) is False
    assert stages.fail_resummarize(transcribed.id, "some-other-task", "boom") is False


def test_a_redelivered_result_is_applied_once(transcribed, stub_summarize, user, monkeypatch):
    monkeypatch.setattr(
        stages,
        "submit_task",
        lambda kind, payload, **kw: (_ for _ in ()).throw(stages.StageAwaiting("t-1", kind)),
    )
    stages.start_resummarize(transcribed, user=user)
    result = {"status": "ok", "summary": "Once."}

    assert stages.resume_resummarize(transcribed.id, "t-1", result) is True
    assert stages.resume_resummarize(transcribed.id, "t-1", result) is False

    assert len(_resummarized_events(transcribed.id)) == 1


def test_a_failed_task_closes_the_job_without_a_receipt(
    transcribed, stub_summarize, user, monkeypatch
):
    monkeypatch.setattr(
        stages,
        "submit_task",
        lambda kind, payload, **kw: (_ for _ in ()).throw(stages.StageAwaiting("t-2", kind)),
    )
    job, _ = stages.start_resummarize(transcribed, user=user)

    assert stages.fail_resummarize(transcribed.id, "t-2", "provider exploded") is True

    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error["detail"] == "provider exploded"
    assert _resummarized_events(transcribed.id) == []


def test_a_bus_that_refuses_the_submission_is_a_503(transcribed, user, monkeypatch):
    monkeypatch.setattr(
        stages,
        "submit_task",
        lambda kind, payload, **kw: (_ for _ in ()).throw(
            stages.StageRetryable("llm.summarize_submit_failed", "broker down")
        ),
    )

    with pytest.raises(stages.SummarizationUnavailable):
        stages.start_resummarize(transcribed, user=user)

    # Nothing was queued, so nothing is left behind: the Job row rolls back
    # with the rest of the block rather than showing the user a run that
    # never started.
    assert not Job.objects.filter(recording=transcribed, type=JobType.SUMMARIZE).exists()
