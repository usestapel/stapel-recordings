# Changelog

All notable changes to stapel-recordings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

Security hardening from the 2026-08-11 audit of a product built on this
module (SHARE-01, REC-01, REC-02, REC-03, STORE-01). Each finding was raised
against product code; each is fixed here because the product could only have
made it by hand-rolling something this library never published, or by
inheriting a default this library set.

### Added

- **`stapel_recordings.shares` — public share links with passcode unlock
  (SHARE-01).** The module published no sharing primitive at all, so every
  consumer that needed one invented it, and the audited one accepted *any
  nonempty* `X-Share-Token`: the unlock token it issued was random, never
  stored, and never verified. Sharing a recording is an authorization
  decision about a recording, so it now belongs to the module that owns
  recordings. `RecordingShare` + `create_share` / `resolve_share` /
  `unlock_share` / `access_share` / `require_permission` / `revoke_share` /
  `set_share_passcode` give: a 32-byte link token returned once and stored
  only as a SHA-256 digest; a passcode behind Django's password hasher; a
  signed, purpose-salted, time-limited unlock token bound to the share id
  and to a `token_version` that a passcode change or a revoke bumps
  (rotation without tracking issued tokens); a persisted attempt counter
  and lockout (`SHARE_UNLOCK_MAX_ATTEMPTS`, `SHARE_UNLOCK_LOCKOUT_SECONDS`);
  an `F()` access counter; and one total entry point — `access_share`
  enforces revocation, expiry, the recording's own soft-delete, and the
  passcode, so a consumer cannot skip a check by calling a different
  function. Permissions are a grant (`view` / `transcript` / `summary` /
  `media`), defaulting to the minimum, and `shared_recording_to_dto`
  renders exactly what the share grants. No HTTP endpoints ship with it:
  the payload and mount point stay the host's, the decision does not.
- **`Recording.workflow_state` — a server-only field, and
  `stapel_recordings.metadata` to keep it that way (REC-01).** The pipeline
  kept its start marker, completed-stage cursor, awaiting-task handle and
  carried stage `ctx` in `Recording.metadata` — the same dict the audited
  product exposed to a client PATCH, so a member could mark stages
  complete, suppress the start, or inject the context a stage reads. State
  the server decides from now lives in its own column, and the driver reads
  nothing else. `sanitize_user_metadata` / `UserMetadataField` /
  `set_user_metadata` reject reserved keys **recursively** (library keys
  plus the host's `RESERVED_METADATA_KEYS` — a billing waiver flag belongs
  in that list), so the two halves cannot be re-merged by the next
  endpoint.
- **`RECORDING_POLICY` object-policy seam (REC-03).** Who may read, edit,
  delete, upload to or reprocess a recording is now one replaceable class
  (`stapel_recordings.policy`), default `OwnerOnlyPolicy`, instead of a
  queryset rebuilt in each view body. That is what lets a host widen
  *reading* (workspace members see the workspace) without widening the
  destructive verbs with it — the way member-wide mutation authority gets
  built by accident.
- `RecordingStorage.read_prefix(key, length)` — a ranged read, implemented
  for the Django and S3 backends. A finalize-time content check must never
  pull a multi-gigabyte object into memory; a backend that cannot serve one
  raises `NotImplementedError` and the content gate reports itself as not
  applied rather than silently downloading everything.
- **`stapel_recordings.media` + media endpoints — authorized delivery
  (STORE-01).** The audited deployment served recordings by making the
  bucket anonymously downloadable and proxying it publicly, so every
  authorization decision in this module was advisory: whoever held (or
  guessed) an object key read the audio. The module published no delivery
  path at all — no endpoint returned a media URL, and the one presigned GET
  it did mint (`shared_recording_to_dto`) had no route to reach it. Now
  bytes are reached only through `GET /recordings/<id>/media` (object
  policy `can_read`) and `GET /shares/<token>/media` (a share granting
  `media`); both authorize first and then mint a short-lived presigned GET
  (`MEDIA_URL_TTL_SECONDS` / `SHARE_MEDIA_URL_TTL_SECONDS`, 300s each),
  with `?redirect=1` for a player element. A URL is a bearer credential
  once minted, so a backend that cannot bound it in time is refused:
  `RecordingStorage.signs_get_urls` declares whether `presigned_get_url`
  really signs, and a backend that says no answers 503 rather than handing
  out a permanent URL — which means the **default** `DjangoStorageBackend`
  serves no media until the host uses `S3Backend` or vouches for its
  storage with `STORAGE_SIGNS_GET_URLS = True`. With this in place the
  bucket can be (and must be) private.
- **The public share HTTP surface** — `GET /shares/<token>`,
  `POST /shares/<token>/unlock`, `GET /shares/<token>/media`. Previously
  left to the host on the grounds that only the *decision* was the
  library's; the audit showed the split does not survive contact — a
  correct primitive behind a hand-rolled route is still a hand-rolled
  authorization check, and without a route the presigned share path could
  not exist end to end. Anonymous by design (the link token is the
  credential, verified by `shares.access_share` on every call); unlock
  tokens travel in the `X-Share-Unlock-Token` header, never a query string.
- `stapel_recordings.media_types` — content classification for stored
  uploads, with the `UPLOAD_CONTENT_POLICY` setting
  (`reject_known_bad` default / `require_known_media` / `off`).
- Settings: `MAX_MULTIPART_PARTS`, `UPLOAD_CONTENT_POLICY`,
  `SHARE_UNLOCK_TOKEN_TTL_SECONDS`, `SHARE_UNLOCK_MAX_ATTEMPTS`,
  `SHARE_UNLOCK_LOCKOUT_SECONDS`, `SHARE_MEDIA_URL_TTL_SECONDS`,
  `RECORDING_POLICY`, `MEDIA_URL_TTL_SECONDS`, `STORAGE_SIGNS_GET_URLS`,
  `TRANSCRIBE_AUDIO_URL_TTL_SECONDS`.
- System check `W007`: the presigned audio URL handed to the ASR provider
  must outlive `TRANSCRIBE_TIMEOUT_SECONDS`. With a private bucket that URL
  is the provider's only way in, and a late-starting provider fetching an
  expired signature fails in a way that reads as a transcription error.

### Fixed

- **Upload limits and object validation are now enforced, not advisory
  (REC-02).** `max_size_bytes` was recorded and never used; the multipart
  part count came from an arbitrary caller-declared size; and
  `finalize_upload` completed the multipart *before* validating, accepted a
  missing or zero-byte object by falling back to the caller's declared
  size, never rejected a measured size above the maximum, and never looked
  at the bytes. Now: a declared size is validated before any storage state
  exists and becomes the session's enforced ceiling; the part count is
  capped (`MAX_MULTIPART_PARTS`) and the part list is validated before the
  multipart is completed; finalize requires a successful HEAD with
  `0 < actual <= ceiling`, applies the content policy to the object's
  leading bytes, and on any failure cleans up the object and session,
  leaves the recording out of `queued` and does **not** emit
  `recording.uploaded` — no downstream work is enqueued by an upload that
  never satisfied its invariants.
- One live upload session per recording: opening a new one aborts and
  removes the previous unfinalized one, instead of leaving orphan multipart
  uploads in the bucket for every client retry.
- `create_upload_session` binds `content_type` into the presigned PUT where
  the backend supports it.
- The transcribe stage's audio URL lifetime is configuration
  (`TRANSCRIBE_AUDIO_URL_TTL_SECONDS`) instead of a hardcoded hour.
- The anonymous read scope. The inline queryset returned **every**
  non-deleted recording when the request had no authenticated user; only
  the view-level permission class stood between that and a response. The
  default policy returns nothing.

### Changed — breaking for consumers

- **UPGRADE NOTE — `POST /recordings` now requires membership of the
  workspace it writes into.** The endpoint carried `IsNotAnonymousUser` and
  nothing else: it passed the caller-supplied `workspace_id` straight into
  `Recording.objects.create(...)` and opened an upload session against it,
  so any account could mint a recording row — and, since storage keys are
  namespaced by workspace id, an object — inside **any** organization's
  workspace, where that workspace's members then saw it in their listing.
  Creation names a workspace, so it is a membership question, and it is now
  asked with the same fail-closed seam the workspace listing already used
  (`services.check_workspace_membership` → `workspaces.check_membership`).
  Non-members get 403 `error.403.recording_workspace_forbidden` and nothing
  is created — the check runs before the row, the session and the key exist.
  **The check fails closed**, so a deployment where the workspaces module
  cannot answer (not deployed, comm route not configured) refuses *every*
  create. Wire up `workspaces.check_membership`, or — for a stand that has
  no workspaces module and mints workspace ids itself — say so explicitly:
  `STAPEL_RECORDINGS = {"REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": False}`.
  The safe value is the default; opening it is the explicit act, and it is
  not readable from the environment (see `no_env` below).
- **Media is no longer served by the storage backend's plain URL.** With the
  default `DjangoStorageBackend`, `shared_recording_to_dto` now returns
  `media_url: null` and the media endpoints answer 503 — previously the
  share payload carried whatever `storage.url()` produced, which for the
  common deployment was a permanent, unauthenticated URL. Hosts on S3/MinIO
  switch `STORAGE` to `stapel_recordings.storage.S3Backend`; hosts whose
  Django storage backend signs its `url()` set
  `STORAGE_SIGNS_GET_URLS = True`. **Deployments must make the recordings
  bucket private and remove any public proxy in front of it** — that is the
  configuration this delivery path exists to replace, and leaving it in
  place leaves STORE-01 open regardless of the code.
- `finalize_upload` now **raises** instead of finalizing on a broken
  upload: `UploadNotStored` (nothing/zero bytes at the key),
  `UploadTooLarge` (measured or declared size over the ceiling),
  `InvalidMultipartParts` (malformed/oversized part list),
  `media_types.UnsupportedUploadContent` (rejected bytes). A consumer that
  relied on finalize always succeeding — in particular on the
  caller-declared size being accepted when the object is missing — must
  handle these. The bundled `FinalizeUploadView` maps them to 413 / 415 /
  409.
- `Recording.file_size_bytes` is always the size storage reports; a
  client-declared `file_size_bytes` is only ever checked, never stored.
- `start_multipart_upload` requires a positive `file_size_bytes` within
  `MAX_UPLOAD_BYTES` (previously any integer was accepted, including zero
  and negatives).
- Custom `RecordingStorage` implementations are unaffected (`read_prefix`
  has a default), but they get no content gate until they implement it.

### Changed — breaking for consumers (continued)

- Pipeline state moved from `Recording.metadata` to
  `Recording.workflow_state`. A consumer reading `metadata["pipeline"]`,
  `metadata["last_error"]` or `metadata["recovered_error"]` — for a status
  UI, a watchdog, a report — must read `workflow_state` instead. Migration
  `0004` moves existing rows (and folds them back on reverse, so a rollback
  to code that reads `metadata` still finds its cursor).
- `reprocess_recording` records the finished run's artifact keys in
  `workflow_state["previous_run"]` before requeueing, so a host that
  regenerates derived data can still find (and keep) the previous
  transcript for its retention window. The module still deletes nothing.

### Migrations

- `0003_recordingshare` — the `RecordingShare` table.
- `0004_recording_workflow_state` — the `workflow_state` column plus a data
  move of `pipeline` / `last_error` / `recovered_error` out of `metadata`
  (reversible).

## [0.13.1] — 2026-08-08

### Fixed

- The "task store app missing" system check moved from the already-taken
  `stapel_recordings.E001` to `stapel_recordings.E004`. The id collision
  wasn't cosmetic: hosts silence and search checks by id
  (`SILENCED_SYSTEM_CHECKS`). Silencing E001 for "STORAGE not importable"
  would have silently disabled this check too, blocking a real startup
  failure. A new guard (`test_check_ids_are_unique`) reads the check
  module's SOURCE, not a live run, so an id collision is caught even for a
  check that returns nothing under the current config.

## [0.13.0] — 2026-08-08

### Added

- `ffmpeg_normalize(..., max_duration_seconds=...)` — a duration cap at the
  pipeline entrance, the basis for free-tier plans ("first N minutes of any
  recording"). The cut happens RIGHT HERE: everything downstream
  (transcription, diarization, summary, embeddings) works on the capped
  audio without knowing about plans, and can't process (or pay a provider
  for) minutes the client didn't buy. Returns the duration of the file
  actually WRITTEN, not the source.
- `probe_duration(path)` — a public duration probe without transcoding.
  Needed for an honest "first 10 of 47 minutes" label; without it a host
  would reach into the private `_probe_audio` or add a second ffprobe call
  that could drift from this one.

## [0.12.0] — 2026-08-08

### Fixed

- `metadata["last_error"]` now clears once the pipeline recovers. It used
  to never clear — not on a successful retry, not on requeue, not even on
  reaching `completed` — so a fully processed recording could keep
  carrying the reason for a long-resolved failure. The reason isn't
  discarded: it moves to `metadata["recovered_error"]` with a
  `recovered_at` marker, keeping the diagnosis for ops without it posing as
  current state.

## [0.11.0] — 2026-08-08

### Added

- `vector.qa.answer_question()` — question answering over transcripts:
  hybrid search → prompt built from the found excerpts → `llm.complete`
  with an output schema. Every citation points at a real segment;
  fabricated references are dropped. Transcript text is treated as
  untrusted input (`sanitize_for_rag` + separation of instructions and
  data).

## [0.10.0] — 2026-08-08

### Added

- A task bridge for microservices deployments (`task_delegates.py`): a
  stage submits a task via the Task primitive, and the agent service does
  the work. The bridge registers ONLY for `kind`s nobody has claimed — in a
  monolith, a real handler always wins.

## [0.9.0] — 2026-08-08

### Fixed

- **Long-running work now goes through the Task primitive instead of a
  synchronous Function call.** `call("llm.transcribe", …)` used to be
  called without `timeout=`, i.e. at comm's 5-second default, against a
  real transcription taking ~14s and a summary ~36s: transcription ALWAYS
  failed, retried three times, and hit the DLQ after ~2.5 hours. Stages now
  return `StageAwaiting`, and `task.completed` / `task.failed` complete
  them (`resume_stage` / `fail_stage`).
- Explicit S3/MinIO call timeouts instead of botocore defaults.

## [0.8.1] — 2026-08-02

### Changed

- Contract documents ship in the wheel (`package-data`) (#184).
- Badge canon + Python 3.14 classifier.
- `docs/llms.txt` — the fifth contract artifact (badge-canon §3), emitted
  by `stapel_tools.llms_txt` and checked by the `make contract-check`
  drift gate.

## [0.8.0] — 2026-07-30

### Changed (BREAKING for anonymous callers) — a recording needs an owner who still exists tomorrow (#168)

`stapel-core` 0.16 turns the `AUTH_ANONYMOUS` axis into a question this
module never answered. A guest session is `is_authenticated`, so a bare
`IsAuthenticated` gate lets it through — and all four views were gated on
exactly that (`stapel_core.adoption` W002 reported all four against a real
deployment).

The answer is uniform here, because it follows from what a recording *is*:

> **a recording is a durable, owned artifact with a processing pipeline
> behind it — an anonymous session is not an owner.**

All four views now carry `IsNotAnonymousUser`; an anonymous session gets
**403** where it previously got 200/201.

- `POST /recordings` is the one that was genuinely open, and the most
  expensive endpoint in the module: it mints a row, opens an upload session
  and enqueues transcription, diarization and summarization. Metering that on
  an account stops meaning anything when a session costs one unauthenticated
  POST to mint.
- `POST /recordings/{id}/finalize` is what actually starts that pipeline, and
  `POST /recordings/{id}/reprocess` is the one verb that can spend its cost a
  second time.
- `GET /recordings` and `GET /recordings/{id}` were already owner-scoped
  (`_owned_qs`) or membership-scoped (`?workspace_id=`), so a guest's answers
  were an empty list and 404 all along. For those two the change moves an
  existing refusal to the door, where it can be read from the class header.

No consumer is affected: nothing in the fleet calls this module's HTTP
surface under a guest session, and the one product that mounts it
(meettoday) had already closed its own six recording views the same way.

Minor per this project's pre-1.0 rule (minor = breaking): for a deployment
with `AUTH_ANONYMOUS` on this is a behaviour change on a live surface, and it
is visible in the published contract — `docs/schema.json` now documents
`IsNotAnonymousUser` on all five operations. Deployments without guest
sessions are unaffected; an ordinary authenticated user passes
`IsNotAnonymousUser` exactly as before.

New `tests/test_guest_surface.py` pins the door shut, and pins that it is
shut for *anonymous* rather than for *authenticated*.

### Changed

- Minimum `stapel-core` raised to `>=0.16` (the release that added
  `ANONYMOUS_ALLOWED` / `ANONYMOUS_DENIED`).

## [0.7.0] — 2026-07-29

### Added
- **`transcript_hash()` / `transcript_content()`** — a version key for a
  transcript. Anything derived from a transcript (a summary, an LLM extraction
  whose evidence anchors point at turn indices, a user's edit log) needs to say
  which transcript it came from, and needs that answer to survive being asked
  again months later by a different process.
- The key hashes a **content projection**, not the record. Content is defined
  as *what the model saw, plus what an anchor indexes into*: turn ids, times,
  text, speaker attribution, the speaker names that get rendered in place of
  labels, and the header fields (duration, language). Everything the transcript
  carries for other reasons — provenance, QA verdicts, colours, join keys, the
  word grid — is outside the key, because changing it moves no turn.
- Getting that boundary wrong fails quietly in both directions, so both are
  tested. Hash the recording row wholesale — the obvious implementation — and
  `updated_at` (an `auto_now` field) mints a new key on every save: every
  summary and every user correction reads as stale forever, and a real edit
  becomes indistinguishable from a touched row. Hash too little and a summary
  keeps quoting a turn that was edited out.
- Both halves of the classification are declared explicitly, so adding a field
  to the schema fails `test_version_key` until someone decides which half it
  belongs to. A field nobody classified is a field nobody thought about, and
  defaulting it to "not content" is the wrong default: if it turns out to be
  rendered, edits to it silently stop invalidating anything.

### Fixed
- **The canonical transcript was not, in fact, canonical.** Speakers are
  numbered positionally (`spk_0`, `spk_1`, …) from `recording.speakers`, and
  that queryset had no `ORDER BY` — so the database was free to return the rows
  in any order it liked. The same untouched recording could canonicalize two
  different ways between two reads: different speaker ids on the segments,
  a different transcript rendered to the LLM, and a different version key.
  `Speaker.Meta.ordering = ["label", "id"]` (migration `0002`, metadata only,
  no SQL) plus an explicit `order_by` at the canonicalization site, stated
  where it is relied upon rather than inherited silently from a model two files
  away.

### Changed
- **BREAKING** — for a recording whose speaker rows were not inserted in label
  order, `spk_N` ids now differ from what earlier versions emitted. Stored
  `transcript.json` artifacts keep their old ids; a rebuild produces the
  corrected ones. This is the fix above, not a separate decision.
- Requires `stapel-core>=0.15.10` for `stapel_core.hashing`. Imported at module
  level, so an older core is an ImportError at startup — not a missing feature
  discovered later.

## [0.6.2] — 2026-07-26

### Fixed
- **The reconcile watchdog survives a dropped database connection.** Its
  loop has no request boundary, so nothing retired a connection the server
  had closed underneath it (restart, failover, pgbouncer idle-kill, a
  stand's database recreated) — Django reused the dead handle and every
  later pass raised `server closed the connection unexpectedly`, forever,
  paging on each tick while the watchdog looked alive. `close_old_connections()`
  now runs at the top of each pass and again after a failed one, the same
  line Celery and Channels put in their loops.

## [0.6.1] — 2026-07-25

### Fixed
- **The vector app's install requirement is now stated and checked**
  (`stapel_recordings.E003`). Its embedding tables carry an HNSW index,
  which Django only builds when `django.contrib.postgres` is in
  INSTALLED_APPS — the two-step install doc never said so, so a host that
  followed it to the letter died at container boot with Django's own
  `postgres.E005` pointed at a model class (ironmemo stand, 2026-07-25).
  The check names the fix in this module's vocabulary, the install steps
  in `vector/__init__.py` list both apps, and the postgres test harness
  now installs `django.contrib.postgres` the way a host must — the
  harness omitting it is precisely why the requirement stayed
  undocumented (the suite never runs `manage.py check`).

## [0.6.0] — 2026-07-25

Minor (**behaviour change in vector search**): embedding spaces are now
isolated per model, and there is a supported way back after an embedder
swap.

### Changed
- **Vector arm filters by the query's embedding model** (`vector/
  search.py`). Candidates are restricted to `SegmentEmbedding` rows whose
  `model` equals the model `llm.embed` reported for THIS query — the same
  string the embed stage stamps on every row. Previously the filter only
  applied when `VECTOR["MODEL"]` was pinned (it is `""` by default), so a
  host that changed embedders silently mixed two incomparable spaces of
  the same width and cosine ranking degraded to noise. Now old-model rows
  simply stop matching, which is decidable and repairable. Opt out with
  `VECTOR["SEARCH_MODEL_FILTER"] = False` (e.g. to keep serving during a
  migration).
- `embed_recording(recording, store=None, *, force=False)` gained
  `force`: with `VECTOR["MODEL"]` unpinned the content-hash check cannot
  tell "already embedded by the CURRENT model" from "embedded by the
  previous one" (the model name only arrives in the `llm.embed`
  response), so a plain re-run after a swap skipped everything and left
  the new space empty. The pipeline stage never sets it.

### Added
- **`manage.py recordings_reembed`** — the reindex path after an embedder
  change. `--dry-run` reports the scope and stored rows per model without
  calling any provider; `--force` re-embeds texts already stored;
  `--prune-other-models --keep-model <name>` deletes rows left on the old
  model (never implicit); scope narrows with `--workspace` / `--recording`
  (repeatable) / `--limit`. A pass that embeds nothing without `--force`
  says why.
- `VECTOR["SEARCH_MODEL_FILTER"]` (default `True`).

## [0.5.2] — 2026-07-24

### Added
- **Optional rerank stage for search** (`VECTOR["RERANK"]`, default off):
  one post-ranking pass in every `search_recordings` mode — after RRF
  fusion (or text/vector ranking), the top `TOP_K` hits' full segment
  texts go through the `llm.rerank` comm Function (stapel-agent ≥ 0.5)
  and that block is re-ordered by rerank score; hits the reranker didn't
  score (`TOP_N` cut, or beyond `TOP_K`) keep their pre-rerank order
  after it, then the result truncates to `limit` as before. Arms
  over-fetch to `TOP_K` when enabled. `FAIL_OPEN` (default True): any
  rerank failure (comm error, failure envelope, malformed response) logs
  a warning and returns the un-reranked order; `False` raises
  `VectorSearchUnavailable`. `SearchHit` gains `reranked: bool = False`;
  a reranked hit's `score` is the provider's rerank score (the RRF and
  rerank scales are not comparable — list order is the contract).
  Privacy: with rerank enabled, segment texts go to the rerank provider —
  the same trust boundary as `llm.transcribe`/`llm.summarize`. Knobs:
  `ENABLED`/`PROVIDER`/`TOP_K` (50)/`TOP_N` (20; 0 = score all)/
  `TIMEOUT_SECONDS` (60)/`FAIL_OPEN`; the block deep-merges like the
  rest of `VECTOR` via `vector_config()`.

## [0.5.1] — 2026-07-24

### Fixed
- 0.5.0 tag never published: docs/capabilities.json still carried 0.4.4
  (version-stamped contract artifact; drift gate red in CI). Regenerated.

## [0.5.0] — 2026-07-24

Opt-in vector/search layer. Minor bump (pre-1.0): the default `PIPELINE`
grows a fifth stage name (`embed`) — hosts pinning an explicit `PIPELINE`
list are unaffected; hosts on the default get a no-op stage unless they
opt in. Zero burden without the extra: the base package (and its sqlite
test suite) works with `pgvector` absent.

### Added
- **`stapel_recordings.vector`** — a separate opt-in Django app (hosts add
  it to `INSTALLED_APPS` themselves): `SegmentEmbedding` (unique per
  segment+model, HNSW cosine index) and `RecordingEmbedding` (summary
  chunks, unique per recording+model+chunk). `VectorField` dim + HNSW
  params come from `STAPEL_RECORDINGS["VECTOR"]` at model-load/migrate
  time. The app's `0001` runs pgvector's vendor-guarded
  `CREATE EXTENSION IF NOT EXISTS vector` first.
- **`embed` pipeline stage** (registered after `merge`, in the default
  pipeline): no-op unless the vector app is installed AND
  `VECTOR["ENABLED"]` (default False) — the DiarizeStage pattern. When
  active, batches segment texts + the chunked summary through the
  `llm.embed` comm Function (stapel-agent ≥ 0.4) and upserts embedding
  rows. Outbox canon: content-hash idempotent, retry-safe
  (`StageRetryable` on comm failures, `StageFatal` on a dim mismatch).
- **Hybrid search service** — `vector/search.py::search_recordings(query,
  *, workspace_id=None, recording_ids=None, mode="hybrid"|"text"|"vector",
  limit)` returning segment hits (segment id, recording id, score,
  snippet). Text arm: postgres FTS with a per-recording-language config
  map (fallback `simple`), degrading to `icontains` off postgres. Vector
  arm: `llm.embed` + pgvector cosine. Hybrid: reciprocal-rank fusion
  (`RRF_K`/`RRF_WEIGHTS` in settings). On sqlite / app-absent,
  `vector`/`hybrid` raise `VectorSearchUnavailable` — hosts decide.
- `STAPEL_RECORDINGS["VECTOR"]` settings block (`DEFAULT_VECTOR` +
  `vector_config()` merge helper): dim, model/provider, batch size,
  timeout, summary chunking, HNSW params, FTS config map, RRF knobs.
- `[project.optional-dependencies] vector = ["pgvector>=0.3"]`
  (`all` now includes it); packaged `stapel_recordings.vector(.migrations)`.
- System check **W006**: `VECTOR["ENABLED"]` without the vector app in
  `INSTALLED_APPS` (the embed stage would silently no-op).
- Opt-in postgres test harness: `STAPEL_RECORDINGS_TEST_DB=postgres://…`
  runs the suite on postgres with the vector app installed and real
  migrations, unlocking the vendor-gated `tests/test_vector_postgres.py`
  (VectorField rows, FTS ranking, cosine ordering, extension + HNSW
  migration). The canonical sqlite suite is unchanged and stays the
  no-extra gate.

## [0.4.3] — 2026-07-17

Fix-up #2: 0.4.2's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.4.3 already in `pyproject.toml`; verified match,
suite green.

## [0.4.2] — 2026-07-17

Fix-up: 0.4.1's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.4.1 bump.
Regenerated via `make contract`; no other diff.

## [0.4.1] — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.4.0] — 2026-07-17

Legacy-compat scrub: the extension-less `…/audio` upload key is gone —
`filename` is now **required** everywhere. Minor bump (pre-1.0 breaking).

### Removed
- **Legacy extension-less upload key (`…/audio`).** `filename` is now
  required on `POST /recordings/api/v1/recordings` (was optional /
  allow_blank; omitting it kept the backward-compatible `…/audio` object
  key). The object key is always `…/audio.<validated-ext>`.
- `services.create_upload_session` / `services.start_multipart_upload` /
  `services.validated_upload_ext` / `_storage_key`: the
  `filename: str | None = None` dual signature is gone — `filename: str`
  is required; a missing/empty filename raises
  `UnsupportedUploadExtension` instead of producing the legacy key.
- Test of the legacy path
  (`test_create_upload_session_without_filename_keeps_legacy_key`) replaced
  by required-filename rejection tests (service + API 400).
- `docs/schema.json` regenerated: `filename` joins the request's
  `required` list.

## [0.3.3] — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules).
- `docs/capabilities.json` regenerated (version bump); no other drift.

## [0.3.0] — 2026-07-10

Service-backlog tails: the `reprocess` transition gains an HTTP verb, and the
listing gains a `resource_key` filter. Minor bump — the API contract grows (a
new endpoint, a new query parameter, a new error key), additive but a minor
per the frontend-pair regen schedule (schema changes → pair minor).

### Added — `reprocess` HTTP verb

- `POST /recordings/api/recordings/{id}/reprocess` exposes the
  `pipeline.reprocess_recording` transition (added as a bare service transition
  in 0.2.0): re-run the whole pipeline from stage 0 for a **completed**
  recording, clearing the progress cursor. Owner-scoped like every other
  per-recording verb — an unknown/foreign/deleted recording is `404`. The
  transition is allowed **only** from `completed`; from any other status the
  endpoint answers `409 error.409.recording_invalid_state` (new domain error
  key) and leaves the recording untouched. On success it returns the requeued
  recording (now `queued`).

### Added — `resource_key` listing filter

- `GET /recordings/api/recordings?resource_key=<opaque-token>` narrows the
  listing to the single recording that opaque, signed handle references
  (resolved via `resolve_resource_key`). It composes with `?workspace_id=`
  (workspace scope stays membership-gated) and with the default owner scope.
  A missing/forged/tampered key resolves to nothing and the listing comes back
  **empty** (not `400`) — the token is tamper-evident and opaque by design, so
  the surface neither leaks whether a token is genuine nor invents a distinct
  error for a value the client only ever obtains from a prior server response.
  Anchor pagination is unchanged.

## [0.2.1] — 2026-07-10

### Fixed
- Re-release of 0.2.0: its publish gate failed on CI missing stapel-tools
  (contract-emission dependency); no code changes beyond the CI fix.

## [Unreleased]

## [0.3.2] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): URL set moved to
  `urls_v1.py`; the new root `urls.py` mounts it under `api/v1/` (the `api/`
  segment historically lives inside this package, so the version slots in
  right after it, per canon). Host mount `recordings/` unchanged: endpoints
  now serve at `/recordings/api/v1/...`; bare `/recordings/api/...` no longer
  exists (sweep lands before the §3 API00x gates are enabled).
- Contract artifacts regenerated (`make contract`): `/v1/` in schema paths.
- `_capabilities.py` canonical_prefix → `/recordings/api/v1`.
- Lint hygiene to a clean `stapel-verify`: explicit `# noqa` on pre-existing
  findings.

## [0.2.0] - 2026-07-09

Client-validation gap closure (G4/G5/G9/G10). Minor bump: the API contract
grows (new response field, new request field, new error keys) and the module
gains two public transitions/registries — additive, but a minor per the
frontend-pair regen schedule (schema changes → pair minor).

### Added — G4: workspace-scoped listing + opaque `resource_key`

- `GET /recordings/api/recordings?workspace_id=<uuid>` lists **every**
  recording in a workspace the caller is a member of, not just their own.
  Membership is verified by comm name (`workspaces.check_membership`, no
  import of that app) and **fails closed** — a non-member, or any wiring
  failure (workspaces not deployed / route unconfigured), returns
  `403 error.403.recording_workspace_forbidden`, never another member's data.
  Without `workspace_id` the endpoint stays owner-scoped as before.
- Every recording payload now carries an opaque, tamper-evident
  `resource_key` (a `SECRET_KEY`-signed handle over the id via
  `django.core.signing`) so cross-owner listings hand back a reference token
  instead of leaking internal identifiers. `stapel_recordings.resources`
  exposes `resource_key()` / `resolve_resource_key()`.

### Added — G5: filename/extension in the upload key

`create_upload_session` (and `start_multipart_upload`) accept an optional
`filename`; the create endpoint accepts a `filename` field. Its extension is
validated against the new `UPLOAD_EXTENSION_ALLOWLIST` setting and appended to
the object key (`…/audio.mp3`). A disallowed/extension-less filename is
rejected (`415 error.415.recording_unsupported_media` domain key; `400` at the
serializer boundary). **Backward compatible**: omit `filename` for the prior
extension-less `…/audio` key.

### Added — G9: `SourceType` is a settings-overlay registry, not a code enum

Recording source kinds are now an open merge-registry
(`stapel_recordings.sources`): the four built-ins (`meet` / `dictaphone` /
`upload` / `other`, derived from the model enum) merged over a
`STAPEL_RECORDINGS["SOURCE_TYPES"]` overlay — a host adds `zoom` / `teams` /
`phone` from settings, no enum edit, no migration
(`Recording.source_type` is a free `CharField`). The create endpoint validates
`source_type` against the resolved registry. Declared as a `merge_registry`
extension point in `capabilities.meta.json`.

### Added — G10: explicit `reprocess` transition (completed → queued)

`pipeline.reprocess_recording(id)` re-runs the whole pipeline from stage 0 for
a **finished** recording, clearing the pipeline progress cursor
(`completed` / `completed_index` / carried `ctx`) so every stage re-runs — the
counterpart to `retry_recording` (`error → queued`, which *resumes*). Allowed
only from `completed`; every other status (`created` / `uploading` / `queued`
/ in-flight / `error` / `deleted`) is a forbidden no-op returning `False`. The
module never destroys transcript data on its own — stages self-guard on
persisted artifacts, so a host that needs derived data regenerated clears the
relevant keys as part of its reprocess flow.

### Contract / tests

- Regenerated `docs/{schema,errors,capabilities}.json` (`make contract`):
  `resource_key` + `filename` in the schema, 44 → 46 error keys, the
  `SOURCE_TYPES` extension point.
- New tests per gap (workspace list + membership fail-closed + resource_key
  round-trip; filename allowlist + API 400/201; source-type registry overlay +
  API accept/reject; reprocess allowed/forbidden transition matrix). Suite
  90 → 118, green.

## [0.1.3] - 2026-07-09

### Added — `docs/capabilities.json`, the fourth contract artifact (A6 sweep)

Emits `docs/capabilities.json` alongside the schema/flows/errors triad below —
same per-module contract-emission harness, extended to also declare the
module's capability surface for the A6 capabilities mechanism. Enforces
Python 3.12 for emission (rendering-skew guard, keeps the artifact
byte-stable across contributor machines).

### Added — per-module contract emission: `schema` + `flows` + `errors` triad (contract-pipeline.md Wave 1)

stapel-recordings now emits its **own** API contract per-module — the same
`docs/{schema,flows,errors}.json` triad stapel-auth established as the etalon
and stapel-profiles copied — a prerequisite for a future `recordings-react`
pair (client priority #1, needed by client migrations).

- **Harness** (reuses `stapel_tools.codegen`, ~90 lines of per-module config,
  copied from auth/profiles):
  - `_codegen_settings.py` — single source of truth for the
    `settings.configure` block, shared with `conftest.py` (extracted, no
    test-behavior change beyond adding `drf_spectacular` +
    `stapel_core.django.apps.CommonDjangoConfig` to `INSTALLED_APPS` — the
    latter provides the `generate_flow_docs`/`generate_error_keys`
    management commands the harness needs); `contract=True` swaps in the
    production `REST_FRAMEWORK`.
  - `codegen_urls.py` — mounts `stapel_recordings.urls` at the canonical
    `recordings/` prefix (the module's own `urls.py` already bakes
    `api/recordings` into its path entries, so the resulting public prefix
    is `/recordings/api/recordings`, matching `urls.py`'s own documented
    mount recipe).
  - `_codegen.py` — pins `spectacular_settings.SCHEMA_PATH_PREFIX = "/"` and
    **explicitly calls `_register_jwt_auth_extension()`** before emission
    (the profiles-finding: without a co-mounted sibling to trigger this
    registration as a side effect, protected endpoints would emit without
    their `security: [{"JWTCookieAuth": []}]` entry — recordings has no
    co-mounted sibling, so it needs the explicit call like profiles did).
- **Gate:** `make contract` / `make contract-check`; `tests/test_contract.py`
  (drift + determinism + canonical-prefix + `$ref`-closure self-containment +
  JWT-security presence).
- **Validation shape differs from auth/profiles:** stapel-recordings is
  **not mounted in stapel-example-monolith**, so there is no monolith
  aggregate slice to assert byte-identity against. `tests/test_contract.py`
  validates standalone instead — see MODULE.md's "Contract emission"
  section for the four checks this implies.
- Artifacts: 3 paths, 0 flows (`flows.json = []` — no `@flow_step`
  annotations yet), 44 error keys. Zero cross-module `$ref` (recordings
  references `workspace_id`/`owner` only as bare UUIDs, never a `User` FK),
  so the `{recordings + core}` harness needs no sibling installed for
  closure.

## [0.1.2] - 2026-07-08

### Added — admin-suite AS-5: `@access` category rollout + `StapelModelAdmin`

Applies the `stapel_core.access` category decorators (admin-suite §0/AS-5
sweep, docs/admin-suite.md) to this module's models and switches the
affected `ModelAdmin`s to `stapel_core.django.admin.base.StapelModelAdmin`.

- `@access.ops` (read-only journal, forbids add/change/delete for everyone
  including superuser; view requires HIGH clearance): `UploadSession` (a
  TTL-bounded upload-in-progress tracker — every row is created/mutated/
  removed exclusively by the service layer, never through the admin) and
  `Job` (a processing-job ledger matching the doc's own `TaskRecord`
  example — no code path in this repo writes a row today; flagged in
  MODULE.md as a ledger for a future consumer, not an active staff
  workflow).
- `Recording`, `Speaker`, `Segment` stay undecorated (implicit
  `@access.standard`) — business tables (the transcript data itself); this
  module's admin already kept them read-only as its own pre-existing
  choice, unrelated to this rollout.
- Attribute-only change: no migrations (`makemigrations recordings --check
  --dry-run` reports no changes).

## [0.1.1] — 2026-07-07

Initial port from a prior service. `0.1.0` shipped to
PyPI with different content than what is described below; this entry — and
the version bump — cover the actual first published state of the package
(PyPI releases are immutable, so a re-publish of the same content requires a
new version number).

### Fixed
- CI harness incident (library-standard §7.5–§7.6): the test job installed
  the package non-editable, so `stapel_recordings.tests` (excluded from the
  wheel by design, §4) was unimportable and `ROOT_URLCONF` blew up with
  `ModuleNotFoundError` on first view access. Test job now installs with
  `pip install -e .`; `publish.yml` gained its own test job and `build`
  depends on it, so a red test run blocks publication.

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Blocking (library-standard §7.5): the stapel dependency graph is now fully
  on PyPI, so a green run here is a precondition for a `vX.Y.Z` tag.

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_recordings.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).

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
- 77 tests: full pipeline run, split producer/consumer halves, state-machine
  transitions, idempotent re-delivery (incl. duplicate deliveries of
  completed stages), pipeline edits under live recordings, pipeline
  extension points (custom/reordered/subset/swapped stages + resolver
  seam), retry/DLQ + explicit retry transition, reconcile, storage-seam
  swap, upload/multipart, GDPR (incl. erasure retry), summarize, checks,
  schema validation, HTTP surface.

### Fixed (adversarial-review findings — folded into the pending 0.1.0)

At-least-once / mutable-pipeline semantics hardening (per-step atomicity was
already clean; these fix idempotency and pipeline-edit consistency):

- **Progress cursor is now stage *names*, not positions** (H1). The driver
  persists the completed stage names (`metadata.pipeline.completed`) and on
  every delivery runs the first not-yet-completed stage of the *currently*
  resolved pipeline; the event's `stage_index` is only a dedup hint.
  Editing a pipeline under live recordings no longer skips the wrong stage
  or finalizes early. Decisions: a removed pending stage is **skipped with
  a warning** (list edits are operator intent; DLQing every in-flight
  recording on an edit would fail recordings for a routine action); an
  **empty resolver list DLQs** (`empty_pipeline`) instead of silently
  emitting `recording.completed` for a recording with no transcript.
- **Stage completion is persisted in the success transaction** (H2):
  `completed_index` + name are written atomically with
  `recording.stage_completed`/next-`recording.stage`. A duplicate delivery
  of a completed stage (broker redelivery, reconcile racing a live worker)
  is now a total no-op — it no longer re-emits public events with fresh
  event_ids (billing on `stage_completed` can't double-charge). Crash
  before the commit still re-runs the (idempotent) stage.
- **Reconcile can no longer duplicate live work by default** (H2):
  `STUCK_THRESHOLD_SECONDS` default raised 600 → 2100 (transcribe timeout
  1800 + headroom); new system check **W005** warns when the threshold
  doesn't exceed `TRANSCRIBE_TIMEOUT_SECONDS`. Decision: the claim-pattern
  (short claim txn → work outside the lock → fence-checked commit txn) was
  evaluated and rejected for 0.1.0 — it forfeits the single-transaction
  atomicity anchor of `run_stage` and needs fencing tokens to stay correct;
  the completed-cursor guard already makes premature re-drives semantically
  harmless (the residual cost is a duplicate parked on the row lock, which
  the raised threshold avoids). Revisit if stage durations outgrow sensible
  thresholds.
- **`error` is terminal for deliveries** (M): added to the driver's
  terminal guard, so a redelivered `recording.stage` can't resurrect a
  DLQ'd recording and emit `recording.completed` after `recording.failed`.
  Retry is an explicit transition: new **`pipeline.retry_recording(id)`**
  (`error → queued`, resumes at the first not-yet-completed stage).
- **GDPR erasure is retryable and race-free** (M): `delete_object` failures
  are collected and re-raised (`GDPRStorageDeleteError`) instead of
  swallowed, and the affected rows are **kept** so `user.deleted`
  redelivery / the GDPR orchestrator retry the erasure (previously the row
  was deleted anyway — the object with PII was orphaned forever and every
  retry path saw "success"). Rows are locked (`select_for_update`) before
  the key snapshot, so a live convert/merge can't commit a new storage key
  for a row being erased. Clean rows still erase on partial failure.
- **Resolver/overlay failures no longer crash-loop in the outbox** (M-L): a
  crashing `PIPELINE_RESOLVER` parks the recording as a retryable failure
  (bounded by `MAX_STAGE_RETRIES`, then DLQ); `get_stage` now imports
  handlers lazily, so one broken `STAGES` dotted-path DLQs only the
  pipelines that include that stage instead of breaking every recording.
- **Small races closed** (L): `start_pipeline` now locks the row and writes
  the started marker in the same transaction as `recording.stage(0)`
  (concurrent `recording.uploaded` duplicates emit a single stage 0);
  `cleanup_abandoned_uploads` uses a conditional per-row `UPDATE` (can't
  clobber a recording that finalized after the sweep's snapshot);
  `reconcile_once` treats any non-terminal/non-upload status as transient
  (recordings parked in *custom* stage statuses are re-driven) and emits
  inside `transaction.atomic()` (no outside-atomic warning noise).

### Internal (still unreleased — folded into the pending 0.1.0)
- Wired the `stapel_core.lint.emit_check` outbox-atomicity gate into CI and the
  pre-commit/pre-push hooks (guard-fall back to skip when stapel-core < 0.3.3).
- `pipeline._finalize` / `pipeline._dlq`: the terminal `save()` + `emit_*()` pair
  is now wrapped in `stapel_core.comm.mutate_and_emit()` (was flagged EMIT003).
  Both are only ever called from within `run_stage`'s `transaction.atomic()`, so
  this nests as a savepoint joining the outer transaction — no behaviour change —
  but makes the mutation+emit unit lexically atomic and correct even if a future
  caller invokes them outside `run_stage`.

### Changed from the source service (provenance)
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
- **Hardcoded legacy `*.recordings.*` topic strings → schema'd comm names**
  under `schemas/emits/`.

### Not ported (app-layer)
- Zoom/Meet/Teams ingestion (OAuth, webhooks, TOFU binding), credits, share
  links, and export formats (SRT/VTT/DOCX/PDF). See MODULE.md → App-layer.

### Security / release
- Opus-authored. **Must NOT be released** until an independent adversarial
  review passes and a PyPI pending trusted publisher is registered.
