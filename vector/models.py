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
    """One embedding vector per (segment, model). ``content_hash`` is the
    sha256 of the embedded text — the embed stage's idempotency key (a
    redelivery with an unchanged hash is skipped; an edited segment is
    re-embedded in place via upsert)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(
        Segment, on_delete=models.CASCADE, related_name="embeddings"
    )
    vector = VectorField(dimensions=_dim())
    model = models.CharField(max_length=128, blank=True, default="")
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recordings_segment_embedding"
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "model"], name="rec_segemb_seg_model_uniq"
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
