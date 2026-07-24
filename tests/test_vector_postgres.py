"""DB-bound vector-layer tests — PostgreSQL + pgvector only.

Explicitly gated (the strategy stated in tests/test_vector.py): the whole
module skips unless the suite runs against postgres WITH the vector app
installed, i.e. under the opt-in harness

    STAPEL_RECORDINGS_TEST_DB=postgres://user:pass@host/dbname python3 -m pytest

(see ``_codegen_settings._postgres_database`` — the env var flips the DB
engine and appends ``stapel_recordings.vector`` to INSTALLED_APPS; the
target server must allow ``CREATE EXTENSION vector`` or ship it in
template1, since test tables are created from models). On the canonical
sqlite suite every test here reports as skipped — that is by design, the
logic-level coverage lives in test_vector.py against the fake store.
"""
import pytest
from django.db import connection
from django.test import override_settings

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="vector DB tests need PostgreSQL + pgvector "
        "(run with STAPEL_RECORDINGS_TEST_DB=postgres://…)",
    ),
]


def _vector_installed() -> bool:
    from stapel_recordings.vector import vector_app_installed

    return vector_app_installed()


_VEC = {
    "VECTOR": {
        "ENABLED": True,
        "DIM": 3,
        "BATCH_SIZE": 64,
        "SUMMARY_CHUNK_CHARS": 0,
    }
}


@pytest.fixture(autouse=True)
def _require_vector_app():
    if not _vector_installed():
        pytest.skip("stapel_recordings.vector not in INSTALLED_APPS (postgres harness required)")


@pytest.fixture
def stub_embed():
    from stapel_core.comm import register_function

    class Recorder:
        def __init__(self):
            self.calls = []
            # Orthogonal-ish fixed vectors keyed by first word, so cosine
            # ordering is predictable.
            self.by_word = {
                "alpha": [1.0, 0.0, 0.0],
                "charlie": [0.0, 1.0, 0.0],
                "echo": [0.0, 0.0, 1.0],
            }

        def __call__(self, payload):
            self.calls.append(payload)
            vectors = [
                self.by_word.get(t.split()[0].lower(), [0.5, 0.5, 0.5])
                for t in payload["texts"]
            ]
            return {
                "status": "ok",
                "embeddings": {"provider": "stub", "model": "stub-embed-1", "dim": 3, "vectors": vectors},
            }

    recorder = Recorder()
    register_function("llm.embed", recorder)
    return recorder


@pytest.fixture
def embedded_recording(make_recording, stub_embed):
    from stapel_recordings.models import Segment
    from stapel_recordings.vector.embedding import embed_recording

    rec = make_recording(status="completed", language="en")
    for i, text in enumerate(["alpha bravo topic", "charlie delta subject", "echo foxtrot theme"]):
        Segment.objects.create(
            recording=rec, sequence_num=i, start_time=float(i),
            end_time=float(i + 1), text=text,
        )
    with override_settings(STAPEL_RECORDINGS=_VEC):
        embed_recording(rec)
    return rec


def test_orm_store_persists_vectorfield_rows(embedded_recording):
    from stapel_recordings.vector.models import SegmentEmbedding

    rows = SegmentEmbedding.objects.filter(segment__recording=embedded_recording)
    assert rows.count() == 3
    row = rows.get(segment__sequence_num=0)
    assert list(row.vector) == [1.0, 0.0, 0.0]
    assert row.model == "stub-embed-1"
    assert len(row.content_hash) == 64


def test_embed_stage_upsert_is_idempotent_on_db(embedded_recording, stub_embed):
    from stapel_recordings.vector.embedding import embed_recording
    from stapel_recordings.vector.models import SegmentEmbedding

    calls_before = len(stub_embed.calls)
    with override_settings(STAPEL_RECORDINGS=_VEC):
        embed_recording(embedded_recording)
    assert len(stub_embed.calls) == calls_before  # hash-skip, no re-embed
    assert SegmentEmbedding.objects.filter(segment__recording=embedded_recording).count() == 3


def test_vector_search_orders_by_cosine(embedded_recording, stub_embed):
    from stapel_recordings.vector.search import search_recordings

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings("charlie something", mode="vector", limit=2)
    assert hits
    # query vector == charlie's vector → its segment is the closest hit
    top = hits[0]
    assert "charlie" in top.snippet
    assert top.recording_id == embedded_recording.id
    assert top.score == pytest.approx(1.0)


def test_text_search_uses_fts_ranking(embedded_recording):
    from stapel_recordings.vector.search import search_recordings

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings("delta", mode="text", limit=5)
    assert len(hits) == 1
    assert hits[0].score > 0.0  # a real SearchRank, not the degraded 1.0
    assert "delta" in hits[0].snippet


def test_hybrid_search_fuses_both_arms(embedded_recording, stub_embed):
    from stapel_recordings.vector.search import search_recordings

    with override_settings(STAPEL_RECORDINGS=_VEC):
        hits = search_recordings("charlie delta", mode="hybrid", limit=3)
    assert hits
    # The segment ranked #1 by BOTH arms must fuse to the top.
    assert "charlie" in hits[0].snippet
    ids = [h.segment_id for h in hits]
    assert len(ids) == len(set(ids))  # fusion dedupes across arms


def test_summary_chunks_persist(make_recording, stub_embed):
    from stapel_recordings.vector.embedding import embed_recording
    from stapel_recordings.vector.models import RecordingEmbedding

    rec = make_recording(status="completed", summary="alpha " * 10)
    cfg = {"VECTOR": {**_VEC["VECTOR"], "SUMMARY_CHUNK_CHARS": 20, "SUMMARY_CHUNK_OVERLAP": 0}}
    with override_settings(STAPEL_RECORDINGS=cfg):
        embed_recording(rec)
    chunks = RecordingEmbedding.objects.filter(recording=rec).order_by("chunk_index")
    assert chunks.count() >= 2
    assert list(chunks.values_list("chunk_index", flat=True)) == list(range(chunks.count()))
