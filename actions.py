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
# Both handlers below live in THIS module on purpose. The probe answer is
# what tells the orchestrator this owner is reachable, and an answer emitted
# from anywhere else would only prove that a container is deployed — not
# that the subscriber which erases is actually being consumed. Co-location
# is the evidence; `gdpr.W006` and GET /gdpr/api/v1/owners/health read it.


def _receipt(correlation_id, subject_type, subject_key, counts) -> None:
    """Confirm this owner's slice of one erasure. Caller is inside the same
    transaction as the erasure — outbox discipline: the receipt leaves iff
    the deletion committed, so a receipt can never certify a rollback."""
    from stapel_core.comm import emit

    from .erasure import OWNER

    emit(
        "gdpr.section.erased",
        {
            "correlation_id": str(correlation_id),
            "owner": OWNER,
            "subject_type": subject_type,
            "subject_key": str(subject_key),
            "counts": counts,
        },
        key=str(correlation_id),
    )


@on_action("gdpr.erasure.requested")
def handle_erasure_requested(event):
    """Erase everything this module owns about one subject, then receipt it.

    Idempotent: a redelivery finds nothing left, removes nothing and sends a
    zero-count receipt — the orchestrator needs the receipt, not the rows.
    A subject type this module does not claim is ignored silently (the
    action is broadcast; the orchestrator only opened a part for the owners
    that claim it, so answering for the others would invent receipts).
    """
    from django.db import transaction

    from .erasure import SUBJECT_TYPES, erase

    payload = event.payload
    subject_type = payload.get("subject_type")
    subject_key = payload.get("subject_key")
    correlation_id = payload.get("correlation_id")
    if not subject_type or not subject_key or not correlation_id:
        logger.error(
            "gdpr.erasure.requested missing subject/correlation: %s", event.event_id
        )
        return
    if subject_type not in SUBJECT_TYPES:
        return

    with transaction.atomic():
        counts = erase(
            subject_type, subject_key, workspace_id=payload.get("workspace_id")
        )
        _receipt(correlation_id, subject_type, subject_key, counts)
    logger.info(
        "recordings erased for %s=%s: %s", subject_type, subject_key, counts or "nothing"
    )


@on_action("gdpr.owner.probe")
def handle_owner_probe(event):
    """Answer the daily liveness probe — from the subscriber that erases."""
    from django.db import transaction

    from stapel_core.comm import emit

    from .erasure import OWNER, SUBJECT_TYPES

    payload = {"owner": OWNER, "subject_types": list(SUBJECT_TYPES)}
    correlation_id = event.payload.get("correlation_id")
    if correlation_id:
        payload["correlation_id"] = str(correlation_id)
    with transaction.atomic():
        emit("gdpr.owner.alive", payload, key=OWNER)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase a user's recordings + their storage objects (GDPR Art. 17).

    The deprecated half of the protocol: stapel-gdpr keeps firing
    ``user.deleted`` alongside ``gdpr.erasure.requested`` for account
    erasures until 0.6.0, and hosts still on 0.4.x fire only this one. It
    runs the SAME ``erase(subject_type="account")`` code as the erasure
    handler and receipts identically when the payload carries a
    correlation_id, so neither path is a second implementation and a
    double delivery is two idempotent erasures plus two identical receipts.
    """
    from django.db import transaction

    from .erasure import SUBJECT_ACCOUNT, erase

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    correlation_id = event.payload.get("correlation_id")
    with transaction.atomic():
        counts = erase(SUBJECT_ACCOUNT, user_id)
        if correlation_id:
            _receipt(correlation_id, SUBJECT_ACCOUNT, user_id, counts)
    logger.info("recordings erased for deleted user %s: %s", user_id, counts or "nothing")


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
        if not get_user_model().objects.filter(pk=into_user_id).exists():
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
