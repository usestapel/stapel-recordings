"""Action subscriptions of stapel-recordings.

Handlers are idempotent (delivery is at-least-once — outbox retries, broker
redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.
"""
import logging

from stapel_core.comm import on_action

from . import events

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has recordings to carry over but there is
    no local user row to point their FKs at yet. Raising is the comm layer's
    retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


@on_action(events.ACTION_UPLOADED)
def handle_uploaded(event):
    """A file landed — start the pipeline driver."""
    from .pipeline import start_pipeline

    recording_id = event.payload.get("recording_id")
    if not recording_id:
        logger.error("recording.uploaded without recording_id: %s", event.event_id)
        return
    start_pipeline(recording_id)


@on_action(events.ACTION_STAGE)
def handle_stage(event):
    """Run one stage of the resolved pipeline (the generic driver step)."""
    from .pipeline import run_stage

    recording_id = event.payload.get("recording_id")
    stage_index = event.payload.get("stage_index")
    if recording_id is None or stage_index is None:
        logger.error("recording.stage missing fields: %s", event.event_id)
        return
    run_stage(recording_id, int(stage_index))


# ─── Erasure (stapel-gdpr's subject-scoped protocol) ───────────────────
#
# Not here. ``apps.ready()`` calls ``stapel_core.gdpr.register_gdpr_owner``,
# which subscribes gdpr.erasure.requested, gdpr.owner.probe and the
# deprecated user.deleted from ONE module in core — the same handlers this
# file used to carry by hand. Co-location still holds: the probe is answered
# by the subscriber that erases. What is ours is ``erasure.erase_subject``.


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away account's recordings over to the survivor.

    stapel-auth absorbs an anonymous guest into an existing account and then
    DELETES the guest row. Every user column this module owns is
    ``SET_NULL``, so without this handler the guest's recordings are not
    erased — they are stranded: still on disk, owned by nobody, invisible to
    the person who made them. Reassignment happens here, in one transaction,
    before that deletion lands.

    Three columns carry a user here and all three move:
    ``Recording.owner``, ``Job.owner`` and ``RecordingShare.created_by``.
    Nothing else in this module names a user — segments, speakers and upload
    sessions hang off the recording, so they follow it by id.

    Two different "unknown id" situations, and conflating them loses data:

    * the guest owns nothing here (never uploaded, or a previous delivery
      already moved it all) — a genuine no-op, returned quietly;
    * the guest owns rows but the survivor has no user row here yet — NOT a
      no-op. :class:`MergeTargetNotReady` is raised so the event is
      redelivered, because returning success would let the outbox mark it
      delivered and strand the recordings for good.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from .models import Job, Recording, RecordingShare

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    with transaction.atomic():
        # Every read, and the decision they feed, happens inside the
        # transaction and before the first write, so the "not yet" path below
        # can never leave half the rows moved.
        try:
            owns_something = (
                Recording.objects.filter(owner_id=from_user_id).exists()
                or Job.objects.filter(owner_id=from_user_id).exists()
                or RecordingShare.objects.filter(created_by_id=from_user_id).exists()
            )
            # The survivor probe is read here, under the same guard, because a
            # malformed *into* id must not escape as a poison pill either.
            survivor_exists = get_user_model().objects.filter(
                pk=into_user_id
            ).exists()
        except (ValidationError, ValueError, TypeError):
            # A key that cannot address a row here names nothing. Saying so
            # quietly beats a redelivery loop over a malformed payload.
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not owns_something:
            # Nothing to carry: the guest never reached this service, or a
            # previous delivery already moved everything. Quiet by design —
            # this is also the at-least-once idempotency path.
            return
        if not survivor_exists:
            # The guest HAS rows but the survivor has no row here yet, so
            # nothing can point a FK at them. Raising is this comm layer's
            # retry signal, so the transfer lands once the survivor's user
            # projection arrives.
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-recordings yet; redeliver "
                f"once its projection has landed"
            )

        # No user-scoped unique constraint exists in this module (the only
        # unique column is RecordingShare.link_token_hash, a secret digest),
        # so a plain reassignment cannot collide.
        moved_recordings = Recording.objects.filter(owner_id=from_user_id).update(
            owner_id=into_user_id
        )
        moved_jobs = Job.objects.filter(owner_id=from_user_id).update(
            owner_id=into_user_id
        )
        moved_shares = RecordingShare.objects.filter(
            created_by_id=from_user_id
        ).update(created_by_id=into_user_id)

    logger.info(
        "user.merged %s -> %s: %s recordings, %s jobs, %s shares carried over",
        from_user_id,
        into_user_id,
        moved_recordings,
        moved_jobs,
        moved_shares,
    )


# ─── Resuming a stage that was awaiting a task ─────────────────────────
#
# Long-running work (transcription, summarization) goes through the Task
# primitive: the stage submits a task and releases the worker, then resumes
# here once the result arrives. This used to be a synchronous call that held
# the whole system for as long as the model took.
#
# We don't filter by ``kind`` here: the ``task.completed`` subscription is
# process-wide, so tasks from other modules pass through too. The recording
# itself decides — matching ``task_id`` against what it's awaiting lives in
# ``resume_stage``.


def _recording_of(event):
    """The recording id from the task's correlation_id (set by ``submit_task``)."""
    return event.payload.get("correlation_id") or ""


@on_action("task.completed")
def handle_task_completed(event):
    from stapel_core.comm import status

    from .pipeline import resume_stage
    from .stages import resume_resummarize

    recording_id = _recording_of(event)
    task_id = event.payload.get("task_id")
    if not recording_id or not task_id:
        return
    try:
        snapshot = status(task_id)
    except Exception:
        logger.exception("task.completed: failed to read task %s", task_id)
        return
    # A standalone re-summary is NOT a pipeline stage — it runs on a finished
    # recording, whose status the driver treats as terminal — so it is asked
    # first. It claims only the task ids its own Job rows are waiting on and
    # answers False for everything else, which is what makes this an ordering
    # and not a fork.
    if resume_resummarize(recording_id, task_id, snapshot.result):
        return
    resume_stage(recording_id, task_id, snapshot.result)


@on_action("task.failed")
def handle_task_failed(event):
    from .pipeline import fail_stage
    from .stages import fail_resummarize

    recording_id = _recording_of(event)
    task_id = event.payload.get("task_id")
    if not recording_id or not task_id:
        return
    error = event.payload.get("error") or ""
    if fail_resummarize(recording_id, task_id, error):
        return
    fail_stage(recording_id, task_id, error)
