"""Generic, data-driven pipeline driver.

The pipeline is not a hardcoded chain of consumers — it is an ordered list
of stage names run by one generic driver over the open stage registry
(``stages.py``). This is the flagship extension point: reorder / subset /
insert / replace stages purely through settings, a resolver, or the
runtime ``register_stage`` API — no forking.

Flow (each step outbox-backed, at-least-once, idempotent):

    finalize_upload -> emit recording.uploaded
        -> start_pipeline: emit recording.stage(index=0)
            -> run_stage(0): run stage handler, on success
               emit recording.stage(index=1) ... until the list is
               exhausted -> status=completed, emit recording.completed.

Progress cursor — **names, not positions**. The pipeline may be edited at
runtime (``PIPELINE_RESOLVER``), so a positional index alone cannot be
trusted across deliveries. ``run_stage`` persists, in the same transaction
as each successful stage, the *names* of the completed stages
(``workflow_state.pipeline.completed``) plus the position of the last completion
(``completed_index``). On every delivery it:

- drops the event if ``stage_index <= completed_index`` (a duplicate of an
  already-completed stage — no re-run, **no re-emit** of public events);
- otherwise runs the *first stage in the currently resolved pipeline whose
  name has not completed* (the event index is only a dedup hint). Editing
  the list under a live recording is therefore safe: each named stage runs
  at most once, removed stages are skipped (with a warning when the
  removed stage was pending), inserted stages run, and an empty/exhausted
  list finalizes only when every listed stage has completed. A resolver
  that returns an **empty list** is treated as a misconfiguration → DLQ
  (never a silent ``completed``). Stage names within one pipeline must be
  unique — the completed-set treats a repeated name as already done.

Reuse is keyed on the INPUT, not on the stage's name. A completed stage is
skipped only while the artifact it left behind was computed from the input
the stage would read now: each completion records the stage's
``input_fingerprint`` (:mod:`stapel_recordings.checkpoints`), and a stage
whose fingerprint has moved — or one an app layer declared stale through
:func:`invalidate_from` — re-runs, together with everything downstream of
it. Without that, "already done" meant "an artifact with this name exists",
and a re-run over a changed input handed back the previous answer while
reporting success. A retry over an UNCHANGED input still resumes for free:
same fingerprint, same checkpoint, nothing re-bought.

Run identity — ``run_id`` / ``attempt``. A recording can go through the
pipeline more than once (:func:`reprocess_recording`), and every run costs
real money to whoever hosts it. Each run therefore carries its own
identity: a uuid ``run_id`` minted when the run starts (the first
:func:`start_pipeline`, and again on every reprocess) plus a monotonic
``attempt`` counter, both kept in ``workflow_state["pipeline"]`` next to the
cursor and both carried in the public run events
(``recording.stage_completed`` / ``recording.completed`` /
``recording.failed``). Without it the terminal event of the second run is
byte-identical to the first, so a consumer that meters or bills on it can
only build an idempotency key per *recording* — and every re-run after the
first is free. ``retry_recording`` deliberately does NOT mint a new one: a
DLQ'd run that is resumed is the same run, and finishing it must not be
billed twice. :func:`run_identity` reads the pair back.

Where that state lives — ``Recording.workflow_state``, never
``Recording.metadata`` — is load-bearing (audit REC-01). ``metadata`` is the
client's field; a cursor kept there is a cursor a client PATCH can rewrite,
which means marking stages complete, suppressing the start marker, or
injecting the ``ctx`` the next stage reads. Nothing in this driver reads the
client's field.

``run_stage`` locks the recording (``select_for_update``) for the whole
stage and classifies stage errors into retry vs DLQ. ``error`` is terminal
for deliveries: a DLQ'd recording is only revived through the explicit
:func:`retry_recording` transition, never by a redelivered event.

``needs_payment`` is the third terminal parking spot, and it is a STATUS,
not an error. A stage raising ``StageNeedsPayment`` (a host's affordability
gate, reached through the normalizer seam or a stage of its own) leaves the
recording parked with a ``workflow_state["needs_payment"]`` block and one
public ``recording.needs_payment`` event — no ``recording.failed``, because
nothing failed and a refund consumer has nothing to refund. It is terminal
for deliveries for the DLQ's reason plus one: re-driving it would be the
pipeline spending money the account does not have. The way back is
:func:`resume_after_payment`, which keeps the run identity — the same run
finishes, and it is billed once.

The stage list comes from ``PIPELINE_RESOLVER`` (default: the ``PIPELINE``
setting) — point that seam at a DB/per-workspace source to edit pipelines
at runtime. Resolver failures are parked as retryable (bounded by
``MAX_STAGE_RETRIES``, then DLQ) instead of crash-looping the delivery.
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from . import checkpoints, events
from .conf import recordings_settings
from .models import Recording, RecordingStatus
from .stages import StageAwaiting, StageFatal, StageNeedsPayment, StageRetryable, get_stage

logger = logging.getLogger(__name__)

# ERROR is terminal for event deliveries: after DLQ the only way back into
# the pipeline is the explicit retry_recording() transition below. Without
# this, a broker redelivery could "resurrect" a recording whose
# recording.failed event already reached consumers (refunds, notifications).
#
# NEEDS_PAYMENT is terminal for the same reason and one more: the recording is
# parked on an empty wallet, so a redelivery that re-drove it would be the
# pipeline spending money the account does not have. Its only way out is
# resume_after_payment().
_TERMINAL = {
    RecordingStatus.COMPLETED,
    RecordingStatus.ERROR,
    RecordingStatus.NEEDS_PAYMENT,
    RecordingStatus.DELETED,
}


# ─── Resolver seam ─────────────────────────────────────────────────────


def default_pipeline_resolver(recording) -> list[str]:
    """Default resolver: the ``PIPELINE`` setting. A host swaps this for a
    DB/per-recording/per-workspace source via ``PIPELINE_RESOLVER``."""
    return list(recordings_settings.PIPELINE)


def resolve_pipeline(recording) -> list[str]:
    resolver = recordings_settings.PIPELINE_RESOLVER  # import_strings resolves it
    return list(resolver(recording))


# ─── Entry ─────────────────────────────────────────────────────────────


def start_pipeline(recording_id: str) -> None:
    """Kick the pipeline for a freshly uploaded recording. Idempotent under
    concurrent duplicate deliveries: the row is locked and a
    ``workflow_state.pipeline`` marker is written in the same transaction as the
    ``recording.stage(0)`` emit, so a second delivery (or a concurrent one —
    it serializes on the lock) sees the marker and skips.

    The marker carries this run's identity (``run_id`` / ``attempt``), minted
    here once — a duplicate delivery skips on the marker and therefore never
    re-mints, so the whole first run reports one run_id."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("start_pipeline: recording %s not found", recording_id)
            return
        if recording.status in _TERMINAL:
            return
        if (recording.workflow_state or {}).get("pipeline"):
            return  # already started
        state = dict(recording.workflow_state or {})
        state["pipeline"] = {
            "started_at": timezone.now().isoformat(),
            "run_id": str(uuid.uuid4()),
            "attempt": 1,
        }
        recording.workflow_state = state
        recording.save(update_fields=["workflow_state", "updated_at"])
        events.emit_stage(recording.id, 0)


# ─── Checkpoint validity ───────────────────────────────────────────────


def stage_input_fingerprint(recording, stage_name: str, ctx=None):
    """The fingerprint *stage_name* declares for the input it would read now.

    ``None`` when the stage declares none, cannot be resolved, or raises —
    a stage that cannot say what its input is has not said the input
    changed, and an exception in a fingerprint must never be the reason a
    pipeline stops. Read it to explain a re-run; compare it yourself only
    through :func:`stapel_recordings.checkpoints.is_stale`, which knows
    what an unknown means.
    """
    try:
        stage = get_stage(stage_name)
    except (KeyError, TypeError, ImportError):
        return None
    if ctx is None:
        ctx = _pipeline_meta(recording).get("ctx") or {}
    try:
        value = stage.input_fingerprint(recording, ctx)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "pipeline: stage %s could not fingerprint its input for %s",
            stage_name, recording.id, exc_info=True,
        )
        return None
    return str(value) if value else None


def stage_needs_recompute(recording, stage_name: str, ctx=None) -> bool:
    """Is *stage_name*'s persisted result unusable for the current input?"""
    return checkpoints.is_stale(
        recording, stage_name, stage_input_fingerprint(recording, stage_name, ctx)
    )


def _next_pending(recording, pipeline: list[str]) -> tuple[int | None, bool]:
    """``(index of the stage to run, is it a recomputation)``.

    The first listed stage whose name has not completed — or, before that,
    the first COMPLETED stage whose checkpoint no longer matches its input.
    A stage that has to recompute takes the whole tail of the pipeline with
    it: its output is every later stage's input, so their artifacts were
    computed from something that is about to stop existing. They are
    dropped from the cursor here and re-run in order, each still consulting
    its own checkpoint when its turn comes — so a stage whose input turns
    out unchanged after all (a diarizer fed the same audio) is free to
    say so.

    The fingerprints themselves are deliberately KEPT while the cursor is
    dropped: they describe the artifacts still sitting in storage, and a
    stage two steps down needs its old fingerprint intact to notice that
    its input has moved.
    """
    completed = set(_completed_stages(recording))
    for index, name in enumerate(pipeline):
        if name not in completed:
            return index, False
        if stage_needs_recompute(recording, name):
            logger.info(
                "pipeline: recording %s stage %s was computed from a different "
                "input — recomputing it and every stage after it",
                recording.id, name,
            )
            _drop_cursor_from(recording, pipeline, index)
            return index, True
    return None, False


def _drop_cursor_from(recording, pipeline: list[str], index: int) -> None:
    """Un-complete *pipeline[index:]* (in memory). Fingerprints survive."""
    tail = set(pipeline[index:])
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    kept = [name for name in (pl.get("completed") or []) if name not in tail]
    pl["completed"] = kept
    pl["completed_index"] = index - 1
    pl.pop("ctx", None)
    state["pipeline"] = pl
    recording.workflow_state = state


def invalidate_from(recording_id: str, stage: str) -> bool:
    """Declare *stage* and every stage after it stale. Returns True if applied.

    The explicit half of :mod:`stapel_recordings.checkpoints` — for what a
    fingerprint cannot see by itself. Two live reasons: a parameter that
    does not live on the row changed (a plan that trimmed the audio was
    paid for, so the source is no longer the same input), and an artifact
    produced before fingerprints were recorded at all, whose provenance is
    unknown and therefore trusted.

    Declares only. It does NOT requeue, does not touch status, does not
    delete a single artifact: the host stays in charge of the transition
    (:func:`reprocess_recording` / :func:`retry_recording`), and the user
    keeps seeing the previous result until the new one replaces it. Pair it
    with the transition — declare first, requeue second — so a requeue that
    lands before the declaration cannot run the pipeline over checkpoints
    it was supposed to distrust.

    Unknown stage names are accepted and recorded: a stage may be absent
    from the currently resolved pipeline and present in the next delivery's.
    """
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("invalidate_from: recording %s not found", recording_id)
            return False
        try:
            pipeline = resolve_pipeline(recording)
        except Exception:
            logger.exception("invalidate_from: pipeline resolver failed for %s", recording_id)
            pipeline = []
        if stage in pipeline:
            doomed = pipeline[pipeline.index(stage):]
        else:
            doomed = [stage]
        checkpoints.declare_invalid(recording, doomed)
        recording.save(update_fields=["workflow_state", "updated_at"])
    logger.info(
        "pipeline: recording %s — stages %s declared stale, they will recompute",
        recording_id, ", ".join(doomed),
    )
    return True


# ─── Driver ────────────────────────────────────────────────────────────


def run_stage(recording_id: str, stage_index: int) -> None:
    """Run the next pending stage of the recording's resolved pipeline and,
    on success, emit the next stage event (or finalize). All within one
    locked, atomic unit.

    *stage_index* (from the event payload) is a dedup hint only: an index
    at or below the persisted ``completed_index`` is a duplicate of an
    already-completed stage and is dropped without re-running or
    re-emitting anything. Which stage actually runs is decided **by name**
    against the currently resolved pipeline (first listed stage whose name
    has not completed), so a pipeline edited under a live recording never
    causes a wrong stage to run at a stale position."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("run_stage: recording %s not found", recording_id)
            return

        if recording.status in _TERMINAL:
            return

        try:
            pipeline = resolve_pipeline(recording)
        except Exception as exc:
            # A broken resolver (missing per-workspace row, DB glitch) must
            # not crash the delivery into an unbounded outbox retry loop:
            # park it as a retryable stage failure — bounded by
            # MAX_STAGE_RETRIES, then DLQ.
            logger.exception("run_stage: pipeline resolver failed for %s", recording_id)
            _handle_retry(recording, "<pipeline_resolver>", "pipeline_resolver_error", str(exc))
            return

        if not pipeline:
            # An empty pipeline for a recording that still has work queued is
            # a misconfiguration; completing it silently would publish a lie.
            _dlq(recording, stage="<pipeline>", reason="empty_pipeline")
            return

        next_index, recompute = _next_pending(recording, pipeline)
        if not recompute and stage_index <= _completed_index(recording):
            return  # duplicate delivery of an already-completed stage — no-op
        if next_index is None:
            _finalize(recording)  # every listed stage has completed
            return
        stage_name = pipeline[next_index]

        started = (recording.workflow_state or {}).get("pipeline", {}).get("stage")
        if (
            started
            and started != stage_name
            and started not in pipeline
            and started not in set(_completed_stages(recording))
        ):
            logger.warning(
                "pipeline: recording %s pending stage %r was removed from the "
                "resolved pipeline — skipping to %r",
                recording.id, started, stage_name,
            )

        try:
            stage = get_stage(stage_name)
        except (KeyError, TypeError, ImportError) as exc:
            _dlq(recording, stage=stage_name, reason=f"unresolvable_stage: {exc}")
            return

        _set_current(recording, next_index, stage_name)
        _ensure_run(recording)  # a run started before run ids existed gets one here
        if stage.status:
            recording.status = stage.status
        recording.save(update_fields=["status", "workflow_state", "updated_at"])

        ctx = (recording.workflow_state or {}).get("pipeline", {}).get("ctx") or {}
        # Read BEFORE the stage consumes its input — see _mark_completed.
        fingerprint = stage_input_fingerprint(recording, stage_name, ctx)
        try:
            new_ctx = stage.run(recording, ctx) or {}
        except StageAwaiting as exc:
            # Work was SUBMITTED, not failed: the stage doesn't complete,
            # but no attempt is spent either. Remember task_id so resume
            # (and only resume) can complete this exact stage; the
            # recording's status stays stage-specific — the user sees
            # "transcribing", not a blank.
            _set_awaiting(
                recording, next_index, stage_name, exc.task_id, exc.kind,
                fingerprint=fingerprint,
            )
            recording.save(update_fields=["workflow_state", "updated_at"])
            logger.info(
                "pipeline: recording %s stage %s awaiting task %s (%s)",
                recording.id, stage_name, exc.task_id, exc.kind,
            )
            return
        except StageNeedsPayment as exc:
            _park_for_payment(
                recording, stage=stage_name, reason=exc.reason, detail=exc.detail,
                extra=exc.extra,
            )
            return
        except StageFatal as exc:
            _dlq(recording, stage=stage_name, reason=exc.reason, detail=exc.detail)
            return
        except StageRetryable as exc:
            _handle_retry(recording, stage_name, exc.reason, exc.detail)
            return
        except Exception as exc:  # unexpected — reconcile can re-drive
            logger.exception("run_stage: unexpected error in %s for %s", stage_name, recording_id)
            _handle_retry(recording, stage_name, "unexpected", str(exc))
            return

        recording.retry_count = 0
        _reset_reconcile_redrives(recording)
        _clear_last_error(recording)
        _store_ctx(recording, new_ctx)
        # Persist "stage N completed" in the same transaction as the success
        # events: a redelivery of a completed stage is now distinguishable
        # from crash recovery (crash before this commit re-runs the stage;
        # after it, the duplicate is dropped by the completed_index guard
        # above and public events are never re-emitted with fresh event_ids).
        _mark_completed(recording, next_index, stage_name, fingerprint)
        recording.save(update_fields=["retry_count", "workflow_state", "updated_at"])

        events.emit_stage_completed(recording, stage_name, next_index, **run_identity(recording))
        events.emit_stage(recording.id, next_index + 1)


def resume_stage(recording_id: str, task_id: str, result) -> None:
    """Complete a stage that was awaiting task *task_id*, using its result.

    Called by the ``task.completed`` subscriber. Mirrors the success tail of
    :func:`run_stage`: same lock, same completion marker, same events — the
    only difference is the work happened elsewhere.

    GUARDS AGAINST A FOREIGN RESULT. Delivery is at-least-once and the task
    could have been restarted, so we check ``task_id`` against the one we
    recorded. A mismatch means this is a response to a cancelled or stale
    attempt, which must not be applied to the current stage.
    """
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("resume_stage: recording %s not found", recording_id)
            return
        if recording.status in _TERMINAL:
            return

        awaiting = _pipeline_meta(recording).get("awaiting") or {}
        if awaiting.get("task_id") != str(task_id):
            logger.info(
                "resume_stage: recording %s is awaiting %r, ignoring result from %r",
                recording_id, awaiting.get("task_id"), task_id,
            )
            return

        stage_name = awaiting.get("stage") or ""
        stage_index = int(awaiting.get("index", 0))
        fingerprint = awaiting.get("fingerprint")
        try:
            stage = get_stage(stage_name)
        except (KeyError, TypeError, ImportError) as exc:
            _dlq(recording, stage=stage_name, reason=f"unresolvable_stage: {exc}")
            return

        ctx = _pipeline_meta(recording).get("ctx") or {}
        try:
            new_ctx = stage.resume(recording, ctx, result) or {}
        except StageNeedsPayment as exc:
            _clear_awaiting(recording)
            _park_for_payment(
                recording, stage=stage_name, reason=exc.reason, detail=exc.detail,
                extra=exc.extra,
            )
            return
        except StageFatal as exc:
            _dlq(recording, stage=stage_name, reason=exc.reason, detail=exc.detail)
            return
        except StageRetryable as exc:
            _clear_awaiting(recording)
            _handle_retry(recording, stage_name, exc.reason, exc.detail)
            return
        except Exception as exc:  # unexpected — reconcile can re-drive
            logger.exception("resume_stage: unexpected error in %s for %s", stage_name, recording_id)
            _clear_awaiting(recording)
            _handle_retry(recording, stage_name, "unexpected", str(exc))
            return

        recording.retry_count = 0
        _reset_reconcile_redrives(recording)
        _clear_awaiting(recording)
        _clear_last_error(recording)
        _store_ctx(recording, new_ctx)
        _mark_completed(recording, stage_index, stage_name, fingerprint)
        _ensure_run(recording)
        recording.save(update_fields=["retry_count", "workflow_state", "updated_at"])

        events.emit_stage_completed(recording, stage_name, stage_index, **run_identity(recording))
        events.emit_stage(recording.id, stage_index + 1)


def fail_stage(recording_id: str, task_id: str, error: str) -> None:
    """The task a stage was awaiting has failed for good.

    The Task primitive has already exhausted its own ``max_attempts``, so
    this is DLQ, not a retry: retrying again would just double the wait the
    user already sat through.
    """
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            return
        if recording.status in _TERMINAL:
            return
        awaiting = _pipeline_meta(recording).get("awaiting") or {}
        if awaiting.get("task_id") != str(task_id):
            return
        stage_name = awaiting.get("stage") or "<awaiting>"
        _clear_awaiting(recording)
        recording.save(update_fields=["workflow_state", "updated_at"])
        _dlq(recording, stage=stage_name, reason="task_failed", detail=error)


def retry_recording(recording_id: str) -> bool:
    """Explicit ``error -> queued`` transition: re-enter the pipeline after a
    DLQ. Returns True if the recording was requeued.

    This is the *only* way back from ``error`` — event redeliveries never
    resurrect a DLQ'd recording. Completed stages are kept (the cursor is
    the persisted completed-set), so the retry resumes at the first
    not-yet-completed stage of the currently resolved pipeline. Expose this
    from an app-layer endpoint/admin action as needed.

    The run identity is deliberately KEPT: resuming a failed run is the same
    run, and a consumer that meters on ``recording.completed`` must charge
    once for a run that needed a retry to finish, not twice."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("retry_recording: recording %s not found", recording_id)
            return False
        if recording.status != RecordingStatus.ERROR:
            return False
        refusal = retry_refusal(recording)
        if refusal:
            logger.info(
                "retry_recording: recording %s not retried (%s)", recording_id, refusal
            )
            return False
        recording.status = RecordingStatus.QUEUED
        recording.retry_count = 0
        recording.save(update_fields=["status", "retry_count", "updated_at"])
        events.emit_stage(recording.id, _completed_index(recording) + 1)
    logger.info("pipeline: recording %s requeued for retry", recording_id)
    return True


#: :func:`retry_refusal` answers: the upload never reached storage.
RETRY_REFUSED_UPLOAD_INCOMPLETE = "upload_incomplete"
#: :func:`retry_refusal` answers: the pipeline failed for want of a source
#: object that is still not there.
RETRY_REFUSED_NO_SOURCE = "no_source"


def retry_refusal(recording: Recording) -> str | None:
    """Why a retry of this errored recording cannot succeed, or None.

    A retry re-runs the pipeline over the stored source. When the failure is
    that there IS no source, re-running changes nothing: it fails
    ``convert`` with ``missing_raw_storage_key`` again, once per click, and
    the person who pressed the button learns only that it broke twice.
    Only a finished upload (or a restored source) can help, so those cases
    are refused here and the host tells the person to upload.

    - the last error is an upload-stage failure (``upload_abandoned`` and
      friends): no object was ever finalized;
    - the last error is ``missing_raw_storage_key`` and the row still has
      no source pointer (a restore that put one back makes it retryable).
    """
    state = recording.workflow_state or {}
    last = state.get("last_error") if isinstance(state, dict) else None
    if not isinstance(last, dict):
        return None
    has_source = bool(recording.file_storage_key or recording.normalized_storage_key)
    if last.get("stage") == "upload" and not has_source:
        return RETRY_REFUSED_UPLOAD_INCOMPLETE
    if last.get("reason") == "missing_raw_storage_key" and not has_source:
        return RETRY_REFUSED_NO_SOURCE
    return None


def resume_after_payment(recording_id: str) -> bool:
    """Explicit ``needs_payment -> queued`` transition: the account paid, so
    put the recording back in the queue. Returns True if it was requeued.

    The mirror of :func:`retry_recording`, and separate from it on purpose
    rather than an argument to it. The two transitions answer to different
    authorities: a retry is the host saying "try the same thing again", and
    whoever may click it may click it at any time, because the previous
    attempt cost nothing that succeeded. This one is the host saying "the
    balance is now there" — an assertion only the code that watched the money
    move is allowed to make. Folding it into ``retry_recording`` as a flag
    would put both behind one call site, and the first caller that passed the
    flag from a request parameter would have let a client requeue work it
    never paid for. Separate functions, so an app layer can expose the retry
    button to users and keep this one to its payment webhook.

    Everything else mirrors a retry: the completed-stage cursor is kept, so
    the pipeline resumes at the first stage whose name has not completed, and
    the ``run_id`` is KEPT — the same run is being finished, and a consumer
    metering ``recording.completed`` must charge for it once, not once per
    top-up. The ``needs_payment`` block is cleared (moved to
    ``paid_for``, so the reason stays readable in diagnostics without still
    posing as current state), and ``retry_count`` is reset because the stage
    never actually failed.

    Allowed only from ``needs_payment``. Every other status returns False
    without side effects — including ``error``, which is
    :func:`retry_recording`'s.
    """
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("resume_after_payment: recording %s not found", recording_id)
            return False
        if recording.status != RecordingStatus.NEEDS_PAYMENT:
            return False
        state = dict(recording.workflow_state or {})
        parked = state.pop("needs_payment", None)
        if parked is not None:
            state["paid_for"] = {**parked, "paid_at": timezone.now().isoformat()}
        recording.workflow_state = state
        recording.status = RecordingStatus.QUEUED
        recording.retry_count = 0
        recording.save(
            update_fields=["status", "retry_count", "workflow_state", "updated_at"]
        )
        events.emit_stage(recording.id, _completed_index(recording) + 1)
    logger.info("pipeline: recording %s requeued after payment", recording_id)
    return True


def reprocess_recording(recording_id: str) -> bool:
    """Explicit ``completed -> queued`` transition: re-run the WHOLE pipeline
    from stage 0 for an already-finished recording. Returns True if requeued.

    Distinct from :func:`retry_recording` (``error -> queued``, which *resumes*
    at the first not-yet-completed stage keeping the completed-set): reprocess
    is for a recording that finished successfully but a host wants re-run
    (e.g. after changing the pipeline, the transcription provider, or a
    stage's config). It
    **clears the pipeline progress cursor** (``completed`` / ``completed_index``
    / carried ``ctx``) so every stage runs again from the top, then re-emits
    ``recording.stage(0)``.

    A reprocess is a NEW RUN, so it mints a new ``run_id`` and increments
    ``attempt``. Everything the run publishes — every
    ``recording.stage_completed`` and the terminal ``recording.completed`` —
    carries them. This is what makes the second run distinguishable from the
    first for a consumer that meters or bills post-hoc: keyed on the
    recording alone, the terminal events of run 1 and run 2 are identical
    and the re-run is free.

    Allowed only from ``completed``. Every other status — ``created`` /
    ``uploading`` / ``queued`` / any in-flight processing status / ``error``
    (use ``retry_recording``) / ``deleted`` — is a forbidden transition and
    returns False without side effects. Stages remain idempotent and
    self-guard on persisted artifacts, so a host that needs derived data
    (segments, transcript, summary) regenerated clears the relevant keys as
    part of its reprocess flow — the module never destroys transcript data on
    its own.

    ORDER MATTERS (audit REC-03). Everything that can refuse — the row
    exists, the status allows it — is checked before anything is written,
    and the artifacts of the finished run are recorded in
    ``workflow_state["previous_run"]`` before the pipeline is requeued. A
    reprocess flow that deletes first and validates afterwards turns a
    refused transition into permanent data loss; a host wiring its own
    cleanup should delete only after this returns True, and only after its
    retention window on the snapshot has passed."""
    with transaction.atomic():
        try:
            recording = Recording.objects.select_for_update().get(pk=recording_id)
        except Recording.DoesNotExist:
            logger.warning("reprocess_recording: recording %s not found", recording_id)
            return False
        if recording.status != RecordingStatus.COMPLETED:
            return False
        state = dict(recording.workflow_state or {})
        pl = dict(state.get("pipeline") or {})
        pl.pop("completed", None)
        pl.pop("completed_index", None)
        pl.pop("ctx", None)
        pl.pop("stage", None)
        pl.pop("stage_index", None)
        pl["reprocess_at"] = timezone.now().isoformat()
        # A new run, and it must say so: new run_id, next attempt.
        pl["run_id"] = str(uuid.uuid4())
        pl["attempt"] = int(pl.get("attempt") or 1) + 1
        pl["started_at"] = timezone.now().isoformat()
        # Note what the finished run produced BEFORE re-running it. The
        # module never deletes transcript artifacts (see the docstring), and
        # this snapshot is what makes that promise usable: after a reprocess
        # overwrites the pointers, the previous artifact is still named
        # somewhere the host can find it for its retention window.
        state["previous_run"] = {
            "transcript_storage_key": recording.transcript_storage_key,
            "normalized_storage_key": recording.normalized_storage_key,
            "segments_count": recording.segments_count,
            "at": timezone.now().isoformat(),
        }
        state["pipeline"] = pl
        recording.workflow_state = state
        recording.status = RecordingStatus.QUEUED
        recording.retry_count = 0
        recording.save(update_fields=["status", "retry_count", "workflow_state", "updated_at"])
        events.emit_stage(recording.id, 0)
    logger.info("pipeline: recording %s requeued for reprocess", recording_id)
    return True


# ─── terminal transitions ──────────────────────────────────────────────


def _finalize(recording: Recording) -> None:
    if recording.status == RecordingStatus.COMPLETED:
        return
    recording.status = RecordingStatus.COMPLETED
    # The terminal event is the one a host bills on, so it must never go out
    # without run identity — mint it here too if the run predates run ids
    # (which is why workflow_state is in update_fields).
    _ensure_run(recording)
    # Save + terminal emit as one unit. run_stage() already holds the outer
    # transaction.atomic(); this nests as a savepoint (joins the outer txn,
    # events still leave only on outer commit) and keeps the pair atomic even
    # if a future caller invokes _finalize() outside run_stage.
    with mutate_and_emit():
        recording.save(update_fields=["status", "workflow_state", "updated_at"])
        events.emit_completed(recording, **run_identity(recording))
    logger.info("pipeline: recording %s completed", recording.id)


def _handle_retry(recording: Recording, stage_name: str, reason: str, detail=None) -> None:
    recording.retry_count = (recording.retry_count or 0) + 1
    _set_last_error(recording, stage_name, reason, detail)
    if recording.retry_count > int(recordings_settings.MAX_STAGE_RETRIES):
        _dlq(recording, stage=stage_name, reason=f"retries_exhausted: {reason}", already_errored=True)
        return
    # Park it: reconcile re-emits recording.stage(current) after the stuck
    # threshold. Avoids a tight in-process retry loop.
    recording.status = RecordingStatus.QUEUED
    recording.save(update_fields=["retry_count", "status", "workflow_state", "updated_at"])
    logger.info("pipeline: %s stage %s retryable (%s), attempt %d — parked",
                recording.id, stage_name, reason, recording.retry_count)


def note_reconcile_redrive(recording: Recording) -> bool:
    """Count one watchdog re-drive. False means "do not re-drive it again".

    THE HOLE THIS CLOSES. Every other ladder in this pipeline declares a
    ceiling — ``MAX_STAGE_RETRIES`` for the stage, ``max_attempts`` for
    the task, and :func:`stages.transcribe_attempt_ceiling` proves the
    two do not multiply. The watchdog declared none. It re-emits
    ``recording.stage`` for anything non-terminal that has not moved in
    ``STUCK_THRESHOLD_SECONDS``, and a re-drive that lands on
    ``StageAwaiting`` returns without touching ``retry_count`` — so a
    recording the pipeline cannot finish is re-driven every 17.5 minutes
    for as long as it exists. A client stand ran that way for a
    fortnight against an empty download allowlist (2026-08-20). Nothing
    charged money in that particular case, because the agent refused
    before the provider; that was luck, not design — the agent's
    checkpoint expires in 7 days and the watchdog does not.

    So: a counter on the recording, a declared cap, and a DLQ when it is
    spent. Reset whenever the pipeline actually moves (see
    :func:`_reset_reconcile_redrives`), because what the cap must bound
    is "re-driven and got nowhere", not "took a long time".
    """
    cap = int(recordings_settings.RECONCILE_MAX_REDRIVES)
    state = recording.workflow_state or {}
    pipeline_state = dict(state.get("pipeline") or {})
    used = int(pipeline_state.get("reconcile_redrives") or 0)

    if cap and used >= cap:
        stage_name = str(pipeline_state.get("stage") or "") or "pipeline"
        _dlq(
            recording,
            stage=stage_name,
            reason="reconcile_exhausted",
            detail=(
                f"the watchdog re-drove this recording {used} time(s) "
                f"(cap {cap}) and it never moved on"
            ),
        )
        return False

    pipeline_state["reconcile_redrives"] = used + 1
    recording.workflow_state = {**state, "pipeline": pipeline_state}
    recording.save(update_fields=["workflow_state", "updated_at"])
    return True


def _reset_reconcile_redrives(recording: Recording) -> None:
    """Forget the watchdog's count — the pipeline moved on its own.

    Mutates in place; the caller is mid-transaction and saves.
    """
    state = recording.workflow_state or {}
    pipeline_state = state.get("pipeline") or {}
    if pipeline_state.get("reconcile_redrives"):
        recording.workflow_state = {
            **state,
            "pipeline": {**pipeline_state, "reconcile_redrives": 0},
        }


def _dlq(recording: Recording, *, stage: str, reason: str, detail=None, already_errored=False) -> None:
    recording.status = RecordingStatus.ERROR
    if not already_errored:
        _set_last_error(recording, stage, reason, detail)
    _ensure_run(recording)  # a refund consumer needs to know WHICH run failed
    # Save + terminal DLQ emit as one unit. run_stage() already holds the outer
    # transaction.atomic(); this nests as a savepoint (joins the outer txn,
    # events still leave only on outer commit) and keeps the pair atomic even
    # if a future caller invokes _dlq() outside run_stage.
    with mutate_and_emit():
        recording.save(update_fields=["status", "workflow_state", "updated_at"])
        events.emit_failed(
            recording,
            stage=stage,
            reason=reason,
            user_retryable=True,
            **run_identity(recording),
        )
    logger.warning("pipeline: recording %s DLQ at stage %s (%s)", recording.id, stage, reason)


def _park_for_payment(
    recording: Recording, *, stage: str, reason: str, detail=None, extra=None
) -> None:
    """Park a recording the account cannot pay for. Not a failure.

    The block goes to ``workflow_state["needs_payment"]``, deliberately NOT
    to ``last_error``: that field is what a UI renders as "something broke",
    and an empty wallet is not a breakage. Keeping them apart also means a
    genuine error recorded before the park is still readable next to it.

    ``extra`` — from ``StageNeedsPayment.extra`` / ultimately
    ``normalize.NormalizePaymentRequired.extra`` — lets the park name the
    number it was refused on (``estimated_credits``,
    ``original_duration_seconds``) instead of only a reason code. Optional;
    an absent or falsy value is written as ``{}``.

    The run identity is minted if missing and otherwise untouched — the run
    is not over, it is waiting, and the same run resumes after payment, so a
    metering consumer must keep seeing one run_id.
    """
    recording.status = RecordingStatus.NEEDS_PAYMENT
    state = dict(recording.workflow_state or {})
    state["needs_payment"] = {
        "stage": stage,
        "reason": reason,
        "detail": (str(detail)[:500] if detail else None),
        "extra": dict(extra) if extra else {},
        "at": timezone.now().isoformat(),
    }
    recording.workflow_state = state
    _ensure_run(recording)
    # Save + the terminal-ish emit as one unit, like _dlq: run_stage holds the
    # outer transaction and this nests as a savepoint, so the status and the
    # event a host bills/notifies on can never disagree.
    with mutate_and_emit():
        recording.save(update_fields=["status", "workflow_state", "updated_at"])
        events.emit_needs_payment(
            recording,
            stage=stage,
            reason=reason,
            **run_identity(recording),
        )
    logger.info(
        "pipeline: recording %s parked at stage %s — needs payment (%s)",
        recording.id, stage, reason,
    )


# ─── workflow-state helpers ──────────────────────────────────────────────────


def _pipeline_meta(recording: Recording) -> dict:
    return (recording.workflow_state or {}).get("pipeline") or {}


def run_identity(recording: Recording) -> dict:
    """This recording's current pipeline run as ``{"run_id", "attempt"}``.

    The pair every public run event carries. Read it to correlate a
    ``recording.completed`` / ``recording.failed`` back to the run that
    produced it — an invoice line, a credit hold, an audit trail — instead of
    reaching into ``workflow_state`` and re-deriving the key names.

    Read-only: it never mints. A recording that has not entered the pipeline
    (or whose run predates run ids and has not moved since) answers
    ``{"run_id": None, "attempt": 1}``; the driver mints on its next write.
    """
    meta = _pipeline_meta(recording)
    run_id = meta.get("run_id")
    return {
        "run_id": str(run_id) if run_id else None,
        "attempt": int(meta.get("attempt") or 1),
    }


def _ensure_run(recording: Recording) -> dict:
    """Mint run identity onto *recording* if this run has none, in memory.

    Backfill for runs that started before run ids existed: they are mid-flight
    with a ``pipeline`` marker but no ``run_id``, and their terminal event
    still has to be billable. The caller is inside the stage's locked
    transaction and must include ``workflow_state`` in its ``update_fields``.
    """
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    if not pl.get("run_id"):
        pl["run_id"] = str(uuid.uuid4())
        pl["attempt"] = int(pl.get("attempt") or 1)
        state["pipeline"] = pl
        recording.workflow_state = state
    return run_identity(recording)


def _completed_index(recording: Recording) -> int:
    """Pipeline position of the last completed stage (-1 = none). Used as a
    cheap duplicate-delivery guard; the completed *names* are authoritative
    for what still has to run."""
    return int(_pipeline_meta(recording).get("completed_index", -1))


def _completed_stages(recording: Recording) -> list[str]:
    return list(_pipeline_meta(recording).get("completed") or [])


def _mark_completed(
    recording: Recording, stage_index: int, stage_name: str, fingerprint=None
) -> None:
    """Record the completion AND what produced it.

    *fingerprint* is the stage's input fingerprint as read BEFORE it ran,
    not after: a stage is entitled to consume its input (the convert stage
    purges the container it converted), and a fingerprint taken afterwards
    would describe an input nobody will ever present again — which reads as
    "changed" on the next delivery and re-runs the stage for ever.
    """
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    done = list(pl.get("completed") or [])
    if stage_name not in done:
        done.append(stage_name)
    pl["completed"] = done
    pl["completed_index"] = stage_index
    state["pipeline"] = pl
    recording.workflow_state = state
    checkpoints.record_fingerprint(recording, stage_name, fingerprint)
    # The declaration asked for exactly this re-run, and it has happened.
    checkpoints.clear_invalidation(recording, stage_name)


def _set_current(recording: Recording, stage_index: int, stage_name: str) -> None:
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    pl["stage_index"] = stage_index
    pl["stage"] = stage_name
    pl["updated_at"] = timezone.now().isoformat()
    state["pipeline"] = pl
    recording.workflow_state = state


def _set_awaiting(
    recording: Recording,
    stage_index: int,
    stage_name: str,
    task_id: str,
    kind: str,
    fingerprint=None,
) -> None:
    """Remember which stage is awaiting which task.

    The stage NAME is stored alongside the index, for the same reason the
    completed-stages cursor tracks names: the stage list can change under a
    live recording, and an index alone can't be trusted.
    """
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    pl["awaiting"] = {
        "task_id": str(task_id),
        "kind": kind,
        "stage": stage_name,
        "index": stage_index,
        "since": timezone.now().isoformat(),
        # The input this submission was made FOR, read before the stage ran.
        # resume() completes the stage minutes or hours later, by which time
        # the input may no longer be readable the same way; the checkpoint
        # must still record what the result came from.
        "fingerprint": str(fingerprint) if fingerprint else None,
    }
    pl["stage_index"] = stage_index
    pl["stage"] = stage_name
    pl["updated_at"] = timezone.now().isoformat()
    state["pipeline"] = pl
    recording.workflow_state = state


def _clear_awaiting(recording: Recording) -> None:
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    pl.pop("awaiting", None)
    state["pipeline"] = pl
    recording.workflow_state = state


def _store_ctx(recording: Recording, ctx: dict) -> None:
    state = dict(recording.workflow_state or {})
    pl = dict(state.get("pipeline") or {})
    pl["ctx"] = ctx
    state["pipeline"] = pl
    recording.workflow_state = state


def _set_last_error(recording: Recording, stage: str, reason: str, detail=None) -> None:
    state = dict(recording.workflow_state or {})
    state["last_error"] = {
        "stage": stage,
        "reason": reason,
        "detail": (str(detail)[:500] if detail else None),
        "at": timezone.now().isoformat(),
    }
    recording.workflow_state = state


def _clear_last_error(recording: Recording) -> None:
    """Clear the error marker once the pipeline is moving again.

    ``last_error`` is set on stage failure and used to never clear: not on a
    successful retry, not on manual requeue, not on completion. A recording
    could reach ``completed`` while still carrying a long-resolved failure
    reason, which then misled decisions made from that field.

    The reason isn't discarded, it moves to ``recovered_error``: kept for
    diagnostics but no longer posing as current state. Cleared on EVERY
    successfully completed stage, not just at the end, so a partially
    recovered recording also tells the truth.
    """
    state = dict(recording.workflow_state or {})
    previous = state.pop("last_error", None)
    if previous is None:
        return
    state["recovered_error"] = {**previous, "recovered_at": timezone.now().isoformat()}
    recording.workflow_state = state


__all__ = [
    "default_pipeline_resolver",
    "resolve_pipeline",
    "start_pipeline",
    "run_stage",
    "retry_recording",
    "retry_refusal",
    "RETRY_REFUSED_UPLOAD_INCOMPLETE",
    "RETRY_REFUSED_NO_SOURCE",
    "resume_after_payment",
    "reprocess_recording",
    "invalidate_from",
    "stage_input_fingerprint",
    "stage_needs_recompute",
    "run_identity",
]
