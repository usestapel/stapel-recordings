## What this is

**An audio service, not a video host.** Upload whatever your users record
on — a screen capture, a phone video, a voice memo — and this module keeps
the audio track and only the audio track: extracted, downmixed to mono,
stored once. The container is transport. It is deleted as soon as its audio
is out, there is no field and no URL that hands it back, and that holds for
a 4 GB screen recording and a 3 MB voice memo alike. A host's storage bill
therefore scales with **hours of speech** (~10.8 MB/hour at the default
Opus profile), not with what the recording happened to be made on.

Owns the lifecycle **capture/upload → storage → transcribe → summarize**:
`Recording` + `Speaker` + `Segment` (the unified transcript), presigned /
multipart upload sessions, and a data-driven, outbox-backed processing
pipeline with retry, DLQ and reconcile.

Speech-to-text and summarization are **delegated to
[stapel-agent](https://github.com/usestapel/stapel-agent)** via the
`llm.transcribe` / `llm.summarize` comm Functions — this module does not
implement STT or LLM calls. Object storage goes through a swappable seam.

## Uploads: two ceilings, because two different things

Because the container is discarded, "what we accept" and "what we keep" are
different questions and one number cannot answer both:

| Setting | Default | Bounds |
| --- | --- | --- |
| `MAX_CONTAINER_UPLOAD_BYTES` | 16 GiB | what we are willing to **receive** and run through extraction — bandwidth, temp disk and ffmpeg time |
| `MAX_STORED_BYTES` | 512 MiB | what we are willing to **keep**, enforced on the extracted audio (≈47 h at the Opus profile) |
| `MAX_UPLOAD_BYTES` | 2 GiB | the ceiling when nothing is extracted (`AUDIO_ONLY_INGEST = False`), where received and stored are the same object |

`services.accepted_upload_limit()` picks between the first and the last, and
it is that number the `413` quotes. It fails safe: drop `convert` from the
`PIPELINE`, or point `NORMALIZER` at `passthrough_normalize`, and the
accepted ceiling falls back to the storage-shaped one on its own — the
raised limit cannot outlive the promise that made it large. System check
`stapel_recordings.E007` tells the operator they are in that state, and
`E006` refuses a deploy whose ffmpeg is missing or has no `libopus`.

**Read the limits before uploading**, so a client refuses an oversized file
locally and names the real number:

```
GET /recordings/api/v1/recordings/upload-limits
{"max_upload_bytes": 17179869184, "max_stored_bytes": 536870912,
 "audio_only_ingest": true, "stored_audio_codec": "opus",
 "stored_audio_channels": 1, "stored_audio_sample_rate": 16000,
 "stored_bytes_per_hour": 10800000, "multipart_part_size": 10485760,
 "max_multipart_parts": 10000, "allowed_extensions": ["3gp", "aac", …]}
```

A refusal is an **answer**, not a 500: every upload error this module raises
is a DRF-aware `StapelServiceError`, so a host view that calls
`services.start_multipart_upload` directly answers `413`
`error.413.recording_too_large` with `{size, limit}` in the standard
envelope, with no `try/except` of its own (`400` for a size that is not a
size or a malformed part list, `415` for the file type, `409` for nothing
stored, `503` when the content gate could not run).

### The stored profile

Mono, 16 kHz, Ogg/Opus at 24 kbps — `AUDIO_CHANNELS`, `AUDIO_SAMPLE_RATE`,
`AUDIO_CODEC`, `AUDIO_BITRATE_BPS`. Mono is the default because every
downstream consumer here reads a single mixed track: diarization is the ASR
provider separating speakers within it, not channel separation, and this
module has downmixed since its first release. A host whose provider *does*
separate by channel sets `AUDIO_CHANNELS = 2` and pays for it in bytes.
`AUDIO_CODEC = "wav"` restores 16-bit PCM (~115 MB/hour) for a provider that
will not take Opus.

Every upload is re-encoded to the profile, including one that arrives as
audio already: a "this one is fine as it is" branch would have to be right
about container, codec, channel layout and sample rate at once, and it would
make the stored bytes depend on what the client happened to send.

To keep originals anyway — a documented exception, off by default — set
`AUDIO_ONLY_INGEST = False`; the accepted ceiling drops to `MAX_UPLOAD_BYTES`
in the same move.

`python manage.py recordings_audio_census` reports, read-only, how many
recordings still hold an uploaded container, what they weigh, and what the
same recordings would occupy as mono audio.

## Quick start

The base install uses the Django-storage backend; add the `s3` extra for the
boto3 S3/MinIO backend:

```bash
pip install "stapel-recordings[s3]"
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_core.django.outbox",   # transactional outbox (pipeline reliability)
    "stapel_recordings",
]

# urls.py
path("recordings/", include("stapel_recordings.urls"))
```

The `transcribe` / `merge` stages call stapel-agent by comm name — install
and configure stapel-agent (or provide `llm.transcribe` / `llm.summarize`
providers) for the pipeline to complete. The default `convert` stage needs
`ffmpeg`/`ffprobe` on PATH (or set `NORMALIZER` to `passthrough_normalize`).

## The pipeline is data you can edit

```python
STAPEL_RECORDINGS = {
    # Reorder / subset / insert stages — no fork:
    "PIPELINE": ["convert", "transcribe", "redact_pii", "merge"],
    # Replace or add stage handlers (merge-over-builtins; None removes):
    "STAGES": {"diarize": "myproject.stages.PyannoteDiarizer"},
    # Or source the list at runtime (DB / per-workspace / per-recording):
    "PIPELINE_RESOLVER": "myproject.pipelines.resolve",
    # Swap the object store:
    "STORAGE": "stapel_recordings.storage.S3Backend",
}
```

A generic driver runs the resolved stage list, advancing the status machine
and emitting the next stage through the outbox. A stage that has already
run is skipped only while its INPUT has not changed: each completion
records the stage's `input_fingerprint` (content hash + parameters), so a
re-run over a different source re-runs every stage from the one that
changed — and a retry over the same source resumes without re-buying the
priced call. `pipeline.invalidate_from(recording_id, stage)` declares a
stage stale for the cases a fingerprint cannot see. See
[MODULE.md](https://github.com/usestapel/stapel-recordings/blob/main/MODULE.md)
for the stage contract and worked examples.

## Settings

All configuration lives in the `STAPEL_RECORDINGS` namespace (dict setting,
flat setting, or env var — resolved lazily). See the full table in
[MODULE.md](https://github.com/usestapel/stapel-recordings/blob/main/MODULE.md).
Highlights: `PIPELINE`, `STAGES`, `PIPELINE_RESOLVER`, `STORAGE`,
`NORMALIZER`, `SUMMARIZE_ENABLED`, `MAX_STAGE_RETRIES`.

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Action (emit) | `recording.uploaded`, `recording.stage_completed`, `recording.completed`, `recording.failed` | pipeline lifecycle (public); the run events carry `run_id` + `attempt` — a reprocess is a new run, so meter on `recording_id` + `run_id` |
| Action (consume) | `recording.uploaded`, `recording.stage`, `user.deleted` | driver + GDPR erase |
| Function (call) | `llm.transcribe`, `llm.summarize` | provided by stapel-agent |

## Operations

```bash
python manage.py recordings_reconcile --once   # re-drive stuck recordings
```

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff jsonschema djangorestframework
./setup-hooks.sh
pytest tests/
```
