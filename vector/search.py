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

The module imports no Django models at import time, so it is importable —
and its pure pieces (:func:`reciprocal_rank_fusion`, :func:`make_snippet`)
unit-testable — without postgres, pgvector, or the vector app.
"""
from __future__ import annotations

from dataclasses import dataclass

MODES = ("text", "vector", "hybrid")


class VectorSearchUnavailable(RuntimeError):
    """vector/hybrid search requires PostgreSQL, the ``[vector]`` extra and
    the ``stapel_recordings.vector`` app in INSTALLED_APPS."""


@dataclass(frozen=True)
class SearchHit:
    segment_id: object
    recording_id: object
    score: float
    snippet: str


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
    best first."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    query = (query or "").strip()
    if not query or limit <= 0:
        return []

    from ..conf import vector_config

    cfg = vector_config()

    if mode == "text":
        return _text_arm(query, workspace_id, recording_ids, int(limit), cfg)

    _require_vector_search()
    if mode == "vector":
        return _vector_arm(query, workspace_id, recording_ids, int(limit), cfg)

    # hybrid
    arm_limit = max(int(limit), int(cfg["ARM_LIMIT"]))
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
    return [
        SearchHit(
            segment_id=seg_id,
            recording_id=by_id[seg_id].recording_id,
            score=score,
            snippet=by_id[seg_id].snippet,
        )
        for seg_id, score in fused[: int(limit)]
    ]


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
