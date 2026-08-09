"""Default task executor: keep the queue here, hand the work to the agent.

WHY THIS BRIDGE EXISTS. The Task primitive keeps its state in the
``TaskRecord`` table, and only a process that can SEE that table can
execute a task. In a monolith that's one database, and stapel-agent's
``llm.transcribe`` handler lives in the same process — no bridge needed,
nothing is registered.

In a microservices layout the databases are DIFFERENT. A task submitted by
the recordings pipeline lives in the recordings database, and the agent
doesn't know about it — ``handle_task_requested`` in its process silently
skips the unknown kind, and the recording stays PENDING forever.

SO: the queue, state, attempts and observability stay with the recording —
where the recording itself lives, and the person waiting on the result. The
executor hands the work to the agent as an ordinary Function call over the
bus, same as any other cross-service call.

This is NOT a return to the synchronous call we moved away from:

* a background task executor waits, not an HTTP worker — worker time is no
  longer spent on model latency;
* the timeout is explicit and genuinely long, not the five-second
  ``FUNCTION_TIMEOUT`` default that every real recording used to exceed;
* while work is in flight, ``status(task_id)`` is available: pending /
  running, attempt count — none of which a synchronous call ever had.

Disable with ``STAPEL_RECORDINGS["DELEGATE_TASKS_TO_AGENT"] = False`` for a
deployment that executes ``llm.*`` some other way.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Task kinds the bridge takes on, and the timeout setting for each.
#: Key is the timeout setting name; None would mean falling back to the
#: general transcription budget.
DELEGATED = {
    "llm.transcribe": "TRANSCRIBE_TIMEOUT_SECONDS",
    "llm.summarize": "SUMMARIZE_TIMEOUT_SECONDS",
}


def _make_delegate(kind: str, timeout_setting: str):
    def delegate(payload: dict):
        from stapel_core.comm import call

        from .conf import recordings_settings

        timeout = float(getattr(recordings_settings, timeout_setting))
        # noqa: R009 — this IS the sanctioned bridge, not the defect the
        # rule targets: (1) a background task executor waits, not an HTTP
        # worker; (2) the timeout is explicit and long; (3) waiting state is
        # visible externally via status(). The rule forbids waiting on a
        # long operation WHERE waiting isn't safe, not cross-service calls
        # as such.
        return call(kind, payload, timeout=timeout)  # noqa: R009

    delegate.__name__ = f"delegate_{kind.replace('.', '_')}"
    delegate.__doc__ = f"Hand {kind} to the agent over the bus and return its response."
    return delegate


def register_default_task_delegates() -> None:
    """Register the bridge for kinds NOBODY has claimed yet.

    Order matters and rests on one property: if stapel-agent already
    registered a real handler in this same process (monolith), we leave it
    alone — ``register_task`` on a taken name would raise ValueError, and
    silently swapping the real executor for the bridge stub would be the
    worst possible outcome.
    """
    from stapel_core.comm import tasks as task_primitive

    from .conf import recordings_settings

    if not recordings_settings.DELEGATE_TASKS_TO_AGENT:
        return

    taken = set(task_primitive.registered_kinds())
    for kind, timeout_setting in DELEGATED.items():
        if kind in taken:
            continue  # a real handler is already here — no bridge needed
        task_primitive.register_task(kind, _make_delegate(kind, timeout_setting))
        logger.debug("recordings: bridging task %s -> Function of the same name", kind)
