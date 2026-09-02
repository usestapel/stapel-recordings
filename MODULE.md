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
- A REST surface (create + upload session, detail, list — owner- /
  workspace- / `resource_key`-scoped —, finalize, reprocess, resummarize)
  with serializer seams, and a GDPR provider + `user.deleted` consumer.
- An **opt-in vector/search layer** (`stapel_recordings.vector` — a
  separate Django app + the `[vector]` extra): segment/summary embeddings
  written by the `embed` pipeline stage via `llm.embed`, and a hybrid
  (FTS + pgvector cosine, RRF-fused) segment search service. Zero burden
  when not installed — see the section below.

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
   (default `["convert", "transcribe", "diarize", "merge", "embed"]`). Drop
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
at-least-once. `status` should be a `RecordingStatus`-compatible transient
value; `reconcile` treats *any* status that is neither pre-pipeline
(`created`/`uploading`) nor terminal (`completed`/`error`/`deleted`) as
in-pipeline, so custom statuses are re-driven too.

**Editing pipelines under live recordings — the cursor contract.**
Because the list can change at runtime (resolver/UI), the driver's progress
cursor is **stage names, not positions**: the names of completed stages are
persisted on `metadata.pipeline.completed` (plus `completed_index`, the
position of the last completion, used as a duplicate-delivery guard) in the
same transaction as the stage's success events. On every delivery the
driver runs the *first stage in the currently resolved list that has not
completed*. Consequences — all safe with in-flight recordings:

- **Remove a stage**: it is skipped (a warning is logged when the removed
  stage had already started); already-completed work is kept. Removal is
  treated as operator intent, not an error.
- **Insert a stage** (even before the cursor): it runs; completed stages
  never re-run. The list is declarative — each named stage runs at most
  once per recording.
- **Reorder**: remaining stages run in the new order.
- **Empty list from the resolver**: treated as a misconfiguration → DLQ
  (`empty_pipeline`), never a silent `completed`. Recover with
  `retry_recording()` after fixing the config.
- Stage names must be **unique within one pipeline** (a repeated name is
  considered already-completed by the second occurrence).

A crashing `PIPELINE_RESOLVER` parks the recording as a retryable failure
(bounded by `MAX_STAGE_RETRIES`, then DLQ) instead of crash-looping the
delivery in the outbox. A broken dotted-path in the `STAGES` overlay only
affects pipelines that include that stage (`get_stage` imports lazily);
those recordings DLQ as `unresolvable_stage`.

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

The five built-ins:

| Stage | Status | Does | Delegates to |
|---|---|---|---|
| `convert` | `normalizing` | Normalize media to 16 kHz mono WAV (`NORMALIZER` seam), store it, drop the raw | ffmpeg (default) |
| `transcribe` | `transcribing` | Call `llm.transcribe`, persist `Speaker`/`Segment` rows | **stapel-agent** |
| `diarize` | `diarizing` | **No-op by default** (diarization is returned inline by `llm.transcribe`); swap in a real diarizer via the registry | — |
| `merge` | `merging` | Build + store the unified transcript JSON, then `llm.summarize` | **stapel-agent** |
| `embed` | — (keeps prior) | **No-op unless** the opt-in vector app is installed AND `VECTOR["ENABLED"]` (the diarize pattern); then batches segment texts + the chunked summary through `llm.embed` and upserts embedding rows (content-hashed, idempotent) | **stapel-agent** |

### Settings — `STAPEL_RECORDINGS` namespace (`conf.py`)

Resolution per key: `settings.STAPEL_RECORDINGS[key]` → flat Django setting →
env var → default. Lazy; caches invalidate on `setting_changed`.

| Key | Default | Semantics | Customizes |
|---|---|---|---|
| `PIPELINE` | `["convert","transcribe","diarize","merge","embed"]` | value | Ordered stage list |
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
| `STUCK_THRESHOLD_SECONDS` | `2100` | value | Reconcile: age before re-drive. **Must exceed the longest stage duration** (built-ins: `TRANSCRIBE_TIMEOUT_SECONDS`), or reconcile re-drives stages that are still running (checked: W005) |
| `ABANDONED_UPLOAD_THRESHOLD_SECONDS` | `3600` | value | Reconcile: abandoned-upload age |
| `PURGE_AFTER_DAYS` | `30` | value | Days a soft-deleted recording is kept before the purge opens an erasure (below) |
| `PURGE_SCHEDULE` | `{"hour": 4, "minute": 20}` | value | Crontab kwargs for the purge's beat entry |
| `ERASURE_CLIENT` | `…erasure.GDPRErasureClient` | **replace** (dotted path) | Who is asked to OPEN an erasure (never deletes directly) |
| `POLL_INTERVAL_SECONDS` | `5` | value | The hint a client is given while the pipeline owns the recording (`Retry-After` + `poll_after_seconds`) |
| `JOB_POLL_INTERVAL_SECONDS` | `10` | value | `Retry-After` on a 202 that accepted background work |
| `TRANSCRIPT_PAGE_SIZE` | `200` | value | Default page of the owner transcript read |
| `TRANSCRIPT_MAX_PAGE_SIZE` | `1000` | value | Ceiling a client's `?limit=` is capped to |
| `S3_*` | — | value | Config for the optional `S3Backend` |
| `VECTOR` | see `DEFAULT_VECTOR` | **merge** (one nested level, via `vector_config()`) | Tuning block for the opt-in vector layer (below) |

### Storage seam — `RecordingStorage` (`storage.py`)

Single-strategy **replace** seam via `STORAGE`. Ships `DjangoStorageBackend`
(default — rides Django's `default_storage`; presigned URLs degrade to the
served URL; synthetic multipart shim) and `S3Backend` (boto3 presigned +
native multipart; `pip install stapel-recordings[s3]`). Implement the ABC to
target any store. `get_storage()` resolves + caches it.

### Media delivery — `media.py` (audit STORE-01)

**The bucket is private; the endpoint is the door.** Bytes are reached only
through `GET .../recordings/<id>/media` (object policy `can_read`) or
`GET .../shares/<token>/media` (a share that grants `media`) — each
authorizes first, then mints a **short-lived presigned GET** via
`media.issue_media_url`. `?redirect=1` answers 302 for an `<audio src>`;
the default JSON body carries the expiry so a client can refresh in time.
TTLs are settings and short by default: `MEDIA_URL_TTL_SECONDS` (300),
`SHARE_MEDIA_URL_TTL_SECONDS` (300).

A URL, once minted, is a bearer credential — nothing checks the policy
again — so a backend that cannot bound it in time is **refused**:
`RecordingStorage.signs_get_urls` declares whether `presigned_get_url`
really signs, and a backend that says no yields 503, never a permanent URL.
`DjangoStorageBackend` says no (its `storage.url()` never expires), so the
default configuration serves no media at all. Deliberate: a host on
S3/MinIO uses `S3Backend`; a host whose django-storages backend genuinely
signs sets `STORAGE_SIGNS_GET_URLS = True`.

The transcribe stage's audio URL for the ASR provider is presigned too
(`TRANSCRIBE_AUDIO_URL_TTL_SECONDS`, must exceed
`TRANSCRIBE_TIMEOUT_SECONDS` — W007 warns).

### Reading a transcript — `GET .../recordings/<id>/transcript`

**The owner's transcript is reachable without publishing it.** Until 0.20.0
speaker-attributed segments left this module through exactly one door — the
projection inside a public share link — so an owner who wanted to render
their own transcript had to create a share to themselves.
`RecordingDTO.transcript_storage_key` was not a second door: it is a raw
object key, and nothing signs it (the media endpoint signs the *media*
object).

Authority is the same as every other per-recording read: owner scope
(`RECORDING_POLICY.visible_queryset`) **and then** `can_read`, so a host that
narrows the policy narrows this with it, and an unknown / foreign / deleted
recording is a 404. A recording with no segments yet answers `200` with an
empty page — "not transcribed yet" is a normal stage, and a 404 there would
be indistinguishable from "not yours".

The wire shape is `TranscriptSegmentDTO` (`sequence_num`, `start_time`,
`end_time`, `speaker`, `text`) — **the same DTO the share projection
carries**, so a transcript renderer is written once and serves both doors.
`speaker` is already flattened to the name a reader prints: the human
`display_name` when a voice has been named, the provider's own label
(`speaker_0`) otherwise.

Pages are anchored on `sequence_num`, ascending (`TranscriptPagination` over
core's `AnchorPagination`): a transcript is read forward, so `direction=next`
walks *later* segments, and the anchor is what keeps a page stable while the
pipeline is still appending to the end. `anchor` / `limit` / `direction` are
declared in the schema, not merely accepted — an undeclared parameter is one
that disappears from every generated client.

### Progress — polling, and the module says when

There is no WebSocket here (no consumer, no routing module, no Channels
dependency), so a client learns that a recording moved by **reading it
again**. That interval is part of the contract rather than something each
frontend invents:

- `RecordingDTO.is_processing` / `RecordingDTO.poll_after_seconds` on every
  recording payload, and a `Retry-After` header carrying the same number —
  one computation (`dto.poll_after_seconds`) behind both, so they cannot
  disagree;
- present **only** while the pipeline owns the next transition
  (`RecordingStatus.is_processing`: `queued`, `analyzing`, `normalizing`,
  `transcribing`, `diarizing`, `merging`). `created` / `uploading` wait on the
  client's own upload and `completed` / `error` / `deleted` are terminal — for
  those the field is `null` and the header is absent, which is how a client is
  told to **stop**;
- `GET .../transcript` carries the same hint, because a client watching a
  transcript fill in polls the transcript, not the recording;
- a `202` from `/resummarize` carries `Retry-After:
  JOB_POLL_INTERVAL_SECONDS` — accepted is not finished, and the receipt says
  when the result is worth looking for.

### Audio normalization seam — `NORMALIZER` (`normalize.py`)

`(src_path, dst_path) -> float | None`. Default `ffmpeg_normalize` (needs
ffmpeg/ffprobe on PATH); `passthrough_normalize` for environments without
ffmpeg or already-normalized input. Raise `NormalizeFatal` for unfixable
input.

### Vector/search layer — opt-in app (`vector/`)

Semantic + full-text search over transcripts, packaged so that hosts who
don't want it pay **nothing**: no pgvector import, no extra tables, no new
dependency — the `embed` stage in the default pipeline is a no-op (exactly
the diarize pattern). The base sqlite test suite runs (and must stay green)
with the extra absent.

**Opt in** (all four steps host-side):

```python
# pip install stapel-recordings[vector]        # pulls pgvector
INSTALLED_APPS = [..., "stapel_recordings", "stapel_recordings.vector"]
STAPEL_RECORDINGS = {"VECTOR": {"ENABLED": True, "DIM": 1536, "MODEL": "..." }}
# then: manage.py migrate   (postgres only — the app's 0001 runs the
# standard `CREATE EXTENSION IF NOT EXISTS vector`, vendor-guarded)
```

**Models** (`vector/models.py`): `SegmentEmbedding` (FK `Segment`,
`VectorField(DIM)`, `model`, `scheme`, `content_hash`, unique per
segment+model+scheme+chunk_index, HNSW cosine index with `VECTOR["HNSW"]`
build params) and `RecordingEmbedding`
(summary chunks: FK `Recording`, `chunk_index`, `text_hash`, `vector`,
`model`, unique per recording+model+chunk). `DIM`/HNSW are read from
settings by **both** the models and the migration — set them before the
first migrate; changing `DIM` later is a host migration + re-embed.

**Embed stage**: after `merge`. Batches segment texts (`BATCH_SIZE`, and
`BATCH_MAX_CHARS` when set — a server budgets a batch in tokens, so a count
tuned for short utterances over-fills once the unit gets longer) →
`call("llm.embed", {"texts", "model"?, "provider"?, "timeout_seconds"})`
(stapel-agent ≥ 0.4) → upserts rows. Idempotent under the outbox's
at-least-once delivery: every row stores the sha256 of the embedded text
and unchanged texts are never re-sent; an edited segment re-embeds only
itself. Comm failures → `StageRetryable`; a `dim` that contradicts
`VECTOR["DIM"]` → `StageFatal` (misconfiguration, retry can't fix it).
The summary is chunked (`SUMMARY_CHUNK_CHARS`/`_OVERLAP`, plain character
windows — smarter chunking is host know-how) and stale tail chunks are
pruned. Half-configured state (`ENABLED` without the app) → check W006.

**Embedded unit** (`VECTOR["SEGMENT_SCHEME"]`, `vector/chunking.py`): what
one row stands for.

- `"segment"` (default) — one STT utterance. Cheap, exact, and often far
  too small: the median utterance on a real deployment is 37 characters,
  which is neither an embedding worth ranking nor a passage anyone can
  answer from.
- `"window"` — consecutive utterances packed to `SEGMENT_WINDOW`
  `TARGET_CHARS` (600) and never past `MAX_CHARS` (800), with
  `OVERLAP_CHARS` of the previous window's whole utterances carried in, and
  a `[mm:ss Speaker]` prefix per line so the retrieved passage says who
  spoke and when. Rows are anchored at the run's first segment.

Rows carry the scheme that built them and search reads exactly one, so the
two coexist: `manage.py recordings_reembed --scheme window` builds the new
index while the live one keeps serving, and flipping `SEGMENT_SCHEME` (in
either direction) is what adopts or reverts it. Measure before flipping —
`manage.py recordings_search_eval` exists for that.

**Search** (`vector/search.py`): `search_recordings(query, *,
workspace_id=None, recording_ids=None, mode="hybrid"|"text"|"vector",
limit=20) -> [SearchHit(segment_id, recording_id, score, snippet)]`.

- *text* — postgres FTS over `Segment.text`. Matching is `tsvector @@
  tsquery` and `ts_rank` only orders what matched: `ts_rank` answers
  `1e-20`, not `0`, for a multi-term query that matches nothing, so it is
  not usable as a predicate and no threshold on it ever will be.
  `VECTOR["FTS_SEARCH_TYPE"]` shapes the query — `"plain"` (default,
  every term required), `"websearch"` (phrases + `-exclusion`), or
  `"any"` (terms OR'd, ranking rather than filtering decides). The FTS
  config resolves per recording's `language` via `VECTOR["FTS_CONFIGS"]`,
  which is keyed on ISO 639-1 — the language is NORMALIZED first
  (`stapel_recordings.languages.to_iso639_1`), so the ISO 639-2/3 tags
  speech-to-text actually emits (`rus`, `eng`, `spa`, `zho`) resolve
  instead of silently falling back to unstemmed `'simple'`. No stored
  search column — hosts at scale add their own GIN index /
  SearchVectorField. Off postgres this mode degrades to `icontains`
  (score 1.0) so it works everywhere.
- *vector* — the query goes through `llm.embed`, then cosine distance over
  `SegmentEmbedding` (score = 1 − distance). Candidates are restricted to
  rows whose `model` equals the model that embedded **this query** (the
  name `llm.embed` reports — exactly what the embed stage stamps), because
  two models are two incomparable spaces of the same width: without the
  filter a swapped embedder degrades ranking to noise instead of returning
  nothing. **Tenant reach**: an HNSW scan does not know about
  `workspace_id`, so the predicate runs on what the scan already returned
  and a small tenant loses nearly all of its candidates to the post-index
  filter. The arm therefore sets `hnsw.iterative_scan` (pgvector ≥ 0.8;
  `VECTOR["HNSW"]["ITERATIVE_SCAN"]`, bounded by `MAX_SCAN_TUPLES`) for the
  duration of its query, and on older pgvector widens `hnsw.ef_search` by
  the tenant's measured share of the corpus instead. Rows on an older model
  simply stop matching — rebuild them with
  `manage.py recordings_reembed --force` (`--dry-run` lists what is stored
  per model, `--prune-other-models --keep-model <name>` drops the old
  space). `VECTOR["SEARCH_MODEL_FILTER"] = False` opts out, e.g. to keep
  serving during a migration.
- *hybrid* — both arms fetch up to `VECTOR["ARM_LIMIT"]` candidates and are
  fused with **reciprocal-rank fusion**: `score = Σ_arm WEIGHT_arm /
  (RRF_K + rank_arm)` — rank-based, so the arms' incomparable score scales
  need no calibration. A key repeated inside one arm's list counts once,
  at its best rank — RRF is defined over distinct documents, and summing a
  repeat lets it leapfrog every genuine hit. Knobs: `RRF_K` (60),
  `RRF_WEIGHTS` (`{"text": 1.0, "vector": 1.0}`).

**Evaluation** (`vector/evaluation.py`, `manage.py
recordings_search_eval`): recall@k and MRR over a labeled question set, so
that "this ranking is better" is a measurement rather than an opinion.
Questions are labeled with `(recording_id, start, end)` TIME SPANS, not row
ids — re-chunking the index changes which row carries a passage, and an
id-labeled set would stop measuring the moment the thing it exists to
measure changes. `--set KEY=VALUE` A/Bs any `VECTOR` key for one run
(`--set SEGMENT_SCHEME=window`), `--mode` scores arms separately, `--json`
makes a before/after pair a diff. Run it against a restored COPY of real
data — it is one embedding call per question per mode.

**Optional rerank** (`VECTOR["RERANK"]`, default off): one post-ranking
pass in **every** mode (rerank is provider-agnostic quality — the top
`TOP_K` candidates go to the reranker regardless of arm). The `TOP_K`
best hits' *full segment texts* go through `call("llm.rerank", ...)`
(stapel-agent ≥ 0.5) and that block is re-ordered by rerank score
(`SearchHit.score` becomes the rerank score, `reranked=True`); hits the
reranker didn't score (`TOP_N` cut, or beyond `TOP_K`) follow in their
pre-rerank order, then the result truncates to `limit`. `FAIL_OPEN`
(default True): any rerank failure logs a warning and returns the
un-reranked order; `False` raises `VectorSearchUnavailable`. Privacy:
segment texts DO go to the rerank provider — the same trust boundary as
`llm.transcribe`/`llm.summarize`. Knobs: `ENABLED` (False), `PROVIDER`
(`""` = agent default), `TOP_K` (50), `TOP_N` (20; 0 = score all),
`TIMEOUT_SECONDS` (60), `FAIL_OPEN` (True).

On sqlite or with the app absent, `vector`/`hybrid` raise
`VectorSearchUnavailable` — a decidable error, never a silently empty
result; hosts choose their degradation.

**Testing**: logic-level tests (batching, hash-skip idempotency, chunking,
RRF, error mapping, sqlite degradation) run on the canonical sqlite suite
with the comm seam stubbed and persistence faked at the `get_store()` seam.
DB-bound tests (real VectorField, FTS ranking, cosine ordering, the
extension/HNSW migration) are explicitly vendor-gated in
`tests/test_vector_postgres.py` and run under
`STAPEL_RECORDINGS_TEST_DB=postgres://… python3 -m pytest` (the env var
also installs the vector app and pins test `DIM=3`).

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
| Action (emit) | `recording.stage_completed` | public, per-stage; carries `run_id` + `attempt` | `schemas/emits/recording.stage_completed.json` |
| Action (emit) | `recording.completed` | public terminal; carries `run_id` + `attempt` (the metering key) | `schemas/emits/recording.completed.json` |
| Action (emit) | `recording.failed` | public DLQ terminal; carries `run_id` + `attempt` | `schemas/emits/recording.failed.json` |
| Action (emit) | `recording.resummarized` | public — a summary was regenerated on request (a host meters/bills on this; `job_id` is the idempotency key) | `schemas/emits/recording.resummarized.json` |
| Action (consume) | `recording.uploaded` | start the pipeline | — |
| Action (consume) | `recording.stage` | run stage N (driver) | — |
| Action (consume) | `gdpr.erasure.requested` | erase one subject + receipt (owned by **stapel-gdpr**) | `schemas/consumes/gdpr.erasure.requested.json` |
| Action (consume) | `gdpr.owner.probe` | answer `gdpr.owner.alive` from the erasure subscriber | `schemas/consumes/gdpr.owner.probe.json` |
| Action (consume) | `user.deleted` | account erase, deprecated (removed with gdpr 0.6.0) | `schemas/consumes/user.deleted.json` |
| Action (emit) | `gdpr.section.erased` | this owner's receipt, with counts | owned by **stapel-gdpr** |
| Action (emit) | `gdpr.owner.alive` | probe answer: `{owner, subject_types}` | owned by **stapel-gdpr** |
| Function (**call**) | `llm.transcribe` | STT | provided by **stapel-agent** |
| Function (**call**) | `llm.summarize` | summary | provided by **stapel-agent** |
| Function (**call**) | `llm.embed` | embeddings (opt-in vector layer only) | provided by **stapel-agent** ≥ 0.4 |

### Reliability primitives

- **Outbox** — `emit` writes the event with the caller's DB transaction;
  the driver walks stages after commit. At-least-once + idempotent stages.
- **Completion cursor** — "stage N completed" is persisted (name + index)
  in the same transaction as the success events, so a duplicate delivery of
  a completed stage is a total no-op: no re-run, and crucially **no
  re-emit** of `recording.stage_completed`/`recording.stage` with fresh
  event_ids (safe to hang billing on `stage_completed`).
- **DLQ** — a `StageFatal` (or retries exhausted) sets `status=error` and
  emits `recording.failed`. `error` is **terminal for deliveries**: a
  redelivered event never resurrects a DLQ'd recording. The only way back
  is the explicit **`pipeline.retry_recording(recording_id)`** transition
  (`error → queued`, resumes at the first not-yet-completed stage) — expose
  it from an app-layer endpoint or admin action.
- **Run identity** — every pipeline run carries a `run_id` (uuid) and a
  monotonic `attempt`, minted when the run starts and kept in
  `workflow_state["pipeline"]` beside the cursor. Both travel in
  `recording.stage_completed` / `recording.completed` / `recording.failed`,
  and **`pipeline.run_identity(recording)`** reads the pair back. This is
  what makes a run billable: a recording can be run again
  (`reprocess_recording`), so without it the terminal event of run 2 is
  byte-identical to run 1's and a consumer metering post-hoc can only key on
  `recording:<id>` — its second debit short-circuits on the first run's
  transaction and every re-run is free. Key on `recording:<id>:<run_id>`.
  `retry_recording` deliberately keeps the identity: resuming a DLQ'd run is
  the *same* run, and finishing it must not be billed twice. A run that
  started before this existed (0.19.0) is backfilled with an id on its next
  driver write, so no in-flight recording completes without one.
- **Reprocess** — **`pipeline.reprocess_recording(recording_id)`** re-runs
  the whole pipeline from stage 0 for a **`completed`** recording
  (`completed → queued`, clears the progress cursor so every stage re-runs,
  mints a new `run_id` and increments `attempt`;
  the counterpart to `retry_recording`'s resume). Forbidden from any other
  status (no-op → `False`). Stages self-guard on persisted artifacts, so a
  host that wants derived data (segments/transcript/summary) regenerated
  clears the relevant keys as part of its reprocess flow — the module never
  destroys transcript data itself.
- **Re-summary (summarize only)** — **`stages.start_resummarize(recording,
  user=...)`**, exposed as `POST /recordings/api/v1/recordings/{id}/resummarize`.
  Re-runs the *same* `llm.summarize` call the `merge` stage makes, over the
  transcript already stored: no STT, no diarization, no pipeline, and the
  recording's `status` never moves. Reprocess is the wrong tool for the case
  that actually happens — the transcript is right (often because a human just
  corrected it) and only the summary built from an older transcript is out of
  date; reprocess would re-transcribe and bill for it, which is why the only
  regenerate path products ended up shipping was staff-only. Runs on the
  module's `Job` ledger (`type="summarize"`), not on the pipeline cursor, so
  it can neither move a status nor disturb a run in flight. **Idempotent**: a
  request arriving while one is in flight joins that Job — a double-clicked
  button costs one summary. Answers `409`
  (`error.409.recording_no_transcript`) when there is nothing to summarize
  and `503` (`error.503.recording_summarize_unavailable`) when the deployment
  has summaries off or the bus refused the work; `202` + a `JobDTO` when
  accepted. Authority is the object policy's `can_resummarize`, which
  delegates to `can_reprocess` unless a host splits them — and since 0.18.0
  a denial may state its reason: returning
  `PolicyDecision.deny("error.402.myhost_out_of_credits", status=402)`
  instead of `False` answers `402` with the host's own key, so the metered
  host that debits on `recording.resummarized` can also refuse for an empty
  balance without the UI being told the recording does not exist. A bare
  `bool` is still accepted and still renders the fail-closed `404`. Success emits
  `recording.resummarized` **inside the storing transaction**, which is the
  hook a host debits/captures credits on — this module imports no billing.
  Hosts driving a broker must route `task.completed`/`task.failed` through
  `stages.resume_resummarize` / `stages.fail_resummarize` **before**
  `pipeline.resume_stage` / `fail_stage` (this module's `actions.py` already
  does): a re-summary runs on a finished recording, whose status the pipeline
  driver treats as terminal.
- **The summary carries its version key** — `stages.store_summary()` is the
  one writer for a produced summary, and it pins
  `metadata["derived"]["summary"]["transcript_hash"]` (the canonical
  transcript's own hash) plus clears `metadata["staleness"]["summary"]`.
  Staleness is therefore *computed* — recorded key vs current key — so an
  edit path that never heard of staleness still invalidates the summary.
  Both keys are in `metadata.LIBRARY_RESERVED_KEYS`: a client can neither
  forge freshness nor drop the receipt by writing metadata.
- **Reconcile** — `python manage.py recordings_reconcile [--once]` re-emits
  `recording.stage` for recordings stuck past `STUCK_THRESHOLD_SECONDS` in
  any non-terminal, non-upload status (custom stage statuses included;
  index read from `metadata['pipeline']['stage_index']`) and fails
  abandoned uploads (conditional update — never clobbers a finalized row).
  **Tuning invariant:** `STUCK_THRESHOLD_SECONDS` > the longest legitimate
  stage duration (`TRANSCRIBE_TIMEOUT_SECONDS` for the built-ins, plus your
  slowest custom stage). `run_stage` holds the row lock for the whole stage
  — a premature re-drive parks a duplicate on that lock, wasting a
  worker/connection even though the completion cursor keeps it
  semantically harmless. System check W005 warns on inconsistency.

### Erasure

This module is a **gdpr data owner** (`recordings`) for four subject types:
`account`, `workspace`, `meeting`, `recording`. Declare it in the host's
inventory so an erasure of any of them waits for this module's receipt:

```python
STAPEL_GDPR = {
    "DATA_OWNERS": {
        "recordings": ["account", "workspace", "meeting", "recording"],
        ...
    },
}
```

**One destruction path.** `erasure.erase(subject_type, subject_key,
workspace_id=None)` is the only code that destroys a recording, and
everything routes through it: the comm subscriber
(`@on_action("gdpr.erasure.requested")`), the deprecated
`@on_action("user.deleted")` (account subject; stapel-gdpr keeps firing it
until 0.6.0), and `RecordingsGDPRProvider.delete()` (the in-process provider
the monolith orchestrator calls). It removes the `Recording` rows for the
subject, everything cascading from them (`Speaker`, `Segment`,
`UploadSession`, `RecordingShare`, `Job`, and — with the opt-in vector app —
`SegmentEmbedding` / `RecordingEmbedding`) and every storage object they
reference, through the **STORAGE seam** the uploads use. It returns Django's
own per-model tally, so the receipt carries counts rather than a claim.

Subject resolution: `account` → `owner_id`; `workspace` → `workspace_id`;
`recording` → the row's id; `meeting` → the recording with that id **and**
every recording whose `metadata["meeting_id"]` matches (a recording IS the
meeting where the host has no separate entity — `transcript_schema` already
numbers transcripts by `meeting_id = recording.id` — while a host that does
have one links its recordings through that key). A `workspace_id` on the
request narrows the meeting lookup, since a host's meeting id is unique
inside a workspace, not across the platform.

**Reliability contract** (idempotent, at-least-once): rows are locked
(`select_for_update`) before their object keys are read — serializing with a
live `run_stage`, so a stage cannot commit a new object key for a row being
deleted — and a row is deleted only after **all** of its objects were
deleted. Storage failures keep the affected rows and raise
`GDPRStorageDeleteError` after the clean rows' deletion commits, so the
redelivery / orchestrator retry re-drives erasure for exactly the remaining
rows. A second erasure of the same subject removes nothing, raises nothing,
and still receipts — zero counts is an answer; silence is a timeout.

**Liveness.** `@on_action("gdpr.owner.probe")` answers `gdpr.owner.alive
{owner: "recordings", subject_types}` **from the same module** as the
erasure handler. That co-location is the point: an answer proves the erasure
path is being consumed, not that a container was deployed. It is what
`gdpr.W006` and `GET /gdpr/api/v1/owners/health` read.

**Retention — the soft delete finally ends.** Deleting a recording stamps
`deleted_at`; nothing used to remove it afterwards. `tasks.
purge_soft_deleted_recordings` (daily) takes every recording soft-deleted
longer than `PURGE_AFTER_DAYS` (30) ago with no erasure already in flight and
**opens a gdpr erasure** for it through the `ERASURE_CLIENT` seam — it never
deletes directly, so a single-recording delete is receipted by every owner
that claims the `recording` subject and the product can show its state. Wire
it up:

```python
from stapel_recordings.tasks import get_recordings_beat_schedule

CELERY_BEAT_SCHEDULE = {**get_recordings_beat_schedule(), ...}
```

Celery is optional (the task is a plain callable any scheduler can invoke),
but a host that drives a beat schedule containing nothing from this module
gets **`stapel_recordings.W010`** at boot — a retention policy nobody
schedules is a promise, not a mechanism.

### Admin categories (`stapel_core.access`, admin-suite AS-5)

`Recording`, `Speaker`, and `Segment` are business tables — the transcript
data itself, the doc's own `Listing`/`Wallet`/`Profile` shape — and stay
undecorated (implicit `@access.standard`). This module's admin has always
kept them read-only via a local `_ReadOnlyAdmin` base regardless (a
pre-existing choice independent of this rollout).

`UploadSession` and `Job` are decorated `@access.ops` and their `ModelAdmin`s
subclass `stapel_core.django.admin.base.StapelModelAdmin`:

- `UploadSession` — a TTL-bounded upload-in-progress tracker. Every row is
  created by `services.create_upload_session` / `start_multipart_upload`,
  mutated only by `finalize_upload`, and removed by
  `abort_multipart_upload_session` or expiry — there is no staff
  add/change/delete workflow through the admin for `ops` to break.
- `Job` — a processing-job ledger matching the doc's own `TaskRecord`
  example. No code path in this repo currently writes a `Job` row (the
  pipeline driver tracks progress on `Recording.status`/`metadata` instead,
  see `pipeline.py`); the model is a ledger a host/consumer may populate.
  Treated as machinery nobody hand-authors through the admin, not as an
  active staff-facing tracker — flagged here in case a future consumer
  starts writing rows and wants a different category.

Attribute-only change: no migrations (`makemigrations recordings --check
--dry-run` reports no changes).

### Contract emission — the `schema` + `flows` + `errors` triad

This module emits its **own** machine-readable API contract, per-module, so a
future frontend codegen reads a committed, version-pinned artifact instead of
depending on a monolith aggregate (contract-pipeline.md §2, verdict **A**:
contract = a reviewable commit, following the pattern stapel-auth established
as the etalon). The triad lives in `docs/`:

```
docs/schema.json   drf-spectacular OpenAPI, this module only, canonical /recordings/api/ prefix
docs/flows.json    generate_flow_docs machine artifact — [] here, no @flow_step annotations yet
docs/errors.json   generate_error_keys registry
```

**stapel-recordings is not mounted in `stapel-example-monolith`** (no route
for it in `svc-app/core/urls.py` as of this writing — recordings ships ahead
of its own frontend pair). Unlike auth/profiles/notifications/billing/
workspaces, there is therefore no monolith aggregate slice to diff this
artifact against for byte-identity. Validation is **standalone** instead
(`tests/test_contract.py`): determinism (two independent emissions are
byte-identical), a self-contained `$ref` closure (no dangling references),
canonical-prefix paths, and `security: [{"JWTCookieAuth": []}]` present on
every protected operation. If recordings is later mounted in the monolith,
add a `test_matches_monolith_recordings_slice` byte-identity test mirroring
auth's/profiles' (see their `tests/test_contract.py`).

**Harness** (three ~30-100-line files, plus the shared mechanism in
`stapel_tools.codegen`):
- `_codegen_settings.py` — the single `settings.configure(**kwargs)` block,
  shared with `conftest.py` so the test instance and the codegen instance can
  never drift. `contract=True` swaps in the production `REST_FRAMEWORK` (DRF
  caches it on first access, so it must be right at configure time) and the
  shared `INSTALLED_APPS` gained `stapel_core.django.apps.CommonDjangoConfig`
  (needed for the `generate_flow_docs`/`generate_error_keys` management
  commands — recordings' original conftest predated this and never called
  them) and `drf_spectacular`.
- `codegen_urls.py` — mounts `stapel_recordings.urls` at the canonical
  `recordings/` prefix (the module's own `urls.py` already bakes
  `api/recordings` into its path entries, so the resulting public prefix is
  `/recordings/api/recordings`, matching `urls.py`'s own documented mount
  recipe).
- `_codegen.py` — configures the instance on `codegen_urls`, forces
  `spectacular_settings.SCHEMA_PATH_PREFIX = "/"` on the drf-spectacular
  singleton (reproduces the aggregate-style operationId convention,
  `recordings_api_*`, that every other pair-backend's harness also pins),
  **explicitly calls `stapel_core.django.openapi.swagger._register_jwt_auth_extension()`**
  before emission, then calls the shared `emit_schema` / `emit_flows` /
  `emit_errors`. The explicit JWT-extension call is the **profiles-finding
  gap**: a real all-modules deployment registers this drf-spectacular
  security-scheme extension as a side effect of its own dev-only Swagger
  URLs; auth's harness gets it for free only because its co-mounted
  `stapel_gdpr` sibling happens to trigger the same registration.
  stapel-recordings has no co-mounted sibling, so — like profiles — it must
  call the registration function itself, or every protected endpoint here
  (all three views are `permission_classes = [IsAuthenticated]`) would emit
  without its `security` entry. `tests/test_contract.py::
  test_protected_endpoints_carry_jwt_security` gates this regression.

**Gate:** `make contract` re-emits; `make contract-check` regenerates into a
temp dir and diffs (`PYTHON=<venv>/bin/python make contract-check` — the
default `PYTHON ?= python3` targets the system interpreter, not this
workspace's venv). The CI-enforced gate is `tests/test_contract.py` (pytest,
run in the module's venv). Regenerate after any serializer/view/url/error
change:

    make contract        # or: python -m stapel_recordings._codegen --out docs

then commit `docs/{schema,flows,errors}.json`.

**Adding contract emission to a module with no monolith mount** (this
module's own precedent, in case another standalone pair-backend needs it):
copy stapel-auth's/stapel-profiles' three harness files verbatim, retarget
the module name + canonical prefix, and swap the byte-identity-vs-monolith
test for the four standalone checks above (determinism, closure, prefix,
security) — there is nothing to diff against without a monolith mount.

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

These were app-specific in the source origin and are intentionally
**not** ported; build them in the host project:

- **Zoom / Meet / Teams ingestion** (OAuth, webhooks, TOFU account binding)
  — an app-layer source that creates a `Recording`, downloads to storage,
  and calls `finalize_upload`. Emits into the same pipeline.
- **Credits / billing** — react to `recording.completed` /
  `recording.stage_completed` in the billing module. Key the debit on
  `recording_id` **+ `run_id`**, never on `recording_id` alone: a recording
  can be put through the pipeline again (`reprocess_recording`), and an
  idempotency key per recording makes every re-run free.
- **Export formats** (SRT/VTT/DOCX/PDF) — app-layer views over the stored
  transcript JSON.
- **Share links**: NOT app-layer any more, decision *or* route (audit
  SHARE-01 / STORE-01, 2026-08-11). `stapel_recordings.shares` owns the
  token, the passcode, the rotation, the lockout and the permission grant;
  a consumer that hand-rolls those gets them wrong in the same four ways
  every time. The three routes ship with it — `GET /shares/<token>`,
  `POST /shares/<token>/unlock`, `GET /shares/<token>/media` — because a
  correct primitive behind a hand-rolled route is still a hand-rolled
  authorization check, and because the media route is what lets a shared
  recording play from a **private** bucket. What stays app-layer is the
  rate limit in front of them and any richer payload (subclass the view's
  serializer seam). A host that wants a different mount remounts the
  views.
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
