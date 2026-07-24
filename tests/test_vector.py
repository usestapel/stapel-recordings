"""Opt-in vector layer — the parts testable on the canonical sqlite suite.

Layering (explicit, per the vector-layer test strategy):

- **Pure units** (hashing, batching, chunking, RRF, snippets) — no DB, no
  pgvector, run everywhere.
- **Enabled-path logic** — the comm seam (``llm.embed``) is faked at the
  boundary with a registered stub Function, and persistence is faked at the
  ``get_store()`` seam (sqlite cannot host a pgvector ``VectorField``);
  batching / hash-skip idempotency / summary chunking / error mapping are
  fully exercised here.
- **DB-bound paths** (real VectorField rows, FTS ranking, cosine ordering,
  HNSW migration) live in ``tests/test_vector_postgres.py`` behind an
  explicit ``connection.vendor`` skip — run them via the
  ``STAPEL_RECORDINGS_TEST_DB=postgres://...`` harness.

The vector app is NOT in this suite's INSTALLED_APPS — that absence is
itself under test (the embed stage must no-op, vector search must refuse
clearly).
"""
import uuid

import pytest
from django.db import connection
from django.test import override_settings

from stapel_recordings.vector.embedding import (
    batched,
    chunk_text,
    content_hash,
    embed_recording,
    embed_texts,
)
from stapel_recordings.vector.search import (
    SearchHit,
    VectorSearchUnavailable,
    make_snippet,
    reciprocal_rank_fusion,
    search_recordings,
)

# The degradation-contract tests assert what happens WITHOUT postgres/the
# vector app; under the opt-in postgres harness (STAPEL_RECORDINGS_TEST_DB,
# which also installs the app) those premises don't hold — skip them there,
# explicitly. Their postgres counterparts live in test_vector_postgres.py.
sqlite_harness_only = pytest.mark.skipif(
    connection.vendor == "postgresql",
    reason="asserts the no-postgres/no-vector-app degradation contract",
)

# ─── Pure units (no DB) ────────────────────────────────────────────────


def test_content_hash_is_stable_sha256():
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("hello!")
    assert len(content_hash("hello")) == 64
    assert content_hash("") == content_hash(None)


def test_batched_slices():
    assert list(batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(batched([], 10)) == []
    assert list(batched([1, 2], 0)) == [[1], [2]]  # size clamps to >= 1


def test_chunk_text_short_and_empty():
    assert chunk_text("", 100) == []
    assert chunk_text("   ", 100) == []
    assert chunk_text("short", 100) == ["short"]
    assert chunk_text("whatever", 0) == ["whatever"]  # 0 disables chunking


def test_chunk_text_windows_with_overlap():
    text = "abcdefghij"  # 10 chars
    assert chunk_text(text, 4, 0) == ["abcd", "efgh", "ij"]
    chunks = chunk_text(text, 4, 2)
    assert chunks[0] == "abcd"
    assert all(len(c) <= 4 for c in chunks)
    # overlap: each successive chunk re-carries the previous tail
    assert chunks[1].startswith("cd")
    # full coverage
    joined = chunks[0] + "".join(c[2:] for c in chunks[1:])
    assert joined == text


def test_rrf_fuses_and_orders():
    fused = reciprocal_rank_fusion({"text": ["a", "b", "c"], "vector": ["b", "a"]}, k=60)
    scores = dict(fused)
    # b: 1/62 + 1/61 ; a: 1/61 + 1/62 — equal; c only in one arm
    assert scores["a"] == pytest.approx(scores["b"])
    assert scores["c"] < scores["a"]
    assert fused[0][0] in ("a", "b")
    assert fused[-1][0] == "c"


def test_rrf_weights_bias_an_arm():
    fused = reciprocal_rank_fusion(
        {"text": ["a"], "vector": ["b"]}, k=60, weights={"text": 2.0, "vector": 1.0}
    )
    assert fused[0][0] == "a"
    zeroed = reciprocal_rank_fusion(
        {"text": ["a"], "vector": ["b"]}, k=60, weights={"text": 0.0}
    )
    assert [key for key, _ in zeroed] == ["b"]  # zero-weight arm drops out


def test_make_snippet_windows_around_match():
    text = ("x " * 200) + "NEEDLE in the haystack " + ("y " * 200)
    snip = make_snippet(text, "needle", width=60)
    assert "NEEDLE" in snip
    assert len(snip) <= 62 + 2  # width + ellipses
    assert make_snippet("short text", "anything") == "short text"
    no_match = make_snippet("z " * 300, "needle", width=40)
    assert no_match.endswith("…")


# ─── Fakes for the enabled path ────────────────────────────────────────


_VECTOR_TEST_SETTINGS = {
    "VECTOR": {
        "ENABLED": True,
        "DIM": 3,
        "MODEL": "",
        "BATCH_SIZE": 64,
        "SUMMARY_CHUNK_CHARS": 0,
        "SUMMARY_CHUNK_OVERLAP": 0,
    }
}


class FakeVectorStore:
    """In-memory stand-in for ORMVectorStore (sqlite can't host VectorField)."""

    def __init__(self):
        self.segments: dict = {}   # (segment_id, model) -> (hash, vector)
        self.chunks: dict = {}     # (chunk_index, model) -> (hash, vector)
        self.pruned: list = []

    def segment_hashes(self, recording):
        out: dict = {}
        for (seg_id, model), (h, _vec) in self.segments.items():
            out.setdefault(seg_id, set()).add((model, h))
        return out

    def summary_hashes(self, recording):
        return {(ci, m, h) for (ci, m), (h, _vec) in self.chunks.items()}

    def upsert_segment(self, segment, *, model, content_hash, vector):
        self.segments[(segment.id, model)] = (content_hash, vector)

    def upsert_summary_chunk(self, recording, *, chunk_index, model, text_hash, vector):
        self.chunks[(chunk_index, model)] = (text_hash, vector)

    def prune_summary_chunks(self, recording, *, model, keep):
        self.pruned.append((model, keep))
        for ci, m in [k for k in self.chunks if k[0] >= keep and k[1] == model]:
            del self.chunks[(ci, m)]


@pytest.fixture
def stub_embed():
    """Register a stub ``llm.embed`` comm Function (the canonical seam —
    same pattern as stub_transcribe/stub_summarize). Returns 3-dim vectors
    derived from each text so assertions can tie vectors to inputs."""
    from stapel_core.comm import register_function

    class Recorder:
        def __init__(self):
            self.calls = []
            self.model = "stub-embed-1"
            self.dim = 3
            self.error = None
            self.result_override = None

        def __call__(self, payload):
            self.calls.append(payload)
            if self.error is not None:
                raise self.error
            if self.result_override is not None:
                return self.result_override
            vectors = [[float(len(t)), 1.0, 0.0][: self.dim] + [0.0] * max(0, self.dim - 3)
                       for t in payload["texts"]]
            return {
                "status": "ok",
                "embeddings": {
                    "provider": "stub",
                    "model": self.model,
                    "dim": self.dim,
                    "vectors": vectors,
                },
            }

    recorder = Recorder()
    register_function("llm.embed", recorder)
    return recorder


@pytest.fixture
def recording_with_segments(make_recording):
    from stapel_recordings.models import Segment

    rec = make_recording(status="completed", language="en")
    for i, text in enumerate(["alpha bravo", "charlie delta", "echo foxtrot"]):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=text,
        )
    return rec


pytestmark = pytest.mark.django_db


# ─── EmbedStage gating (the DiarizeStage no-op pattern) ────────────────


def test_embed_stage_is_registered_after_merge():
    from stapel_recordings.conf import DEFAULT_PIPELINE
    from stapel_recordings.stages import BUILTIN_STAGES

    assert "embed" in BUILTIN_STAGES
    assert list(DEFAULT_PIPELINE).index("embed") == list(DEFAULT_PIPELINE).index("merge") + 1


@sqlite_harness_only
def test_embed_stage_noops_when_app_absent(recording_with_segments, stub_embed):
    """Vector app not installed (this suite) — the stage must be a pure
    no-op even with ENABLED=True: no llm.embed call, ctx passed through."""
    from stapel_recordings.stages import EmbedStage

    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        ctx = {"carried": 1}
        assert EmbedStage().run(recording_with_segments, ctx) == {"carried": 1}
    assert stub_embed.calls == []


def test_embed_stage_noops_when_disabled(recording_with_segments, stub_embed, monkeypatch):
    """App installed but ENABLED left at its default (False) — no-op."""
    import stapel_recordings.vector as vector_pkg

    monkeypatch.setattr(vector_pkg, "vector_app_installed", lambda: True)
    from stapel_recordings.stages import EmbedStage

    ctx = EmbedStage().run(recording_with_segments, {"x": 2})
    assert ctx == {"x": 2}
    assert stub_embed.calls == []


@sqlite_harness_only
def test_full_pipeline_completes_with_embed_noop(ready_recording, stub_transcribe, stub_summarize, stub_embed, drain):
    """The default pipeline (now 5 stages) still completes end-to-end on a
    host without the vector layer; embed completes as a no-op."""
    from stapel_recordings import events
    from stapel_recordings.models import Recording, RecordingStatus

    events.emit_stage(ready_recording.id, 0)
    drain()
    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.COMPLETED
    assert "embed" in r.metadata["pipeline"]["completed"]
    assert stub_embed.calls == []


# ─── Enabled path (comm seam stubbed, store seam faked) ────────────────


@pytest.fixture
def enabled_vector(monkeypatch):
    """Flip both gates open and route persistence into a FakeVectorStore."""
    import stapel_recordings.vector as vector_pkg
    import stapel_recordings.vector.embedding as embedding_mod

    store = FakeVectorStore()
    monkeypatch.setattr(vector_pkg, "vector_app_installed", lambda: True)
    monkeypatch.setattr(embedding_mod, "get_store", lambda: store)
    return store


def test_embed_stage_embeds_segments(recording_with_segments, stub_embed, enabled_vector):
    from stapel_recordings.stages import EmbedStage

    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        EmbedStage().run(recording_with_segments, {})

    assert len(stub_embed.calls) == 1
    assert stub_embed.calls[0]["texts"] == ["alpha bravo", "charlie delta", "echo foxtrot"]
    assert "timeout_seconds" in stub_embed.calls[0]
    stored = enabled_vector.segments
    assert len(stored) == 3
    # rows keyed by the provider-reported model; vectors are what llm.embed returned
    assert all(model == "stub-embed-1" for _sid, model in stored)
    h, vec = stored[(recording_with_segments.segments.get(sequence_num=0).id, "stub-embed-1")]
    assert h == content_hash("alpha bravo")
    assert vec == [11.0, 1.0, 0.0]


def test_embed_is_idempotent_via_content_hash(recording_with_segments, stub_embed, enabled_vector):
    """Second delivery with unchanged texts calls llm.embed zero times —
    the outbox at-least-once / retry-safety contract."""
    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        embed_recording(recording_with_segments)
        assert len(stub_embed.calls) == 1
        embed_recording(recording_with_segments)
        assert len(stub_embed.calls) == 1  # nothing new to embed

        # An edited segment re-embeds only itself.
        seg = recording_with_segments.segments.get(sequence_num=1)
        seg.text = "charlie delta EDITED"
        seg.save(update_fields=["text"])
        embed_recording(recording_with_segments)
    assert len(stub_embed.calls) == 2
    assert stub_embed.calls[1]["texts"] == ["charlie delta EDITED"]


def test_embed_batches_by_batch_size(recording_with_segments, stub_embed, enabled_vector):
    settings = {"VECTOR": {**_VECTOR_TEST_SETTINGS["VECTOR"], "BATCH_SIZE": 2}}
    with override_settings(STAPEL_RECORDINGS=settings):
        embed_recording(recording_with_segments)
    assert [len(c["texts"]) for c in stub_embed.calls] == [2, 1]


def test_embed_forwards_model_and_provider(recording_with_segments, stub_embed, enabled_vector):
    settings = {"VECTOR": {**_VECTOR_TEST_SETTINGS["VECTOR"], "MODEL": "text-embed-9", "PROVIDER": "acme"}}
    with override_settings(STAPEL_RECORDINGS=settings):
        embed_recording(recording_with_segments)
    assert stub_embed.calls[0]["model"] == "text-embed-9"
    assert stub_embed.calls[0]["provider"] == "acme"


def test_embed_summary_chunks_and_prunes(make_recording, stub_embed, enabled_vector):
    rec = make_recording(status="completed", summary="A" * 25)
    settings = {"VECTOR": {**_VECTOR_TEST_SETTINGS["VECTOR"], "SUMMARY_CHUNK_CHARS": 10, "SUMMARY_CHUNK_OVERLAP": 0}}
    with override_settings(STAPEL_RECORDINGS=settings):
        counters = embed_recording(rec)
    assert counters == {"segments_embedded": 0, "summary_chunks_embedded": 3}
    assert sorted(ci for ci, _m in enabled_vector.chunks) == [0, 1, 2]
    assert enabled_vector.pruned == [("stub-embed-1", 3)]

    # Shorter re-summary: stale tail chunks are pruned.
    rec.summary = "B" * 12
    rec.save(update_fields=["summary"])
    with override_settings(STAPEL_RECORDINGS=settings):
        embed_recording(rec)
    assert sorted(ci for ci, _m in enabled_vector.chunks) == [0, 1]


def test_embed_comm_error_is_retryable(recording_with_segments, stub_embed, enabled_vector):
    from stapel_recordings.stages import StageRetryable

    stub_embed.error = RuntimeError("provider down")  # call() wraps into CommError
    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        with pytest.raises(StageRetryable) as exc_info:
            embed_recording(recording_with_segments)
    assert exc_info.value.reason == "embed_call_failed"
    assert enabled_vector.segments == {}


def test_embed_unregistered_function_is_retryable(recording_with_segments, enabled_vector):
    """No llm.embed provider (agent not wired yet) → retryable, not a crash."""
    from stapel_recordings.stages import StageRetryable

    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        with pytest.raises(StageRetryable):
            embed_recording(recording_with_segments)


def test_embed_failure_status_is_retryable(recording_with_segments, stub_embed, enabled_vector):
    from stapel_recordings.stages import StageRetryable

    stub_embed.result_override = {"status": "failure", "reason": "quota"}
    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        with pytest.raises(StageRetryable) as exc_info:
            embed_recording(recording_with_segments)
    assert exc_info.value.reason == "embed_failed"


def test_embed_dim_mismatch_is_fatal(recording_with_segments, stub_embed, enabled_vector):
    """Provider dim vs VECTOR['DIM'] is a configuration error — DLQ, no retry."""
    from stapel_recordings.stages import StageFatal

    stub_embed.dim = 5
    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        with pytest.raises(StageFatal) as exc_info:
            embed_recording(recording_with_segments)
    assert exc_info.value.reason == "embed_dim_mismatch"


def test_embed_short_vector_list_is_retryable(recording_with_segments, stub_embed, enabled_vector):
    from stapel_recordings.stages import StageRetryable

    from stapel_recordings.conf import vector_config

    stub_embed.result_override = {
        "status": "ok",
        "embeddings": {"provider": "stub", "model": "m", "dim": 3, "vectors": [[1.0, 0.0, 0.0]]},
    }
    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        with pytest.raises(StageRetryable) as exc_info:
            embed_texts(["a", "b"], vector_config())
    assert exc_info.value.reason == "embed_bad_response"


# ─── Search: sqlite degradation contract ───────────────────────────────


@sqlite_harness_only
def test_text_search_degrades_to_icontains_on_sqlite(recording_with_segments):
    hits = search_recordings("charlie", mode="text", limit=10)
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, SearchHit)
    assert hit.recording_id == recording_with_segments.id
    assert hit.score == 1.0
    assert "charlie" in hit.snippet


def test_text_search_scopes_by_workspace_and_recordings(recording_with_segments, make_recording):
    from stapel_recordings.models import Segment

    other = make_recording(status="completed")
    Segment.objects.create(recording=other, sequence_num=0, start_time=0, end_time=1, text="charlie elsewhere")

    all_hits = search_recordings("charlie", mode="text", limit=10)
    assert len(all_hits) == 2
    ws_hits = search_recordings("charlie", mode="text", workspace_id=recording_with_segments.workspace_id, limit=10)
    assert {h.recording_id for h in ws_hits} == {recording_with_segments.id}
    rec_hits = search_recordings("charlie", mode="text", recording_ids=[other.id], limit=10)
    assert {h.recording_id for h in rec_hits} == {other.id}
    none = search_recordings("charlie", mode="text", workspace_id=uuid.uuid4(), limit=10)
    assert none == []


@sqlite_harness_only
def test_vector_and_hybrid_raise_clearly_without_the_layer(recording_with_segments):
    """On sqlite / vector-app-absent the host gets a decidable error, never
    a silently empty result."""
    with pytest.raises(VectorSearchUnavailable, match="INSTALLED_APPS"):
        search_recordings("charlie", mode="vector")
    with pytest.raises(VectorSearchUnavailable):
        search_recordings("charlie", mode="hybrid")


@sqlite_harness_only
def test_vector_search_requires_postgres_even_with_app(recording_with_segments, monkeypatch):
    import stapel_recordings.vector as vector_pkg

    monkeypatch.setattr(vector_pkg, "vector_app_installed", lambda: True)
    with pytest.raises(VectorSearchUnavailable, match="PostgreSQL"):
        search_recordings("charlie", mode="vector")


def test_search_input_validation(recording_with_segments):
    with pytest.raises(ValueError):
        search_recordings("q", mode="fuzzy")
    assert search_recordings("", mode="text") == []
    assert search_recordings("   ", mode="hybrid") == []  # empty query short-circuits pre-gate
    assert search_recordings("charlie", mode="text", limit=0) == []


# ─── System check ──────────────────────────────────────────────────────


@sqlite_harness_only
def test_w006_warns_on_enabled_without_app():
    from stapel_recordings.checks import check_vector_layer

    with override_settings(STAPEL_RECORDINGS=_VECTOR_TEST_SETTINGS):
        findings = check_vector_layer(None)
    assert [f.id for f in findings] == ["stapel_recordings.W006"]
    assert check_vector_layer(None) == []  # default: disabled → quiet
