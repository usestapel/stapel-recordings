"""Per-stage telemetry: each ``llm.*`` call says which recording, stage, run
and attempt it is for, each stage keeps what its calls cost, and convert
keeps what the uploaded file was."""
import json
import subprocess

import pytest
from django.test import override_settings

from stapel_recordings import events, stages
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def _metering(**overrides):
    return {
        "provider": "stub-asr",
        "model": "stub-model",
        "cost_usd": 0.25,
        "cost_basis": "pricing_estimate",
        "priced_by": "stub-card",
        "input_tokens": None,
        "output_tokens": None,
        "thinking_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "audio_duration_ms": 12_000,
        "duration_ms": 900,
        "fallback_used": False,
        "calls": 1,
        **overrides,
    }


def _run_pipeline(recording, drain):
    events.emit_stage(recording.id, 0)
    drain()
    return Recording.objects.get(pk=recording.id)


class TestContextIsSent:
    def test_transcribe_and_summarize_name_recording_stage_run_attempt(
        self, ready_recording, stub_transcribe, stub_summarize, drain
    ):
        r = _run_pipeline(ready_recording, drain)

        run_id = r.workflow_state["pipeline"]["run_id"]
        assert stub_transcribe.calls[0]["context"] == {
            "recording_id": str(r.id),
            "stage": "transcribe",
            "run_id": run_id,
            "attempt": 1,
        }
        assert stub_summarize.calls[0]["context"] == {
            "recording_id": str(r.id),
            "stage": "merge",
            "run_id": run_id,
            "attempt": 1,
        }

    def test_resummarize_says_so(self, ready_recording, stub_transcribe, stub_summarize, drain, user):
        r = _run_pipeline(ready_recording, drain)
        stub_summarize.result = {
            **stub_summarize.result,
            "metering": _metering(provider="llm", cost_usd=0.01),
        }

        stages.start_resummarize(r, user=user)

        assert stub_summarize.calls[-1]["context"]["stage"] == "resummarize"
        r.refresh_from_db()
        assert r.workflow_state["metering"]["resummarize"]["cost_usd"] == 0.01


class TestMeteringIsKept:
    def test_each_stage_keeps_its_reply_metering(
        self, ready_recording, stub_transcribe, stub_summarize, drain
    ):
        stub_transcribe.result = {**stub_transcribe.result, "metering": _metering()}
        stub_summarize.result = {
            **stub_summarize.result,
            "metering": _metering(
                provider="llm", model="m-1", cost_usd=0.003, input_tokens=100,
                output_tokens=20, audio_duration_ms=None, calls=3,
            ),
        }

        r = _run_pipeline(ready_recording, drain)

        assert r.status == RecordingStatus.COMPLETED
        meters = r.workflow_state["metering"]
        run_id = r.workflow_state["pipeline"]["run_id"]
        assert meters["transcribe"]["cost_usd"] == 0.25
        assert meters["transcribe"]["provider"] == "stub-asr"
        assert meters["transcribe"]["run_id"] == run_id
        assert meters["merge"]["calls"] == 3
        assert meters["merge"]["input_tokens"] == 100
        assert "metering" not in (r.metadata or {})

    def test_a_reply_without_metering_leaves_state_untouched(
        self, ready_recording, stub_transcribe, stub_summarize, drain
    ):
        r = _run_pipeline(ready_recording, drain)
        assert r.status == RecordingStatus.COMPLETED
        assert "metering" not in r.workflow_state

    def test_a_failed_call_is_still_kept(self, ready_recording, stub_transcribe, stub_summarize, drain):
        stub_transcribe.result = {
            "status": "failure",
            "reason": "all STT providers failed",
            "metering": _metering(cost_usd=0.5, calls=2),
        }
        r = _run_pipeline(ready_recording, drain)
        assert r.workflow_state["metering"]["transcribe"]["cost_usd"] >= 0.5

    def test_calls_of_one_stage_are_summed(self, make_recording):
        r = make_recording()
        stages.record_metering(r, "embed", {"metering": _metering(cost_usd=0.1, calls=1)})
        stages.record_metering(
            r, "embed",
            {"metering": _metering(cost_usd=0.2, calls=2, cost_basis="unpriced",
                                   provider="p2", input_tokens=5)},
        )
        entry = r.workflow_state["metering"]["embed"]
        assert entry["cost_usd"] == pytest.approx(0.3)
        assert entry["calls"] == 3
        assert entry["cost_basis"] == "unpriced"
        assert entry["provider"] == "p2"
        assert entry["input_tokens"] == 5
        assert entry["audio_duration_ms"] == 24_000

    def test_cached_parts_keep_a_priced_basis(self, make_recording):
        r = make_recording()
        stages.record_metering(r, "merge", {"metering": _metering(cost_usd=0, cost_basis="cached")})
        assert r.workflow_state["metering"]["merge"]["cost_basis"] == "cached"
        stages.record_metering(r, "merge", {"metering": _metering(cost_usd=0.1)})
        assert r.workflow_state["metering"]["merge"]["cost_basis"] == "pricing_estimate"

    def test_a_new_run_replaces_the_stage_entry(self, make_recording):
        r = make_recording(workflow_state={"pipeline": {"run_id": "run-1", "attempt": 1}})
        stages.record_metering(r, "transcribe", {"metering": _metering(cost_usd=1.0)})
        r.workflow_state = {**r.workflow_state, "pipeline": {"run_id": "run-2", "attempt": 2}}
        stages.record_metering(r, "transcribe", {"metering": _metering(cost_usd=0.4)})
        entry = r.workflow_state["metering"]["transcribe"]
        assert (entry["cost_usd"], entry["calls"], entry["run_id"]) == (0.4, 1, "run-2")

    def test_no_metering_no_write(self, make_recording):
        r = make_recording(workflow_state={"x": 1})
        assert stages.record_metering(r, "merge", {"status": "ok"}) is False
        assert r.workflow_state == {"x": 1}


class TestEmbedStage:
    SETTINGS = {
        "VECTOR": {
            "ENABLED": True, "DIM": 3, "MODEL": "", "BATCH_SIZE": 2,
            "SUMMARY_CHUNK_CHARS": 0, "SUMMARY_CHUNK_OVERLAP": 0,
        }
    }

    def test_every_batch_sends_context_and_is_summed(self, make_recording, monkeypatch):
        from stapel_core.comm import register_function

        import stapel_recordings.vector as vector_pkg
        import stapel_recordings.vector.embedding as embedding_mod
        from stapel_recordings.models import Segment
        from stapel_recordings.tests.test_vector import FakeVectorStore

        monkeypatch.setattr(vector_pkg, "vector_app_installed", lambda: True)
        monkeypatch.setattr(embedding_mod, "get_store", lambda: FakeVectorStore())
        calls = []

        def embed(payload):
            calls.append(payload)
            return {
                "status": "ok",
                "embeddings": {
                    "model": "e-1", "dim": 3,
                    "vectors": [[1.0, 0.0, 0.0] for _ in payload["texts"]],
                },
                "metering": _metering(provider="emb", cost_usd=0.0, input_tokens=4),
            }

        register_function("llm.embed", embed)
        rec = make_recording(status="completed", language="en")
        for i, text in enumerate(["a b", "c d", "e f"]):
            Segment.objects.create(
                recording=rec, sequence_num=i, start_time=float(i),
                end_time=float(i + 1), text=text,
            )

        with override_settings(STAPEL_RECORDINGS=self.SETTINGS):
            stages.EmbedStage().run(rec, {})

        assert len(calls) == 2
        assert all(c["context"]["stage"] == "embed" for c in calls)
        assert calls[0]["context"]["recording_id"] == str(rec.id)
        entry = rec.workflow_state["metering"]["embed"]
        assert (entry["calls"], entry["input_tokens"], entry["cost_usd"]) == (2, 8, 0.0)


FFPROBE_JSON = {
    "format": {"format_name": "matroska,webm", "duration": "61.5", "bit_rate": "128000"},
    "streams": [
        {"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000",
         "channels": 2, "bit_rate": None},
        {"codec_type": "video", "codec_name": "vp8"},
    ] + [{"codec_type": "data", "codec_name": "x"}] * 20,
}


class TestProbeAtConvert:
    def _run_convert(self, recording):
        recording.refresh_from_db()
        return stages.ConvertStage().run(recording, {})

    def test_the_probe_summary_is_stored(self, ready_recording, monkeypatch):
        def fake_run(cmd, **kwargs):
            assert "-show_streams" in cmd
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(FFPROBE_JSON).encode(), stderr=b""
            )

        monkeypatch.setattr("stapel_recordings.normalize.subprocess.run", fake_run)
        self._run_convert(ready_recording)
        ready_recording.refresh_from_db()

        probe = ready_recording.metadata["probe"]
        assert probe["format_name"] == "matroska,webm"
        assert probe["duration"] == 61.5
        assert probe["bit_rate"] == 128000
        assert probe["streams"][0] == {
            "codec_type": "audio", "codec_name": "opus", "sample_rate": 48000,
            "channels": 2, "bit_rate": None,
        }
        assert probe["streams"][1]["codec_type"] == "video"
        assert len(probe["streams"]) == 8
        assert ready_recording.normalized_storage_key

    def test_a_failed_probe_writes_nothing_and_converts(self, ready_recording, monkeypatch):
        def missing(cmd, **kwargs):
            raise FileNotFoundError("ffprobe")

        monkeypatch.setattr("stapel_recordings.normalize.subprocess.run", missing)
        self._run_convert(ready_recording)
        ready_recording.refresh_from_db()

        assert "probe" not in (ready_recording.metadata or {})
        assert ready_recording.normalized_storage_key

    def test_a_nonzero_exit_is_no_summary(self, monkeypatch):
        from stapel_recordings.normalize import probe_summary

        monkeypatch.setattr(
            "stapel_recordings.normalize.subprocess.run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"bad"),
        )
        assert probe_summary("/nonexistent") is None

    def test_a_client_cannot_write_the_probe(self):
        from stapel_recordings.metadata import ReservedMetadataKey, sanitize_user_metadata

        with pytest.raises(ReservedMetadataKey):
            sanitize_user_metadata({"probe": {"format_name": "forged"}})
