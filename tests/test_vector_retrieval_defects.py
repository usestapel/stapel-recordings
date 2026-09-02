"""Regression tests for the four retrieval defects found by the 2026-09-02
launch audit of a live deployment. Each test was written RED — it failed
against the code as shipped in 0.20.2 — and names the defect it pins.

1. ``ts_rank`` is not a match predicate. PostgreSQL's ``calc_rank_and``
   starts at ``-1.0`` and ``calc_rank`` clamps a negative result to
   ``1e-20``, so a two-or-more-term query that matches NOTHING scores
   ``1e-20 > 0`` on every row in the table. ``.filter(rank__gt=0.0)``
   therefore filtered nothing and the text arm returned the whole corpus,
   ranked by accident, taking ranks 1..N of the RRF fusion.
2. The tenant predicate is not reachable by the HNSW index, so with the
   default ``hnsw.ef_search`` a small workspace's rows are thrown away by
   the post-index filter and the vector arm under-returns (measured on the
   stand: 0 rows for a LIMIT 5 query over a workspace with 234 eligible
   rows).
3. Speech-to-text writes ISO 639-2/3 (``rus``, ``eng``, ``spa``, ``zho``)
   and ``FTS_CONFIGS`` is keyed on ISO 639-1, so real languages silently
   fell through to unstemmed ``simple``.
4. The embedded unit was one STT utterance (median 37 characters on the
   stand) — too small to carry an answer.

The pure ones run on the canonical sqlite suite; the DB-bound ones need
the postgres harness (``STAPEL_RECORDINGS_TEST_DB=postgres://…``) and skip
otherwise, same contract as ``tests/test_vector_postgres.py``.
"""
import uuid

import pytest
from django.db import connection
from django.test import override_settings

from stapel_recordings.conf import vector_config

# ─── 3. language normalization (pure) ──────────────────────────────────


def test_iso639_2_language_tags_resolve_to_a_stemmed_fts_config():
    """STT emits ISO 639-2/3; the config map is keyed on ISO 639-1."""
    from stapel_recordings.vector.search import _fts_config

    cfg = vector_config()
    # Terminological (T) codes — what most STT providers emit.
    assert _fts_config("rus", cfg) == "russian"
    assert _fts_config("eng", cfg) == "english"
    assert _fts_config("spa", cfg) == "spanish"
    assert _fts_config("fra", cfg) == "french"
    assert _fts_config("nld", cfg) == "dutch"
    assert _fts_config("por", cfg) == "portuguese"
    assert _fts_config("deu", cfg) == "german"
    assert _fts_config("ita", cfg) == "italian"
    # Bibliographic (B) codes — the other half of ISO 639-2.
    assert _fts_config("ger", cfg) == "german"
    assert _fts_config("fre", cfg) == "french"
    assert _fts_config("dut", cfg) == "dutch"


def test_language_normalization_is_not_a_hand_typed_shortlist():
    """The mapping is the real ISO 639-2 table, not the codes someone
    happened to see in one production database."""
    from stapel_recordings.languages import ISO639_2_TO_1, to_iso639_1

    # The whole ISO 639-2 set that has a 639-1 equivalent: 184 languages
    # plus the 20 bibliographic aliases (ger/deu, fre/fra, chi/zho, …).
    assert len(ISO639_2_TO_1) == 204
    # Languages with no Postgres FTS config still normalize correctly —
    # normalization and config lookup are different questions.
    assert to_iso639_1("zho") == "zh"
    assert to_iso639_1("kor") == "ko"
    assert to_iso639_1("hin") == "hi"
    assert to_iso639_1("tha") == "th"
    assert to_iso639_1("msa") == "ms"
    assert to_iso639_1("nep") == "ne"


def test_language_normalization_keeps_the_existing_contract():
    """Two-letter tags, regional subtags, casing and junk behave as before."""
    from stapel_recordings.languages import to_iso639_1

    assert to_iso639_1("en") == "en"
    assert to_iso639_1("de-CH") == "de"
    assert to_iso639_1("RUS") == "ru"
    assert to_iso639_1("rus-RU") == "ru"
    assert to_iso639_1("") == ""
    assert to_iso639_1(None) == ""
    assert to_iso639_1("qqq") == "qqq"  # unknown: unchanged, falls back


def test_unmapped_language_still_falls_back_to_the_configured_default():
    from stapel_recordings.vector.search import _fts_config

    cfg = vector_config()
    assert _fts_config("zho", cfg) == cfg["FTS_FALLBACK_CONFIG"]
    assert _fts_config(None, cfg) == cfg["FTS_FALLBACK_CONFIG"]


# ─── 4. chunking (pure) ────────────────────────────────────────────────


def test_segment_windows_group_utterances_into_answer_sized_units():
    """One STT utterance is a 37-character median — too small to answer
    from. Windows group consecutive utterances up to a target size."""
    from stapel_recordings.vector.chunking import Utterance, build_windows

    utterances = [
        Utterance(
            id=uuid.uuid4(), sequence_num=i, start_time=float(i * 3),
            end_time=float(i * 3 + 3), speaker="Alice" if i % 2 else "Bob",
            text=f"utterance number {i} " + "padding " * 5,
        )
        for i in range(40)
    ]
    windows = build_windows(utterances, target_chars=600, max_chars=800)

    assert windows, "no windows built"
    for w in windows:
        assert len(w.text) <= 800
        assert w.anchor_id == w.utterance_ids[0]
        assert w.start_time <= w.end_time
    # Every utterance lands in exactly one window, in order, none lost.
    covered = [uid for w in windows for uid in w.utterance_ids]
    assert covered == [u.id for u in utterances]
    # Median window is answer-sized, not utterance-sized.
    assert min(len(w.text) for w in windows[:-1]) >= 400
    # Speaker and timestamps travel with the text.
    assert "Alice" in windows[0].text or "Bob" in windows[0].text
    assert "[" in windows[0].text


def test_a_single_huge_utterance_is_not_silently_dropped():
    from stapel_recordings.vector.chunking import Utterance, build_windows

    big = Utterance(
        id=uuid.uuid4(), sequence_num=0, start_time=0.0, end_time=90.0,
        speaker="Bob", text="x" * 5000,
    )
    windows = build_windows([big], target_chars=600, max_chars=800)
    assert windows
    # Split across windows rather than dropped or truncated: every window
    # is attributed to the one utterance, and the text is all there.
    assert {uid for w in windows for uid in w.utterance_ids} == {big.id}
    assert all(len(w.text) <= 800 for w in windows)
    assert sum(w.text.count("x") for w in windows) == 5000


def test_overlap_carries_whole_utterances_across_a_window_boundary():
    from stapel_recordings.vector.chunking import Utterance, build_windows

    utterances = [
        Utterance(
            id=i, sequence_num=i, start_time=float(i * 3), end_time=float(i * 3 + 3),
            speaker="Alice", text=f"utterance number {i} " + "padding " * 5,
        )
        for i in range(40)
    ]
    plain = build_windows(utterances, target_chars=600, max_chars=800)
    lapped = build_windows(
        utterances, target_chars=600, max_chars=800, overlap_chars=100
    )
    assert len(lapped) >= len(plain)  # overlap costs windows, never loses them
    for w in lapped:
        assert len(w.text) <= 800
    # Nothing lost, order preserved on first appearance.
    seen, order = set(), []
    for w in lapped:
        for uid in w.utterance_ids:
            if uid not in seen:
                seen.add(uid)
                order.append(uid)
    assert order == [u.id for u in utterances]
    # The boundary really is shared: some utterance appears in two windows.
    assert sum(len(w.utterance_ids) for w in lapped) > len(utterances)


def test_overlap_never_pushes_a_window_past_the_ceiling():
    """Found on real transcripts, not in theory: with a carried overlap in
    hand, a following utterance that on its own nearly fills a window
    produced windows of ``overlap + max_chars``. MAX_CHARS is a promise
    about the embedded unit — overlap is the thing that yields."""
    from stapel_recordings.vector.chunking import Utterance, build_windows

    utterances = [
        Utterance(
            id=i, sequence_num=i, start_time=float(i), end_time=float(i + 1),
            speaker="Alice",
            # Alternating tiny and near-window-sized utterances: the tiny
            # ones become the carry, the big ones arrive right after it.
            text=("short one" if i % 2 else "w " * 380),
        )
        for i in range(12)
    ]
    windows = build_windows(
        utterances, target_chars=600, max_chars=800, overlap_chars=100
    )
    assert windows
    assert max(len(w.text) for w in windows) <= 800
    # Still nothing lost.
    assert {u.id for u in utterances} <= {
        uid for w in windows for uid in w.utterance_ids
    }


def test_window_scheme_is_off_by_default():
    """The owner's standing rule: a behaviour change to the index ships
    switched off and stays off until the eval says otherwise."""
    cfg = vector_config()
    assert cfg["SEGMENT_SCHEME"] == "segment"


# ─── DB-bound: postgres harness only ───────────────────────────────────

pg_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="needs PostgreSQL + pgvector (STAPEL_RECORDINGS_TEST_DB=postgres://…)",
)

_VEC = {
    "VECTOR": {
        "ENABLED": True,
        "DIM": 3,
        "BATCH_SIZE": 64,
        "SUMMARY_CHUNK_CHARS": 0,
    }
}


@pytest.fixture
def _require_vector_app():
    from stapel_recordings.vector import vector_app_installed

    if not vector_app_installed():
        pytest.skip("stapel_recordings.vector not in INSTALLED_APPS")


@pytest.mark.django_db
@pg_only
def test_text_arm_returns_nothing_for_a_query_that_matches_nothing(
    _require_vector_app, make_recording,
):
    """Defect 1. Two terms, neither present: ``ts_rank`` scores every row
    ``1e-20`` and the old ``rank__gt=0.0`` let the whole corpus through."""
    from stapel_recordings.models import Segment
    from stapel_recordings.vector.search import search_recordings

    rec = make_recording(status="completed", language="en")
    for i, text in enumerate(
        ["alpha bravo topic", "charlie delta subject", "echo foxtrot theme"]
    ):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=text,
        )

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings("zulu quebec", mode="text", limit=50)
    assert hits == []


@pytest.mark.django_db
@pg_only
def test_text_arm_still_finds_a_real_match(_require_vector_app, make_recording):
    """The counterweight to the test above: the predicate must not be a
    threshold that also throws away genuine, low-scoring hits."""
    from stapel_recordings.models import Segment
    from stapel_recordings.vector.search import search_recordings

    rec = make_recording(status="completed", language="en")
    for i, text in enumerate(
        ["alpha bravo topic", "charlie delta subject", "echo foxtrot theme"]
    ):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=text,
        )

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings("delta", mode="text", limit=5)
    assert len(hits) == 1
    assert "delta" in hits[0].snippet


@pytest.mark.django_db
@pg_only
def test_garbage_text_hits_do_not_outrank_genuine_vector_hits(
    _require_vector_app, make_recording, request,
):
    """Defect 1, as the audit met it: three unrelated questions came back
    with the same five irrelevant segments at the top of a HYBRID search,
    because the text arm had handed fusion a full-length ranking of noise."""
    from stapel_core.comm import register_function

    from stapel_recordings.models import Segment
    from stapel_recordings.vector.embedding import embed_recording
    from stapel_recordings.vector.search import search_recordings

    def fake_embed(payload):
        vectors = [
            {"alpha": [1.0, 0.0, 0.0], "charlie": [0.0, 1.0, 0.0]}.get(
                t.split()[0].lower(), [0.0, 0.0, 1.0]
            )
            for t in payload["texts"]
        ]
        return {
            "status": "ok",
            "embeddings": {
                "provider": "stub", "model": "stub-embed-1",
                "dim": 3, "vectors": vectors,
            },
        }

    register_function("llm.embed", fake_embed)

    rec = make_recording(status="completed", language="en")
    texts = ["alpha bravo topic", "charlie delta subject"] + [
        f"noise segment {i} about unrelated matters" for i in range(20)
    ]
    for i, text in enumerate(texts):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=text,
        )
    with override_settings(STAPEL_RECORDINGS=_VEC):
        embed_recording(rec)
        hits = search_recordings("charlie zulu quebec", mode="hybrid", limit=5)

    assert hits
    # The vector arm's #1 (the "charlie" segment) must lead. Before the fix
    # the text arm's 1e-20 ranking of the noise segments won the fusion.
    assert "charlie" in hits[0].snippet


@pytest.mark.django_db
@pg_only
def test_vector_arm_reaches_a_small_tenant_behind_a_large_corpus(
    _require_vector_app, make_recording,
):
    """Defect 2. The workspace predicate is applied AFTER the HNSW scan,
    so a tenant holding a few percent of the rows loses most of its
    candidates to the post-index filter unless the scan is told to keep
    going."""
    from stapel_core.comm import register_function

    from stapel_recordings.models import Segment
    from stapel_recordings.vector.models import SegmentEmbedding
    from stapel_recordings.vector.search import search_recordings

    def fake_embed(payload):
        return {
            "status": "ok",
            "embeddings": {
                "provider": "stub", "model": "stub-embed-1", "dim": 3,
                "vectors": [[0.0, 0.0, 1.0] for _ in payload["texts"]],
            },
        }

    register_function("llm.embed", fake_embed)

    import math
    import random

    rng = random.Random(20260902)

    def unit():
        v = [rng.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    # A big neighbour workspace and a small one — the stand's shape (the
    # small tenant held 8% of the rows).
    big = make_recording(status="completed", language="en")
    small = make_recording(status="completed", language="en")
    rows, segs = [], []
    for owner, n in ((big, 2000), (small, 60)):
        for i in range(n):
            segs.append(
                Segment(
                    recording=owner, sequence_num=i, start_time=float(i),
                    end_time=float(i + 1), text=f"segment {i} of {owner.id}",
                )
            )
    Segment.objects.bulk_create(segs, batch_size=500)
    for seg in Segment.objects.all().only("id"):
        rows.append(
            SegmentEmbedding(
                segment_id=seg.id, vector=unit(), model="stub-embed-1",
                content_hash="0" * 64,
            )
        )
    SegmentEmbedding.objects.bulk_create(rows, batch_size=500)
    # Without stats the planner thinks the table is empty and picks an exact
    # seq scan — which is precisely the plan production does NOT get.
    with connection.cursor() as cur:
        cur.execute("ANALYZE recordings_segment_embedding")
        cur.execute("ANALYZE recordings_segment")
        cur.execute("ANALYZE recordings_recording")

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings(
            "anything", mode="vector", workspace_id=small.workspace_id, limit=5
        )
    assert len(hits) == 5, (
        f"vector arm returned {len(hits)}/5 candidates for a tenant with 60 "
        "eligible rows — the HNSW scan is stopping before the predicate is "
        "satisfied"
    )


@pytest.mark.django_db
@pg_only
def test_russian_recording_is_stemmed_not_simple(
    _require_vector_app, make_recording,
):
    """Defect 3, end to end: an ISO 639-2 tag must buy real stemming."""
    from stapel_recordings.models import Segment
    from stapel_recordings.vector.search import search_recordings

    rec = make_recording(status="completed", language="rus")
    Segment.objects.create(
        recording=rec, sequence_num=0, start_time=0.0, end_time=1.0,
        text="Мы приняли решения по расписанию релиза",
    )
    with override_settings(STAPEL_RECORDINGS=_VEC):
        # "решение" (nominative singular) must match "решения" — that is
        # what the russian stemmer is for; `simple` would miss it.
        hits = search_recordings("решение", mode="text", limit=5)
    assert len(hits) == 1


def test_a_key_repeated_within_one_arm_counts_once():
    """RRF is defined over a ranked list of DISTINCT documents.

    Found while measuring the window scheme: one utterance long enough to
    fill several windows anchors all of them, so the vector arm listed the
    same segment id a dozen times. Summing those contributions is not a
    rounding error — two appearances at ranks 20 and 21 sum to more than a
    single appearance at rank 1, so the duplicate leapfrogged every genuine
    hit and quietly cost the fused ranking its top five slots."""
    from stapel_recordings.vector.search import reciprocal_rank_fusion

    fused = dict(
        reciprocal_rank_fusion(
            {"vector": ["best", *["dup"] * 12, "other"]}, k=60
        )
    )
    assert fused["best"] > fused["dup"]
    # ...and the repeat is scored at its BEST rank, not its last.
    assert fused["dup"] == pytest.approx(1.0 / 62.0)


@pytest.mark.django_db
@pg_only
def test_vector_arm_returns_each_segment_once(
    _require_vector_app, make_recording,
):
    """A hit IS a segment id downstream — the fusion key, the QA citation.
    Two window rows anchored at the same segment are one result."""
    from stapel_core.comm import register_function

    from stapel_recordings.models import Segment
    from stapel_recordings.vector.models import SegmentEmbedding
    from stapel_recordings.vector.search import search_recordings

    register_function(
        "llm.embed",
        lambda payload: {
            "status": "ok",
            "embeddings": {
                "provider": "stub", "model": "stub-embed-1", "dim": 3,
                "vectors": [[1.0, 0.0, 0.0] for _ in payload["texts"]],
            },
        },
    )
    rec = make_recording(status="completed", language="en")
    seg = Segment.objects.create(
        recording=rec, sequence_num=0, start_time=0.0, end_time=90.0,
        text="one very long utterance",
    )
    for index in range(4):
        SegmentEmbedding.objects.create(
            segment=seg, scheme="window", chunk_index=index, span=1,
            text=f"window piece {index}", model="stub-embed-1",
            content_hash=f"{index:064d}", vector=[1.0, 0.0, float(index) / 10],
        )

    with override_settings(
        STAPEL_RECORDINGS={"VECTOR": {**_VEC["VECTOR"], "SEGMENT_SCHEME": "window"}}
    ):
        hits = search_recordings("anything", mode="vector", limit=10)
    assert [h.segment_id for h in hits] == [seg.id]
