"""Stage checkpoints keyed by the INPUT that produced them.

A pipeline stage that costs real money — a transcription, a summary, an
embedding pass — must run once per input and not once per delivery. The
driver's completed-stage cursor (:mod:`stapel_recordings.pipeline`) answers
"has this stage run", and every stage additionally self-guards on its
persisted artifact ("segments already exist, nothing to do"). Both answer
the cheap question. Neither answers the expensive one:

    was the result that is already here computed from the input we are
    being asked about NOW?

Until it is asked, "already done" means "an artifact with this name
exists", and a re-run over a DIFFERENT input silently hands back the old
answer. That is not a hypothetical: a recording processed under a trim,
then re-queued in full after the customer paid for the whole thing, found
every stage holding an artifact from the trimmed input and completed in 55
seconds without re-running anything. The customer paid and kept the
trimmed result.

So a checkpoint here is a pair: the artifact, and the FINGERPRINT OF THE
INPUT AND PARAMETERS it was computed from. A stage declares its own
fingerprint (:meth:`stapel_recordings.stages.Stage.input_fingerprint` —
content hash of what it reads, plus whatever parameters change the answer),
the driver records it next to the completion, and a result is reused only
while the two agree. Change the input and the stage recomputes; retry the
same input and it resumes for free.

Two ways a checkpoint stops being valid:

* **Computed** — the recorded fingerprint differs from the current one.
  This needs nobody to remember anything: whatever changed the input
  invalidated the result by changing it.
* **Declared** — :func:`stapel_recordings.pipeline.invalidate_from` marks a
  stage (and everything downstream of it) as needing recomputation. The
  escape hatch for what a fingerprint cannot see: a parameter that lives
  outside the row, and every artifact produced before fingerprints were
  recorded at all. Without it, an upgrade would leave every existing
  recording with an unrecorded fingerprint, which compares equal to
  nothing and therefore reuses forever.

WHY A MISSING FINGERPRINT MEANS "VALID". The comparison refuses to guess:
an artifact whose provenance was never recorded is trusted, exactly as it
was before this module existed. The alternative — treating unknown as
stale — would re-run, and therefore re-pay for, every stage of every
recording that predates the upgrade, and would re-pay again on every crash
recovery where the artifact was written but its completion marker was not.
An unknown provenance is a reason to ask explicitly (``invalidate_from``),
never a reason to spend money.

Both values live in ``Recording.workflow_state``, never in
``Recording.metadata`` (audit REC-01): metadata is the client's column, and
a fingerprint a client could PATCH is a fingerprint a client could forge to
be handed another recording's paid transcript — or to force a re-run on the
host's invoice.

The writers below mutate ``recording.workflow_state`` IN MEMORY and never
save: every call site is already inside the driver's locked transaction and
includes ``workflow_state`` in its own ``update_fields``.
"""
from __future__ import annotations

from typing import Iterable, Optional

#: ``{stage name: fingerprint}`` — the input each persisted artifact was
#: computed from. Under ``workflow_state["pipeline"]``, beside the cursor.
FINGERPRINTS_KEY = "fingerprints"

#: Stage names explicitly declared stale. Cleared per stage as it completes.
INVALIDATED_KEY = "invalidated"


def _pipeline(recording) -> dict:
    return (recording.workflow_state or {}).get("pipeline") or {}


def _write_pipeline(recording, pipeline: dict) -> None:
    state = dict(recording.workflow_state or {})
    state["pipeline"] = pipeline
    recording.workflow_state = state


def recorded_fingerprint(recording, stage: str) -> Optional[str]:
    """The input fingerprint *stage*'s persisted artifact was computed from.

    ``None`` means "not recorded" — see the module docstring: unknown is
    trusted, never assumed stale.
    """
    value = (_pipeline(recording).get(FINGERPRINTS_KEY) or {}).get(stage)
    return str(value) if value else None


def invalidated_stages(recording) -> set[str]:
    """Stage names declared stale by :func:`pipeline.invalidate_from`."""
    return {str(name) for name in (_pipeline(recording).get(INVALIDATED_KEY) or [])}


def is_stale(recording, stage: str, current: Optional[str]) -> bool:
    """Must *stage* recompute, given the fingerprint of its input right now?

    *current* is what the stage's :meth:`input_fingerprint` answers for the
    input it is about to read; ``None`` means the stage does not declare one.

    True when the stage was declared stale, or when a recorded fingerprint
    exists and disagrees with *current*. False in every other case,
    including both unknowns.
    """
    if stage in invalidated_stages(recording):
        return True
    recorded = recorded_fingerprint(recording, stage)
    if not recorded or not current:
        return False
    return recorded != str(current)


def record_fingerprint(recording, stage: str, fingerprint: Optional[str]) -> None:
    """Note (in memory) the input *stage*'s fresh artifact was computed from.

    A stage that declares no fingerprint records nothing rather than
    recording a null: the absence is the honest value, and it is what keeps
    a host that never adopted fingerprints behaving exactly as before.
    """
    pipeline = dict(_pipeline(recording))
    prints = dict(pipeline.get(FINGERPRINTS_KEY) or {})
    if fingerprint:
        prints[stage] = str(fingerprint)
    else:
        prints.pop(stage, None)
    if prints:
        pipeline[FINGERPRINTS_KEY] = prints
    else:
        pipeline.pop(FINGERPRINTS_KEY, None)
    _write_pipeline(recording, pipeline)


def declare_invalid(recording, stages: Iterable[str]) -> None:
    """Mark *stages* as needing recomputation (in memory).

    Additive: a stage already declared stale stays declared, and the order
    is kept stable so the value reads the way the pipeline runs.
    """
    pipeline = dict(_pipeline(recording))
    listed = [str(name) for name in (pipeline.get(INVALIDATED_KEY) or [])]
    for name in stages:
        if str(name) not in listed:
            listed.append(str(name))
    if listed:
        pipeline[INVALIDATED_KEY] = listed
    _write_pipeline(recording, pipeline)


def clear_invalidation(recording, stage: str) -> None:
    """Drop *stage*'s declared staleness (in memory) — it has just re-run.

    Called on completion, and only on completion: a declaration that
    outlived the recomputation it asked for would ask for it again on the
    next delivery, for ever.
    """
    pipeline = dict(_pipeline(recording))
    listed = [str(name) for name in (pipeline.get(INVALIDATED_KEY) or []) if str(name) != stage]
    if listed:
        pipeline[INVALIDATED_KEY] = listed
    else:
        pipeline.pop(INVALIDATED_KEY, None)
    _write_pipeline(recording, pipeline)


__all__ = [
    "FINGERPRINTS_KEY",
    "INVALIDATED_KEY",
    "recorded_fingerprint",
    "invalidated_stages",
    "is_stale",
    "record_fingerprint",
    "declare_invalid",
    "clear_invalidation",
]
