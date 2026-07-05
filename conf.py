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

#: Default pipeline: the four built-in stages in canonical order. Hosts
#: override with STAPEL_RECORDINGS["PIPELINE"] (reorder / subset / extend)
#: or a PIPELINE_RESOLVER for runtime/per-recording definitions.
DEFAULT_PIPELINE = ("convert", "transcribe", "diarize", "merge")

recordings_settings = AppSettings(
    "STAPEL_RECORDINGS",
    defaults={
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
    },
    import_strings=("STORAGE", "NORMALIZER", "PIPELINE_RESOLVER"),
)

__all__ = ["recordings_settings", "DEFAULT_PIPELINE"]
