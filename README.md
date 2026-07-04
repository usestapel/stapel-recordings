# stapel-recordings

Recording lifecycle and transcription for the [Stapel framework](https://github.com/usestapel) —
composable Django apps that deploy as a monolith or as microservices
without changing module code.

Owns the lifecycle **capture/upload → storage → transcribe → summarize**:
`Recording` + `Speaker` + `Segment` (the unified transcript), presigned /
multipart upload sessions, and a data-driven, outbox-backed processing
pipeline with retry, DLQ and reconcile.

Speech-to-text and summarization are **delegated to
[stapel-agent](https://github.com/usestapel/stapel-agent)** via the
`llm.transcribe` / `llm.summarize` comm Functions — this module does not
implement STT or LLM calls. Object storage goes through a swappable seam.

## Install

```bash
pip install stapel-recordings          # default: Django-storage backend
pip install "stapel-recordings[s3]"    # + boto3 S3/MinIO backend
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
and emitting the next stage through the outbox. See **[MODULE.md](MODULE.md)**
for the stage contract and worked examples.

## Settings

All configuration lives in the `STAPEL_RECORDINGS` namespace (dict setting,
flat setting, or env var — resolved lazily). See the full table in
[MODULE.md](MODULE.md). Highlights: `PIPELINE`, `STAGES`,
`PIPELINE_RESOLVER`, `STORAGE`, `NORMALIZER`, `SUMMARIZE_ENABLED`,
`MAX_STAGE_RETRIES`.

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Action (emit) | `recording.uploaded`, `recording.stage_completed`, `recording.completed`, `recording.failed` | pipeline lifecycle (public) |
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

## License

MIT
