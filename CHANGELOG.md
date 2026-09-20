# Changelog


## [0.29.0] — 2026-09-20

### Fixed — the merge deadline killed the retries it declared

`MergeStage` submitted `llm.summarize` with
`deadline_seconds = SUMMARIZE_TIMEOUT_SECONDS` — the very number this
package's task delegate uses as the CALL timeout — while leaving
`max_attempts` at three. Attempt one consumed the whole deadline, the
60-second task sweep then failed the row with "deadline exceeded" before
attempt two could exist, and the pipeline DLQ'd the recording at
`merge/task_failed`. A retry ladder nobody could climb (a client stand,
2026-09-13).

Two numbers now, and they are different by construction:

* `summarize_budget_seconds(recording)` — what ONE call may take, sized
  to the meeting (`SUMMARIZE_TIMEOUT_SECONDS` + hours ×
  `SUMMARIZE_SECONDS_PER_HOUR`, capped at
  `SUMMARIZE_TIMEOUT_MAX_SECONDS`). A flat 300 s cannot bound a
  map-reduce over a four-hour transcript.
* `task_deadline_seconds(budget, attempts)` — budget × attempts +
  `TASK_DEADLINE_HEADROOM_SECONDS` (420). Call it wherever a stage
  passes both `deadline_seconds` and `max_attempts`.

The per-recording budget reaches the executor as `task_timeout_seconds`
in the payload, which `task_delegates` POPS before the Function call —
it is ours, and the agent's contract rightly refuses keys it does not
declare.

### Fixed — a summary retry no longer re-buys the summary

`_summarize_payload` now sends `idempotency_key = summary:<transcript
hash>` (stapel-agent >= 0.29.0), so every attempt after the first is
served from the agent's per-part checkpoint. That is what makes
`SUMMARIZE_TASK_MAX_ATTEMPTS` = 2 affordable, and
`summarize_attempt_ceiling()` states the product of the two ladders the
way `transcribe_attempt_ceiling()` does for transcription.

### Fixed — the watchdog had no ceiling

`recordings_reconcile` re-emits `recording.stage` for anything
non-terminal that has not moved, and a re-drive that lands on
`StageAwaiting` never touches `retry_count`. So a recording the pipeline
could not finish was re-driven every `STUCK_THRESHOLD_SECONDS` for as
long as it existed — a client stand did that for a fortnight against an
empty download allowlist (2026-08-20), and nothing but the agent's
7-day checkpoint stood between that loop and a second invoice.

`pipeline.note_reconcile_redrive()` counts each re-drive on the
recording and refuses past `RECONCILE_MAX_REDRIVES` (5), failing it with
`last_error.reason = "reconcile_exhausted"` where a person can see it.
The count resets whenever a stage actually completes, because what the
cap bounds is "re-driven and got nowhere", not "took a long time". 0
restores the old unbounded behaviour, written down rather than reached
by accident.

## [0.28.0] — 2026-09-19

### Fixed — a checkpoint is only valid for the input that produced it

**The defect, in one line: "already done" meant "an artifact with this name
exists".** A recording processed under a host's trim, re-queued in full
after the customer paid for the whole meeting, completed in 55 seconds and
ran nothing. The driver cleared its cursor as designed; every stage then
found its own artifact of the trimmed run — a normalized object, segment
rows, a stored transcript, a summary — and returned early on its existence.
Status `completed`, duration 599 seconds of a 7116-second meeting, and an
invoice the customer had already paid.

A stage checkpoint is now a PAIR: the artifact, and the fingerprint of the
input and parameters it was computed from
(`stapel_recordings.checkpoints`). A stage declares its own —
`Stage.input_fingerprint(recording, ctx)`, a content hash plus whatever
changes the answer — the driver records it beside the completion, and the
result is reused only while the two still agree. Change the input and the
stage recomputes, together with every stage downstream of it; retry the
same input and it resumes for free, which is the half that keeps a failed
stage from re-buying a priced call it already has an answer for.

- `ConvertStage` fingerprints the uploaded object and the audio profile;
  `TranscribeStage` fingerprints the normalized audio's content hash and
  the parameters the provider is told (NOT the detected language, which the
  stage writes back — a fingerprint may only read what its own stage leaves
  alone). `MergeStage` keys on the transcript hash, so a re-transcription
  rewrites `transcript.json` and re-summarizes instead of finding both
  fields populated and stopping.
- `pipeline.invalidate_from(recording_id, stage)` — the explicit half, for
  a parameter that does not live on the row and for artifacts produced
  before fingerprints existed. It DECLARES only: no requeue, no status
  change, nothing deleted. Pair it with `reprocess_recording`.
- An unrecorded fingerprint means unknown, not stale. Nothing in an
  existing deployment re-runs on upgrade, and a crash between "artifact
  written" and "completion committed" still recovers without paying twice.

**Segment rows are now REPLACED, atomically.** `_persist_transcript`
deletes the previous transcript's segments and speakers inside the same
transaction that writes the new ones. It used to only insert, so a second
transcription of the same meeting interleaved two transcripts into one
list with duplicated sequence numbers. The delete happens at the END of the
paid work rather than when the re-run is scheduled: the user keeps seeing
the previous result — all of it — until the new one is complete, and the
whole of it if the re-run fails.

**`ConvertStage.keep_source(recording, ctx)`** — a hook for the one host
that needs the uploaded container to survive the conversion (a recording
trimmed to a free allowance, where the rest of the meeting is a sale away).
It got there by swapping the storage backend's `delete_object` for a no-op
around the stage, which kept the object and did not stop the purge from
clearing `file_storage_key`: the bytes stayed in the bucket and the row
forgot where they were, so the paid re-run had nothing to convert. The
purge is one decision and now has one switch — object and pointer together.

**`stages.stage_input_dedupe_key`** replaces `stage_dedupe_key` inside
`submit_task`. The storage key is stable across a re-conversion (the
normalized object is written back to the same path), so an object-path
dedupe key called a re-transcribed two-hour meeting the same work as the
ten minutes it replaced. The key now carries the input fingerprint: two
clicks on one re-run coalesce into one paid call, a different input does
not. `submit_task(stage=...)` therefore takes the stage OBJECT where it
used to take its name; a name still works and keeps the old key.


## [0.27.0] — 2026-09-18

### Changed — the erasure protocol is core's, and a transcript anchor is an integer

**One protocol, one place.** This module hand-wrote the data-owner side of
the erasure protocol — `gdpr.erasure.requested`, `gdpr.owner.probe` and the
deprecated `user.deleted`, sixty lines that nine libraries carried verbatim.
`apps.ready()` now declares the owner instead:

```python
register_gdpr_owner("recordings", SUBJECT_TYPES, erasure.erase_subject)
```

Core builds the same three handlers, with the deterministic receipt id and
the receipt inside the erase's transaction. What stays here is what was ever
ours: `erasure.erase` and the new `erasure.erase_subject`, which answers
`None` for a subject type this module does not claim so an erasure the
orchestrator opened no part for is never receipted.

The owner name, the four subject types and the rows each one destroys are
unchanged. What changes on the wire is the receipt envelope: it now carries
`receipt_id` (`recordings:<subject_type>:<subject_key>:<correlation_id>`,
derived so a redelivery mints the same one) and is keyed by the subject
rather than the correlation.

Why it matters beyond tidiness: a library that both registers a
`GDPRProvider` and hand-writes the protocol made core's provider bridge
stand down for the whole **app** rather than for the named **section** —
`gdpr.W012`. A named registration makes that question exact, and one erasure
leaves exactly one receipt per part. Two receipts assert the deletion
happened twice, which is a false legal record rather than a duplicate log
line.

`stapel-core>=0.85.1` is the new floor: `register_gdpr_owner` and the bridge
that yields to it.

### Fixed — `TranscriptPage.next_anchor` / `prev_anchor` are declared `integer`

The contract declared both as `string` and the wire sent an **integer** on
every page that had a neighbour: `TranscriptPagination` anchors on
`sequence_num`, and the paginator copies the raw field value into the
envelope, stringifying only values that carry `.isoformat()`. Every other
anchor paginator in the fleet anchors on a datetime, where the claim is
true; this one never could be. Nothing failed loudly — a generated client
typed `next_anchor` as `string | null` and handed `4` back as the `anchor`
query parameter, which works — so a TypeScript consumer simply believed a
lie about every transcript longer than one page.

The anchor IS an integer, so it is declared one:
`TranscriptPageSerializer.next_anchor/prev_anchor` are
`IntegerField(allow_null=True)` and `TranscriptPagination.anchor_type` is
`"integer"`. `docs/schema.json` changes `string` → `integer` for both, and
`tests/test_contract_wire.py::KNOWN_MISMATCHES` is now empty.

**This is a wire change.** A client that parsed `next_anchor` as a string
gets a number; regenerate the client. The empty transcript page (both
anchors `null`) is unaffected — it was always honest.


## [0.26.0] — 2026-09-17

### Changed — the admin no longer renders what a meeting was ABOUT

A ModelAdmin with neither `fields` nor `exclude` renders every column on the
detail page, readonly or not. So "staff may view recordings" silently meant
"staff may read the AI summary of any customer's meeting", and a fleet that
wanted to grant its operators status and metadata — enough to answer "did
this run" — could not: `view_recording` also rendered `summary`, and a
permission fixture had no way to mean less. The safe answer was not
expressible, so that deployment granted nothing and its operators got nothing.

`_ReadOnlyAdmin.CONTENT_FIELDS` now names the columns that carry what a
meeting was about rather than how it was processed, and they are excluded
from the detail view:

* **Recording** — `summary`, `title`, `metadata`, and the three storage keys
  (pointers to the audio and the transcript). `title` is the judgement call
  worth arguing with: metadata by schema, content by privacy — "Acme
  acquisition, legal review" says what the meeting was about as surely as the
  summary does. It leaves `list_display` and `search_fields` with it, because
  printing a title in a results table is the same disclosure by another
  route, and searching by one is worse.
* **Segment** — `text`, `original_text`, `words_json`. A segment IS the
  transcript.
* **Speaker** — `display_name`. A person who attended the meeting.

What stays is what a pipeline is debugged from: status, durations, provider,
retries, timings, counts.

**This narrows the admin for existing deployments**, deliberately. A host
that has actually decided its staff may read customers' meetings subclasses
and narrows the tuple — a reviewable line in that host, rather than a silent
consequence of a permission name.

## [0.25.0] — 2026-09-14

### Added — an empty wallet is a STATUS, not an error

A recording whose owner has no credits left ended in `error` with the
reason `insufficient_credits`. Everything downstream then treated it as a
breakage: the UI rendered "processing failed" over a recording that was
perfectly fine, `recording.failed` told refund and alerting consumers a run
had died when nothing had been spent, and the only offered way out —
`retry_recording` — re-ran the same stage into the same empty balance. The
one thing that would actually move it, money, had no state to arrive into.

`needs_payment` is that state.

* **`RecordingStatus.NEEDS_PAYMENT`** (migration `0007`, choices only). It
  is NOT in `PROCESSING_STATUSES`: `is_processing` answers False,
  `poll_after_seconds` and `Retry-After` are absent, and a client is told to
  stop asking — the next move is a person's, not the pipeline's.
* **`stages.StageNeedsPayment(reason, detail)`** — the signal, the same
  shape as `StageFatal`. The driver parks the recording, writes
  `workflow_state["needs_payment"]` (`stage` / `reason` / `detail` / `at`)
  and emits **`recording.needs_payment`**. Deliberately its own block, not
  `last_error`: a UI that reads `last_error` renders an error, and this is
  not one.
* **`normalize.NormalizePaymentRequired`** — so a host's affordability gate
  (iron-recordings' free-cap check) can say "this account cannot buy this
  recording" through the existing `NORMALIZER` seam,
  `(src, dst) -> duration`, with no second seam and no host import in the
  driver. The `convert` stage catches it **before** `NormalizeFatal` — it is
  a subclass, and the base-class handler reached first would swallow a park
  into a DLQ.
* **`pipeline.resume_after_payment(recording_id) -> bool`** — the mirror of
  `retry_recording`: `needs_payment -> queued`, resuming at the first stage
  whose name has not completed. It **keeps the `run_id`**, so a consumer
  metering `recording.completed` charges once for a run that needed a top-up
  to finish, not once per top-up. The park block moves to `paid_for` rather
  than vanishing. Every other status returns False with no side effects.

`needs_payment` is terminal for event deliveries, next to `error`: a broker
redelivery must not resurrect a parked recording, and the reconcile watchdog
no longer sweeps one as stuck — either would have the pipeline spend money
the account does not have, on every pass, forever.

**What a host must do.** Raise `StageNeedsPayment` (or
`NormalizePaymentRequired` from its normalizer) instead of failing the
stage; subscribe to `recording.needs_payment` (`recording_id`,
`workspace_id`, `owner_id`, `stage`, `reason`, `run_id`, `attempt`) to ask
its user for money; call `pipeline.resume_after_payment` from the code that
sees the payment land — NOT from a user-facing retry button, which is why
this is a separate transition and not a flag on `retry_recording`. A client
reads the new status plus `needs_payment_reason` off the recording payload;
the park's `detail` can carry balance internals and stays server-side, the
same line the error seam draws.

### Added — transcription that drops audio now leaves a mark

`run_qa` checked that segments were monotonic, inside the duration and
non-empty. A transcript with a **minute of speech missing out of the
middle** passed all three: the segments around the hole are monotonic, they
fit, and there are plenty of them. The only witness to dropped audio is the
distance between one segment's end and the next one's start, and nothing
looked at it.

* New check **`gap`** in `qa.checks`, same shape as its siblings:
  `"SKIP"` (fewer than two segments — nothing to compare),
  `"PASS: largest <n>ms"`, or
  `"FAIL: <n> gap(s) > 5000ms, first <n>ms between seg <a> and seg <b>"`,
  which also drops `qa.passed` to False. Overlapping and back-to-back
  segments are 0 or negative deltas and are never gaps.
* The threshold is **`transcript_schema.MAX_SEGMENT_GAP_MS`** (5000),
  exported, so an operator whose recordings are genuinely quieter than that
  can see and tune the number instead of finding it inside a comparison.

Caveat, stated because the check cannot state it itself: a **trimmed or
spliced** source has a legitimate hole at its seam, and this schema carries
nothing that says it was spliced — no offset, no trim marker, and
`duration_ms` against the segment span describes only the tail. A host that
splices sources knows it does; dropped audio has no other witness, so the
check stays.

### Changed

* `RecordingDTO` gains `needs_payment_reason` (null unless the status is
  `needs_payment`) — additive, and it clears itself when the recording moves
  on, so a paid, finished recording never still shows the reason it waited.

## [0.24.0] — 2026-09-12

### Fixed — one recording, six paid transcriptions, because two retry ladders multiply

Production data from a client stand: ONE 148-minute recording reached
ElevenLabs **six times** — two tasks × three attempts — and **59.7% of a
23,736-credit quota went to machine duplication of identical media**.
Nothing had failed at the provider; the transcription succeeded every
time. What failed was the reply (an 8.6 MB transcript against a broker
that carries 8), downstream of the money.

Two defects, one in each ladder, and this module owned both.

**Every stage submission now carries a `dedupe_key`**
(`<recording_id>:<storage_key>:<stage>`, `stages.stage_dedupe_key`). The
column has existed in core's task ledger since 0.60.0 and was **empty on
every row this module ever wrote**, so nothing coalesced the second task
into the first. Core reuses a task whose key is still PENDING/RUNNING —
that alone closes the two-tasks half. The storage key is in the key
because the OBJECT is what gets transcribed: a recording re-converted to
a new normalized key is genuinely new work.

**The priced call gets ONE task attempt**
(`TRANSCRIBE_TASK_MAX_ATTEMPTS`, was 3 via `submit_task`'s default). A
transport retry of a transcription is a second invoice for the first
transcript: the provider charges for a job it completed even when our
side never read the answer. A deliberate re-run belongs to the stage —
and with stapel-agent ≥ 0.24.0 it is free.

**The ceiling is now stated and asserted.** `MAX_STAGE_RETRIES` (3) and
the task's `max_attempts` (3) MULTIPLY, and nobody had done that
multiplication: 9 possible, 6 spent. `stages.transcribe_attempt_ceiling()`
is that product, `tests/test_retry_ceiling.py` holds it at ≤ 3, and every
attempt after the first is served from the agent's checkpoint at no
charge.

### Fixed — the agent was not given what it needs to avoid charging twice

`TranscribeStage.build_payload` now sends:

* **`audio_content_hash`** — the agent's checkpoint key (stapel-agent
  ≥ 0.24.0). `ConvertStage` computes it over the NORMALIZED object while
  the file is still local (the one moment it is), and stores it in
  `workflow_state`, not `metadata`: REC-01 — a client PATCH must never be
  able to write a value a server decision reads, and here that value is
  the key to a paid transcript. The key is reserved in `metadata` too, so
  a host that populates it there (still honoured, as the second source)
  cannot take it from a client either.
* **`audio_duration_ms`** — how much audio is being SUBMITTED, from
  `recording.duration_seconds` (ffmpeg measured it during convert). The
  agent's ledger meters this instead of the transcript's own duration,
  which several providers derive from the last word's end timestamp — two
  paid calls in the same incident sat at 0 minutes.

A retry re-derives both identically (the presigned URL changes, the key
does not), which is what makes the retryable `transcript_handoff_failed`
path safe: it re-runs the PUT, not the transcription.

### Changed

* `stapel-core>=0.60.0` (was 0.26.0) — `comm.start(dedupe_key=...)`.
* `submit_task(..., dedupe_key=None, stage=None)` — the key is derived
  from the stage when not given. Hosts calling `submit_task` from their
  own stages get one automatically; a host that passed positional
  arguments is unaffected (all new parameters are keyword-only).
* New setting `TRANSCRIBE_TASK_MAX_ATTEMPTS` (default 1).
* New reserved metadata key `audio_content_hash`.

## [0.23.1] — 2026-09-09

### Fixed — the transcript handoff object must not outlive the transcript

0.23.0 has `llm.transcribe` write `transcript.raw.json` into the
recording's prefix. Nothing deleted it, and **no field of the row points at
it** — so it was a second verbatim copy of a private meeting that erasure
could never find and retention could never reach.

* **The stage discards it** once the Segment/Speaker rows are committed. It
  is a postbox, not an artifact: the transcript's permanent home is those
  rows and the unified `transcript.json`. A failure before the commit
  leaves the object for the retry.
* **`TranscribeStage.resume` is idempotent** — it returns early when
  segments already exist. `task.completed` is at-least-once, and a
  redelivery must not try to re-read a handoff the stage has consumed.
* **Erasure sweeps a leftover** — a recording that died between the agent's
  write and the stage's read still has one. The key is derived
  (`stages.handoff_key`) and **probed before deleting**, so a receipt never
  counts an object that was not there.

## [0.23.0] — 2026-09-09

### Fixed — a meeting past ~2h28m was dropped at the transcribe stage

`llm.transcribe` answered with the whole transcript inline, and a long
meeting's transcript does not fit one broker message. Measured on a client
stand: **8 647 617 bytes against a NATS `max_payload` of 8 388 608**, the
reply refused, the recording parked in the DLQ at `transcribe`. Twice for
the same meeting, because the user uploaded it again.

The audio had always travelled to the agent as a presigned GET. The answer
now travels the same way.

* **`TranscribeStage.build_payload`** mints a presigned PUT for
  `<prefix>/<workspace>/<recording>/transcript.raw.json` and sends it as
  `transcript_put_url` + `transcript_key`; `resume` reads the object back
  through the STORAGE seam. On a real broker at 8 MiB the reply went from
  8 777 687 bytes to **550**.
* **`build_payload` is a HOOK.** Hosts that add a field (vocabulary
  biasing is the live example) used to copy the payload body into a
  subclass and keep the copy in lockstep by hand — which is how a fix to
  this stage silently misses a host's pipeline. Override or extend
  `build_payload`; `run` and `resume` stay the module's.
* **`transcript_from_result`** turns either answer shape — a reference or
  an inline transcript — into the transcript dict, in ONE place, so no
  caller grows a branch. A short read against the reference's byte count
  is refused as `transcript_truncated` rather than persisted: that is the
  failure this shape could otherwise hide, and it would surface as a
  recording missing its last hour.
* **`TRANSCRIPT_HANDOFF`** — `"auto"` (default), True or False. "auto"
  asks the storage backend (`RecordingStorage.signs_put_urls`, new, True
  on `S3Backend`), because a backend that cannot sign a PUT would be
  handed a URL that reads like an upload target and is not one. Resolved
  once from configuration, never per recording — a rule that depended on
  how big a transcript came out would be the same cliff with a longer
  fuse. The deployments it leaves off are those with no broker between the
  services, and so no ceiling to hit.

**TWO ARTIFACTS, ONE WRITER EACH.** `transcript.raw.json` is the agent's
provider-shaped `NormalizedTranscript` (seconds, `words`/`utterances`,
`raw`) and exists to get the answer off the wire. `transcript_storage_key`
still points at `transcript.json`, the **UnifiedTranscript** (schema 1.0,
`start_ms`/`end_ms`, `speaker_id`, QA and engine meta, content hash) that
`MergeStage` builds from the persisted Segment/Speaker rows and the API
serves. They are different schemas for different readers, so MergeStage's
write is **not** redundant and is unchanged.

**Floor: stapel-agent >= 0.22.0.** The `llm.*` schemas are
`additionalProperties: false`, so an older agent REJECTS a payload carrying
`transcript_put_url` — `checks.W011` says so at boot where it can see the
version, and in a split deployment **the agent service must be upgraded
first**.

### Fixed — a transcript with no speaker labels became ONE segment

`_utterances_from_words` — the fallback that builds segments when
`llm.transcribe` returns words but no utterances, which is what every
provider sends with diarization off — cut on the speaker changing and
nothing else. With no speaker ids the comparison is never true, so the
whole recording became one segment. On the same stand, **24 of 83
completed recordings render as a single segment and 7 as none**; one
ten-minute meeting is a single 8592-character turn.

It now cuts on a **0.65 s** pause, sentence-ending punctuation, and
unconditionally at **30 s / 500 characters** — the same numbers as
`stapel_agent.stt.segmentation`, derived from 94 608 real word gaps, and
asserted equal by a test. Duplicated rather than imported because this
module does not depend on the agent; a transcript that arrives without
utterances must not be cut by a different rule than one that arrives with
them.

A segment is what a timestamp anchors to, so this is also what decides
whether a citation can point anywhere inside a long recording.

**Existing recordings are not rewritten.**

## [0.22.1] — 2026-09-08

### Fixed — the refusals no view raises answer the fleet envelope

Patch, no API change, no schema change, no dependency change. One shipped file
moves: `_codegen_settings.py`, whose `contract=False` branch returned
`rest_framework = None` and therefore built settings with **no**
`REST_FRAMEWORK` dict at all — and that branch is what `conftest.py` runs the
whole suite on.

A settings module that writes its own `REST_FRAMEWORK` must carry
`EXCEPTION_HANDLER`, or DRF falls back to `rest_framework.views.exception_handler`
and every refusal **no view code raises** — 401/403 from authenticators and
permission classes, 404 from `get_object_or_404`, 405/406/415 from dispatch,
429 from a throttle — answers a bare `{"detail": …}` instead of
`{localizable_error, error, params, error_language}`.
`stapel_core.error_envelope.W001` (stapel-core 0.61.1) reports it.

`tests/test_guest_surface.py` is where it showed: five tests fire a guest
session at `IsNotAnonymousUser`-gated verbs and assert `403` — a status code
that is identical with or without the handler, so the whole file passed while
the body was DRF's. `test_the_guest_refusal_is_the_fleet_envelope` now asserts
the body, and fails on the previous harness with
`{'detail': 'You do not have permission to perform this action.'}`.

No pre-existing test changed behaviour.

The key is read off `stapel_core.testing.BASE_REST_FRAMEWORK` rather than
re-typed, and it is the only key set — DRF's own defaults stay where the
harness had them, so no permission, renderer or authentication behaviour
changes. The `contract=True` branch is untouched and the emitted contracts are
byte-identical.

All notable changes to stapel-recordings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

## [0.22.0] — 2026-09-07

This is an audio service, not a video host. Two things made that not quite
true: a size refusal that left the library as an unhandled exception, and a
`convert` stage that deleted the uploaded container on a best-effort basis
while leaving the key that pointed at it. Both are closed here, and the
storage limits are restated in terms of what is **kept** rather than what is
**sent**.

**Minor, not patch** (pre-1.0: minor = breaking). The stored object changes
format, the uploaded container is now guaranteed to disappear along with its
key, and the media endpoint no longer serves the container back. A host
upgrading should read *Host adoption* at the end of this entry.

### Fixed — a size refusal is an answer, not a 500

From a host's production log: `POST …/recordings/<id>/multipart` declaring
4 727 057 010 bytes against a 2 GiB ceiling ended in **HTTP 500**.
`services.start_multipart_upload` raised `UploadTooLarge`, a plain
`ValueError` subclass, and it propagated out of the host's DRF view as an
unhandled exception. Nothing in the host was wrong: the library gave it no
way to be right short of a `try/except` in every view that touches an
upload.

Every upload refusal is now a `StapelServiceError` carrying its own status,
registry key and params, so `stapel_exception_handler` — the handler every
deployed stapel service already runs — turns it into the standard envelope
wherever it escapes:

| Raised | HTTP | Key | Params |
| --- | --- | --- | --- |
| `UploadTooLarge` | 413 | `error.413.recording_too_large` | `{size, limit}` |
| `InvalidUploadSize` | 400 | `error.400.recording_upload_size_invalid` | `{limit}` |
| `InvalidMultipartParts` | 400 | `error.400.recording_multipart_parts_invalid` | `{max_parts}` |
| `MultipartMisconfigured` | 500 | `error.500.internal` | — |
| `UnsupportedUploadExtension`, `UnsupportedUploadContent` | 415 | `error.415.recording_unsupported_media` | — |
| `UploadNotStored` | 409 | `error.409.recording_invalid_state` | — |
| `UploadContentUncheckable` | 503 | `error.503.recording_upload_unverifiable` | — |

Each keeps its original built-in base (`ValueError` / `RuntimeError`), so a
worker, a management command or a script that catches those still catches
these — the HTTP mapping is additive, and nothing outside a view has to know
about DRF. `InvalidUploadSize` (the new "that is not a size at all": missing
when required, non-numeric, zero, negative) subclasses `UploadTooLarge` for
the same reason. `MultipartMisconfigured` is the one that is deliberately
**not** a 4xx: a part budget that cannot cover the ceiling is the operator's
mistake, and telling the client to fix its request would be a lie.

The 413's English text now carries `{size}` and `{limit}`, and the ru/es
catalogs carry the same slots, so the refusal can name the real numbers in
the language the user reads.

### Added — the limits are readable before the first byte

`GET /recordings/api/v1/recordings/upload-limits` (`services.upload_limits`,
`UploadLimitsDTO`) serves `max_upload_bytes`, `max_stored_bytes`,
`audio_only_ingest`, `stored_audio_codec` / `_channels` / `_sample_rate`,
`stored_bytes_per_hour`, `multipart_part_size`, `max_multipart_parts` and
`allowed_extensions`. A frontend can refuse an oversized file locally, name
the real limit, and tell someone what an hour of recording will cost them,
instead of discovering all three from a rejected request.

### Changed — audio-only ingest is the module's contract

Uploaded containers are transport. The `convert` stage extracts the audio
track, downmixes to **mono**, stores that one object and **deletes the
container** — every upload, whatever its size, whether or not it carried
video. Specifically:

* the source object is deleted **and `file_storage_key` is cleared**. Before
  this release the delete was best-effort and the key survived it, so the
  row claimed an object that was gone (or, when the delete failed, kept
  pointing at a video nobody meant to keep);
* a delete that fails is now `StageRetryable`, not a log line. The stage's
  idempotent re-entry runs the purge again. A warning in a log is not a
  deletion, and the raised container ceiling exists *because* the container
  goes away;
* `media.media_storage_key()` returns the extracted audio and, while
  audio-only ingest is on, **only** that. It used to prefer
  `file_storage_key`, so a media request before the pipeline reached
  `convert` answered with the raw video the deployment had already decided
  not to keep. Nothing in the API hands a container back now;
* the working copy lives in a temp directory removed in `finally` — success
  and every failure path, including a normalizer that dies mid-write.

**Stored profile: mono, 16 kHz, Ogg/Opus at 24 kbps** (`AUDIO_CHANNELS`,
`AUDIO_SAMPLE_RATE`, `AUDIO_CODEC`, `AUDIO_BITRATE_BPS`). That is
**~10.8 MB/hour**, against ~115 MB/hour for the 16 kHz mono PCM WAV this
stage wrote before, and against several hundred MB to a few GB per hour for
the video it is extracted from. Mono is not new — the normalizer has forced
`-ac 1` since the module's first release — so nothing downstream loses
channel separation it had: diarization here is the ASR provider separating
speakers **within** one mixed track, not channel separation. A host whose
provider does separate by channel sets `AUDIO_CHANNELS = 2` and pays for it
in bytes. `AUDIO_CODEC = "wav"` restores PCM for a provider that will not
take Opus.

**Every upload is re-encoded to the profile, including one that arrives as
audio already.** The alternative — skip the transcode when the source is
"already fine" — would have to be right about container, codec, channel
layout and sample rate at once, and it would make the stored bytes depend on
what the client happened to send, which is exactly the unpredictability the
profile removes. One generation of Opus at 24 kbps mono costs nothing an ASR
or a diarizer can see; a stored object whose size nobody can predict costs a
storage plan.

Keeping originals is a documented exception:
`STAPEL_RECORDINGS["AUDIO_ONLY_INGEST"] = False`.

### Changed — two ceilings, because they answer two questions

| Setting | Default | Bounds |
| --- | --- | --- |
| `MAX_CONTAINER_UPLOAD_BYTES` | 16 GiB | what we **receive** and run through extraction: bandwidth, temp disk, ffmpeg time |
| `MAX_STORED_BYTES` | 512 MiB | what we **keep**, enforced on the extracted audio (≈47 h at the Opus profile) |
| `MAX_UPLOAD_BYTES` | 2 GiB (unchanged) | the ceiling when nothing is extracted, where received and stored are the same object |

`services.accepted_upload_limit()` picks between the first and the last, and
it is the single source of the 413 line and of the upload-limits read. It
fails safe: drop `convert` from the `PIPELINE`, or point `NORMALIZER` at
`passthrough_normalize`, and the accepted size falls back to
`MAX_UPLOAD_BYTES` on its own — a raised ceiling cannot outlive the promise
that made it large. Extracted audio over `MAX_STORED_BYTES` is
`StageFatal("stored_audio_too_large")`.

### Added — three system checks, so the environment is not discovered at upload time

* `stapel_recordings.E006` — `NORMALIZER` is `ffmpeg_normalize` but
  ffmpeg/ffprobe are not on PATH, or the build carries no `libopus` for
  `AUDIO_CODEC = "opus"` (a normal thing to find in a slim image), or
  `AUDIO_CODEC` is not one this module can write. All of it was previously
  discovered as `NormalizeFatal` on a recording someone was waiting for.
* `stapel_recordings.E007` — `AUDIO_ONLY_INGEST` is on but nothing extracts:
  no `convert` stage in the `PIPELINE`, or a passthrough normalizer. Without
  this, the promise inverts silently and every container is stored, at the
  raised ceiling, forever.
* `stapel_recordings.E005` — the multipart part budget, now checked against
  the **accepted** ceiling rather than `MAX_UPLOAD_BYTES`; checking the
  smaller number would pass a deployment whose every real upload fails.

### Added — `Recording.stored_size_bytes` and a read-only census

`file_size_bytes` measures what was **received**, and with audio-only ingest
that object stops existing minutes later — so a host sizing a bucket from it
was reading the size of something that no longer exists.
`stored_size_bytes` (migration `0006`, nullable, additive) is what is kept,
written by `convert`.

`python manage.py recordings_audio_census [--measure] [--json] [--workspace]`
reports how many recordings still hold an uploaded container, how many are
named as video containers, their total bytes, and what the same recordings
would occupy as mono audio at the configured profile. **It reads and
reports; it changes nothing** — no delete, no re-encode, no field written.
Reclaiming that space is a decision, and a backfill that acts on this census
would be a separate command in a separate release.

### Fixed — a duplicate finalize after conversion

`_finalize_upload_locked` treated a non-empty `file_storage_key` as "already
finalized". Now that `convert` clears that key, a late duplicate finalize
would have re-verified an object that no longer exists and answered 409 for
an upload that in fact succeeded. It now also accepts the session's
`finalized_at` as the marker.

### Host adoption

A host on the defaults needs to configure **nothing**, but should know:

1. **ffmpeg must have libopus.** `manage.py check` is now red without it
   (E006). `ffmpeg -encoders | grep libopus` on the image settles it; the
   alternative is `AUDIO_CODEC = "wav"` at ~10.7x the stored bytes.
2. **Raise the accepted ceiling deliberately.** The default
   `MAX_CONTAINER_UPLOAD_BYTES` is 16 GiB; the reverse proxy in front
   (nginx `client_max_body_size`) and the multipart part budget have to
   agree with it — E005 checks the second, nothing here can check the first.
3. **Existing recordings are not touched.** Rows that already have both a
   `file_storage_key` and a `normalized_storage_key` keep both objects until
   `convert` runs for them again; the census names them
   (`already_extracted`), and their containers are pure waste — nothing has
   to be re-encoded to reclaim them.
4. **Media playback changes shape.** A recording that has not reached
   `convert` yet now answers `409 error.409.recording_media_not_stored`
   instead of handing back the uploaded container; after `convert` it serves
   the extracted audio (`.opus`, `audio/ogg`), so a player that assumed
   `audio/wav` needs to not.
5. **`@stapel/recordings-react` needs a regen** — a new endpoint and a
   changed error catalog.

## [0.21.1] — 2026-09-03

### Added — `VECTOR["BATCH_MAX_CHARS"]`, because BATCH_SIZE is the wrong unit

`BATCH_SIZE` counts texts. A text-embeddings server budgets **tokens**
(TEI's `--max-batch-tokens`). Those agree only while the embedded unit
keeps the size it had when someone tuned the count — so a batch size
chosen for 37-character utterances refuses the same number of
600-character windows, and refuses it by dropping the connection, which
the embed stage can only read as a transient failure: retry, retry, DLQ.

That trap is now reachable from a setting this package itself added one
release ago (`SEGMENT_SCHEME="window"`), so the bound belongs next to the
unit rather than in a host's memory. `BATCH_MAX_CHARS` closes a batch once
its texts total that many characters; `0` (the default) is count-only and
byte-identical to before. A single item over the budget still goes out
alone — a server that cannot take it should say so, which beats losing it
here.

Hosts changing `SEGMENT_SCHEME`, `SUMMARY_CHUNK_CHARS`, or the embedder
should set it to roughly `max_batch_tokens x chars_per_token`.

## [0.21.0] — 2026-09-03

A launch audit of a live deployment measured the Ask AI retrieval path and
found four defects, none of which raised an error, failed a test, or showed
up on a dashboard. All four are fixed here, and the fifth item is the
instrument that would have caught them: an offline eval. Measured on that
deployment's own transcripts (26 labeled RU+EN questions), the mode the
product actually calls — `hybrid` — goes from **MRR 0.312 / recall@10
0.769** to **MRR 0.522 / recall@10 0.923**.

### Fixed — the text arm returned the whole corpus for every query

`ts_rank` is a ranking function, not a match predicate, and it does not
answer 0 for a non-match. PostgreSQL's `calc_rank_and` starts at `-1.0` and
`calc_rank` clamps a negative result to `1e-20`, so a query of two or more
terms that matches **nothing** scores `1e-20` — strictly greater than zero —
on every row in the table. `.filter(rank__gt=0.0)` therefore filtered
nothing at all: the text arm handed reciprocal-rank fusion a full-length
ranking of noise, which took ranks 1..N and outranked the vector arm's
genuine hits. Live symptom: three unrelated questions came back with the
same five irrelevant segments at the top.

Matching is now `tsvector @@ tsquery` (`.filter(search=sq)`) and `ts_rank`
only orders what already matched. A larger threshold was not an option: it
is the same bug with a bigger constant, and it starts discarding real faint
matches on the way.

`VECTOR["FTS_SEARCH_TYPE"]` is new and shapes the query — `"plain"`
(default, unchanged semantics: every term required), `"websearch"`, or
`"any"` (terms OR'd; ranking rather than filtering decides). The default is
untouched because the eval says so: on the measured corpus `"any"` scores
better as a standalone arm (MRR 0.283 vs 0.000 for a question-shaped query)
and worse once fused (hybrid MRR 0.467 vs 0.522).

### Fixed — the vector arm under-returned under a tenant filter

An HNSW index scan knows nothing about `workspace_id`. The predicate runs
on what the scan already returned, so with the default `hnsw.ef_search` a
workspace holding a few percent of the corpus has nearly all of its
candidates thrown away *after* the fact. Measured on the deployment: a
`LIMIT 5` query over a workspace with **234 eligible rows returned 0**
(`EXPLAIN`: the HNSW scan stopped at 54 tuples, none of them in the
workspace). Nothing errored — the arm just quietly retrieved less, which is
why it survived to production.

The arm now sets pgvector's `hnsw.iterative_scan` for the duration of its
query (`VECTOR["HNSW"]["ITERATIVE_SCAN"]`, default `relaxed_order`, bounded
by `MAX_SCAN_TUPLES`), which is the mechanism built for exactly this and
needs pgvector ≥ 0.8 — the version is read from the catalog, not assumed.
On older pgvector it falls back to widening `hnsw.ef_search` by the tenant's
measured share of the corpus. `relaxed_order` returns near-neighbours
slightly out of order, so the arm re-sorts on the distance it already has.
Same `EXPLAIN`, after: 5 of 5, the scan continuing to 343 tuples.

### Fixed — 64 of 70 recordings were searched without a stemmer

Speech-to-text reports ISO 639-2/3 (`rus`, `eng`, `spa`, `zho`);
`VECTOR["FTS_CONFIGS"]` is keyed on ISO 639-1. Nothing reconciled them, so
Russian was searched as `simple` — no stemming, no stopwords — and the only
symptom was worse results.

New `stapel_recordings.languages.to_iso639_1` normalizes a tag (regional
subtag dropped, 639-2/3 mapped) before every config lookup. The table is
the complete ISO 639-2 set that has a 639-1 equivalent — 204 entries,
generated (`tools/gen_iso639.py`), including both the bibliographic and
terminological forms (`ger`/`deu`, `fre`/`fra`, `chi`/`zho`, `dut`/`nld`),
which is precisely the class of case a hand-written shortlist gets wrong.
Hosts keep keying `FTS_CONFIGS` on two-letter subtags; nothing to change.

### Fixed — RRF double-counted a key an arm listed twice

Reciprocal-rank fusion is defined over a ranked list of *distinct*
documents. Summing a repeat is not a rounding error: two appearances at
ranks 20 and 21 sum to more than a single appearance at rank 1, so one
duplicated key leapfrogs the entire result. An arm can produce repeats
honestly — the vector arm keys hits by segment, and one long utterance can
anchor a dozen windows. A repeat now counts once, at its best rank, and the
vector arm returns each segment once (nearest wins), since a hit *is* a
segment id to the fusion, the citation and the host DTO alike.

### Added — a second embedding scheme: answer-sized windows

The embedded unit was one STT utterance. On the measured deployment the
median such unit is **37 characters** — neither an embedding worth ranking
nor a passage anyone can answer from. `vector/chunking.py` packs
consecutive utterances into windows of ~600 characters (never past 800),
each line prefixed `[mm:ss Speaker]` so the retrieved passage carries who
said it and when, with whole-utterance overlap across boundaries.

This is a change to what is IN the index, so it is not a cutover.
`SegmentEmbedding` gains `scheme` / `text` / `span` / `chunk_index`
(migration `0002`, expand-only; existing rows are stamped `"segment"`,
which is what they are), the uniqueness key widens to
`(segment, model, scheme, chunk_index)`, and search reads exactly one
scheme. So `manage.py recordings_reembed --scheme window` builds the new
index while the live one keeps serving, and `VECTOR["SEGMENT_SCHEME"]`
adopts it — or reverts it — as one setting.

**The default stays `"segment"`.** On the measured corpus windows are
better (hybrid MRR 0.522 → 0.581, recall@5 0.692 → 0.808; English MRR
0.567 → 0.726, Russian 0.476 → 0.436), but "better on one corpus" is not
"better for every host", and a library default that silently re-chunks
everyone's index is not a default. Measure, then flip.

### Added — `manage.py recordings_search_eval`, the number behind all of this

`vector/evaluation.py` + the command score a labeled question set with
recall@k and MRR. Questions are labeled with `(recording_id, start, end)`
**time spans**, not row ids: re-chunking the index changes which row
carries a passage, so an id-labeled set stops measuring the moment the
thing it exists to measure changes. `--set KEY=VALUE` A/Bs any `VECTOR`
key for a single run, `--mode` scores arms separately, `--json` makes a
before/after pair a diff instead of two screenshots. Run it against a
restored copy of real data — it is one embedding call per question per
mode, and it belongs on a workstation, not on the box serving users.

### Compatibility

- **Migration required** (`recordings_vector.0002`) — additive, and it
  applied to a 2 870-row production copy in 50 ms.
- **Custom `ORMVectorStore` substitutes** must accept the new
  `scheme` / `text` / `span` / `chunk_index` keywords on `upsert_segment`
  and the `scheme` argument on `segment_hashes`. This is the breaking
  change behind the minor bump.
- The text arm returns far fewer rows than it used to. That is the fix:
  what it returned before was not results.
- No new dependency. No reranker was enabled.

## [0.20.2] — 2026-08-30

### Fixed — a malformed id in an action payload was a poison pill

`ValidationError` is not a `ValueError`. Django answers a key it cannot coerce
to a column's type — a malformed UUID above all — with
`django.core.exceptions.ValidationError`, which does **not** subclass
`ValueError` or `TypeError`. The `user.deleted` / `user.merged` guards here
caught only `(ValueError, TypeError)`, so a bad id walked straight through
them, the handler raised, `consume_actions` re-raised to the bus, and the
event came back forever: a redelivery loop over a payload no retry can repair,
burning the consumer's retry budget while looking exactly like a downstream
outage.

The consumed contracts do not save anyone from this. They type an id as
`{"type": "string"}` — and where they do say `format: uuid`, `jsonschema`
does not enforce `format` unless a format checker is passed, which the comm
registry does not do. A malformed id is a well-formed payload.

`recordings_for()` promised in its own docstring to return an empty queryset —
"never raises" — for a subject key that cannot address a recording, and named
"a malformed uuid" as the case it covered. Its account branch caught only
`(ValueError, TypeError)`, so that was exactly the case it did not cover, and
it took `user.deleted` and `gdpr.erasure.requested` down with it.

`handle_user_merged` had a second door: the *from* id was probed under the
guard but the survivor probe, `get_user_model().objects.filter(pk=into_user_id)`,
sat outside it, so a malformed *into* id still escaped whenever the guest
genuinely owned rows. That read moved inside the guarded block — still before
the first write.

`MergeTargetNotReady` is untouched: a survivor id that *parses* but has no row
here still raises, because that one is a real ordering lag.



## [0.20.1] — 2026-08-30

### Fixed — a guest's recordings survived their sign-in, but nobody could find them

stapel-auth folds an anonymous guest into the account it just proved it owns
and then deletes the guest row, announcing it with `user.merged`. This module
never subscribed. Its three user columns are all `SET_NULL`, so the recordings
were not erased — they were stranded: still on disk, owned by nobody, and
invisible to the person who made them. The only record of where they went
lived in an event nothing consumed.

`handle_user_merged` carries `Recording.owner`, `Job.owner` and
`RecordingShare.created_by` onto the survivor in one transaction. Soft-deleted
recordings move too — a row inside its restore window still belongs to
somebody. A guest that owns nothing here is a quiet no-op (which is also the
at-least-once idempotency path); a guest that owns rows while the survivor has
no user projection here yet raises `MergeTargetNotReady`, so the outbox
redelivers rather than marking the transfer done and losing it.

## [0.20.0] — 2026-08-24

### Added — the owner can read their own transcript

**The finding:** speaker-attributed segments left this module through exactly
one door — `SharedRecordingDTO.segments`, the projection behind a **public
share link**. An owner who wanted to render their own transcript had to
publish it to the internet first. `RecordingDTO.transcript_storage_key` was
not a second door: it is a raw object key and nothing signs it (the media
endpoint signs the *media* object). So the module shipped a transcription
pipeline whose output its own owner could not read, and "player with
transcript" was un-buildable for anyone but an anonymous visitor.

- **`GET /recordings/api/v1/recordings/{id}/transcript`** — owner-facing,
  paginated. Owner scope **and then** the object policy's `can_read`, so a
  host that narrows `RECORDING_POLICY` narrows this with it and the
  transcript is not a side channel around the verb the detail and media
  endpoints ask; an unknown / foreign / deleted recording is `404`.
- **Same DTO as the share path.** `SharedSegmentDTO` is renamed
  **`TranscriptSegmentDTO`** (same five fields) and both doors now project
  through one mapper, `dto.segment_to_dto` — a transcript renderer is written
  once. This is the release's only breaking change, and it is a rename of a
  schema component, not a change of shape.
- **Anchored on `sequence_num`, ascending** (`TranscriptPagination` over
  core's `AnchorPagination`): a transcript is read forward, so
  `direction=next` walks later segments, and the anchor keeps a page stable
  while the pipeline is still appending. `anchor` / `limit` / `direction` are
  **declared** in the emitted schema — an undeclared query parameter is one
  that disappears from every generated client. Page size and ceiling are
  settings (`TRANSCRIPT_PAGE_SIZE` 200, `TRANSCRIPT_MAX_PAGE_SIZE` 1000).
- A recording with no segments yet answers `200` with an empty page. "Not
  transcribed yet" is a normal stage; a `404` there is indistinguishable from
  "not yours".

### Added — progress says when to ask again, and when to stop

This module serves no WebSocket (no consumer, no routing module, no Channels
dependency), so a client learns that a recording moved by reading it again.
Nothing in the response said whether that was worth doing — two frontend
hooks documented themselves as polling and shipped no interval, because there
was no number to ship. There is now:

- **`RecordingDTO.is_processing` / `RecordingDTO.poll_after_seconds`** on
  every recording payload, plus a **`Retry-After`** header carrying the same
  number. One computation (`dto.poll_after_seconds`) behind both, so the body
  and the header cannot disagree.
- Present **only** while the pipeline owns the next transition — the new
  `RecordingStatus.is_processing` (`queued`, `analyzing`, `normalizing`,
  `transcribing`, `diarizing`, `merging`). `created` / `uploading` wait on the
  client's own upload; `completed` / `error` / `deleted` are terminal. For
  those the field is `null` and the header is absent, and **that absence is
  the "stop asking"** — a client polling a failed recording forever is the
  defect this shape exists to prevent.
- `GET .../transcript` carries the same hint (a client watching a transcript
  fill in polls the transcript, not the recording), and the `202` from
  `/resummarize` carries `Retry-After: JOB_POLL_INTERVAL_SECONDS` — accepted
  is not finished.
- New settings: `POLL_INTERVAL_SECONDS` (5), `JOB_POLL_INTERVAL_SECONDS` (10).

### Fixed — re-summary of a recording with no language failed to submit

`_summarize_payload` fell back to `UnifiedTranscript.language` when the
recording had none of its own — but that attribute is a `LanguageMeta`
struct (routed / detected / path), not a language tag, and a task payload has
to be JSON. So `POST /resummarize` raised on exactly the case the fallback
was written for (`language_mode="auto"`, nothing detected yet). It now reads
`routed` / `detected` off the struct and always yields a string. Found while
testing the 202's `Retry-After`; every existing test happened to run the
pipeline first, which sets `recording.language` and hid it.

### Changed

- Every response that carries a recording goes through one helper, so no
  recording-bearing endpoint can ship without the polling hint.

## [0.19.0] — 2026-08-24

### Added — every pipeline run says which run it is (`run_id` / `attempt`)

**Additive; no payload field changes meaning, none is removed, and the new
ones are optional in the committed schemas.**

A recording can be put through the pipeline more than once —
`pipeline.reprocess_recording` clears the cursor and re-runs every stage —
and each run costs whoever hosts it real money. Until now the terminal
`recording.completed` of the second run was byte-identical to the first
run's: same `recording_id`, same counts, same provider. A consumer that
meters or bills **post-hoc on that event** could therefore only build the
idempotency key `recording:<id>`, its second debit short-circuited on the
first run's transaction, and **every re-run was free**. That is the exact
shape of the documented monetization path — a free user's upload is trimmed
to the cap, they buy credits, they reprocess to unlock the rest — so the
full run it exists to sell charged zero.

Runs now carry identity:

- **`run_id`** (uuid) is minted when a run starts: the first
  `start_pipeline`, and again on **every** `reprocess_recording`.
  **`attempt`** counts the runs of one recording (`1` = initial processing,
  `+1` per reprocess). Both live in `workflow_state["pipeline"]` beside the
  progress cursor — the driver's field, never the client's `metadata` (audit
  REC-01) — so **no migration**: `workflow_state` is already a JSONField.
- Both travel in the public run events: **`recording.completed`**,
  **`recording.stage_completed`** and **`recording.failed`**. The key a
  metering consumer builds is `recording:<id>:<run_id>`.
- **`pipeline.run_identity(recording)`** reads the pair back
  (`{"run_id", "attempt"}`) for an invoice line, a credit hold or an audit
  trail, instead of re-deriving the driver's private cursor key names. It
  never mints — a recording that has not entered the pipeline answers
  `{"run_id": None, "attempt": 1}`.
- **`retry_recording` deliberately keeps the identity.** Resuming a DLQ'd
  run is the *same* run; a run that needed a retry to finish must be billed
  once, not twice. Only a reprocess is a new run.
- A run already in flight when this version ships has a `pipeline` marker
  but no `run_id`; the driver backfills one on its next write, so no
  in-flight recording reaches `recording.completed` without one.

`recording.resummarized` was audited for the same gap and has none: its
`job_id` is minted per re-summary, so two re-summaries of one recording
already produce two keys (0.17.0). `recording.uploaded` needs none — it is
emitted once, when the file lands.

Schemas: `run_id` (string) and `attempt` (integer ≥ 1) added to
`schemas/emits/recording.{completed,stage_completed,failed}.json`, **not**
in `required` — a payload emitted by 0.18.0 still validates, so this widens
the contract rather than breaking it. The driver always sends both.

Consumer side: a host billing on `recording.completed` must re-key its
idempotency to `recording:<id>:<run_id>` to actually collect for a reprocess.

- llms.txt budget 6500 → 7000 (Makefile + `tests/test_contract.py`): the new
  surface entry landed the artifact at 6488/6500, on the ceiling. Per the
  standing rule, the ceiling moves; `intent` lines are not shortened to fit.

## [0.18.0] — 2026-08-23

### Removed — `Recording.asr_tier`, a column nothing ever read

**Breaking (minor): a column is dropped and a model attribute disappears.**
`asr_tier` (`fast` / `accurate`) was written on every recording since
`0001_initial` and consulted by nothing: the transcribe stage builds its
payload from the provider settings, never from this column. It was never
serialized either — no DTO, no serializer, no schema entry carried it — so
**no response body changes**; what breaks is Python that touches
`recording.asr_tier`, filters on it, or passes it to `Recording(...)`, and
any SQL against the `asr_tier` column.

A write-only column is not a seam a host can use, and it is worse than
absent: one host had already deleted its own surface for the setting and
could not drop the column, because dropping another app's column is not its
migration to write. So the deletion happens here, in one release —
`models.ASRTier` and the field, `migrations/0005_drop_asr_tier` for the
column. `Recording` is otherwise unchanged.

- `0005_drop_asr_tier` is marked `# stapel: contract-phase`. The cutover
  marker is the machine-checked one and it wants a data-carrying `RunPython`
  before the destructive operation; there is no data to carry — the values
  are being discarded, deliberately — so this is a contract-phase drop of a
  column no code path reads. Rolling back re-adds the column with its old
  default, not the old values.

### Changed — a policy refusal states its reason (402 is now expressible)

`RecordingPolicy.can_reprocess` / `can_resummarize` may now answer with a
**`PolicyDecision`** instead of a bare `bool`:

```python
class MeteredPolicy(OwnerOnlyPolicy):
    def can_resummarize(self, user, recording):
        if not self.can_read(user, recording):
            return PolicyDecision.deny()                       # 404, as before
        if credits_of(user) < 1:
            return PolicyDecision.deny("error.402.myapp_out_of_credits", status=402)
        return PolicyDecision.allow()
```

**Why it had to change shape.** 0.17.0 shipped the re-summary as the endpoint
a host *bills* against — the `recording.resummarized` event fires inside the
storing transaction precisely so a host can debit there. But the authority
seam in front of it answered `True`/`False`, and the view rendered every
`False` as the module's fail-closed `404 error.404.recording_not_found`. A
host that meters re-summaries therefore had exactly one way to refuse an
out-of-credit user: tell them the recording does not exist. The bit was one
bit short of the answer — the moment it decided the user may not proceed is
the moment the reason mattered most, and that is where it was thrown away.
(Same defect the frontend's `ActionAvailability` closes on the rendering
side; there is no Python-side canon for it in stapel-core, so this is the
module's own dataclass, deliberately the same shape: allowed, or refused
*with* a code.)

- The host's `error_code` reaches the client untouched in the StapelError
  envelope, so a key the host registered through `register_service_errors`
  renders its own sentence and the UI can branch on it (offer a top-up
  instead of an "it's gone").
- Both endpoints honour it: `POST .../{id}/resummarize` and
  `POST .../{id}/reprocess`.
- **`bool` stays accepted for one minor** and is coerced to the old
  semantics (`True` allows, `False` → `404`), which is what the shipped
  `OwnerOnlyPolicy` still returns — no host has to move. Returning a bool
  from the two metered verbs is deprecated: a host that wants any answer
  other than 404 must return a `PolicyDecision`.
- `PolicyDecision.__bool__` follows `allowed`, so a host that returns a
  decision from a verb whose call site still expects a bool
  (read/edit/delete/upload) is refused, not accidentally granted.
- Order of checks is unchanged: unknown/foreign/deleted recording is still
  `404` before the policy is consulted, and the policy is still consulted
  before the `409`/`503` state refusals.

**Two new error keys** (`docs/errors.json`, ru + es catalogs), used as
fallbacks when a decision names a status but no key — never as a rewrite of
a key the host supplied:

- `error.402.recording_payment_required` — "This action requires available
  credit"
- `error.403.recording_action_denied` — "You are not allowed to do that with
  this recording"

### Docs

- `pipeline.reprocess_recording`'s docstring (and the matching capability
  intent) no longer offers "changing the ASR tier" as a reason to reprocess:
  with `asr_tier` gone that named nothing in this module. It now says the
  transcription provider, which is what a host actually changes.

## [0.17.1] — 2026-08-23

### Fixed

- **0.17.0 was tagged but never published.** `docs/errors.json` is generated
  from the *live* error registry, which includes every key stapel-core
  registers — and the workspace venv that generated it was pinned to
  stapel-core 0.26.0 while CI installs core from git main (0.34.0). The
  committed artifact was missing `error.503.mandate_unavailable`, a key core
  gained in between, so the drift gate failed on every CI row while passing
  locally. Regenerated against core main; the error count goes 56 → 57. No
  library code changed — 0.17.1 is 0.17.0's content, published.

  The lesson is about the gate, not the key: an artifact generated from an
  installed dependency is only as current as that dependency, so "make
  contract is green" means nothing until the venv that ran it matches what CI
  installs.

## [0.17.0] — 2026-08-23 (tagged, unpublished — see 0.17.1)

### Added — the cheap regenerate: a summary re-run that is not a reprocess

Minor, not patch: a new endpoint, a new public Action, a new policy verb, two
new error keys and two newly reserved metadata keys. Nothing existing changes
shape.

**The hole.** A transcript gets corrected — a speaker renamed, a turn
reassigned, a word fixed — and the summary built from the *old* transcript is
now wrong. This module could already tell you that (the summary is pinned to
the transcript it came from, see below); what it could not do was fix it. The
only thing that re-ran summarization was `pipeline.reprocess_recording`, which
re-runs **every** stage from zero: a second transcription, a second
diarization, a second bill — for a recording whose transcript is already
correct and was probably just corrected by hand. That is why the only
regenerate path products ended up shipping was staff-only. The cheap version
did not exist, so the expensive one got locked away instead.

**`POST /recordings/api/v1/recordings/{id}/resummarize`** is the cheap version.
It re-runs the *same* `llm.summarize` call the `merge` stage makes, over the
transcript already stored, and stores the result through the *same* writer —
one summarize implementation, two ways in. No STT, no diarization, no
pipeline, and the recording's `status` never moves: a re-summary is work
*about* a finished recording, and putting a completed recording back into a
processing status would tell every listing that the transcript is in doubt
when it is not.

- **It is not a pipeline entry, and it could not be one.** The driver refuses
  to touch a recording in a terminal status (`completed` is terminal) and
  walks a stage *list* from a cursor. So the re-summary is its own small
  runner in `stages.py` — `start_resummarize` / `resume_resummarize` /
  `fail_resummarize` — tracking its life on the module's existing `Job` ledger
  (`type="summarize"`) instead of on the pipeline cursor, where it can neither
  move a status nor disturb a run in flight.
- **`202` + a job reference** (`JobDTO`), because the work is accepted, not
  finished. **Idempotent on the job in flight**: a second request joins the
  first Job instead of paying for a second summary, so a double-clicked button
  costs one summary. `409 error.409.recording_no_transcript` when there is
  nothing to summarize yet; `503 error.503.recording_summarize_unavailable`
  when the deployment has summaries switched off or the bus refused the
  submission — the same 409/503 split media delivery already uses: 409 is the
  caller's state, 503 is the deployment's.
- **`RecordingPolicy.can_resummarize`**, defaulting to that policy's own
  `can_reprocess` answer. A brand-new verb that defaulted to deny would break
  every host that had already narrowed reprocess; one that defaulted to allow
  would widen them. Delegating asks the question the host already answered,
  and one method override still buys the split hosts actually want: users may
  pay to re-summarize, only staff may re-run the pipeline.
- **`recording.resummarized {recording_id, workspace_id, user_id, job_id}`** —
  emitted inside the same transaction as the summary write (outbox
  discipline), so a host that debits credits for this is never told about a
  summary that rolled back and always told about one that did not. `job_id`
  travels because delivery is at-least-once and a debit needs a key that
  identifies *this* re-summary, not merely this recording. **No billing is
  imported here** — the hold/capture belongs to the host; this module only
  says what happened, once, per summary produced. A failed run emits nothing.

### Fixed — a regenerated summary that read as stale forever

`stages.store_summary()` is now the one writer for a produced summary, used by
both the `merge` stage and the re-summary, and it stores two things: the text,
and **which transcript produced it** —
`metadata["derived"]["summary"]["transcript_hash"]`, the canonical
transcript's own deterministic hash — while clearing the older boolean
`metadata["staleness"]["summary"]`.

Before, `merge` set `recording.summary` and nothing else. A consumer that
recorded a version key when it generated a summary, and computed staleness by
comparing it, would therefore see a *pipeline*-produced summary carrying no
key at all — and a summary regenerated by anything that only cleared the flag
kept the previous transcript's key, so it read as stale forever: the flag says
fresh, the key says stale, and the key wins. Writing the key at the moment the
summary is produced is the only time it can be right.

`derived` and `staleness` join `metadata.LIBRARY_RESERVED_KEYS`. They are
server-written values living in the client's column (they are there because
the answer crosses the seam — hosts serialize "is this summary current" onto
the wire), and reserving them is what keeps them server-owned: a client can no
longer hand in a version key and forge freshness. `set_user_metadata` now also
**carries reserved keys across a write** — refusing to let a client write them
was only half the guard, because a metadata write is a whole-document replace,
and a client that simply omitted them deleted the server's receipts. Losing
the version key reads exactly like "this summary is current", which is the one
lie the receipt exists to prevent.

### Internal

- `stages.summary_from_result()` — one truthiness rule for "a summary was
  produced", shared by both callers.
- `actions.handle_task_completed` / `handle_task_failed` ask the re-summary
  runner first and fall through to the pipeline driver: the runner claims only
  the task ids its own Job rows wait on, which makes this an ordering rather
  than a fork.
- llms.txt budget 6000 → 6500 (five new usage-surface entries). Raised
  deliberately, per the Makefile's own rule — intents are not shortened to fit.

## [0.16.1] — 2026-08-23

### Fixed

- **0.16.0 was tagged but never published.** Two of its new tests reached for
  `celery` to build the beat entry, and celery is not a dependency of this
  package — it is optional by design, since `purge_soft_deleted_recordings`
  is a plain callable any scheduler can invoke. The tests passed in a
  workspace venv that happened to have celery and failed on every CI matrix
  row. The beat-factory test now skips without celery; the system-check test
  builds its schedule from `PURGE_TASK_NAME` instead, so the check stays
  pinned everywhere. No library code changed — 0.16.1 is 0.16.0's content,
  published.

## [0.16.0] — 2026-08-23 (tagged, unpublished — see 0.16.1)

### Added — a deleted recording is finally deleted (subject-scoped erasure)

Minor, not patch: three new settings, two new modules on the public surface,
a new system check, and two comm actions this module now consumes and
answers. Nothing existing changes shape.

Two halves of the same hole, closed together.

**The first: the soft delete had no end.** Deleting a recording stamped
`deleted_at`, the row left every listing, and the audio, the transcript and
the embeddings stayed exactly where they were — forever. The only thing that
could still erase the bytes was closing the whole account. `tasks.
purge_soft_deleted_recordings` is the missing half: daily, every recording
soft-deleted longer than `PURGE_AFTER_DAYS` (30) ago with no erasure already
in flight gets one **opened** for it. It deletes nothing itself, and that is
the design — routing the purge through a gdpr erasure is what gets a
single-recording delete receipted by every owner that claims it (this module
for the rows and objects, media for derived files, the agent for prompts
about it) and lets the product show "pending deletion until X" instead of
guessing. `get_recordings_beat_schedule()` wires it (fleet canon:
`stapel_gdpr.tasks.get_gdpr_beat_schedule`); celery stays optional, the task
is a plain callable. A host that drives a beat schedule containing nothing
from this module gets **`stapel_recordings.W010`** at boot — a retention
policy nobody schedules is a promise, not a mechanism, and from the outside
it is indistinguishable from one that works.

**The second: the account was the only subject anyone could erase.** With
stapel-gdpr 0.5.0 the subject of an erasure became a parameter, so this
module now declares itself a data owner for four of them — `account`,
`workspace`, `meeting`, `recording` — and `actions.py` handles
`gdpr.erasure.requested` for all four: erase, then confirm with
`gdpr.section.erased` carrying the **counts** it removed. Counts, because
"it says it ran" and "it says what it did" are not the same receipt.

`erasure.erase(subject_type, subject_key, workspace_id=None)` is now the ONLY
code that destroys a recording. `RecordingsGDPRProvider.delete()` is a call
into it (`subject_type="account"`), and so is the deprecated
`@on_action("user.deleted")` — which keeps working for one minor, since
stapel-gdpr keeps firing it until 0.6.0, and now receipts too when the
payload carries a correlation_id. One destruction path, four subjects: rows,
everything cascading from them (`Speaker`, `Segment`, `UploadSession`,
`RecordingShare`, `Job`, and — with the opt-in vector app —
`SegmentEmbedding` / `RecordingEmbedding`), and every storage object, through
the STORAGE seam the uploads already use. No second object client: a bucket
reached by a path this module does not otherwise use is a bucket erasure
will one day miss.

`meeting` resolves to the recording with that id **and** every recording
whose `metadata["meeting_id"]` matches — a recording IS the meeting where the
host has no separate entity (`transcript_schema` already numbers transcripts
by `meeting_id = recording.id`), and a host that does have one links through
that key. A `workspace_id` on the request narrows it, since a meeting id is
unique inside a workspace and not across the platform.

`@on_action("gdpr.owner.probe")` answers `gdpr.owner.alive {owner,
subject_types}` **from that same module**. That co-location is the whole
value of the answer: it proves the erasure path is being consumed, not that a
container was deployed. It is what `gdpr.W006` and `GET
/gdpr/api/v1/owners/health` read — the fleet finding that seven declared
owners were silent becomes a warning at boot instead of an erasure that times
out thirty days later.

Erasure is idempotent by construction: a second one for the same subject
removes nothing, raises nothing, and still receipts. Zero counts is an
answer; silence is a timeout.

### Added — settings

| Key | Default | What it decides |
|---|---|---|
| `PURGE_AFTER_DAYS` | `30` | Days a soft-deleted recording is kept before the purge opens an erasure |
| `PURGE_SCHEDULE` | `{"hour": 4, "minute": 20}` | Crontab kwargs for the purge's beat entry |
| `ERASURE_CLIENT` | `…erasure.GDPRErasureClient` | Who is asked to OPEN an erasure. `no_env`, like the other dotted-path seams — a stray value would unhook retention from the receipts path without anything looking wrong |

The default client uses stapel-gdpr's orchestrator in-process when that app
is installed and reports itself **unavailable** when it is not; the purge
then counts the aged rows and logs that nothing was erased, rather than
destroying data outside the receipts path or raising once per row in a
scheduled task. This package still does not depend on stapel-gdpr.

### Host wiring

```python
STAPEL_GDPR = {"DATA_OWNERS": {"recordings": ["account", "workspace", "meeting", "recording"], ...}}
CELERY_BEAT_SCHEDULE = {**get_recordings_beat_schedule(), ...}
```

and a `consume_actions` process for this module — without one, `alive` never
answers and every erasure naming a subject it claims times out.

### Fixed

- `CONFIG.MD` used `settings` in the Source column for the seven `no_env`
  keys. The fleet's manifest vocabulary is `env` / `vault`, so
  `stapel-config-lint` could not parse the file at all and every rule below
  the parse went unchecked. The distinction those rows carry lives where it
  is enforced anyway — in each row's own text and in `conf.no_env`.

## [0.15.0] — 2026-08-21

### Changed — every delegated AI call says who it is for (**requires stapel-agent >= 0.12.0**)

Minor, because of that floor. Read the compatibility note before upgrading.

This package makes no AI call of its own; it delegates all of them to
stapel-agent, which writes one billable ledger row per provider call. Those
rows had no subject, and the reason only becomes visible with both sides in
view: a pipeline stage runs on a queue long after the request that created the
recording, so there is no "current user" to read — and the `llm.*` comm
schemas would have rejected an id anyway, being
`additionalProperties: false`. The result was a product whose entire AI spend
was recorded and belonged to nobody.

stapel-agent 0.12.0 opened optional `user_id` / `workspace_id` on those
schemas. This release fills them, from the only thing that knows: the
`Recording`, which already carries `owner` and `workspace_id`. So there is no
new plumbing for a host to do and no per-call-site argument to remember — the
transcribe and summarize payloads, and the vector layer's embed batches, are
attributed automatically.

Two new public helpers, `stages.identity_payload(recording)` and
`stages.identity_fields(user_id, workspace_id)`, build the block. A host that
adds its own stage delegating to `llm.*` should use them rather than
hand-rolling the dict — a second spelling is how half a ledger goes dark.
Absent ids are omitted rather than sent as null, because the schemas type both
as strings and reject anything else.

`vector.qa.answer_question()` takes a new optional `user_id`, and
`vector.search.search_recordings()` likewise. A question is three billable
calls — the query embedding, the optional rerank, and the answer — and unlike
a pipeline stage it has an obvious subject: a live human is waiting on it. All
of them are now attributed. Both parameters are optional and recorded only;
nothing here gates, entitles or debits on them.

### Compatibility

**Upgrade stapel-agent to >= 0.12.0 first.** The `llm.*` schemas reject
unknown properties, so against an older agent this is not a degraded feature —
it is every transcribe, summarize, embed, rerank and complete call failing
schema validation. The two fields are optional in the agent, so nothing else
about that upgrade is breaking.

A new system check, `stapel_recordings.W009`, says so at startup when it can
see the answer — i.e. in a monolith, where stapel-agent is importable in the
same process. In a split deployment nothing here can read the other side's
version, so the check stays silent rather than guessing, and the floor lives
in this note.

Internal signature changes, listed for anyone who monkeypatches them:
`vector.embedding.embed_texts()` takes a keyword-only `identity`, and the
private `search._vector_arm` / `search._apply_rerank` do too. Keyword-only
deliberately — a positional tail is what a stubbed seam mismatches silently.

### Fixed

`docs/errors.json` regenerated: stapel-core gained
`error.503.mandate_unavailable`, and the committed artifact had drifted behind
it (the contract gate was already red on `main` before this change).

## [0.14.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor lagged behind, so a consumer resolving an older core
regenerated an artifact without `owner` and the drift gate went red — the
field was declared but never required. The floor now matches the artifact
that is committed.

## [0.14.1] — 2026-08-15

### Added — the error catalogs this module owns (ru, es)

This module registers 13 `error.*.recording_*` / `error.*.share_*` keys —
0.14.0 added the whole sharing and media-delivery vocabulary — and shipped
a catalog for none of them. Since stapel-core 0.23.1 a consumer resolves a
key it does not own from the **owner's** catalog, and since 0.22.0 a writer
may only translate keys it owns — so shipping nothing did not leave the gap
open for someone else to fill legally: it made every consumer render the
English literal, and the one that filled it locally was maintaining a
shadow of this module's canon that nothing here would ever update.

`translations/errors.ru.json` + `translations/errors.es.json` (13 keys
each) now ship, with the `translations/.state.json` provenance sidecar, and
the whole `translations/` directory is in the wheel (`package-data`) — a
catalog that reaches only the repository is a catalog no deployment can
read. Languages match what every other stapel library with error keys
promises: en canon in `errors.py`, ru and es as catalogs.

Provenance is recorded, not implied: the curated stapel-translate builtin
corpus carries none of these keys, so every value is a machine translation
(`origin: llm`, the gate's unreviewed counter) reproduced offline from the
table in `tests/test_error_i18n.py`. `tests/test_error_i18n.py` is also the
gate — coverage scoped to ownership, no foreign keys, placeholders
preserved, byte-stable, no drift.

Strings and packaging only: no code, no schema, no migration changed.

## [0.14.0] — 2026-08-14

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

- **UPGRADE NOTE — settings that decide trust are no longer read from the
  environment (`no_env`).** `AppSettings` falls back to
  `os.environ.get(KEY)` for every key not listed, and `STAPEL_RECORDINGS`
  listed none — so a stray environment variable with a very generic name
  could swap the **authorization policy** (`RECORDING_POLICY`), where the
  bytes go (`STORAGE`), which stages run (`PIPELINE_RESOLVER`), what runs at
  the pipeline's subprocess entrance (`NORMALIZER`), the finalize-time
  content gate (`UPLOAD_CONTENT_POLICY`) or the claim that the backend mints
  expiring URLs (`STORAGE_SIGNS_GET_URLS`), plus the two switches added
  above. All ten are now `no_env`: they still resolve from
  `settings.STAPEL_RECORDINGS`, a flat Django setting, or the default — but
  never from the process environment. **A deployment that configured any of
  them by environment variable must move it into settings**; it will
  otherwise silently fall back to the default. Not `no_env` (and unchanged):
  TTLs, size and retry limits, thresholds, `STORAGE_PREFIX` — tuning, whose
  names are specific and whose wrong values degrade rather than swap a trust
  decision.
- **UPGRADE NOTE — `FFMPEG_BIN` / `FFPROBE_BIN` / `FFMPEG_TIMEOUT_S` moved
  from the process environment into settings.** They were module-level
  `os.environ.get` reads in `normalize.py`, which froze them at import (so a
  host could not change them at runtime at all) *and* let any same-named
  environment variable pick argv[0] of a subprocess this module runs over
  user-supplied media. They are now `FFMPEG_BIN`, `FFPROBE_BIN` and
  `FFMPEG_TIMEOUT_SECONDS` (note the rename) in `STAPEL_RECORDINGS`, read at
  call time; the two binaries are `no_env`. **A deployment that pointed
  ffmpeg somewhere with an environment variable must set it in settings** —
  otherwise `ffmpeg`/`ffprobe` are resolved on `PATH` as before.
- New check **W008**: `NORMALIZER` pointing at `passthrough_normalize`
  disables all transcoding (and the duration cap with it), and the seam
  check only ever verified the callable was callable — so turning
  conversion off passed `manage.py check` in silence. It now warns. Stands
  that deliberately run without ffmpeg silence `stapel_recordings.W008`.
- **Booleans are coerced, so `"false"` no longer means True.** `AppSettings`
  hands values through uncoerced, and `bool("false")` is True — on
  `STORAGE_SIGNS_GET_URLS` that turned a host writing the string `"false"`
  into a host *vouching* that its storage signs, which is exactly the
  permanent-URL delivery STORE-01 is about. Boolean settings are now read
  through `conf.flag` / `conf.optional_flag`, which accept
  `1/0 true/false yes/no on/off` in any case and fall back to the **closed**
  answer for anything else.
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
- **UPGRADE NOTE — `GET /recordings?workspace_id=…` now obeys
  `RECORDING_POLICY`.** The workspace branch of the listing built
  `Recording.objects.filter(workspace_id=…)` inline while every other read
  path went through `get_policy().visible_queryset`. Two consequences: the
  listing offered rows that `GET /recordings/<id>` refuses with 404 for the
  same caller, and a host that tightened `RECORDING_POLICY` did not tighten
  this path — the one place the seam exists to control. Membership still
  answers *may you ask about this workspace*; the policy now answers *which
  of its recordings may you read*, which with the default `OwnerOnlyPolicy`
  means **a member now sees only their own recordings in the workspace**.
  Deployments that intend every member to see every recording in a
  workspace say so: `STAPEL_RECORDINGS =
  {"WORKSPACE_LISTING_MEMBERS_SEE_ALL": True}` (also `no_env`), or ship a
  `RECORDING_POLICY` whose `visible_queryset` expresses the real rule —
  which is the better answer, because it keeps the listing and the
  per-object checks the same decision.
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
- **UPGRADE NOTE — a content gate that cannot run now refuses the upload.**
  `read_prefix` has a default on `RecordingStorage`, and that default raises
  `NotImplementedError` — so a host's own backend used to switch
  `UPLOAD_CONTENT_POLICY` off by *inheriting*, writing no code at all:
  finalize logged a warning, fell through, and an `.exe` or HTML polyglot
  landed under an `audio.mp3` key with the recording queued and 200 on the
  wire. Finalize now raises the new `services.UploadContentUncheckable`,
  cleans up object and session like any other rejected upload, and the
  bundled `FinalizeUploadView` answers **503**
  `error.503.recording_upload_unverifiable` — a deployment fault, not a 415
  blaming the file. Custom backends: implement `read_prefix` (both bundled
  backends do), or state the trade-off with `UPLOAD_CONTENT_POLICY = "off"`,
  which is the opt-out that already existed for exactly this case. The safe
  value stays the default.

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
(a meeting app) had already closed its own six recording views the same way.

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
  `postgres.E005` pointed at a model class (a client stand, 2026-07-25).
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
