"""Opt-in vector/search layer for stapel-recordings.

A separate Django app the host adds itself — the base package neither
installs nor imports it, so hosts that don't want vectors carry zero burden
(no pgvector dependency, no extra tables, the ``embed`` pipeline stage
stays a no-op). To opt in:

1. ``pip install stapel-recordings[vector]`` (pulls ``pgvector``);
2. add ``"stapel_recordings.vector"`` to ``INSTALLED_APPS`` (after
   ``"stapel_recordings"``);
3. run this app's migrations against PostgreSQL — the first migration
   issues the standard ``CREATE EXTENSION IF NOT EXISTS vector`` (the
   operation is vendor-guarded: a no-op off postgres);
4. set ``STAPEL_RECORDINGS["VECTOR"] = {"ENABLED": True, ...}`` — see
   ``DEFAULT_VECTOR`` in ``conf.py`` for the tuning block (dim, model,
   batch size, HNSW params, FTS language map, RRF weights).

What you get:

- ``SegmentEmbedding`` / ``RecordingEmbedding`` rows written by the
  ``embed`` pipeline stage (after ``merge``) via the ``llm.embed`` comm
  Function (stapel-agent) — content-hashed, idempotent, retry-safe;
- ``vector/search.py`` — ``search_recordings()``: text (postgres FTS),
  vector (pgvector cosine) and hybrid (reciprocal-rank fusion) segment
  search.

This ``__init__`` deliberately imports nothing model- or pgvector-bound so
the base package can probe :func:`vector_app_installed` cheaply.
"""
from __future__ import annotations

VECTOR_APP_NAME = "stapel_recordings.vector"


def vector_app_installed() -> bool:
    """True when the host added the vector app to INSTALLED_APPS."""
    from django.apps import apps

    return apps.is_installed(VECTOR_APP_NAME)


__all__ = ["VECTOR_APP_NAME", "vector_app_installed"]
