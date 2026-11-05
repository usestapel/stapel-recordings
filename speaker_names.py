"""Speaker-name suggestions: the names a transcript states for its speakers.

``llm.summarize`` asked with ``speaker_names`` (``SUMMARIZE_SPEAKER_NAMES``)
returns, with the summary, one guess per speaker label the model saw:
``{speaker, suggested_name, evidence_text, confidence}``. That is model
output, so nothing of it is stored on trust. :func:`checked_guesses` keeps a
guess only when

* its label is a speaker of THIS transcript that has no name yet (the model
  sees a renamed speaker under its name, so a guess for it is dropped);
* its evidence is a verbatim quote of one segment (whitespace aside) — the
  segment's times become the guess's ``start_ms`` / ``end_ms``, so the times
  are ours, never the model's;
* the suggested name occurs in that quote;
* it is the best guess for its label (higher confidence, then earlier).

What is dropped is counted by reason, and the counts are stored beside the
kept guesses: a suggestion that never shows must be explainable.

The guesses are stored with the summary they came with
(``metadata["derived"]["summary"]["speaker_names"]``) and read through
:func:`speaker_name_guesses`, which also drops a guess whose speaker has
since been named or removed. Accepting one is the ordinary speaker rename.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Key inside ``metadata["derived"]["summary"]``.
SPEAKER_NAMES_KEY = "speaker_names"

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

DROP_UNKNOWN_LABEL = "unknown_label"
DROP_ALREADY_NAMED = "already_named"
DROP_NOT_VERBATIM = "evidence_not_verbatim"
DROP_NAME_NOT_IN_EVIDENCE = "name_not_in_evidence"
DROP_DUPLICATE_LABEL = "duplicate_label"
DROP_MALFORMED = "malformed"

_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def checked_guesses(transcript, guesses) -> dict:
    """``{"guesses": [...], "received": n, "dropped": {reason: n}}``.

    *transcript* is the :class:`~stapel_recordings.transcript_schema.UnifiedTranscript`
    the summary was stored against; *guesses* the reply's
    ``speaker_name_guesses``. Each kept guess is ``{speaker_id, label,
    suggested_name, evidence_text, start_ms, end_ms, confidence}``, where
    ``speaker_id`` is the Speaker row id and ``label`` the transcript's
    ``spk_N``.
    """
    guesses = guesses if isinstance(guesses, list) else []
    speakers = {sp.speaker_id: sp for sp in transcript.speakers}
    names = {_collapse(sp.name).casefold() for sp in transcript.speakers if sp.name}
    segments = [(seg, _collapse(seg.text)) for seg in transcript.segments]
    dropped: dict[str, int] = {}
    best: dict[str, dict] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for raw in guesses:
        if not isinstance(raw, dict):
            _drop(DROP_MALFORMED)
            continue
        label = _collapse(str(raw.get("speaker") or ""))
        name = _collapse(str(raw.get("suggested_name") or ""))
        evidence = _collapse(str(raw.get("evidence_text") or ""))
        confidence = str(raw.get("confidence") or "")
        if not (label and name and evidence) or confidence not in CONFIDENCE_RANK:
            _drop(DROP_MALFORMED)
            continue
        speaker = speakers.get(label)
        if speaker is None:
            _drop(DROP_ALREADY_NAMED if label.casefold() in names else DROP_UNKNOWN_LABEL)
            continue
        if speaker.name:
            _drop(DROP_ALREADY_NAMED)
            continue
        found = next((seg for seg, text in segments if evidence in text), None)
        if found is None:
            _drop(DROP_NOT_VERBATIM)
            continue
        if name.casefold() not in evidence.casefold():
            _drop(DROP_NAME_NOT_IN_EVIDENCE)
            continue
        guess = {
            "speaker_id": speaker.db_id,
            "label": label,
            "suggested_name": name,
            "evidence_text": evidence,
            "start_ms": int(found.start_ms),
            "end_ms": int(found.end_ms),
            "confidence": confidence,
        }
        held = best.get(label)
        if held is not None:
            _drop(DROP_DUPLICATE_LABEL)
            rank = (CONFIDENCE_RANK[confidence], -guess["start_ms"])
            if rank <= (CONFIDENCE_RANK[held["confidence"]], -held["start_ms"]):
                continue
        best[label] = guess
    kept = sorted(best.values(), key=lambda g: (g["start_ms"], g["label"]))
    return {"guesses": kept, "received": len(guesses), "dropped": dropped}


def record(recording, transcript, guesses) -> dict:
    """The stored block for a reply's guesses (see :func:`checked_guesses`),
    logged by counts only — never a name or a quote."""
    block = checked_guesses(transcript, guesses)
    if block["received"]:
        logger.info(
            "speaker names for %s: received=%d kept=%d dropped=%s",
            recording.pk, block["received"], len(block["guesses"]), block["dropped"],
        )
    return block


def speaker_name_guesses(recording, *, min_confidence: str = "medium") -> list[dict]:
    """The stored guesses still worth showing, best first by time.

    Only those stored with the CURRENT summary, at or above
    *min_confidence*, for a speaker that still exists and still has no name.
    """
    if not recording.summary:
        return []
    derived = (recording.metadata or {}).get("derived") or {}
    block = (derived.get("summary") or {}).get(SPEAKER_NAMES_KEY) or {}
    stored = block.get("guesses") if isinstance(block, dict) else None
    if not stored:
        return []
    floor = CONFIDENCE_RANK.get(min_confidence, CONFIDENCE_RANK["medium"])
    unnamed = {
        str(pk)
        for pk, display_name in recording.speakers.values_list("id", "display_name")
        if not (display_name or "").strip()
    }
    return [
        dict(g)
        for g in stored
        if isinstance(g, dict)
        and CONFIDENCE_RANK.get(g.get("confidence"), 0) >= floor
        and str(g.get("speaker_id")) in unnamed
    ]


__all__ = [
    "SPEAKER_NAMES_KEY",
    "CONFIDENCE_RANK",
    "checked_guesses",
    "record",
    "speaker_name_guesses",
]
