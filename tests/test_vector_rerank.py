"""Optional rerank stage over search results (VECTOR["RERANK"]).

Runs entirely on the canonical sqlite suite:

- the comm seam (``llm.rerank``) is faked at the registry boundary with a
  registered stub Function — the same pattern as ``stub_embed``;
- the single post-ranking code path is exercised through **text mode**
  (sqlite's degraded ``icontains`` arm gives a deterministic base order);
- the hybrid/post-fusion placement is exercised by no-op'ing the postgres
  gate and canning the vector arm (internal seams), keeping the rerank
  comm boundary itself real(ly stubbed).
"""
import logging

import pytest
from django.test import override_settings

from stapel_recordings.conf import vector_config
from stapel_recordings.vector.search import (
    SearchHit,
    VectorSearchUnavailable,
    reciprocal_rank_fusion,
    search_recordings,
)

pytestmark = pytest.mark.django_db


def _rerank_settings(**over) -> dict:
    """STAPEL_RECORDINGS with only the RERANK sub-block overridden —
    vector_config() deep-merges it over the defaults."""
    return {"VECTOR": {"RERANK": {"ENABLED": True, **over}}}


@pytest.fixture
def stub_rerank():
    """Register a stub ``llm.rerank`` comm Function (registry boundary).

    By default scores documents in their given order (no reordering);
    tests set ``scores`` ({index: score}) to force an ordering. Honors
    ``top_n`` and sorts results by score descending, per the contract."""
    from stapel_core.comm import register_function

    class Recorder:
        def __init__(self):
            self.calls = []
            self.error = None
            self.result_override = None
            self.scores = None

        def __call__(self, payload):
            self.calls.append(payload)
            if self.error is not None:
                raise self.error
            if self.result_override is not None:
                return self.result_override
            n = len(payload["documents"])
            scores = self.scores or {i: float(n - i) for i in range(n)}
            results = sorted(
                ({"index": i, "score": s} for i, s in scores.items()),
                key=lambda r: -r["score"],
            )
            top_n = payload.get("top_n")
            if top_n:
                results = results[:top_n]
            return {
                "status": "ok",
                "rerank": {
                    "provider": "stub",
                    "model": "stub-rerank-1",
                    "results": results,
                },
                "provider_used": "stub",
            }

    recorder = Recorder()
    register_function("llm.rerank", recorder)
    return recorder


@pytest.fixture
def searchable_recording(make_recording):
    """Five segments all matching 'needle' — the sqlite text arm returns
    them in sequence order (uniform score 1.0), a deterministic base."""
    from stapel_recordings.models import Segment

    rec = make_recording(status="completed", language="en")
    for i, word in enumerate(["alpha", "bravo", "charlie", "delta", "echo"]):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=f"needle {word}",
        )
    return rec


def _segment_ids(recording):
    return list(
        recording.segments.order_by("sequence_num").values_list("id", flat=True)
    )


@pytest.fixture
def hybrid_on_sqlite(searchable_recording, monkeypatch):
    """Unlock the hybrid path on sqlite: no-op the postgres gate and can
    the vector arm (segments c, a, b in that order). The text arm and the
    rerank comm boundary stay real."""
    from stapel_recordings.vector import search as search_mod

    ids = _segment_ids(searchable_recording)
    canned = [
        SearchHit(ids[i], searchable_recording.id, 0.9 - 0.1 * n, f"needle {w}")
        for n, (i, w) in enumerate([(2, "charlie"), (0, "alpha"), (1, "bravo")])
    ]
    monkeypatch.setattr(search_mod, "_require_vector_search", lambda: None)
    monkeypatch.setattr(
        search_mod, "_vector_arm", lambda query, ws, rec, limit, cfg: canned[:limit]
    )
    return canned


# ─── Reordering semantics ──────────────────────────────────────────────


def test_rerank_reorders_top_k_and_preserves_tail(searchable_recording, stub_rerank):
    ids = _segment_ids(searchable_recording)
    stub_rerank.scores = {0: 0.1, 1: 0.9, 2: 0.5}
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(TOP_K=3)):
        hits = search_recordings("needle", mode="text", limit=10)

    # Reranked block (b, c, a by rerank score), then the beyond-TOP_K
    # tail (d, e) untouched, in its pre-rerank order.
    assert [h.segment_id for h in hits] == [ids[1], ids[2], ids[0], ids[3], ids[4]]
    assert [h.reranked for h in hits] == [True, True, True, False, False]
    # Reranked hits carry the rerank score; the tail keeps its arm score.
    assert [h.score for h in hits[:3]] == [0.9, 0.5, 0.1]
    assert all(h.score == 1.0 for h in hits[3:])


def test_rerank_sends_full_texts_and_forwards_knobs(make_recording, stub_rerank):
    from stapel_recordings.models import Segment

    rec = make_recording(status="completed", language="en")
    long_text = "needle " + "haystack " * 60  # >> snippet width (160)
    Segment.objects.create(
        recording=rec, sequence_num=0, start_time=0.0, end_time=1.0, text=long_text,
    )
    settings = _rerank_settings(
        TOP_K=2, TOP_N=2, PROVIDER="acme", TIMEOUT_SECONDS=7,
    )
    with override_settings(STAPEL_RECORDINGS=settings):
        hits = search_recordings("needle", mode="text", limit=10)

    assert len(hits) == 1 and hits[0].reranked
    (payload,) = stub_rerank.calls
    assert payload["query"] == "needle"
    # Full segment text, not the trimmed snippet.
    assert payload["documents"] == [long_text]
    assert "…" not in payload["documents"][0]
    assert payload["provider"] == "acme"
    assert payload["timeout_seconds"] == 7
    assert payload["top_n"] == 1  # clamped to the document count


def test_rerank_top_n_respected(searchable_recording, stub_rerank):
    """TOP_N caps how many hits get rerank scores; docs sent but cut by
    TOP_N keep their pre-rerank order after the reranked block."""
    ids = _segment_ids(searchable_recording)
    stub_rerank.scores = {0: 0.1, 1: 0.2, 2: 0.3, 3: 0.4}
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(TOP_K=4, TOP_N=2)):
        hits = search_recordings("needle", mode="text", limit=10)

    assert stub_rerank.calls[0]["top_n"] == 2
    # Reranked: d, c (the two best). Unscored head: a, b (RRF order).
    # Beyond TOP_K: e.
    assert [h.segment_id for h in hits] == [ids[3], ids[2], ids[0], ids[1], ids[4]]
    assert [h.reranked for h in hits] == [True, True, False, False, False]
    assert [h.score for h in hits[:2]] == [0.4, 0.3]


def test_rerank_overfetches_to_top_k_then_truncates_to_limit(
    searchable_recording, stub_rerank
):
    """limit < TOP_K: the arm over-fetches so the reranker sees the full
    window, and the final result is still truncated to limit."""
    ids = _segment_ids(searchable_recording)
    stub_rerank.scores = {4: 0.9, 0: 0.5, 1: 0.4, 2: 0.3, 3: 0.2}
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(TOP_K=50, TOP_N=0)):
        hits = search_recordings("needle", mode="text", limit=2)

    assert len(stub_rerank.calls[0]["documents"]) == 5  # all candidates went
    assert [h.segment_id for h in hits] == [ids[4], ids[0]]  # top-2 post-rerank


def test_searchhit_reranked_defaults_false():
    assert SearchHit(1, 2, 0.5, "s").reranked is False


# ─── Failure policy ────────────────────────────────────────────────────


def test_rerank_fail_open_on_failure_envelope(searchable_recording, stub_rerank, caplog):
    ids = _segment_ids(searchable_recording)
    stub_rerank.result_override = {"status": "failure", "reason": "quota"}
    with override_settings(STAPEL_RECORDINGS=_rerank_settings()):
        with caplog.at_level(logging.WARNING, logger="stapel_recordings.vector.search"):
            hits = search_recordings("needle", mode="text", limit=10)

    assert [h.segment_id for h in hits] == ids  # un-reranked order survives
    assert all(not h.reranked and h.score == 1.0 for h in hits)
    assert any("quota" in r.message for r in caplog.records)


def test_rerank_fail_open_on_comm_error(searchable_recording, stub_rerank, caplog):
    ids = _segment_ids(searchable_recording)
    stub_rerank.error = RuntimeError("provider down")  # call() wraps into CommError
    with override_settings(STAPEL_RECORDINGS=_rerank_settings()):
        with caplog.at_level(logging.WARNING, logger="stapel_recordings.vector.search"):
            hits = search_recordings("needle", mode="text", limit=10)

    assert [h.segment_id for h in hits] == ids
    assert all(not h.reranked for h in hits)
    assert any("llm.rerank call failed" in r.message for r in caplog.records)


def test_rerank_fail_open_on_unregistered_function(searchable_recording, caplog):
    """No llm.rerank provider wired at all → still fail-open, not a crash."""
    with override_settings(STAPEL_RECORDINGS=_rerank_settings()):
        with caplog.at_level(logging.WARNING, logger="stapel_recordings.vector.search"):
            hits = search_recordings("needle", mode="text", limit=10)
    assert len(hits) == 5
    assert all(not h.reranked for h in hits)


def test_rerank_fail_closed_raises(searchable_recording, stub_rerank):
    stub_rerank.result_override = {"status": "failure", "reason": "quota"}
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(FAIL_OPEN=False)):
        with pytest.raises(VectorSearchUnavailable, match="quota"):
            search_recordings("needle", mode="text", limit=10)

    stub_rerank.result_override = None
    stub_rerank.error = RuntimeError("provider down")
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(FAIL_OPEN=False)):
        with pytest.raises(VectorSearchUnavailable, match="call failed"):
            search_recordings("needle", mode="text", limit=10)


def test_rerank_malformed_response_routes_through_fail_open(
    searchable_recording, stub_rerank, caplog
):
    """An out-of-range index is a provider bug, not a search outage."""
    stub_rerank.result_override = {
        "status": "ok",
        "rerank": {"provider": "stub", "model": "m",
                   "results": [{"index": 99, "score": 1.0}]},
        "provider_used": "stub",
    }
    with override_settings(STAPEL_RECORDINGS=_rerank_settings()):
        with caplog.at_level(logging.WARNING, logger="stapel_recordings.vector.search"):
            hits = search_recordings("needle", mode="text", limit=10)
    assert len(hits) == 5 and all(not h.reranked for h in hits)
    assert any("out of range" in r.message for r in caplog.records)


# ─── Hybrid placement (post-fusion) + disabled byte-identity ───────────


def test_rerank_disabled_hybrid_is_byte_identical_to_rrf(
    searchable_recording, hybrid_on_sqlite, stub_rerank
):
    """RERANK off (the default): hybrid results are exactly the RRF
    fusion of the two arms — pre-computed here — and llm.rerank is never
    called."""
    ids = _segment_ids(searchable_recording)
    text_ids = ids  # sqlite icontains arm: sequence order
    vector_ids = [h.segment_id for h in hybrid_on_sqlite]  # c, a, b
    fused = reciprocal_rank_fusion(
        {"text": text_ids, "vector": vector_ids},
        k=60, weights={"text": 1.0, "vector": 1.0},
    )
    snippets = {ids[i]: f"needle {w}"
                for i, w in enumerate(["alpha", "bravo", "charlie", "delta", "echo"])}
    expected = [
        SearchHit(seg_id, searchable_recording.id, score, snippets[seg_id])
        for seg_id, score in fused[:4]
    ]

    hits = search_recordings("needle", mode="hybrid", limit=4)
    assert hits == expected
    assert stub_rerank.calls == []


def test_rerank_applies_post_fusion_in_hybrid(
    searchable_recording, hybrid_on_sqlite, stub_rerank
):
    """The reranker reorders the *fused* top-K; below-K fused order is
    preserved after the block."""
    ids = _segment_ids(searchable_recording)
    with override_settings(STAPEL_RECORDINGS=_rerank_settings(TOP_K=2, TOP_N=0)):
        baseline = search_recordings("needle", mode="hybrid", limit=10)
        fused_order = [h.segment_id for h in baseline]  # stub default: no reorder
        stub_rerank.scores = {0: 0.1, 1: 0.9}  # flip the fused top-2
        hits = search_recordings("needle", mode="hybrid", limit=10)

    assert set(fused_order) == set(ids)
    assert [h.segment_id for h in hits] == (
        [fused_order[1], fused_order[0]] + fused_order[2:]
    )
    assert [h.reranked for h in hits[:2]] == [True, True]
    assert all(not h.reranked for h in hits[2:])


# ─── Config plumbing ───────────────────────────────────────────────────


def test_vector_config_deep_merges_rerank_block():
    assert vector_config()["RERANK"] == {
        "ENABLED": False, "PROVIDER": "", "TOP_K": 50, "TOP_N": 20,
        "TIMEOUT_SECONDS": 60, "FAIL_OPEN": True,
    }
    with override_settings(STAPEL_RECORDINGS={"VECTOR": {"RERANK": {"ENABLED": True}}}):
        merged = vector_config()["RERANK"]
    assert merged["ENABLED"] is True
    assert merged["TOP_K"] == 50  # untouched knobs keep their defaults
    assert merged["FAIL_OPEN"] is True
