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
  **Model isolation**: the candidate set is filtered to rows whose
  ``model`` equals the model that embedded THIS query (as reported by
  ``llm.embed``, which is exactly what the embed stage stamps on each
  row). Two models produce two incomparable spaces of the same width, so
  without the filter a changed embedder silently degrades ranking to
  noise instead of returning nothing. Rows written under an older model
  simply stop matching — re-embed them with the ``recordings_reembed``
  management command (``VECTOR["SEARCH_MODEL_FILTER"] = False`` opts out,
  e.g. to keep serving during a migration).
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
    descending, ties broken by key repr for determinism.

    A key repeated WITHIN one arm's list counts once, at its best rank.
    RRF is defined over a ranked list of distinct documents, and summing a
    repeat is not a small error: two appearances at ranks 20 and 21 sum to
    more than a single appearance at rank 1, so one duplicated key
    leapfrogs the whole result. An arm can produce repeats honestly — the
    vector arm over multi-utterance windows keys hits by their anchor
    segment, and one long utterance can anchor a dozen windows."""
    weights = weights or {}
    scores: dict = {}
    for arm, keys in rankings.items():
        weight = float(weights.get(arm, 1.0))
        if weight == 0.0:
            continue
        seen: set = set()
        for rank, key in enumerate(keys, start=1):
            if key in seen:
                continue
            seen.add(key)
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
    user_id=None,
) -> list[SearchHit]:
    """Search segments; see the module docstring for mode semantics.

    Scope narrows by ``workspace_id`` and/or an iterable of
    ``recording_ids`` (both optional). Returns at most *limit* hits,
    best first.

    With ``VECTOR["RERANK"]["ENABLED"]`` the ranked candidates are
    additionally passed through ``llm.rerank`` before truncation (any
    mode; see the module docstring for ordering, failure and privacy
    semantics — segment texts go to the rerank provider).

    ``user_id`` attributes the billable calls a search makes — the query
    embedding on the vector arm, and the rerank pass when enabled. Neither
    is free, and a search is the one AI call in this package that a live
    human triggers directly, so it is also the one with an obvious subject
    to charge. Recorded only; nothing here gates on it. It pairs with
    ``workspace_id``, which the signature already takes."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    query = (query or "").strip()
    if not query or limit <= 0:
        return []

    from ..conf import vector_config

    cfg = vector_config()
    limit = int(limit)

    from ..stages import identity_fields

    identity = identity_fields(user_id, workspace_id)

    if mode == "text":
        hits = _text_arm(query, workspace_id, recording_ids, _candidate_limit(limit, cfg), cfg)
        return _apply_rerank(hits, query, cfg, identity=identity)[:limit]

    _require_vector_search()
    if mode == "vector":
        hits = _vector_arm(
            query, workspace_id, recording_ids, _candidate_limit(limit, cfg), cfg,
            identity=identity,
        )
        return _apply_rerank(hits, query, cfg, identity=identity)[:limit]

    # hybrid
    arm_limit = max(_candidate_limit(limit, cfg), int(cfg["ARM_LIMIT"]))
    text_hits = _text_arm(query, workspace_id, recording_ids, arm_limit, cfg)
    vector_hits = _vector_arm(
        query, workspace_id, recording_ids, arm_limit, cfg, identity=identity
    )
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
    return _apply_rerank(hits, query, cfg, identity=identity)[:limit]


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


def _apply_rerank(
    hits: list[SearchHit], query: str, cfg: dict, *, identity: dict | None = None
) -> list[SearchHit]:
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
        **(identity or {}),
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
    """The Postgres text-search config for a recording's language tag.

    The tag is normalized first (:func:`..languages.to_iso639_1`): STT
    providers report ISO 639-2/3 (``rus``, ``eng``, ``spa``) while
    ``FTS_CONFIGS`` is keyed on ISO 639-1, and an unreconciled mismatch is
    silent — it just quietly buys ``simple`` (no stemming, no stopwords)
    for a language Postgres has a real dictionary for."""
    from ..languages import to_iso639_1

    return cfg["FTS_CONFIGS"].get(
        to_iso639_1(language), cfg["FTS_FALLBACK_CONFIG"]
    )


def _build_search_query(query: str, config: str, cfg: dict):
    """The tsquery for *query* under ``FTS_SEARCH_TYPE`` (see conf.py).

    ``"any"`` OR-combines the terms so a question-shaped query can match at
    all; the other two are Postgres' own ``plainto_tsquery`` /
    ``websearch_to_tsquery``, both of which AND."""
    from django.contrib.postgres.search import SearchQuery

    search_type = str(cfg.get("FTS_SEARCH_TYPE") or "plain").lower()
    if search_type == "websearch":
        return SearchQuery(query, config=config, search_type="websearch")
    if search_type == "any":
        terms = query.split()
        combined = None
        for term in terms:
            one = SearchQuery(term, config=config)
            combined = one if combined is None else (combined | one)
        if combined is not None:
            return combined
    return SearchQuery(query, config=config)


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

    from django.contrib.postgres.search import SearchRank, SearchVector

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
        sq = _build_search_query(query, config, cfg)
        vector = SearchVector("text", config=config)
        rows = (
            qs.filter(recording_id__in=rec_ids)
            .annotate(search=vector, rank=SearchRank(vector, sq))
            # `tsvector @@ tsquery` — the ONLY thing that decides whether a
            # row matches. ts_rank is a ranking function, not a predicate:
            # PostgreSQL's calc_rank_and starts at -1.0 and calc_rank
            # clamps a negative result to 1e-20, so a query of two or more
            # terms that matches nothing scores 1e-20 (> 0) on every row in
            # the table. `rank__gt=0.0` therefore filtered NOTHING and this
            # arm handed the fusion the entire corpus in accidental order.
            # A larger threshold would be the same bug with a bigger
            # constant — it would also start cutting genuine faint matches.
            .filter(search=sq)
            .order_by("-rank", "id")[:limit]
        )
        hits.extend(
            SearchHit(row.id, row.recording_id, float(row.rank), make_snippet(row.text, query))
            for row in rows
        )
    hits.sort(key=lambda h: (-h.score, str(h.segment_id)))
    return hits[:limit]


def _pgvector_version() -> tuple[int, int]:
    """``(major, minor)`` of the installed pgvector, ``(0, 0)`` if absent.

    Cached per connection alias: it cannot change under a running process,
    and the alternative is a catalog round-trip on every search."""
    from django.db import connection

    cached = getattr(connection, "_stapel_pgvector_version", None)
    if cached is not None:
        return cached
    version = (0, 0)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            row = cur.fetchone()
        if row and row[0]:
            parts = str(row[0]).split(".")
            version = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception as exc:  # pragma: no cover - catalog unavailable
        logger.debug("could not read the pgvector version: %s", exc)
    connection._stapel_pgvector_version = version
    return version


def _tenant_share(qs) -> float:
    """The scoped candidate set's share of the whole embedding table.

    Only consulted on the pre-0.8 fallback path. The denominator is the
    planner's own estimate (``pg_class.reltuples``) rather than a
    ``COUNT(*)`` — this is a scaling factor, not an accounting figure, and
    it must not cost a full scan of the table to compute."""
    from django.db import connection

    from .models import SegmentEmbedding

    eligible = qs.count()
    if eligible <= 0:
        return 1.0
    total = 0.0
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT reltuples FROM pg_class WHERE oid = %s::regclass",
                [SegmentEmbedding._meta.db_table],
            )
            row = cur.fetchone()
        total = float(row[0]) if row and row[0] and row[0] > 0 else 0.0
    except Exception as exc:  # pragma: no cover
        logger.debug("could not estimate the embedding table size: %s", exc)
    if total <= 0:
        return 1.0
    return min(1.0, max(eligible / total, 1e-4))


def _scan_settings(cfg: dict, limit: int, scoped: bool, qs) -> dict:
    """The pgvector GUCs to apply for one vector-arm query.

    An HNSW scan and a tenant predicate do not compose. The index knows
    nothing about ``workspace_id``, so the predicate runs on what the scan
    already returned; with the default ``hnsw.ef_search`` a workspace
    holding a few percent of the corpus has nearly all of its candidates
    thrown away AFTER the fact, and the arm returns a handful of rows (or
    none) for a query with hundreds of eligible ones. It does not error —
    it just quietly retrieves less, which is why this survived to
    production.

    Two mechanisms, chosen by what the server actually has:

    - **pgvector >= 0.8** — ``hnsw.iterative_scan``: the scan resumes until
      the LIMIT is satisfied or ``hnsw.max_scan_tuples`` is spent. This is
      the mechanism built for exactly this problem, and it is bounded.
    - **older** — widen ``hnsw.ef_search`` by the tenant's measured share
      of the corpus, capped at ``EF_SEARCH_MAX``. A blunter instrument (a
      wider scan costs on every query, and a tenant small enough still
      loses), but strictly better than the default.
    """
    hnsw = cfg["HNSW"]
    ef_max = max(1, int(hnsw.get("EF_SEARCH_MAX") or 1000))
    pinned = int(hnsw.get("EF_SEARCH") or 0)
    iterative = str(hnsw.get("ITERATIVE_SCAN") or "").strip().lower()
    supports_iterative = _pgvector_version() >= (0, 8)

    settings: dict = {}
    if pinned > 0:
        ef_search = pinned
    else:
        # Enough to fill the fetch window with room to spare; the server
        # default (40) is the floor.
        ef_search = max(40, limit * 4)
        if scoped and not (supports_iterative and iterative):
            ef_search = int(ef_search / _tenant_share(qs))
    settings["hnsw.ef_search"] = str(min(ef_max, max(1, ef_search)))

    if supports_iterative and iterative:
        settings["hnsw.iterative_scan"] = iterative
        max_tuples = int(hnsw.get("MAX_SCAN_TUPLES") or 0)
        if max_tuples > 0:
            settings["hnsw.max_scan_tuples"] = str(max_tuples)
    return settings


class _scan_tuning:
    """Apply pgvector GUCs for the enclosed query and put them back.

    ``SET LOCAL`` needs a transaction, and inside an existing one its scope
    is that whole transaction — not this block — so the values are reset
    explicitly on the way out. A host that runs with ATOMIC_REQUESTS gets
    the same isolation as one that does not."""

    def __init__(self, settings: dict):
        self._settings = settings
        self._atomic = None

    def __enter__(self):
        if not self._settings:
            return self
        from django.db import connection, transaction

        self._atomic = transaction.atomic()
        self._atomic.__enter__()
        with connection.cursor() as cur:
            for key, value in self._settings.items():
                cur.execute(f"SET LOCAL {key} = %s", [value])
        return self

    def __exit__(self, *exc):
        if self._atomic is None:
            return False
        from django.db import connection

        try:
            if exc[0] is None:
                with connection.cursor() as cur:
                    for key in self._settings:
                        cur.execute(f"SET LOCAL {key} = DEFAULT")
        finally:
            self._atomic.__exit__(*exc)
        return False


def _vector_arm(
    query, workspace_id, recording_ids, limit, cfg, *, identity=None
) -> list[SearchHit]:
    from ..stages import StageError
    from .embedding import embed_texts

    try:
        query_model, vectors = embed_texts([query], cfg, identity=identity)
    except StageError as exc:
        raise VectorSearchUnavailable(f"query embedding failed: {exc}") from exc
    query_vector = vectors[0]

    from pgvector.django import CosineDistance

    from .models import SegmentEmbedding

    qs = SegmentEmbedding.objects.select_related("segment")
    if cfg["SEARCH_MODEL_FILTER"]:
        # Cosine distance between vectors from DIFFERENT models is
        # meaningless (they are different spaces of the same width), so
        # the ANN candidate set is restricted to rows stamped with the
        # model that just embedded THIS query — the same string the embed
        # stage stamps, since both take it from the llm.embed response.
        qs = qs.filter(model=query_model)
    # Same argument one level up: a "segment" row and a "window" row are
    # different UNITS, and their scores are not comparable rankings of the
    # same thing. The arm searches exactly one scheme — which is what makes
    # re-embedding under a new scheme a background job and the switch
    # itself one setting, in either direction.
    qs = qs.filter(scheme=str(cfg.get("SEGMENT_SCHEME") or "segment"))
    scoped = False
    if workspace_id is not None:
        qs = qs.filter(segment__recording__workspace_id=workspace_id)
        scoped = True
    if recording_ids is not None:
        qs = qs.filter(segment__recording_id__in=list(recording_ids))
        scoped = True

    ranked = qs.annotate(distance=CosineDistance("vector", query_vector)).order_by(
        "distance"
    )[:limit]
    with _scan_tuning(_scan_settings(cfg, limit, scoped, qs)):
        rows = list(ranked)
    # `relaxed_order` returns near-neighbours slightly out of order; the
    # distance is on every row, so the arm's own ranking is exact
    # regardless of what order the index handed them back in.
    rows.sort(key=lambda r: (float(r.distance), str(r.segment_id)))
    # One hit per segment. A hit IS a segment id to everything downstream —
    # the fusion key, the QA citation, the host's own DTO — so two rows
    # anchored at the same segment are one result, and the nearer wins.
    # (Windows are anchored at their first utterance; one utterance long
    # enough to fill several windows anchors all of them.)
    hits: list[SearchHit] = []
    seen: set = set()
    for row in rows:
        if row.segment_id in seen:
            continue
        seen.add(row.segment_id)
        hits.append(
            SearchHit(
                row.segment_id,
                row.segment.recording_id,
                1.0 - float(row.distance),
                make_snippet(row.text or row.segment.text, query),
            )
        )
    return hits


__all__ = [
    "MODES",
    "SearchHit",
    "VectorSearchUnavailable",
    "reciprocal_rank_fusion",
    "make_snippet",
    "search_recordings",
]
