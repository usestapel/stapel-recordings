"""A summary the provider refused is owed, and the watchdog pays the debt.

A client fleet, 2026-09-29 00:34Z..09-30 04:59Z: llm.summarize answered 402
(out of credits) for 28 hours. 23 recordings reached ``completed`` with no
summary and nothing ever asked again. Each test below starts from that shape:
the merge stage's summarize call comes back as the agent now reports a
provider-side refusal.
"""
import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone
from stapel_core.django.outbox.models import OutboxEvent

from stapel_recordings import events, summary_repair
from stapel_recordings.models import Job, JobStatus, Recording, RecordingStatus

pytestmark = pytest.mark.django_db

OUT_OF_CREDITS = {
    "status": "failure",
    "reason": "OpenAI-compatible endpoint returned HTTP 402 (quota)",
    "failure_class": "provider",
    "provider_reasons": ["quota"],
    "provider_attempts": [{"provider": "primary-llm", "reason": "quota"}],
}
REFUSED_INPUT = {
    "status": "failure",
    "reason": "unsupported content block",
    "failure_class": "input",
}
SERVED = {"status": "ok", "summary": "They agreed to ship on Friday.", "usage": {}}


@pytest.fixture
def refused(ready_recording, stub_transcribe, stub_summarize, drain):
    """A recording that completed during the outage: transcript, no summary."""
    stub_summarize.result = OUT_OF_CREDITS
    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert not r.summary
    return r


def _resummarized(recording_id):
    return [
        json.loads(e.event_json)["payload"]
        for e in OutboxEvent.objects.filter(topic=events.ACTION_RESUMMARIZED)
        if json.loads(e.event_json)["payload"]["recording_id"] == str(recording_id)
    ]


def _later(minutes):
    return timezone.now() + timedelta(minutes=minutes)


def test_a_provider_refusal_leaves_the_summary_owed(refused):
    block = summary_repair.pending(refused)

    assert block is not None
    assert block["reason"] == "quota"
    assert block["attempts"] == 0
    # What an alert about this marker names: whose failure, and who was asked.
    assert block["failure_class"] == "provider"
    assert block["providers"] == ["primary-llm"]


def test_an_input_refusal_is_not_owed(ready_recording, stub_transcribe, stub_summarize, drain):
    stub_summarize.result = REFUSED_INPUT
    events.emit_stage(ready_recording.id, 0)
    drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert summary_repair.pending(r) is None


def test_the_watchdog_waits_out_the_first_delay(refused, stub_summarize):
    stub_summarize.result = SERVED
    calls = len(stub_summarize.calls)

    counts = summary_repair.repair_due(now=timezone.now())

    assert counts["started"] == 0
    assert len(stub_summarize.calls) == calls


def test_once_the_provider_answers_the_summary_arrives_free(refused, stub_summarize):
    stub_summarize.result = SERVED

    counts = summary_repair.repair_due(now=_later(16))

    assert counts["started"] == 1
    r = Recording.objects.get(pk=refused.id)
    assert r.summary == "They agreed to ship on Friday."
    assert summary_repair.pending(r) is None
    job = Job.objects.get(recording=r)
    assert job.options["origin"] == "pipeline_repair"
    # The host decides what an origin costs; the event says whose it was.
    (payload,) = _resummarized(r.id)
    assert payload["origin"] == "pipeline_repair"
    assert payload["reason"] == "quota"
    # Only a summary: no transcription was bought again.
    assert r.status == RecordingStatus.COMPLETED


def test_during_the_outage_one_probe_per_tick_not_one_per_recording(
    ready_recording, make_recording, stub_transcribe, stub_summarize, drain
):
    stub_summarize.result = OUT_OF_CREDITS
    events.emit_stage(ready_recording.id, 0)
    drain()
    others = []
    for _ in range(3):
        other = make_recording(status="queued")
        from stapel_recordings.storage import get_storage

        key = f"recordings/{other.workspace_id}/{other.id}/audio"
        other.file_storage_key = key
        other.save(update_fields=["file_storage_key"])
        get_storage().put_bytes(key, b"raw-audio-bytes", content_type="audio/mpeg")
        events.emit_stage(other.id, 0)
        drain()
        others.append(other)

    # First tick after the delay: nothing has failed yet, so a batch goes.
    first = summary_repair.repair_due(now=_later(16))
    assert first["started"] == 4
    assert Job.objects.filter(status=JobStatus.FAILED).count() == 4

    # The provider is still down: every later tick sends ONE probe.
    calls = len(stub_summarize.calls)
    second = summary_repair.repair_due(now=_later(60 * 3))
    assert second["started"] == 1
    assert len(stub_summarize.calls) == calls + 1

    # The provider is back: the probe succeeds and the next tick drains.
    stub_summarize.result = SERVED
    summary_repair.repair_due(now=_later(60 * 6))
    summary_repair.repair_due(now=_later(60 * 6))
    owed = [
        r for r in Recording.objects.all() if summary_repair.pending(r) is not None
    ]
    assert owed == []


def test_the_backoff_doubles(refused, stub_summarize):
    summary_repair.repair_due(now=_later(16))  # still refused
    block = summary_repair.pending(Recording.objects.get(pk=refused.id))
    assert block["attempts"] == 1
    assert block["last_failure"].startswith("OpenAI-compatible endpoint returned HTTP 402")

    # 30 minutes later is before the doubled delay: not due.
    assert summary_repair.repair_due(now=_later(16 + 29))["started"] == 0
    assert summary_repair.repair_due(now=_later(16 + 31))["started"] == 1


def test_past_the_deadline_it_stops_and_says_so(refused, stub_summarize):
    counts = summary_repair.repair_due(now=_later(73 * 60))

    assert counts == {"started": 0, "exhausted": 1, "waiting": 0}
    block = summary_repair.pending(Recording.objects.get(pk=refused.id))
    assert block["exhausted_at"]
    assert summary_repair.repair_due(now=_later(74 * 60))["exhausted"] == 0


def test_a_repair_answered_as_bad_input_gives_up(refused, stub_summarize):
    stub_summarize.result = REFUSED_INPUT

    summary_repair.repair_due(now=_later(16))

    block = summary_repair.pending(Recording.objects.get(pk=refused.id))
    assert block["gave_up"] == "input"
    assert summary_repair.repair_due(now=_later(60 * 5))["started"] == 0


@override_settings(STAPEL_RECORDINGS={"SUMMARY_REPAIR_ENABLED": False})
def test_the_switch_turns_it_off(ready_recording, stub_transcribe, stub_summarize, drain, use_fakes):
    stub_summarize.result = OUT_OF_CREDITS
    events.emit_stage(ready_recording.id, 0)
    drain()

    assert summary_repair.pending(Recording.objects.get(pk=ready_recording.id)) is None


def test_a_user_resummary_still_says_user(refused, stub_summarize):
    from stapel_recordings.stages import start_resummarize

    stub_summarize.result = SERVED
    start_resummarize(refused)

    (payload,) = _resummarized(refused.id)
    assert payload["origin"] == "user"
    assert payload["reason"] is None
    # A served summary settles the debt whoever asked.
    assert summary_repair.pending(Recording.objects.get(pk=refused.id)) is None


def test_an_unknown_origin_is_refused(refused):
    from stapel_recordings.stages import start_resummarize

    with pytest.raises(ValueError):
        start_resummarize(refused, origin="free")


def test_the_warning_names_the_class_not_the_reply(
    ready_recording, stub_transcribe, stub_summarize, drain, caplog
):
    """One outage, one alert-store issue: the WARNING line carries no id and
    no provider reply (those differ per recording and per request)."""
    import logging

    stub_summarize.result = OUT_OF_CREDITS
    with caplog.at_level(logging.INFO, logger="stapel_recordings.stages"):
        events.emit_stage(ready_recording.id, 0)
        drain()

    (warning,) = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "not produced" in r.getMessage()
    ]
    assert warning.getMessage() == "merge: summary not produced (failure_class=provider)"
    assert any(
        str(ready_recording.id) in r.getMessage() and "402" in r.getMessage()
        for r in caplog.records if r.levelno == logging.INFO
    )


def test_an_agent_without_provider_attempts_leaves_providers_unknown():
    assert summary_repair.providers_of({"status": "failure", "failure_class": "provider"}) == []
    assert summary_repair.providers_of(None) == []


def test_providers_are_named_once_in_order():
    result = {
        "provider_attempts": [
            {"provider": "a", "reason": "quota"},
            {"provider": "b", "reason": "server"},
            {"provider": "a", "reason": "quota"},
        ]
    }
    assert summary_repair.providers_of(result) == ["a", "b"]
