"""Hybrid segment search over recordings — text, vector, or fused.

``search_recordings(query, mode=...)`` returns segment hits (segment id,
recording id, score, snippet). Three modes:

- ``"text"`` — Postgres full-text search over ``Segment.text``. The FTS
  language config is resolved **per recording's ``language`` field** via
  ``VECTOR["FTS_CONFIGS"]`` (primary subtag, e.g. ``"de-CH" -> "german"``),
  falling back to ``VECTOR["FTS_FALLBACK_CONFIG"]`` (``'simple'``).
  Off postgres this arm degrades to a plain ``icontains`` filter (uniform
  score 1.0) — usable everywhere, ranked nowhere.
- ``"vector"`` — the query is embedded via ``llm.embed`` and matched by
  cosine distance over ``SegmentEmbedding`` (score = 1 - distance).
  Requires postgres + the vector app; otherwise raises
  :class:`VectorSearchUnavailable` — hosts decide how to degrade.
- ``"hybrid"`` (default) — both arms fetch up to ``VECTOR["ARM_LIMIT"]``
  candidates each and are fused with **reciprocal-rank fusion**:

      score(seg) = Σ over arms  WEIGHT_arm / (RRF_K + rank_arm(seg))

  (rank is 1-based within the arm; a segment absent from an arm simply
  contributes nothing for it). RRF is deliberately the simplest robust
  fusion — rank-based, so the two arms' incomparable score scales never
  need calibrating; the knobs are ``VECTOR["RRF_K"]`` (higher = flatter,
  60 is the literature default) and ``VECTOR["RRF_WEIGHTS"]``
  (``{"text": w, "vector": w}`` — bias one arm without touching code).
  Same availability requirements as ``"vector"``.

**Optional rerank** (``VECTOR["RERANK"]``, default off): one code path
applied post-fusion / post-text-ranking, in **every** mode — reranking is
provider-agnostic result quality, so the top ``TOP_K`` candidates go to
the reranker regardless of which arm produced them. The ``TOP_K`` best
hits' **full segment texts** (not the trimmed snippets) are sent to the
``llm.rerank`` comm Function (stapel-agent >= 0.5) and that block is
re-ordered by rerank score; ``TOP_N`` is forwarded to the provider (score
only the N best; ``0`` scores everything). Ordering semantics: reranked
hits first (provider order, best first), then hits the reranker did not
score (sent but cut by ``TOP_N``) in their pre-rerank order, then hits
beyond ``TOP_K`` in their pre-rerank order; the result is truncated to
``limit`` as before. When rerank is enabled the arms over-fetch to
``TOP_K`` so the reranker sees a full candidate window.

Score semantics with rerank: a reranked hit's ``SearchHit.score`` is
**replaced** by the provider's rerank score and ``reranked=True``;
un-reranked hits keep their RRF/arm score and ``reranked=False`` — the
two scales are not comparable across that boundary, the list order is the
contract.

Failure semantics: ``RERANK["FAIL_OPEN"]`` (default True) → any rerank
failure (comm error, failure envelope, malformed response) logs a warning
and returns the un-reranked order — search must not die because the
reranker hiccuped; ``False`` → :class:`VectorSearchUnavailable`.

Privacy: with rerank enabled, segment texts DO go to the rerank provider.
This is the same trust boundary as ``llm.transcribe``/``llm.summarize`` —
the transcript already transits the agent seam.

The module imports no Django models at import time, so it is importable —
and its pure pieces (:func:`reciprocal_rank_fusion`, :func:`make_snippet`)
unit-testable — without postgres, pgvector, or the vector app.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

MODES = ("text", "vector", "hybrid")


class VectorSearchUnavailable(RuntimeError):
    """vector/hybrid search requires PostgreSQL, the ``[vector]`` extra and
    the ``stapel_recordings.vector`` app in INSTALLED_APPS."""


@dataclass(frozen=True)
class SearchHit:
    """One search result. ``score`` is the producing ranker's score (FTS
    rank / cosine similarity / RRF fused score) — unless ``reranked`` is
    True, in which case it is the ``llm.rerank`` provider's score (the
    scales are not mutually comparable; the list order is the contract)."""

    segment_id: object
    recording_id: object
    score: float
    snippet: str
    reranked: bool = False


# ─── Pure pieces ───────────────────────────────────────────────────────


def reciprocal_rank_fusion(
    rankings: dict[str, list], *, k: int = 60, weights: dict[str, float] | None = None
) -> list[tuple[object, float]]:
    """Fuse per-arm ranked key lists into ``[(key, fused_score), ...]``.

    ``rankings`` maps arm name -> keys in rank order (best first).
    ``score(key) = Σ_arm weight_arm / (k + rank_arm)`` over the arms that
    ranked the key (1-based rank). Result is sorted by fused score
    descending, ties broken by key repr for determinism."""
    weights = weights or {}
    scores: dict = {}
    for arm, keys in rankings.items():
        weight = float(weights.get(arm, 1.0))
        if weight == 0.0:
            continue
        for rank, key in enumerate(keys, start=1):
            scores[key] = scores.get(key, 0.0) + weight / (float(k) + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


def make_snippet(text: str, query: str, *, width: int = 160) -> str:
    """A window of *text* around the first query-term hit (whitespace
    normalized); plain head of the text when no term matches."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    low = text.lower()
    pos = -1
    for term in query.lower().split():
        pos = low.find(term)
        if pos != -1:
            break
    if pos == -1:
        return text[:width].rstrip() + "…"
    start = max(0, pos - width // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


# ─── Service ───────────────────────────────────────────────────────────


def search_recordings(
    query: str,
    *,
    workspace_id=None,
    recording_ids=None,
    mode: str = "hybrid",
    limit: int = 20,
) -> list[SearchHit]:
    """Search segments; see the module docstring for mode semantics.

    Scope narrows by ``workspace_id`` and/or an iterable of
    ``recording_ids`` (both optional). Returns at most *limit* hits,
    best first.

    With ``VECTOR["RERANK"]["ENABLED"]`` the ranked candidates are
    additionally passed through ``llm.rerank`` before truncation (any
    mode; see the module docstring for ordering, failure and privacy
    semantics — segment texts go to the rerank provider)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    query = (query or "").strip()
    if not query or limit <= 0:
        return []

    from ..conf import vector_config

    cfg = vector_config()
    limit = int(limit)

    if mode == "text":
        hits = _text_arm(query, workspace_id, recording_ids, _candidate_limit(limit, cfg), cfg)
        return _apply_rerank(hits, query, cfg)[:limit]

    _require_vector_search()
    if mode == "vector":
        hits = _vector_arm(query, workspace_id, recording_ids, _candidate_limit(limit, cfg), cfg)
        return _apply_rerank(hits, query, cfg)[:limit]

    # hybrid
    arm_limit = max(_candidate_limit(limit, cfg), int(cfg["ARM_LIMIT"]))
    text_hits = _text_arm(query, workspace_id, recording_ids, arm_limit, cfg)
    vector_hits = _vector_arm(query, workspace_id, recording_ids, arm_limit, cfg)
    by_id = {}
    for hit in vector_hits:
        by_id[hit.segment_id] = hit
    for hit in text_hits:  # text-arm snippet (exact-match window) wins
        by_id[hit.segment_id] = hit
    fused = reciprocal_rank_fusion(
        {
            "text": [h.segment_id for h in text_hits],
            "vector": [h.segment_id for h in vector_hits],
        },
        k=int(cfg["RRF_K"]),
        weights=cfg["RRF_WEIGHTS"],
    )
    hits = [
        SearchHit(
            segment_id=seg_id,
            recording_id=by_id[seg_id].recording_id,
            score=score,
            snippet=by_id[seg_id].snippet,
        )
        for seg_id, score in fused
    ]
    return _apply_rerank(hits, query, cfg)[:limit]


# ─── Rerank stage (one path, post-fusion / post-text-ranking) ──────────


class _BadRerankResponse(Exception):
    """Internal: the llm.rerank response was a failure envelope or
    structurally unusable — routed through the FAIL_OPEN policy."""


def _candidate_limit(limit: int, cfg: dict) -> int:
    """Arm fetch size: with rerank enabled the arms over-fetch to
    ``RERANK["TOP_K"]`` so the reranker sees a full candidate window."""
    rerank_cfg = cfg["RERANK"]
    if rerank_cfg.get("ENABLED"):
        return max(limit, int(rerank_cfg["TOP_K"]))
    return limit


def _apply_rerank(hits: list[SearchHit], query: str, cfg: dict) -> list[SearchHit]:
    """Re-order the top ``RERANK["TOP_K"]`` of *hits* via ``llm.rerank``.

    No-op when disabled or on an empty list. Documents are the hits' full
    segment texts (fetched by id — the stored snippet is a trimmed window
    and would starve the reranker). See the module docstring for the
    ordering / failure / privacy contract."""
    rerank_cfg = cfg["RERANK"]
    if not rerank_cfg.get("ENABLED") or not hits:
        return hits

    top_k = max(1, int(rerank_cfg["TOP_K"]))
    head, tail = hits[:top_k], hits[top_k:]

    from stapel_recordings.models import Segment

    texts = dict(
        Segment.objects.filter(
            id__in=[h.segment_id for h in head]
        ).values_list("id", "text")
    )
    payload: dict = {
        "query": query,
        # Full segment text; the snippet is the (unlikely) fallback for a
        # segment deleted between ranking and rerank.
        "documents": [texts.get(h.segment_id) or h.snippet for h in head],
        "timeout_seconds": int(rerank_cfg["TIMEOUT_SECONDS"]),
    }
    top_n = int(rerank_cfg.get("TOP_N") or 0)
    if top_n > 0:
        payload["top_n"] = min(top_n, len(head))
    if rerank_cfg.get("PROVIDER"):
        payload["provider"] = rerank_cfg["PROVIDER"]

    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    try:
        result = call("llm.rerank", payload)
        order = _rerank_order(result, len(head))
    except CommError as exc:
        return _rerank_failed(hits, rerank_cfg, f"llm.rerank call failed: {exc}")
    except _BadRerankResponse as exc:
        return _rerank_failed(hits, rerank_cfg, str(exc))

    scored = {idx for idx, _ in order}
    reranked = [replace(head[idx], score=score, reranked=True) for idx, score in order]
    unscored = [h for idx, h in enumerate(head) if idx not in scored]
    return reranked + unscored + tail


def _rerank_order(result, n_docs: int) -> list[tuple[int, float]]:
    """Validate an llm.rerank envelope into ``[(index, score), ...]`` in
    provider order (contract: best first). Anything unusable raises
    :class:`_BadRerankResponse` for the FAIL_OPEN policy to route."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        reason = (
            result.get("reason", "rerank_failed")
            if isinstance(result, dict) else "rerank_failed"
        )
        raise _BadRerankResponse(f"llm.rerank returned failure: {reason}")
    results = (result.get("rerank") or {}).get("results")
    if not isinstance(results, list):
        raise _BadRerankResponse("llm.rerank response missing rerank.results")
    order: list[tuple[int, float]] = []
    seen: set[int] = set()
    for entry in results:
        try:
            idx, score = int(entry["index"]), float(entry["score"])
        except (TypeError, KeyError, ValueError) as exc:
            raise _BadRerankResponse(
                f"malformed rerank result entry: {entry!r}"
            ) from exc
        if not 0 <= idx < n_docs or idx in seen:
            raise _BadRerankResponse(
                f"rerank result index {idx} out of range or duplicated"
            )
        seen.add(idx)
        order.append((idx, score))
    return order


def _rerank_failed(
    hits: list[SearchHit], rerank_cfg: dict, message: str
) -> list[SearchHit]:
    """FAIL_OPEN policy: warn + return the un-reranked order, or raise."""
    if rerank_cfg.get("FAIL_OPEN", True):
        logger.warning(
            "rerank failed — returning un-reranked order (RERANK['FAIL_OPEN']): %s",
            message,
        )
        return hits
    raise VectorSearchUnavailable(
        f"rerank failed and RERANK['FAIL_OPEN'] is False: {message}"
    )


def _require_vector_search() -> None:
    from django.db import connection

    from . import vector_app_installed

    if not vector_app_installed():
        raise VectorSearchUnavailable(
            "vector search needs the opt-in vector app: pip install "
            "stapel-recordings[vector] and add 'stapel_recordings.vector' "
            "to INSTALLED_APPS (then migrate)."
        )
    if connection.vendor != "postgresql":
        raise VectorSearchUnavailable(
            "vector search requires PostgreSQL with the pgvector extension; "
            f"the default connection vendor is {connection.vendor!r}."
        )


def _scoped_segments(workspace_id, recording_ids):
    from stapel_recordings.models import Segment

    qs = Segment.objects.exclude(text="")
    if workspace_id is not None:
        qs = qs.filter(recording__workspace_id=workspace_id)
    if recording_ids is not None:
        qs = qs.filter(recording_id__in=list(recording_ids))
    return qs


def _fts_config(language: str | None, cfg: dict) -> str:
    primary = (language or "").split("-")[0].strip().lower()
    return cfg["FTS_CONFIGS"].get(primary, cfg["FTS_FALLBACK_CONFIG"])


def _text_arm(query, workspace_id, recording_ids, limit, cfg) -> list[SearchHit]:
    from django.db import connection

    qs = _scoped_segments(workspace_id, recording_ids)

    if connection.vendor != "postgresql":
        # Degraded text arm: substring match, uniform score, stable order.
        rows = qs.filter(text__icontains=query).order_by(
            "recording_id", "sequence_num"
        )[:limit]
        return [
            SearchHit(row.id, row.recording_id, 1.0, make_snippet(row.text, query))
            for row in rows
        ]

    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

    from stapel_recordings.models import Recording

    # Group candidate recordings by the FTS config their language maps to,
    # run one ranked query per config, merge. (No stored search column —
    # hosts wanting scale add their own GIN index / SearchVectorField.)
    rec_qs = Recording.objects.all()
    if workspace_id is not None:
        rec_qs = rec_qs.filter(workspace_id=workspace_id)
    if recording_ids is not None:
        rec_qs = rec_qs.filter(id__in=list(recording_ids))
    by_config: dict[str, list] = {}
    for rec_id, language in rec_qs.values_list("id", "language"):
        by_config.setdefault(_fts_config(language, cfg), []).append(rec_id)

    hits: list[SearchHit] = []
    for config, rec_ids in by_config.items():
        sq = SearchQuery(query, config=config)
        rows = (
            qs.filter(recording_id__in=rec_ids)
            .annotate(rank=SearchRank(SearchVector("text", config=config), sq))
            .filter(rank__gt=0.0)
            .order_by("-rank")[:limit]
        )
        hits.extend(
            SearchHit(row.id, row.recording_id, float(row.rank), make_snippet(row.text, query))
            for row in rows
        )
    hits.sort(key=lambda h: (-h.score, str(h.segment_id)))
    return hits[:limit]


def _vector_arm(query, workspace_id, recording_ids, limit, cfg) -> list[SearchHit]:
    from ..stages import StageError
    from .embedding import embed_texts

    try:
        _model, vectors = embed_texts([query], cfg)
    except StageError as exc:
        raise VectorSearchUnavailable(f"query embedding failed: {exc}") from exc
    query_vector = vectors[0]

    from pgvector.django import CosineDistance

    from .models import SegmentEmbedding

    qs = SegmentEmbedding.objects.select_related("segment")
    if cfg.get("MODEL"):
        qs = qs.filter(model=cfg["MODEL"])
    if workspace_id is not None:
        qs = qs.filter(segment__recording__workspace_id=workspace_id)
    if recording_ids is not None:
        qs = qs.filter(segment__recording_id__in=list(recording_ids))
    rows = qs.annotate(distance=CosineDistance("vector", query_vector)).order_by(
        "distance"
    )[:limit]
    return [
        SearchHit(
            row.segment_id,
            row.segment.recording_id,
            1.0 - float(row.distance),
            make_snippet(row.segment.text, query),
        )
        for row in rows
    ]


__all__ = [
    "MODES",
    "SearchHit",
    "VectorSearchUnavailable",
    "reciprocal_rank_fusion",
    "make_snippet",
    "search_recordings",
]
