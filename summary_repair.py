"""A summary the provider refused is owed, not forgotten.

THE INCIDENT (a client fleet, 2026-09-29 00:34Z..09-30 04:59Z). The only text
provider answered 402 for 28 hours. ``MergeStage`` treats the summary as
best-effort — right, the transcript is the product — so 23 recordings reached
``completed`` with no summary, and nothing ever asked again. The customers had
paid, the ready-letter is gated on a deliverable so none of them got one, and
every repair a person could reach re-bought something: a reprocess re-runs
(and re-bills) the pipeline, a re-summary is a paid user action.

WHAT THIS DOES. When ``llm.summarize`` fails with ``failure_class=provider``
(stapel-agent >= 0.33.0: every provider declined on its own account, the
request was never judged), the merge stage writes
``workflow_state["summary_pending"]``. The reconcile watchdog then re-asks
through the summarize-only path (:func:`stages.start_resummarize`) with
``origin="pipeline_repair"`` — no STT, no diarize, and an origin the host maps
to "not the customer's purchase". Backoff doubles from
``SUMMARY_REPAIR_FIRST_DELAY_SECONDS`` to ``SUMMARY_REPAIR_MAX_DELAY_SECONDS``
until ``SUMMARY_REPAIR_DEADLINE_SECONDS``; after that the marker stays with
``exhausted_at`` and the host's ``summary_missing`` sentinel owns it.

ONE REFUSED CALL PER TICK DURING AN OUTAGE. While a repair job is in flight
the tick waits for its answer; when the last finished repair failed, the tick
sends one probe; only after a success does it send a batch. An outage costs
one refused call per tick, not one per recording.

A genuinely empty or refused-as-input result is not retried: the same request
would get the same answer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

#: ``workflow_state`` key of the marker. Server-only column, like the rest
#: of the pipeline's own state.
PENDING_KEY = "summary_pending"

#: ``Job.options["origin"]`` / ``recording.resummarized`` ``origin``. Absent
#: means ``user`` — every job before this module existed was a user's.
ORIGIN_USER = "user"
ORIGIN_REPAIR = "pipeline_repair"
ORIGIN_STAFF = "staff"
ORIGINS = frozenset({ORIGIN_USER, ORIGIN_REPAIR, ORIGIN_STAFF})

#: The ``failure_class`` values worth asking again later.
RETRYABLE_CLASSES = frozenset({"provider"})


def _setting(key: str):
    from .conf import recordings_settings

    return getattr(recordings_settings, key)


def enabled() -> bool:
    from .conf import flag

    return flag("SUMMARY_REPAIR_ENABLED") and flag("SUMMARIZE_ENABLED")


def is_retryable(result) -> bool:
    """A failed llm.summarize result whose failure was the provider's."""
    return (
        isinstance(result, dict)
        and result.get("status") != "ok"
        and result.get("failure_class") in RETRYABLE_CLASSES
    )


def pending(recording) -> Optional[dict]:
    block = (recording.workflow_state or {}).get(PENDING_KEY)
    return block if isinstance(block, dict) else None


def _delay_seconds(attempts: int) -> int:
    first = max(1, int(_setting("SUMMARY_REPAIR_FIRST_DELAY_SECONDS")))
    ceiling = max(first, int(_setting("SUMMARY_REPAIR_MAX_DELAY_SECONDS")))
    return min(ceiling, first * (2 ** max(0, attempts)))


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def providers_of(result) -> list[str]:
    """The provider names a failed llm.summarize result says it tried, in order.

    stapel-agent >= 0.34.0 sends ``provider_attempts``; older agents send
    none, and an empty list says "unknown" rather than guessing.
    """
    attempts = result.get("provider_attempts") if isinstance(result, dict) else None
    names: list[str] = []
    for row in attempts or []:
        name = row.get("provider") if isinstance(row, dict) else None
        if name and str(name) not in names:
            names.append(str(name)[:60])
    return names


def mark_pending_from(recording, result, *, now=None, save: bool = True) -> dict:
    """:func:`mark_pending` with everything a refused *result* says about itself."""
    return mark_pending(
        recording,
        reason=",".join(result.get("provider_reasons") or []) or "provider",
        detail=result.get("reason"),
        failure_class=result.get("failure_class"),
        providers=providers_of(result),
        now=now,
        save=save,
    )


def mark_pending(
    recording,
    *,
    reason: str,
    detail=None,
    failure_class=None,
    providers=None,
    now=None,
    save: bool = True,
) -> dict:
    """Record that this recording is owed a summary. Idempotent.

    A second mark keeps ``since`` and ``attempts``: the deadline counts from
    the first refusal, not the latest. ``failure_class`` and ``providers``
    are what an alert about the marker names: whose failure, and who was
    asked.
    """
    now = now or timezone.now()
    state = dict(recording.workflow_state or {})
    block = dict(state.get(PENDING_KEY) or {})
    block.setdefault("since", now.isoformat())
    block.setdefault("attempts", 0)
    block["reason"] = str(reason)[:120]
    block["detail"] = str(detail)[:300] if detail else None
    if failure_class:
        block["failure_class"] = str(failure_class)[:40]
    if providers:
        block["providers"] = [str(p)[:60] for p in providers][:8]
    block.setdefault("next_at", (now + timedelta(seconds=_delay_seconds(0))).isoformat())
    state[PENDING_KEY] = block
    recording.workflow_state = state
    if save:
        recording.save(update_fields=["workflow_state", "updated_at"])
    return block


def clear_pending(recording) -> bool:
    """Drop the marker in memory. True if there was one. The caller saves."""
    state = dict(recording.workflow_state or {})
    if PENDING_KEY not in state:
        return False
    state.pop(PENDING_KEY)
    recording.workflow_state = state
    return True


def note_repair_failure(recording, result) -> None:
    """A repair attempt came back without a summary. The caller saves.

    Provider-side: the marker stays and the backoff already scheduled the
    next ask. Anything else: this request will not get a different answer,
    so the marker stops asking (``gave_up``) and the sentinel owns it.
    """
    block = pending(recording)
    if block is None:
        return
    block = dict(block)
    reason = result.get("reason") if isinstance(result, dict) else str(result)
    block["last_failure"] = str(reason)[:300] if reason else None
    if isinstance(result, dict):
        if result.get("failure_class"):
            block["failure_class"] = str(result["failure_class"])[:40]
        providers = providers_of(result)
        if providers:
            block["providers"] = providers[:8]
    if isinstance(result, dict) and not is_retryable(result):
        block["gave_up"] = str(result.get("failure_class") or "not_retryable")
    state = dict(recording.workflow_state or {})
    state[PENDING_KEY] = block
    recording.workflow_state = state


def _due(block: dict, now) -> bool:
    if block.get("exhausted_at") or block.get("gave_up"):
        return False
    next_at = _parse(block.get("next_at"))
    return next_at is None or next_at <= now


def _inflight_repairs():
    from .models import Job, JobStatus, JobType

    stale_after = timedelta(
        seconds=2 * int(_setting("SUMMARIZE_TIMEOUT_MAX_SECONDS"))
    )
    cutoff = timezone.now() - stale_after
    return [
        job
        for job in Job.objects.filter(
            type=JobType.SUMMARIZE,
            status__in=[JobStatus.QUEUED, JobStatus.PROCESSING],
            queued_at__gte=cutoff,
        )
        if (job.options or {}).get("origin") == ORIGIN_REPAIR
    ]


def _last_repair_failed() -> bool:
    from .models import Job, JobStatus, JobType

    finished = Job.objects.filter(
        type=JobType.SUMMARIZE,
        status__in=[JobStatus.COMPLETED, JobStatus.FAILED],
    ).order_by("-completed_at")[:50]
    for job in finished:
        if (job.options or {}).get("origin") == ORIGIN_REPAIR:
            return job.status == JobStatus.FAILED
    return False


def repair_due(*, now=None) -> dict:
    """One watchdog tick. Returns counts: ``started``, ``exhausted``, ``waiting``."""
    from django.db import transaction

    from .models import Recording, RecordingStatus
    from .stages import ResummarizeRefused, start_resummarize

    counts = {"started": 0, "exhausted": 0, "waiting": 0}
    if not enabled():
        return counts
    now = now or timezone.now()

    if _inflight_repairs():
        counts["waiting"] = 1
        return counts
    budget = 1 if _last_repair_failed() else max(1, int(_setting("SUMMARY_REPAIR_BATCH")))
    deadline = timedelta(seconds=int(_setting("SUMMARY_REPAIR_DEADLINE_SECONDS")))

    candidates = (
        Recording.objects.filter(
            status=RecordingStatus.COMPLETED,
            deleted_at__isnull=True,
            workflow_state__has_key=PENDING_KEY,
        )
        .order_by("updated_at")[:500]
    )
    for recording in candidates:
        if counts["started"] >= budget:
            break
        block = pending(recording)
        if block is None or not _due(block, now):
            continue
        since = _parse(block.get("since")) or now
        with transaction.atomic():
            locked = Recording.objects.select_for_update().filter(pk=recording.pk).first()
            if locked is None or pending(locked) is None:
                continue
            block = dict(pending(locked))
            if now - since > deadline:
                block["exhausted_at"] = now.isoformat()
                state = dict(locked.workflow_state or {})
                state[PENDING_KEY] = block
                locked.workflow_state = state
                locked.save(update_fields=["workflow_state", "updated_at"])
                counts["exhausted"] += 1
                logger.warning(
                    "summary_repair: %s still has no summary after %s — giving "
                    "up; the summary_missing sentinel owns it now",
                    locked.id, deadline,
                )
                continue
            attempts = int(block.get("attempts") or 0) + 1
            block["attempts"] = attempts
            block["last_attempt_at"] = now.isoformat()
            block["next_at"] = (now + timedelta(seconds=_delay_seconds(attempts))).isoformat()
            state = dict(locked.workflow_state or {})
            state[PENDING_KEY] = block
            locked.workflow_state = state
            locked.save(update_fields=["workflow_state", "updated_at"])
        try:
            start_resummarize(
                locked, origin=ORIGIN_REPAIR, reason=block.get("reason") or "provider"
            )
        except ResummarizeRefused as exc:
            logger.warning("summary_repair: %s refused: %s", locked.id, exc)
            continue
        counts["started"] += 1
    if counts["started"] or counts["exhausted"]:
        logger.info("summary_repair: %s", counts)
    return counts


__all__ = [
    "ORIGINS",
    "ORIGIN_REPAIR",
    "ORIGIN_STAFF",
    "ORIGIN_USER",
    "PENDING_KEY",
    "clear_pending",
    "enabled",
    "is_retryable",
    "mark_pending",
    "note_repair_failure",
    "pending",
    "repair_due",
]
