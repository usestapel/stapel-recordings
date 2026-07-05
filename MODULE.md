# stapel-recordings — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- The **recording lifecycle**: capture/upload → storage → transcribe →
  summarize. Owns `Recording` + `Speaker` + `Segment` (the unified
  transcript), `UploadSession` (presigned single-PUT and multipart) and a
  `Job` ledger.
- A **data-driven pipeline**: an ordered list of stage names run by one
  generic, outbox-backed driver over an open stage registry. Reorder,
  subset, insert, replace stages — all fork-free (the flagship extension
  point, below).
- A **status state machine**: `created → uploading → queued → analyzing →
  normalizing → transcribing → diarizing → merging → completed`
  (+ `error`, `deleted`).
- A **storage seam** (`RecordingStorage`) — no hard dependency on any S3
  client in the module core.
- A REST surface (create + upload session, detail, finalize) with
  serializer seams, and a GDPR provider + `user.deleted` consumer.

**What it delegates (does NOT implement):**

- **Speech-to-text** and **summarization** live in **stapel-agent**.
  The `transcribe` stage calls the `llm.transcribe` comm Function; the
  `merge` stage calls `llm.summarize`. Provider selection, STT fallback
  chains and language routing are the agent's job — recordings just passes
  an audio URL + options and stores the returned transcript. There is no
  agent import: the calls are by comm name.
- **Object storage** — all I/O goes through the `STORAGE` seam.

## Extension points (fork-free)

### 🚩 The pipeline — data + an open stage registry

The pipeline is **not** a hardcoded chain. It is an ordered list of stage
names run by the generic driver in `pipeline.py`. Three composable ways to
change it, none requiring a fork:

1. **Reorder / subset / extend the list** — `STAPEL_RECORDINGS["PIPELINE"]`
   (default `["convert", "transcribe", "diarize", "merge"]`). Drop
   `diarize`, or insert `redact_pii` before `merge`, by editing the list.
2. **Replace / remove / add a stage handler** — the `STAGES` overlay
   (`{name: dotted-path | None}`, **merge-over-builtins**; `None` removes a
   built-in) or the runtime `register_stage(name, handler)` /
   `unregister_stage(name)` API. A handler is a `Stage` subclass, a `Stage`
   instance, or a `callable(recording, ctx) -> ctx`.
3. **Source the list at runtime** — `PIPELINE_RESOLVER` (dotted path to
   `(recording) -> list[str]`). The default returns the `PIPELINE` setting;
   point it at a DB / per-workspace / per-recording definition so operators
   can edit pipelines in a UI without a redeploy.

**Stage contract** (`stages.Stage`):

```python
class Stage:
    name: str            # registry key
    status: str          # RecordingStatus the driver sets while running (optional)
    def run(self, recording, ctx: dict) -> dict: ...
```

`run` does the work and returns the ctx passed to the next stage. Raise
`StageRetryable` (transient — the driver counts the attempt and parks the
recording for `reconcile`) or `StageFatal` (bad input — straight to DLQ).
Stages MUST be idempotent (guard on status / persisted keys): delivery is
at-least-once.

**Worked example — insert a redaction stage and skip diarize:**

```python
# app config ready()
from stapel_recordings.stages import Stage, register_stage

class RedactPII(Stage):
    name = "redact_pii"
    def run(self, recording, ctx):
        for seg in recording.segments.all():
            seg.text = my_redactor(seg.text); seg.save(update_fields=["text"])
        return ctx

register_stage("redact_pii", RedactPII)

# settings.py
STAPEL_RECORDINGS = {
    "PIPELINE": ["convert", "transcribe", "redact_pii", "merge"],  # no diarize
}
```

The four built-ins:

| Stage | Status | Does | Delegates to |
|---|---|---|---|
| `convert` | `normalizing` | Normalize media to 16 kHz mono WAV (`NORMALIZER` seam), store it, drop the raw | ffmpeg (default) |
| `transcribe` | `transcribing` | Call `llm.transcribe`, persist `Speaker`/`Segment` rows | **stapel-agent** |
| `diarize` | `diarizing` | **No-op by default** (diarization is returned inline by `llm.transcribe`); swap in a real diarizer via the registry | — |
| `merge` | `merging` | Build + store the unified transcript JSON, then `llm.summarize` | **stapel-agent** |

### Settings — `STAPEL_RECORDINGS` namespace (`conf.py`)

Resolution per key: `settings.STAPEL_RECORDINGS[key]` → flat Django setting →
env var → default. Lazy; caches invalidate on `setting_changed`.

| Key | Default | Semantics | Customizes |
|---|---|---|---|
| `PIPELINE` | `["convert","transcribe","diarize","merge"]` | value | Ordered stage list |
| `STAGES` | `{}` | **merge** over builtins (`None` removes) | Stage handler overlay (dotted paths) |
| `PIPELINE_RESOLVER` | `…pipeline.default_pipeline_resolver` | **replace** (dotted path) | Runtime pipeline source |
| `STORAGE` | `…storage.DjangoStorageBackend` | **replace** (dotted path) | Object-storage backend |
| `STORAGE_PREFIX` | `"recordings"` | value | Key prefix |
| `NORMALIZER` | `…normalize.ffmpeg_normalize` | **replace** (dotted path) | Audio normalization callable |
| `MAX_STAGE_RETRIES` | `3` | value | Retries before DLQ |
| `TRANSCRIBE_TIMEOUT_SECONDS` | `1800` | value | Passed to `llm.transcribe` |
| `SUMMARIZE_ENABLED` | `True` | value | Toggle the summarize step |
| `SUMMARIZE_MODEL` | `"medium"` | value | Model size for `llm.summarize` |
| `UPLOAD_SESSION_TTL_SECONDS` | `900` | value | Single-PUT session TTL |
| `MULTIPART_SESSION_TTL_SECONDS` | `86400` | value | Multipart session TTL |
| `MULTIPART_PART_SIZE` | `10 MiB` | value | Multipart part size |
| `MAX_UPLOAD_BYTES` | `2 GiB` | value | Upload cap |
| `STUCK_THRESHOLD_SECONDS` | `600` | value | Reconcile: age before re-drive |
| `ABANDONED_UPLOAD_THRESHOLD_SECONDS` | `3600` | value | Reconcile: abandoned-upload age |
| `S3_*` | — | value | Config for the optional `S3Backend` |

### Storage seam — `RecordingStorage` (`storage.py`)

Single-strategy **replace** seam via `STORAGE`. Ships `DjangoStorageBackend`
(default — rides Django's `default_storage`; presigned URLs degrade to the
served URL; synthetic multipart shim) and `S3Backend` (boto3 presigned +
native multipart; `pip install stapel-recordings[s3]`). Implement the ABC to
target any store. `get_storage()` resolves + caches it.

### Audio normalization seam — `NORMALIZER` (`normalize.py`)

`(src_path, dst_path) -> float | None`. Default `ffmpeg_normalize` (needs
ffmpeg/ffprobe on PATH); `passthrough_normalize` for environments without
ffmpeg or already-normalized input. Raise `NormalizeFatal` for unfixable
input.

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL.

| View | Request serializer | Response serializer |
|---|---|---|
| `RecordingListCreateView` | `CreateRecordingRequestSerializer` | `CreateRecordingResponseSerializer` / `RecordingSerializer` |
| `RecordingDetailView` | — | `RecordingSerializer` |
| `FinalizeUploadView` | `FinalizeUploadRequestSerializer` | `RecordingSerializer` |

### Comm surface

All side effects leave through the **transactional outbox** (no inline
publish → no publish-after-commit loss). Every emitted name has a schema
under `schemas/emits/`, validated in tests.

| Kind | Name | Role | Payload / schema |
|---|---|---|---|
| Action (emit) | `recording.uploaded` | public entry | `schemas/emits/recording.uploaded.json` |
| Action (emit+consume) | `recording.stage` | internal driver step | `schemas/emits/recording.stage.json` |
| Action (emit) | `recording.stage_completed` | public, per-stage | `schemas/emits/recording.stage_completed.json` |
| Action (emit) | `recording.completed` | public terminal | `schemas/emits/recording.completed.json` |
| Action (emit) | `recording.failed` | public DLQ terminal | `schemas/emits/recording.failed.json` |
| Action (consume) | `recording.uploaded` | start the pipeline | — |
| Action (consume) | `recording.stage` | run stage N (driver) | — |
| Action (consume) | `user.deleted` | GDPR erase (owned by auth) | — |
| Function (**call**) | `llm.transcribe` | STT | provided by **stapel-agent** |
| Function (**call**) | `llm.summarize` | summary | provided by **stapel-agent** |

### Reliability primitives

- **Outbox** — `emit` writes the event with the caller's DB transaction;
  the driver walks stages after commit. At-least-once + idempotent stages.
- **DLQ** — a `StageFatal` (or retries exhausted) sets `status=error` and
  emits `recording.failed`.
- **Reconcile** — `python manage.py recordings_reconcile [--once]` re-emits
  `recording.stage` for recordings stuck past `STUCK_THRESHOLD_SECONDS`
  (index read from `metadata['pipeline']['stage_index']`) and fails
  abandoned uploads.

### GDPR

`RecordingsGDPRProvider` (section `recordings`) is registered in
`ready()`; `@on_action("user.deleted")` hard-deletes the user's recordings
and their storage objects (via the seam). Consumer + provider are both
required because this module holds user data.

## Anti-patterns

- **Don't fork to change the pipeline** — reorder/insert/replace stages via
  `PIPELINE` / `STAGES` / `register_stage` / `PIPELINE_RESOLVER`.
- **Don't re-implement STT or summarization here** — call `llm.transcribe`
  / `llm.summarize`. Adding a provider adapter belongs in stapel-agent.
- **Don't talk to boto3 / a bucket directly** — go through `get_storage()`.
- **Don't import other stapel modules** — comm by string name only.
- **Don't `os.getenv` at import time** — use the `STAPEL_RECORDINGS`
  namespace.
- **Don't inline-publish pipeline events** — always through the outbox.
- **Don't split a `save()` from its `emit_*()`** — keep the mutation and its
  event in one `transaction.atomic()` / `stapel_core.comm.mutate_and_emit()`
  unit (`run_stage`, and the terminal `_finalize` / `_dlq` helpers). CI gates
  this with `python -m stapel_core.lint.emit_check .`.

## App-layer (not in this module)

These were app-specific in the the legacy recordings service origin and are intentionally
**not** ported; build them in the host project:

- **Zoom / Meet / Teams ingestion** (OAuth, webhooks, TOFU account binding)
  — an app-layer source that creates a `Recording`, downloads to storage,
  and calls `finalize_upload`. Emits into the same pipeline.
- **Credits / billing** — react to `recording.completed` /
  `recording.stage_completed` in the billing module.
- **Share links** and **export formats** (SRT/VTT/DOCX/PDF) — app-layer
  views over the stored transcript JSON.
- A **real diarizer** (pyannote, etc.) — register a `diarize` stage handler.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam: a settings
key, a registered stage, a subclass + URL remount, a comm subscriber, a
custom storage/normalizer/resolver.

**Upstream** if it needs new model fields/migrations, a new endpoint, a new
settings key or seam, or changes a committed schema.

Litmus: if you'd monkeypatch or edit code inside `stapel_recordings/` — it's
upstream. If a setting, `register_stage`, subclass, receiver or comm call
gets you there — it's app-layer.
