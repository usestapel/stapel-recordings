"""The models that made a recording, stamped where the text is written, and
the speaker names a summary call states, checked before they are kept."""
from contextlib import contextmanager

import pytest
from django.test import override_settings

from stapel_recordings import dto, events, speaker_names, stages, transcript_schema
from stapel_recordings.models import Job, JobStatus, Recording, RecordingStatus

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


@contextmanager
def seams(**keys):
    with override_settings(STAPEL_RECORDINGS={**_FAKE, **keys}):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        yield


def _metered(summary, model, provider="openai-compat"):
    return {
        "status": "ok",
        "summary": summary,
        "usage": {},
        "provider_used": provider,
        "metering": {"provider": provider, "model": model, "fallback_used": False},
    }


@pytest.fixture
def run(ready_recording, stub_transcribe, stub_summarize, drain):
    def _run(**settings):
        with seams(**settings):
            events.emit_stage(ready_recording.id, 0)
            drain()
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.COMPLETED
        return r

    return _run


# ─── W1: the STT wire model ──────────────────────────────────────────────


def test_the_wire_model_is_stored_and_heads_the_transcript(run, stub_transcribe):
    stub_transcribe.result = {**stub_transcribe.result, "model": "scribe_v2", "diar_model": None}
    r = run()
    assert (r.provider_used, r.asr_model, r.asr_model_source, r.diar_model) == (
        "stub-asr", "scribe_v2", "recorded", None,
    )
    engine = transcript_schema.from_db_segments(r).engine
    assert engine.asr_model_id == "scribe_v2"
    assert engine.asr_provider == "stub-asr"
    assert engine.diar_model_id is None
    assert engine.asr_model_source == "recorded"
    assert dto.recording_to_dto(r).asr_model == "scribe_v2"


def test_a_reply_without_a_model_keeps_the_old_header(run):
    r = run()
    assert r.asr_model is None and r.asr_model_source is None
    engine = transcript_schema.from_db_segments(r).engine
    assert engine.asr_model_id == "stub-asr"
    assert engine.asr_model_source is None


def test_the_engine_is_not_content(run, stub_transcribe):
    r = run()
    before = transcript_schema.transcript_hash(transcript_schema.from_db_segments(r))
    Recording.objects.filter(pk=r.pk).update(asr_model="scribe_v2", asr_model_source="recorded")
    r.refresh_from_db()
    assert transcript_schema.transcript_hash(transcript_schema.from_db_segments(r)) == before


# ─── W2: the summary's model, with the summary ───────────────────────────


def test_the_merge_stamps_the_reply_that_wrote_the_text(run, stub_summarize):
    stub_summarize.result = _metered("First.", "x-ai/grok-4.5")
    r = run()
    stamp = stages.summary_stamp(r)
    assert stamp["model"] == "x-ai/grok-4.5"
    assert stamp["provider"] == "openai-compat"
    assert stamp["origin"] == "merge"
    assert stamp["run_id"]
    fields = dto.summary_model_fields(r)
    assert fields == {"summary_model": "x-ai/grok-4.5", "summary_generated_at": stamp["at"]}


def test_a_reply_without_metering_stamps_null_never_an_older_model(run, stub_summarize):
    stub_summarize.result = _metered("First.", "x-ai/grok-4.5")
    r = run()
    stub_summarize.result = {"status": "ok", "summary": "Second.", "usage": {}}
    job, started = stages.start_resummarize(r)
    assert started and job.status == JobStatus.COMPLETED
    r.refresh_from_db()
    assert r.summary == "Second."
    assert stages.summary_stamp(r)["model"] is None
    assert Job.objects.get(pk=job.pk).provider_used is None


def test_a_resummary_stamps_its_model_and_the_job_names_it(run, stub_summarize):
    stub_summarize.result = _metered("First.", "x-ai/grok-4.5")
    r = run()
    first_at = stages.summary_stamp(r)["at"]
    stub_summarize.result = _metered("Second.", "claude-sonnet-5", provider="anthropic")
    job, _ = stages.start_resummarize(r)
    r.refresh_from_db()
    stamp = stages.summary_stamp(r)
    assert (stamp["model"], stamp["origin"], stamp["run_id"]) == (
        "claude-sonnet-5", "resummarize", str(job.id),
    )
    assert stamp["at"] >= first_at
    assert Job.objects.get(pk=job.pk).provider_used == "claude-sonnet-5"


def test_a_failed_resummary_leaves_the_text_and_its_stamp(run, stub_summarize):
    stub_summarize.result = _metered("First.", "x-ai/grok-4.5")
    r = run()
    stub_summarize.result = {
        "status": "failure", "reason": "nope",
        "metering": {"provider": "anthropic", "model": "claude-sonnet-5"},
    }
    stages.start_resummarize(r)
    r.refresh_from_db()
    assert r.summary == "First."
    assert stages.summary_stamp(r)["model"] == "x-ai/grok-4.5"


def test_no_summary_no_stamp(make_recording):
    r = make_recording(status=RecordingStatus.COMPLETED)
    assert stages.summary_stamp(r) is None
    assert dto.summary_model_fields(r) == {"summary_model": None, "summary_generated_at": None}


# ─── P382: speaker names stated in the transcript ────────────────────────


def test_off_the_payload_does_not_ask(run, stub_summarize):
    run()
    assert "speaker_names" not in stub_summarize.calls[0]


def _guess(label, name, evidence, confidence="high"):
    return {"speaker": label, "suggested_name": name, "evidence_text": evidence,
            "confidence": confidence}


def test_on_the_guesses_are_checked_and_stored_with_the_summary(
    run, stub_transcribe, stub_summarize
):
    stub_transcribe.result["transcript"]["utterances"] = [
        {"text": "Hi, I am Alice and I run sales.", "start": 0.0, "end": 2.0,
         "speaker": "speaker_0", "confidence": 0.9, "word_indexes": []},
        {"text": "Thanks   Alice, Bob here.", "start": 2.0, "end": 4.0,
         "speaker": "speaker_1", "confidence": 0.8, "word_indexes": []},
    ]
    stub_summarize.result = {
        **_metered("Notes.", "x-ai/grok-4.5"),
        "speaker_name_guesses": [
            _guess("spk_0", "Alice", "I am Alice"),
            _guess("spk_0", "Alice", "Thanks Alice", "medium"),
            _guess("spk_1", "Bob", "Thanks Alice, Bob here."),
            _guess("spk_7", "Carol", "I am Alice"),
            _guess("spk_1", "Dave", "Bob here."),
            _guess("spk_1", "Bob", "my name is Bob"),
            {"speaker": "spk_1"},
        ],
    }
    r = run(SUMMARIZE_SPEAKER_NAMES=True)
    assert stub_summarize.calls[0]["speaker_names"] is True
    block = r.metadata["derived"]["summary"]["speaker_names"]
    assert block["received"] == 7
    assert block["dropped"] == {
        "duplicate_label": 1, "unknown_label": 1, "name_not_in_evidence": 1,
        "evidence_not_verbatim": 1, "malformed": 1,
    }
    by_label = {g["label"]: g for g in block["guesses"]}
    assert set(by_label) == {"spk_0", "spk_1"}
    alice = by_label["spk_0"]
    assert (alice["suggested_name"], alice["confidence"], alice["start_ms"], alice["end_ms"]) == (
        "Alice", "high", 0, 2000,
    )
    sp0 = r.speakers.get(label="speaker_0")
    assert alice["speaker_id"] == str(sp0.id)
    # Shown: medium and above, unnamed speakers only.
    shown = speaker_names.speaker_name_guesses(r)
    assert {g["label"] for g in shown} == {"spk_0", "spk_1"}
    sp0.display_name = "Alice"
    sp0.save(update_fields=["display_name"])
    assert {g["label"] for g in speaker_names.speaker_name_guesses(r)} == {"spk_1"}


def test_a_named_speaker_is_never_guessed(make_recording):
    r = make_recording(status=RecordingStatus.COMPLETED)
    sp = r.speakers.create(label="speaker_0", display_name="Alice")
    r.segments.create(speaker=sp, sequence_num=0, start_time=0, end_time=1, text="I am Alice")
    transcript = transcript_schema.from_db_segments(r)
    block = speaker_names.checked_guesses(
        transcript, [_guess("Alice", "Alice", "I am Alice"), _guess("spk_0", "Alice", "I am Alice")]
    )
    assert block["guesses"] == []
    assert block["dropped"] == {"already_named": 2}


def test_low_confidence_is_kept_but_not_shown(make_recording):
    r = make_recording(status=RecordingStatus.COMPLETED, summary="S")
    sp = r.speakers.create(label="speaker_0")
    r.segments.create(speaker=sp, sequence_num=0, start_time=0, end_time=1, text="maybe Alice")
    stages.store_summary(
        r, "S", speaker_name_guesses=[_guess("spk_0", "Alice", "maybe Alice", "low")]
    )
    assert len(r.metadata["derived"]["summary"]["speaker_names"]["guesses"]) == 1
    assert speaker_names.speaker_name_guesses(r) == []
    assert len(speaker_names.speaker_name_guesses(r, min_confidence="low")) == 1
