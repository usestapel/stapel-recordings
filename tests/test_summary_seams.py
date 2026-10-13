"""Host seams around the summary call: SUMMARY_PURPOSE_PROVIDER and
SUMMARY_SKIP_POLICY (merge stage and re-summary)."""
import hashlib
import importlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.test import override_settings
from stapel_core.django.outbox.models import OutboxEvent

from stapel_recordings import events, stages, summary_repair, transcript_schema
from stapel_recordings.models import Job, JobStatus, Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}
_HERE = "stapel_recordings.tests.test_summary_seams"

GUIDANCE = "Lead with the objections raised and the agreed next step."
LABEL = "sales call"
DIGEST = hashlib.sha256(f"{GUIDANCE}\n{LABEL}".encode("utf-8")).hexdigest()[:16]

HOOK_CALLS = []
POLICY_CALLS = []


def purpose_hook(recording):
    HOOK_CALLS.append(recording.id)
    return {"guidance": GUIDANCE, "label": LABEL, "version": "texts_v1"}


def purpose_hook_object(recording):
    return SimpleNamespace(guidance=GUIDANCE, label=LABEL, version="texts_v1")


def purpose_hook_none(recording):
    return None


def purpose_hook_label_only(recording):
    return {"label": LABEL, "version": "texts_v1"}


def skip_too_short(recording, transcript):
    POLICY_CALLS.append((recording.id, transcript))
    return "too_short"


def _seen():
    """The hooks as the settings import them (pytest may load this file under
    another module name, with its own lists)."""
    return importlib.import_module(_HERE)


@pytest.fixture(autouse=True)
def _reset_calls():
    _seen().HOOK_CALLS.clear()
    _seen().POLICY_CALLS.clear()


@contextmanager
def seams(**keys):
    with override_settings(STAPEL_RECORDINGS={**_FAKE, **keys}):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        yield


@pytest.fixture
def transcribed(ready_recording, stub_transcribe, stub_summarize, drain):
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    return r


def _hash(recording):
    return transcript_schema.transcript_hash(transcript_schema.from_db_segments(recording))


def _resummarized(recording_id):
    return [
        e for e in OutboxEvent.objects.filter(topic=events.ACTION_RESUMMARIZED)
        if json.loads(e.event_json)["payload"]["recording_id"] == str(recording_id)
    ]


# ─── purpose ─────────────────────────────────────────────────────────────


def test_no_purpose_payload_is_byte_identical(transcribed):
    transcript = transcript_schema.from_db_segments(transcribed)
    expected = {
        "text": transcript_schema.render_markdown(transcript),
        "model": "medium",
        "idempotency_key": f"summary:{transcript_schema.transcript_hash(transcript)}",
        **stages.identity_payload(transcribed),
        "context": stages.llm_context(transcribed, "merge"),
    }
    language = stages._transcript_language(transcribed, transcript)
    if language:
        expected["language"] = language

    assert stages._summarize_payload(transcribed, transcript, purpose=None) == expected
    for hook in (None, f"{_HERE}.purpose_hook_none", f"{_HERE}.purpose_hook_label_only"):
        with seams(SUMMARY_PURPOSE_PROVIDER=hook):
            assert stages._summary_purpose(transcribed) is None


def test_no_hook_pipeline_sends_the_old_key_and_records_nothing(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    events.emit_stage(ready_recording.id, 0)
    drain()
    (payload,) = stub_summarize.calls
    r = Recording.objects.get(pk=ready_recording.id)
    assert payload["idempotency_key"] == f"summary:{_hash(r)}"
    assert "purpose_guidance" not in payload and "purpose_label" not in payload
    assert r.metadata["derived"]["summary"] == {"transcript_hash": _hash(r)}
    assert stages.SUMMARY_PURPOSE_KEY not in r.workflow_state


@pytest.mark.parametrize("hook", ["purpose_hook", "purpose_hook_object"])
def test_hook_payload_key_and_summary_record(
    ready_recording, stub_transcribe, stub_summarize, drain, hook
):
    with seams(SUMMARY_PURPOSE_PROVIDER=f"{_HERE}.{hook}"):
        events.emit_stage(ready_recording.id, 0)
        drain()
    (payload,) = stub_summarize.calls
    r = Recording.objects.get(pk=ready_recording.id)
    assert payload["purpose_guidance"] == GUIDANCE
    assert payload["purpose_label"] == LABEL
    assert payload["idempotency_key"] == f"summary:{_hash(r)}:{DIGEST}"
    assert r.metadata["derived"]["summary"] == {
        "transcript_hash": _hash(r),
        "purpose_prompt_version": "texts_v1",
        "purpose_prompt_digest": DIGEST,
    }


def test_long_transcript_sends_both_fields_under_one_key(transcribed, stub_summarize):
    """The agent splits one call into chunks + merge and derives every part's
    key from this one; the library sends the pair whatever the length."""
    transcript = transcript_schema.from_db_segments(transcribed)
    seg = transcript.segments[0]
    transcript.segments = [seg] * 4000
    with seams(SUMMARY_PURPOSE_PROVIDER=f"{_HERE}.purpose_hook"):
        purpose = stages._summary_purpose(transcribed)
        payload = stages._summarize_payload(transcribed, transcript, purpose=purpose)
    assert len(payload["text"]) > 60_000  # several ~15k-token agent chunks
    assert payload["purpose_guidance"] == GUIDANCE
    assert payload["purpose_label"] == LABEL
    assert payload["idempotency_key"].endswith(f":{DIGEST}")


def test_one_digest_for_every_call_of_a_recording(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    with seams(SUMMARY_PURPOSE_PROVIDER=f"{_HERE}.purpose_hook"):
        events.emit_stage(ready_recording.id, 0)
        drain()
        r = Recording.objects.get(pk=ready_recording.id)
        stages.start_resummarize(r)
    keys = {p["idempotency_key"] for p in stub_summarize.calls}
    assert len(stub_summarize.calls) == 2
    assert keys == {f"summary:{_hash(r)}:{DIGEST}"}
    # Resolved once per summarize request, not per field or per part.
    assert len(_seen().HOOK_CALLS) == 2


def test_resummary_records_the_purpose_on_the_summary(transcribed, stub_summarize):
    stub_summarize.result = {"status": "ok", "summary": "Newer.", "usage": {}}
    with seams(SUMMARY_PURPOSE_PROVIDER=f"{_HERE}.purpose_hook"):
        job, started = stages.start_resummarize(transcribed)
    assert started and job.status == JobStatus.COMPLETED
    r = Recording.objects.get(pk=transcribed.id)
    assert r.summary == "Newer."
    assert r.metadata["derived"]["summary"]["purpose_prompt_digest"] == DIGEST
    assert r.metadata["derived"]["summary"]["purpose_prompt_version"] == "texts_v1"


# ─── skip ────────────────────────────────────────────────────────────────


def test_pipeline_skip_makes_no_call_and_stores_the_outcome(
    ready_recording, stub_transcribe, stub_summarize, drain
):
    with seams(SUMMARY_SKIP_POLICY=f"{_HERE}.skip_too_short"):
        events.emit_stage(ready_recording.id, 0)
        drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert stub_summarize.calls == []
    assert r.status == RecordingStatus.COMPLETED
    assert r.summary is None
    assert stages.summary_skipped(r)["reason"] == "too_short"
    assert summary_repair.PENDING_KEY not in r.workflow_state
    (_, transcript), = _seen().POLICY_CALLS
    assert isinstance(transcript, transcript_schema.UnifiedTranscript)


def test_a_stored_skip_is_never_marked_pending(transcribed):
    stages._record_summary_skipped(transcribed, "no_speech")
    assert summary_repair.mark_pending(transcribed, reason="provider") == {}
    r = Recording.objects.get(pk=transcribed.id)
    assert summary_repair.PENDING_KEY not in r.workflow_state


def test_a_produced_summary_replaces_a_stored_skip(transcribed):
    stages._record_summary_skipped(transcribed, "too_short")
    stages.store_summary(transcribed, "Now there is one.")
    r = Recording.objects.get(pk=transcribed.id)
    assert stages.summary_skipped(r) is None


def test_user_resummary_skip_keeps_the_summary_and_emits_nothing(
    transcribed, stub_summarize
):
    calls = len(stub_summarize.calls)
    before = transcribed.summary
    with seams(SUMMARY_SKIP_POLICY=f"{_HERE}.skip_too_short"):
        job, started = stages.start_resummarize(transcribed)
    assert started
    job = Job.objects.get(pk=job.pk)
    assert job.status == JobStatus.COMPLETED
    assert job.result == {"skipped": True, "reason": "too_short"}
    assert len(stub_summarize.calls) == calls
    r = Recording.objects.get(pk=transcribed.id)
    assert r.summary == before
    assert stages.summary_skipped(r) is None  # the previous summary stays the outcome
    assert _resummarized(transcribed.id) == []


def test_repair_resummary_skip_stores_the_outcome_and_clears_pending(
    transcribed, stub_summarize
):
    Recording.objects.filter(pk=transcribed.id).update(summary=None)
    r = Recording.objects.get(pk=transcribed.id)
    summary_repair.mark_pending(r, reason="provider")
    calls = len(stub_summarize.calls)
    with seams(SUMMARY_SKIP_POLICY=f"{_HERE}.skip_too_short"):
        job, _ = stages.start_resummarize(r, origin=summary_repair.ORIGIN_REPAIR)
    r = Recording.objects.get(pk=transcribed.id)
    assert len(stub_summarize.calls) == calls
    assert Job.objects.get(pk=job.pk).status == JobStatus.COMPLETED
    assert summary_repair.pending(r) is None
    assert stages.summary_skipped(r)["reason"] == "too_short"
    assert _resummarized(transcribed.id) == []


def test_staff_resummary_bypasses_the_skip_policy(transcribed, stub_summarize):
    stub_summarize.result = {"status": "ok", "summary": "Staff asked.", "usage": {}}
    calls = len(stub_summarize.calls)
    with seams(SUMMARY_SKIP_POLICY=f"{_HERE}.skip_too_short"):
        user_job, _ = stages.start_resummarize(transcribed)
        assert user_job.result == {"skipped": True, "reason": "too_short"}
        asked = len(_seen().POLICY_CALLS)
        stages.start_resummarize(transcribed, origin=summary_repair.ORIGIN_STAFF)
    assert len(_seen().POLICY_CALLS) == asked  # staff never asks the policy
    assert len(stub_summarize.calls) == calls + 1
    assert Recording.objects.get(pk=transcribed.id).summary == "Staff asked."
    assert len(_resummarized(transcribed.id)) == 1
