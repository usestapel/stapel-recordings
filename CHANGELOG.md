# Changelog

All notable changes to stapel-recordings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
