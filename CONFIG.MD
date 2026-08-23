# CONFIG.MD — stapel-recordings

Config registry for **stapel-recordings** (`static-scaffold-and-config.md` §2).
One row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_RECORDINGS` settings dict), what it is for,
whether it is required, and its default.

Most keys are read through `recordings_settings`
(`stapel_recordings.conf.AppSettings`, namespace `STAPEL_RECORDINGS`).
Resolution order per key: `settings.STAPEL_RECORDINGS` dict → a flat Django
setting of the same name → environment variable → the default below — except
for the keys marked `no_env`, which skip the environment step entirely (see
*Authorization switches* below, and the `no_env` tuple in `conf.py`).

## stapel-recordings

### Pipeline & seams (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| PIPELINE | env | ordered stage-name list run by the generic driver (reorder/drop/insert stages). | no | [convert, transcribe, diarize, merge] |
| STAGES | env | overlay of custom/replacement stage handlers {name: dotted-path}; None removes a built-in. | no | {} |
| PIPELINE_RESOLVER | env | dotted path to `(recording) -> list[str]` — the runtime pipeline seam. | no | stapel_recordings.pipeline.default_pipeline_resolver |
| STORAGE | env | dotted path to a RecordingStorage implementation (REPLACE). | no | stapel_recordings.storage.DjangoStorageBackend |
| STORAGE_PREFIX | env | object-key prefix for stored recordings. | no | recordings |
| NORMALIZER | env | dotted path to `(src, dst) -> float|None` audio normalizer (REPLACE). | no | stapel_recordings.normalize.ffmpeg_normalize |
| SOURCE_TYPES | env | recording source-kind registry merged OVER the built-ins {key: label}. | no | {} |

### Uploads (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| UPLOAD_SESSION_TTL_SECONDS | env | single-shot upload session lifetime. | no | 900 |
| MULTIPART_SESSION_TTL_SECONDS | env | multipart upload session lifetime. | no | 86400 |
| MULTIPART_PART_SIZE | env | multipart part size (bytes). | no | 10485760 |
| MAX_UPLOAD_BYTES | env | hard cap on an upload's total size. | no | 2147483648 |
| UPLOAD_EXTENSION_ALLOWLIST | env | accepted upload file extensions (lower-case, no dot). | no | mp3, m4a, wav, ogg, … (audio/video) |

### Transcription / summarization / watchdog (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| TRANSCRIBE_TIMEOUT_SECONDS | env | per-transcribe-stage timeout. | no | 1800 |
| MAX_STAGE_RETRIES | env | max retries per pipeline stage. | no | 3 |
| SUMMARIZE_ENABLED | env | whether the summarize step runs (delegated to stapel-agent). | no | true |
| SUMMARIZE_MODEL | env | agent model size used for summaries. | no | medium |
| STUCK_THRESHOLD_SECONDS | env | reconcile watchdog: age at which a running stage is re-emitted (must exceed the longest stage). | no | 2100 |
| ABANDONED_UPLOAD_THRESHOLD_SECONDS | env | age at which an incomplete upload session is abandoned. | no | 3600 |

### ffmpeg shell-out (source: env — but never the process environment)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| FFMPEG_BIN | env | ffmpeg binary path/name. **Not read from the environment** (`no_env`). | no | ffmpeg |
| FFPROBE_BIN | env | ffprobe binary path/name. **Not read from the environment** (`no_env`). | no | ffprobe |
| FFMPEG_TIMEOUT_SECONDS | env | subprocess timeout (seconds) for an ffmpeg/ffprobe call. | no | 1800 |

### Authorization switches (source: env — but never the process environment)

Every key in this section is `no_env`: it resolves from
`settings.STAPEL_RECORDINGS`, a flat Django setting, or its default, and a
same-named environment variable is ignored. Each one decides which code runs
or what is handed out, and each has a generic enough name to collide in a
shared pod. Booleans are read through `conf.flag` / `conf.optional_flag`, so
the string `"false"` means false.

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| RECORDING_POLICY | env | dotted path to the class answering may-this-user-read/edit/delete/upload/reprocess. `can_reprocess`/`can_resummarize` may answer with a `PolicyDecision` carrying the refusal's status + error key (402 for an empty balance); a bare bool still means the fail-closed 404. | no | stapel_recordings.policy.OwnerOnlyPolicy |
| REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE | env | verify membership of the workspace `POST /recordings` writes into. False only where no workspaces module can answer. | no | true |
| WORKSPACE_LISTING_MEMBERS_SEE_ALL | env | `?workspace_id=` returns every member's recordings instead of what RECORDING_POLICY makes visible. | no | false |
| UPLOAD_CONTENT_POLICY | env | finalize-time gate on the stored object's leading bytes: reject_known_bad / require_known_media / off. `off` also waives the refusal when the backend cannot serve a ranged read. | no | reject_known_bad |
| STORAGE_SIGNS_GET_URLS | env | tri-state override of `RecordingStorage.signs_get_urls`; True vouches that the backend mints EXPIRING GET URLs. | no | null (ask the backend) |

### Erasure & retention (source: env — `ERASURE_CLIENT` never from the process environment)

A soft-deleted recording is not a deleted one. `PURGE_AFTER_DAYS` is how long
it is kept, and the scheduled purge
(`stapel_recordings.tasks.purge_soft_deleted_recordings`, wired with
`get_recordings_beat_schedule()`) does not delete it — it opens a gdpr erasure
through `ERASURE_CLIENT`, so the destruction is receipted by every data owner
that claims the `recording` subject. `ERASURE_CLIENT` is `no_env` for the same
reason the other dotted-path seams are: a stray value unhooks retention from
the receipts path without anything looking wrong.

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| PURGE_AFTER_DAYS | env | days a soft-deleted recording is kept before the purge opens a gdpr erasure for it. | no | 30 |
| PURGE_SCHEDULE | env | crontab kwargs for the purge's beat entry (`get_recordings_beat_schedule`). | no | {"hour": 4, "minute": 20} |
| ERASURE_CLIENT | env | dotted path to the ErasureClient that OPENS erasures (never deletes directly). | no | stapel_recordings.erasure.GDPRErasureClient |
