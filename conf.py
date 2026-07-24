"""Settings namespace for stapel-recordings.

All configuration is read through ``recordings_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_RECORDINGS`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior
(the STORAGE / NORMALIZER strategies and the PIPELINE_RESOLVER seam).

The flagship extension point is the **pipeline**: an ordered list of stage
names (``PIPELINE``) run by a generic driver over an open stage registry
(``STAGES`` overlay + ``register_stage`` runtime API). Reorder, subset,
insert or replace stages without forking. See MODULE.md.
"""
from stapel_core.conf import AppSettings

#: Default pipeline: the five built-in stages in canonical order. Hosts
#: override with STAPEL_RECORDINGS["PIPELINE"] (reorder / subset / extend)
#: or a PIPELINE_RESOLVER for runtime/per-recording definitions. ``embed``
#: is a no-op unless the opt-in vector app is installed AND enabled (the
#: DiarizeStage pattern) — its presence costs nothing.
DEFAULT_PIPELINE = ("convert", "transcribe", "diarize", "merge", "embed")

#: Defaults for the opt-in vector/search layer (``stapel_recordings.vector``
#: app + the ``[vector]`` extra). One nested block, not top-level keys: the
#: whole layer is optional and everything in here is tuning for it. Hosts
#: override any subset — :func:`vector_config` merges their block over these
#: defaults (one level deep for the nested dicts), so a host sets only what
#: it changes. Know-how (which embedding model, chunking, ranking weights)
#: deliberately stays host-side via this block.
DEFAULT_VECTOR = {
    # Master switch for the embed stage. The stage additionally requires
    # "stapel_recordings.vector" in INSTALLED_APPS — without both it is a
    # no-op (checked: W006 warns on ENABLED without the app).
    "ENABLED": False,
    # Embedding dimensionality — must match what llm.embed returns for the
    # chosen MODEL. Read by the vector app's models AND its migration, so
    # set it before the first migrate; changing it later means a host-side
    # migration + re-embed.
    "DIM": 1536,
    # Embedding model name forwarded to llm.embed ("" = agent default) and
    # stored on every embedding row. Pin it to make re-embeds per model
    # explicit.
    "MODEL": "",
    # Optional provider override forwarded to llm.embed ("" = agent default).
    "PROVIDER": "",
    # Texts per llm.embed call.
    "BATCH_SIZE": 64,
    # timeout_seconds forwarded to llm.embed.
    "TIMEOUT_SECONDS": 120,
    # Recording summaries are chunked to this many characters before
    # embedding (0 = never chunk); consecutive chunks overlap by
    # SUMMARY_CHUNK_OVERLAP characters.
    "SUMMARY_CHUNK_CHARS": 2000,
    "SUMMARY_CHUNK_OVERLAP": 200,
    # HNSW index parameters for the segment-embedding cosine index (read by
    # the vector app's migration at migrate time).
    "HNSW": {"M": 16, "EF_CONSTRUCTION": 64},
    # Postgres FTS config per recording language (primary subtag, lower
    # case); anything unmapped falls back to FTS_FALLBACK_CONFIG.
    "FTS_CONFIGS": {
        "en": "english", "de": "german", "fr": "french", "es": "spanish",
        "it": "italian", "pt": "portuguese", "nl": "dutch", "ru": "russian",
    },
    "FTS_FALLBACK_CONFIG": "simple",
    # Hybrid search: reciprocal-rank fusion. score(hit) = Σ over arms of
    # WEIGHT_arm / (RRF_K + rank_arm). ARM_LIMIT caps how many candidates
    # each arm contributes before fusion.
    "RRF_K": 60,
    "RRF_WEIGHTS": {"text": 1.0, "vector": 1.0},
    "ARM_LIMIT": 50,
}

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
        # ── Pipeline (flagship extension point) ──────────────────────
        # Ordered stage-name list run by the generic driver. A host can
        # reorder, drop (e.g. skip "diarize") or insert stages (e.g. a
        # "redact_pii" before "merge") purely by changing this list.
        "PIPELINE": list(DEFAULT_PIPELINE),
        # Overlay of custom/replacement stage handlers: {name: dotted-path}.
        # Merge-over-builtins semantics; a value of None removes a built-in.
        "STAGES": {},
        # Resolver seam: dotted path to ``(recording) -> list[str]``. The
        # default returns the PIPELINE setting; point it at a DB / per-
        # workspace source to let operators edit pipelines at runtime.
        "PIPELINE_RESOLVER": "stapel_recordings.pipeline.default_pipeline_resolver",

        # ── Storage seam (single strategy, replace) ──────────────────
        # Dotted path to a RecordingStorage implementation. Default is a
        # Django-storage backend (works with any DEFAULT_FILE_STORAGE);
        # swap for the bundled S3/MinIO backend or your own.
        "STORAGE": "stapel_recordings.storage.DjangoStorageBackend",
        "STORAGE_PREFIX": "recordings",

        # ── Audio normalization seam (single strategy, replace) ──────
        # Dotted path to ``(src_path, dst_path) -> float|None`` returning
        # duration seconds. Default shells out to ffmpeg; a passthrough is
        # provided for environments without ffmpeg / for tests.
        "NORMALIZER": "stapel_recordings.normalize.ffmpeg_normalize",

        # ── Upload sessions ──────────────────────────────────────────
        "UPLOAD_SESSION_TTL_SECONDS": 15 * 60,
        "MULTIPART_SESSION_TTL_SECONDS": 24 * 60 * 60,
        "MULTIPART_PART_SIZE": 10 * 1024 * 1024,
        "MAX_UPLOAD_BYTES": 2 * 1024 * 1024 * 1024,
        # Allowlist of upload file extensions (lower-case, no dot) for the
        # required ``filename`` on ``create_upload_session`` — the object
        # key is suffixed with the validated extension. Tuning,
        # not an axis: extend it for whatever media your NORMALIZER handles.
        "UPLOAD_EXTENSION_ALLOWLIST": [
            "mp3", "m4a", "wav", "ogg", "oga", "opus", "webm", "flac",
            "aac", "aiff", "amr", "wma", "mp4", "mov", "mkv", "3gp",
        ],

        # ── Source-type registry (merge-over-builtins extension point) ─
        # Recording source kinds (meet / dictaphone / upload / other by
        # default) are an OPEN registry, not a hardcoded enum: a host adds
        # ``zoom`` / ``teams`` / ``phone`` by overlaying this map — merged
        # OVER stapel_recordings.sources.DEFAULT_SOURCE_TYPES, the same
        # merge-registry idiom as STAGES / notifications.TYPES. ``{key:
        # label}``. See stapel_recordings/sources.py.
        "SOURCE_TYPES": {},

        # ── Transcription / summarization (delegated to stapel-agent) ─
        "TRANSCRIBE_TIMEOUT_SECONDS": 1800,
        "MAX_STAGE_RETRIES": 3,
        "SUMMARIZE_ENABLED": True,
        "SUMMARIZE_MODEL": "medium",

        # ── Reconcile watchdog ───────────────────────────────────────
        # STUCK_THRESHOLD_SECONDS MUST exceed the longest legitimate stage
        # duration (for the built-ins: TRANSCRIBE_TIMEOUT_SECONDS), or the
        # watchdog will re-emit recording.stage for stages that are still
        # running — the duplicate then piles up on the row lock. Default:
        # transcribe timeout (1800) + 5 min headroom. If you raise
        # TRANSCRIBE_TIMEOUT_SECONDS (or add a slower custom stage), raise
        # this too — a system check (W005) warns on inconsistency.
        "STUCK_THRESHOLD_SECONDS": 35 * 60,
        "ABANDONED_UPLOAD_THRESHOLD_SECONDS": 60 * 60,

        # ── Opt-in vector/search layer (stapel_recordings.vector) ─────
        # Nested tuning block for the optional embeddings app; see
        # DEFAULT_VECTOR above and vector_config() below. Not an axis —
        # the capability switch is installing the vector app itself.
        "VECTOR": dict(DEFAULT_VECTOR),
}

recordings_settings = AppSettings(
    "STAPEL_RECORDINGS",
    defaults=DEFAULTS,
    import_strings=("STORAGE", "NORMALIZER", "PIPELINE_RESOLVER"),
)


def vector_config() -> dict:
    """Effective VECTOR block: the host's ``STAPEL_RECORDINGS["VECTOR"]``
    merged over :data:`DEFAULT_VECTOR` (AppSettings replaces dict values
    wholesale, so the merge lives here). The nested dicts (``HNSW`` /
    ``FTS_CONFIGS`` / ``RRF_WEIGHTS``) merge one level deep too — a host
    adding one FTS language keeps the default map."""
    host = recordings_settings.VECTOR or {}
    merged = {**DEFAULT_VECTOR, **host}
    for key in ("HNSW", "FTS_CONFIGS", "RRF_WEIGHTS"):
        merged[key] = {**DEFAULT_VECTOR[key], **(host.get(key) or {})}
    return merged


__all__ = ["recordings_settings", "vector_config", "DEFAULT_PIPELINE", "DEFAULT_VECTOR"]
