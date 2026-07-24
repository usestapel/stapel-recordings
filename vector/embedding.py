"""Embedding pipeline logic for the opt-in vector layer.

Called by the ``embed`` stage (``stages.EmbedStage``) strictly behind the
installed+enabled gate. Split by import weight:

- the pure helpers (:func:`content_hash`, :func:`batched`,
  :func:`chunk_text`) and :func:`embed_texts` (the ``llm.embed`` comm
  boundary) import no models — they are unit-testable on the sqlite suite
  without pgvector;
- persistence goes through the :class:`ORMVectorStore` seam, whose methods
  import the vector app's models lazily. :func:`get_store` is the
  substitution point tests monkeypatch to run the full enabled path on
  sqlite with a fake store.

Outbox canon: at-least-once delivery, so everything here is idempotent —
every row carries the sha256 of the embedded text, and only texts whose
(hash, model) is not yet stored are sent to ``llm.embed``. A crash between
batches re-runs the stage and embeds only the remainder; transient comm
failures surface as ``StageRetryable``.

``llm.embed`` contract (stapel-agent >= 0.4):

    call("llm.embed", {"texts": [...], "model"?, "provider"?,
                       "timeout_seconds"?})
    -> {"status": "ok",
        "embeddings": {"provider": str, "model": str, "dim": int,
                       "vectors": [[float, ...], ...]}}
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)


# ─── Pure helpers (no Django/pgvector imports) ─────────────────────────


def content_hash(text: str) -> str:
    """sha256 hex digest of the text — the embed idempotency key."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def batched(items: list, size: int):
    """Yield consecutive slices of *items* of at most *size* elements."""
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def chunk_text(text: str | None, chunk_chars: int, overlap: int = 0) -> list[str]:
    """Split *text* into chunks of at most *chunk_chars* characters with
    *overlap* characters of context carried between consecutive chunks.
    ``chunk_chars <= 0`` disables chunking (one chunk). Empty/whitespace
    text yields no chunks. Deliberately simple (character windows, no
    tokenizer) — smarter chunking is host know-how: replace the summary
    text upstream or swap the embed stage via the stage registry."""
    text = (text or "").strip()
    if not text:
        return []
    chunk_chars = int(chunk_chars or 0)
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    overlap = max(0, min(int(overlap or 0), chunk_chars - 1))
    step = chunk_chars - overlap
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_chars])
        if start + chunk_chars >= len(text):
            break
        start += step
    return chunks


# ─── llm.embed boundary ────────────────────────────────────────────────


def embed_texts(texts: list[str], cfg: dict) -> tuple[str, list]:
    """Embed *texts* via ``llm.embed`` in ``BATCH_SIZE`` batches.

    Returns ``(model, vectors)`` with one vector per input text; ``model``
    is what the provider reports (falling back to the configured
    ``VECTOR["MODEL"]``). Transient failures (comm errors, non-ok status,
    a short vector list) raise ``StageRetryable``; a dimensionality that
    contradicts ``VECTOR["DIM"]`` is a configuration error and raises
    ``StageFatal`` (a retry cannot fix it)."""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from ..stages import StageFatal, StageRetryable

    model = str(cfg.get("MODEL") or "")
    vectors: list = []
    for batch in batched(list(texts), cfg["BATCH_SIZE"]):
        payload: dict = {
            "texts": list(batch),
            "timeout_seconds": int(cfg["TIMEOUT_SECONDS"]),
        }
        if cfg.get("MODEL"):
            payload["model"] = cfg["MODEL"]
        if cfg.get("PROVIDER"):
            payload["provider"] = cfg["PROVIDER"]

        try:
            result = call("llm.embed", payload)
        except CommError as exc:
            raise StageRetryable("embed_call_failed", str(exc)) from exc

        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (
                result.get("reason", "embed_failed")
                if isinstance(result, dict) else "embed_failed"
            )
            raise StageRetryable("embed_failed", str(reason))

        emb = result.get("embeddings") or {}
        got = emb.get("vectors") or []
        if len(got) != len(batch):
            raise StageRetryable(
                "embed_bad_response",
                f"sent {len(batch)} texts, got {len(got)} vectors",
            )
        dim = emb.get("dim") or (len(got[0]) if got and got[0] is not None else None)
        if dim is not None and int(dim) != int(cfg["DIM"]):
            raise StageFatal(
                "embed_dim_mismatch",
                f"llm.embed returned dim {dim}, VECTOR['DIM'] is {cfg['DIM']} "
                "— align the setting (and the migrated column) with the model",
            )
        model = str(emb.get("model") or model)
        vectors.extend(got)
    return model, vectors


# ─── Persistence seam ──────────────────────────────────────────────────


class ORMVectorStore:
    """Default persistence: the vector app's models. Every method imports
    lazily so this module stays importable without pgvector/the app; only
    instantiate behind the EmbedStage gate (or substitute via
    :func:`get_store` in tests)."""

    def segment_hashes(self, recording) -> dict:
        """``{segment_id: {(model, content_hash), ...}}`` for the recording."""
        from .models import SegmentEmbedding

        out: dict = {}
        rows = SegmentEmbedding.objects.filter(
            segment__recording=recording
        ).values_list("segment_id", "model", "content_hash")
        for seg_id, model, h in rows:
            out.setdefault(seg_id, set()).add((model, h))
        return out

    def summary_hashes(self, recording) -> set:
        """``{(chunk_index, model, text_hash), ...}`` for the recording."""
        from .models import RecordingEmbedding

        return set(
            RecordingEmbedding.objects.filter(recording=recording)
            .values_list("chunk_index", "model", "text_hash")
        )

    def upsert_segment(self, segment, *, model: str, content_hash: str, vector) -> None:
        from .models import SegmentEmbedding

        SegmentEmbedding.objects.update_or_create(
            segment=segment,
            model=model,
            defaults={"content_hash": content_hash, "vector": vector},
        )

    def upsert_summary_chunk(
        self, recording, *, chunk_index: int, model: str, text_hash: str, vector
    ) -> None:
        from .models import RecordingEmbedding

        RecordingEmbedding.objects.update_or_create(
            recording=recording,
            model=model,
            chunk_index=chunk_index,
            defaults={"text_hash": text_hash, "vector": vector},
        )

    def prune_summary_chunks(self, recording, *, model: str, keep: int) -> None:
        """Drop stale tail chunks (a re-summarized recording may chunk
        shorter than before)."""
        from .models import RecordingEmbedding

        RecordingEmbedding.objects.filter(
            recording=recording, model=model, chunk_index__gte=keep
        ).delete()


def get_store() -> ORMVectorStore:
    """Store factory — the seam tests monkeypatch to exercise the enabled
    path without postgres/pgvector."""
    return ORMVectorStore()


# ─── Stage body ────────────────────────────────────────────────────────


def _hash_known(entries, h: str, cfg_model: str) -> bool:
    """True when *h* is already stored (for the pinned model, if pinned)."""
    return any(
        stored_hash == h and (not cfg_model or stored_model == cfg_model)
        for stored_model, stored_hash in entries
    )


def embed_recording(recording, store=None) -> dict:
    """Embed all missing segment texts + summary chunks for *recording*.

    Returns counters (``segments_embedded`` / ``summary_chunks_embedded``)
    for logging/tests. Assumes the EmbedStage gate already passed."""
    from ..conf import vector_config

    cfg = vector_config()
    store = store or get_store()
    cfg_model = str(cfg.get("MODEL") or "")

    # ── Segments ──
    segments = list(recording.segments.exclude(text="").order_by("sequence_num"))
    known = store.segment_hashes(recording)
    pending = []
    for seg in segments:
        h = content_hash(seg.text)
        if _hash_known(known.get(seg.id, ()), h, cfg_model):
            continue  # idempotent: unchanged text already embedded
        pending.append((seg, h))

    segments_embedded = 0
    if pending:
        model, vectors = embed_texts([seg.text for seg, _ in pending], cfg)
        for (seg, h), vec in zip(pending, vectors):
            store.upsert_segment(seg, model=model, content_hash=h, vector=vec)
        segments_embedded = len(pending)

    # ── Summary chunks ──
    chunks = chunk_text(
        recording.summary, cfg["SUMMARY_CHUNK_CHARS"], cfg["SUMMARY_CHUNK_OVERLAP"]
    )
    chunks_embedded = 0
    if chunks:
        known_chunks = store.summary_hashes(recording)
        pending_chunks = [
            (idx, chunk, content_hash(chunk))
            for idx, chunk in enumerate(chunks)
            if not any(
                ci == idx and hh == content_hash(chunk)
                and (not cfg_model or m == cfg_model)
                for ci, m, hh in known_chunks
            )
        ]
        if pending_chunks:
            model, vectors = embed_texts([c for _, c, _ in pending_chunks], cfg)
            for (idx, _, h), vec in zip(pending_chunks, vectors):
                store.upsert_summary_chunk(
                    recording, chunk_index=idx, model=model, text_hash=h, vector=vec
                )
            chunks_embedded = len(pending_chunks)
            store.prune_summary_chunks(recording, model=model, keep=len(chunks))

    if segments_embedded or chunks_embedded:
        logger.info(
            "embed: recording %s — %d segment(s), %d summary chunk(s) embedded",
            recording.id, segments_embedded, chunks_embedded,
        )
    return {
        "segments_embedded": segments_embedded,
        "summary_chunks_embedded": chunks_embedded,
    }


__all__ = [
    "content_hash",
    "batched",
    "chunk_text",
    "embed_texts",
    "ORMVectorStore",
    "get_store",
    "embed_recording",
]
