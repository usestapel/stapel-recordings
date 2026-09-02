"""Embedding models for the opt-in vector app.

Only importable when ``stapel_recordings.vector`` is in INSTALLED_APPS and
the ``[vector]`` extra (pgvector) is installed — the base package never
imports this module (the ``embed`` stage and the search service import it
lazily, behind the installed/enabled gate).

``VectorField`` dimensionality comes from ``STAPEL_RECORDINGS["VECTOR"]
["DIM"]`` — the model and the migration read the same setting, so they can
never drift from each other; set DIM before the first migrate, and treat a
later change as a host-side migration + re-embed.

House rules (docs/library-standard.md §3.8): index/constraint names <= 30
chars; rows are machine-written by the embed stage (``@access.ops`` — no
staff-authored workflow to protect, same category as UploadSession/Job).
"""
from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import HnswIndex, VectorField
from stapel_core.access import access

from stapel_recordings.conf import vector_config
from stapel_recordings.models import Recording, Segment


def _dim() -> int:
    return int(vector_config()["DIM"])


def _hnsw() -> dict:
    return vector_config()["HNSW"]


@access.ops  # machine-written by the embed pipeline stage, never staff-authored
class SegmentEmbedding(models.Model):
    """One embedding vector per (segment, model, scheme). ``content_hash``
    is the sha256 of the embedded text — the embed stage's idempotency key
    (a redelivery with an unchanged hash is skipped; an edited segment is
    re-embedded in place via upsert).

    ``scheme`` says WHAT was embedded, and is why two chunkings can live in
    one table without corrupting each other's ranking:

    - ``"segment"`` — the segment's own text, one STT utterance. The
      historical scheme and still the default.
    - ``"window"`` — a run of consecutive utterances starting at this
      segment, packed to an answer-sized passage with speaker and timestamp
      prefixes (``vector.chunking``). ``text`` holds exactly what was
      embedded and ``span`` how many utterances it covers, because the
      window is not reconstructible from the anchor row alone and search
      must be able to show what it matched.

    A row's scheme is stamped by whoever wrote it and search reads exactly
    one scheme, so building the second one is a background command
    (``recordings_reembed --scheme window``) and adopting it is a setting
    (``VECTOR["SEGMENT_SCHEME"]``) that flips back as easily as forward."""

    SCHEME_SEGMENT = "segment"
    SCHEME_WINDOW = "window"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(
        Segment, on_delete=models.CASCADE, related_name="embeddings"
    )
    vector = VectorField(dimensions=_dim())
    model = models.CharField(max_length=128, blank=True, default="")
    scheme = models.CharField(max_length=16, default=SCHEME_SEGMENT)
    #: The embedded text when it is NOT the segment's own (window rows);
    #: empty for ``segment`` rows, which would only duplicate Segment.text.
    text = models.TextField(blank=True, default="")
    #: How many consecutive segments this row covers (1 for ``segment``).
    span = models.IntegerField(default=1)
    #: Nth unit anchored at this segment. Always 0 for ``segment`` rows and
    #: for ordinary windows (a window's anchor advances with every window);
    #: >0 only where a single utterance is longer than a whole window and
    #: has to be split across several, which would otherwise collide on the
    #: uniqueness key and silently overwrite itself.
    chunk_index = models.IntegerField(default=0)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recordings_segment_embedding"
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "model", "scheme", "chunk_index"],
                name="rec_segemb_seg_mdl_sch_uq",
            ),
        ]
        indexes = [
            HnswIndex(
                name="rec_segemb_hnsw_idx",
                fields=["vector"],
                m=int(_hnsw()["M"]),
                ef_construction=int(_hnsw()["EF_CONSTRUCTION"]),
                opclasses=["vector_cosine_ops"],
            ),
        ]


@access.ops  # machine-written by the embed pipeline stage, never staff-authored
class RecordingEmbedding(models.Model):
    """Recording-level (summary/chunk) embeddings: the summary is chunked
    (``VECTOR["SUMMARY_CHUNK_CHARS"]``) and each chunk embedded as one row,
    keyed by (recording, model, chunk_index). ``text_hash`` is the chunk's
    sha256 — same idempotency contract as SegmentEmbedding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, related_name="embeddings"
    )
    chunk_index = models.IntegerField(default=0)
    text_hash = models.CharField(max_length=64)
    vector = VectorField(dimensions=_dim())
    model = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recordings_recording_embedding"
        constraints = [
            models.UniqueConstraint(
                fields=["recording", "model", "chunk_index"],
                name="rec_recemb_chunk_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["recording"], name="rec_recemb_rec_idx"),
        ]
