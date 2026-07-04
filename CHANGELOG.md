# Changelog

All notable changes to stapel-recordings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.1.0] — Unreleased

Initial port from `the legacy recordings service` (the legacy backend). **Not released** —
awaits an independent adversarial review and a PyPI pending trusted
publisher before the first `v0.1.0` tag.

### Added
- **Domain**: `Recording` + `Speaker` + `Segment` (unified transcript),
  `UploadSession` (presigned single-PUT + multipart), `Job` ledger, and the
  status state machine `created → … → completed` (+ `error`, `deleted`).
- **Data-driven pipeline** (flagship extension point): an ordered
  `PIPELINE` stage list run by a generic driver over an open stage registry
  (`BUILTIN_STAGES` + `STAGES` overlay with merge-over-builtins +
  `register_stage` runtime API), plus a `PIPELINE_RESOLVER` seam for
  runtime/per-recording pipeline definitions. Built-in stages: `convert`,
  `transcribe`, `diarize` (no-op default), `merge`.
- **Storage seam** `RecordingStorage` (`STORAGE`): `DjangoStorageBackend`
  (default) and `S3Backend` (boto3, `[s3]` extra). No boto3 dependency in
  the module core.
- **Audio normalization seam** `NORMALIZER`: `ffmpeg_normalize` (default) +
  `passthrough_normalize`.
- Upload sessions (single-PUT + multipart) with idempotent `finalize_upload`.
- REST surface (create + upload session, detail, finalize) with serializer
  seams; read-only admin.
- GDPR provider (`section = "recordings"`) + `@on_action("user.deleted")`
  consumer that erases recordings and their storage objects via the seam.
- `recordings_reconcile` management command (re-drive stuck recordings; fail
  abandoned uploads).
- System checks: E for a bad `STORAGE`, W for unknown pipeline stages /
  non-callable `NORMALIZER` / `PIPELINE_RESOLVER`.
- 55 tests: full pipeline run, split producer/consumer halves, state-machine
  transitions, idempotent re-delivery, pipeline extension points
  (custom/reordered/subset/swapped stages + resolver seam), retry/DLQ,
  reconcile, storage-seam swap, upload/multipart, GDPR, summarize, checks,
  schema validation, HTTP surface.

### Changed from the legacy recordings service (provenance)
- **Raw Kafka bus + publish-after-commit → `stapel_core.comm` Actions
  through the transactional outbox.** Fixes the source's dual-write event
  loss; the pipeline is now at-least-once with idempotent stages.
- **Hardcoded convert→transcribe→diarize→merge consumer chain → a generic,
  data-driven driver** over a stage registry (reorderable/replaceable).
- **Direct boto3/MinIO calls → the `STORAGE` seam.**
- **STT provider registry, language routing and fallback → delegated to
  stapel-agent** (`llm.transcribe`). This module persists the returned
  transcript only.
- **`summary_input.json` for an external agent → an in-pipeline
  `llm.summarize` call** whose result is stored on the recording.
- **Scattered `os.getenv` (MINIO_/ELEVENLABS_/PYANNOTE_/…) → the
  `STAPEL_RECORDINGS` conf namespace.**
- **Hardcoded `legacy.recordings.*` topics → schema'd comm names** under
  `schemas/emits/`.

### Not ported (app-layer)
- Zoom/Meet/Teams ingestion (OAuth, webhooks, TOFU binding), credits, share
  links, and export formats (SRT/VTT/DOCX/PDF). See MODULE.md → App-layer.

### Security / release
- Opus-authored. **Must NOT be released** until an independent adversarial
  review passes and a PyPI pending trusted publisher is registered.
