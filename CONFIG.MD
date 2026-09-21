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
| MAX_CONTAINER_UPLOAD_BYTES | env | ceiling on what we **receive** while audio-only ingest runs (the container is discarded after extraction, so this bounds bandwidth and ffmpeg time). | no | 17179869184 |
| MAX_STORED_BYTES | env | ceiling on what we **keep** — enforced on the extracted audio by the convert stage (≈47 h at the Opus profile). | no | 536870912 |
| MAX_UPLOAD_BYTES | env | ceiling when nothing is extracted (`AUDIO_ONLY_INGEST` off), where received and stored are the same object. | no | 2147483648 |
| UPLOAD_EXTENSION_ALLOWLIST | env | accepted upload file extensions (lower-case, no dot). | no | mp3, m4a, wav, ogg, … (audio/video) |

`services.accepted_upload_limit()` picks between `MAX_CONTAINER_UPLOAD_BYTES`
and `MAX_UPLOAD_BYTES` and is the single source of the `413` line and of
`GET /recordings/api/v1/recordings/upload-limits`. Never read either setting
directly for that: the helper falls back to the smaller number on its own
when a deployment stops extracting (no `convert` stage, or a passthrough
NORMALIZER), so the raised ceiling cannot outlive the promise that the
container is thrown away. Check `stapel_recordings.E005` refuses a part
budget that cannot cover the accepted ceiling.

### Audio-only ingest — what is KEPT (source: env)

This module stores audio. A container is transport: its audio track is
extracted, downmixed to mono and stored, and the container is deleted —
every upload, whatever its size.

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| AUDIO_ONLY_INGEST | env | keep only the extracted audio and delete the uploaded container. **Not read from the environment** (`no_env`). Off is a documented exception: the upload becomes the stored object and the accepted ceiling drops to MAX_UPLOAD_BYTES. | no | true |
| AUDIO_CHANNELS | env | channels of the stored object. Mono unless a host's provider separates speakers BY CHANNEL. | no | 1 |
| AUDIO_SAMPLE_RATE | env | sample rate of the stored object (Hz). | no | 16000 |
| AUDIO_CODEC | env | `opus` (Ogg/Opus, ~10.8 MB/hour) or `wav` (16-bit PCM, ~115 MB/hour). | no | opus |
| AUDIO_BITRATE_BPS | env | Opus target bitrate; ignored for `wav`. | no | 24000 |

Checks: `stapel_recordings.E006` fails the deploy when `NORMALIZER` is
`ffmpeg_normalize` and ffmpeg/ffprobe are not on PATH, or the build has no
`libopus` for `AUDIO_CODEC = "opus"`. `stapel_recordings.E007` fails it when
`AUDIO_ONLY_INGEST` is on but nothing extracts (`convert` missing from
`PIPELINE`, or a passthrough NORMALIZER). `python manage.py
recordings_audio_census` reports, read-only, what containers are still
stored and what they would weigh as mono audio.

### Transcription / summarization / watchdog (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| TRANSCRIBE_TIMEOUT_SECONDS | env | per-transcribe-stage timeout. | no | 1800 |
| MAX_STAGE_RETRIES | env | max retries per pipeline stage. | no | 3 |
| TRANSCRIBE_TASK_MAX_ATTEMPTS | env | how many times the TASK primitive re-runs a priced transcription. ONE: the call is not idempotent at the transport level, so a transport retry is a second invoice for the first transcript. This setting and MAX_STAGE_RETRIES MULTIPLY — `stages.transcribe_attempt_ceiling()` is that product, and production measured it as six paid calls for one recording. | no | 1 |
| EMPTY_TRANSCRIPT_MIN_AUDIO_SECONDS | env | audio at least this long that transcribes to no words and no utterances is a FAILED stage (`empty_transcript`, recording parked in `error` with `recording.failed`), not a completed recording with `segments_count=0`. Shorter clips, and clips nobody measured, may be silent. 0 disables. | no | 5.0 |
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
