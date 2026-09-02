"""Grouping STT utterances into answer-sized passages.

The embed stage's original unit was one ``Segment`` — one utterance as the
speech-to-text provider emitted it. On a real deployment the median such
unit is **37 characters**: "Yeah." / "So the deadline?" / a half-sentence
cut at a pause. Two things go wrong with a unit that small, and both are
invisible from inside the pipeline:

- an embedding of five words is mostly noise, so cosine ranking over them
  is close to arbitrary;
- even when retrieval picks the right row, the row does not *contain* the
  answer — the answer is spread over the six utterances around it — so the
  QA prompt gets grounding it cannot ground on.

A **window** is a run of consecutive utterances of one recording, packed to
``target_chars`` and never past ``max_chars``, rendered with a
``[mm:ss Speaker]`` prefix per utterance. That prefix is not decoration: it
is what lets an answer say *who* said a thing and *when*, and it is inside
the embedded text, so "what did Boris commit to" has something to match on.

Windows overlap by ``overlap_chars`` of the previous window's tail so a
question whose answer straddles a boundary is not lost by both windows.
Overlap is by whole utterances — half an utterance is not a citable thing.

This module is pure: no Django, no models, no settings. It takes
:class:`Utterance` records and returns :class:`Window` records; the embed
stage adapts ``Segment`` rows to them. That keeps the packing decisions
unit-testable without a database, and keeps the "what is a unit" question
separable from "where do rows live".
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Utterance:
    """One STT utterance — the pieces of a ``Segment`` a window needs."""

    id: object
    sequence_num: int
    start_time: float
    end_time: float
    text: str
    speaker: str = ""


@dataclass(frozen=True)
class Window:
    """A run of consecutive utterances, embedded as one unit.

    ``anchor_id`` is the id of the FIRST utterance in the run: a window has
    to be addressable by something that already exists and already cascades
    on delete, and the run's own head is the only such thing. Search
    attributes a window hit to its anchor; ``utterance_ids`` is the full
    span, so a caller that wants the exact source rows has them."""

    anchor_id: object
    utterance_ids: list = field(default_factory=list)
    text: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    index: int = 0


def format_timestamp(seconds: float) -> str:
    """``mm:ss`` (or ``h:mm:ss`` past an hour) — the form a person scanning
    a transcript reads, and short enough to spend on every utterance."""
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_utterance(utterance: Utterance) -> str:
    """One line of window text: ``[04:12 Alice] we agreed to ship Friday``.

    The speaker is omitted rather than faked when unknown — an unnamed
    speaker is a fact about the recording, and inventing "Speaker 1" here
    would put a label in the embedding that no one ever said."""
    text = " ".join((utterance.text or "").split())
    stamp = format_timestamp(utterance.start_time)
    speaker = (utterance.speaker or "").strip()
    head = f"[{stamp} {speaker}]" if speaker else f"[{stamp}]"
    return f"{head} {text}".strip()


def _split_oversized(line: str, max_chars: int) -> list[str]:
    """A single utterance longer than a whole window (a provider that did
    not segment at all, a pasted document) is cut on whitespace rather
    than dropped — losing it silently is the failure this guards."""
    if len(line) <= max_chars:
        return [line]
    parts: list[str] = []
    remaining = line
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def build_windows(
    utterances,
    *,
    target_chars: int = 600,
    max_chars: int = 800,
    overlap_chars: int = 0,
) -> list[Window]:
    """Pack *utterances* (in transcript order) into windows.

    A window closes once it reaches ``target_chars``, and never grows past
    ``max_chars``: the next utterance opens a new window instead. When
    ``overlap_chars > 0`` the new window re-opens with as many whole
    utterances from the previous window's tail as fit in that budget, so an
    answer split across a boundary appears whole in one of the two.

    Every utterance appears in at least one window and the sequence of
    first-appearances is the transcript order — nothing is dropped and
    nothing is reordered. Empty texts are skipped; an utterance longer than
    ``max_chars`` by itself is split on whitespace across consecutive
    windows that all carry its id.
    """
    target_chars = max(1, int(target_chars))
    max_chars = max(target_chars, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars or 0), target_chars // 2))

    ordered = sorted(
        (u for u in utterances if (u.text or "").strip()),
        key=lambda u: u.sequence_num,
    )
    if not ordered:
        return []

    windows: list[Window] = []
    # Current window under construction: rendered lines + their utterances.
    lines: list[str] = []
    members: list[Utterance] = []
    length = 0

    def flush() -> None:
        nonlocal lines, members, length
        if not members:
            return
        # Consecutive repeats collapse: an utterance too long for one
        # window contributes several LINES to it, but it is still one
        # utterance, and `span` is a count of utterances covered.
        covered: list = []
        for member in members:
            if not covered or covered[-1] != member.id:
                covered.append(member.id)
        windows.append(
            Window(
                anchor_id=members[0].id,
                utterance_ids=covered,
                text="\n".join(lines),
                start_time=float(members[0].start_time or 0.0),
                end_time=float(max(u.end_time or 0.0 for u in members)),
                index=len(windows),
            )
        )
        # Re-open with the tail that fits the overlap budget (whole
        # utterances only, and never the entire window — that would not
        # advance).
        carry_lines: list[str] = []
        carry_members: list[Utterance] = []
        carried = 0
        for line, member in zip(reversed(lines[1:]), reversed(members[1:])):
            if carried + len(line) + 1 > overlap_chars:
                break
            carry_lines.insert(0, line)
            carry_members.insert(0, member)
            carried += len(line) + 1
        lines, members = carry_lines, carry_members
        length = carried

    for utterance in ordered:
        for piece in _split_oversized(render_utterance(utterance), max_chars):
            if members and length + len(piece) + 1 > max_chars:
                flush()
                if members and length + len(piece) + 1 > max_chars:
                    # The carried overlap plus this piece would still bust
                    # the ceiling. The ceiling wins: overlap is a nicety,
                    # a window wider than max_chars is a broken promise
                    # (and, downstream, a truncated embedding).
                    lines, members, length = [], [], 0
            lines.append(piece)
            members.append(utterance)
            length += len(piece) + 1
            if length >= target_chars:
                flush()
    flush()

    # `flush` renumbers nothing, so indexes are already 0..n-1 in order.
    return windows


def _speaker_name(segment) -> str:
    """The renamed display name when a human gave one, else the provider's
    own label (``speaker_0``) — which is at least true. Never a fabricated
    name: an unnamed speaker stays unnamed in the embedded text."""
    if not getattr(segment, "speaker_id", None):
        return ""
    speaker = segment.speaker
    return (getattr(speaker, "display_name", "") or getattr(speaker, "label", "") or "")


def windows_from_segments(segments, *, target_chars, max_chars, overlap_chars):
    """Adapter: ``Segment`` model rows -> :func:`build_windows`.

    Kept here rather than in the embed stage so the ORM-shaped half of
    windowing is one line and the packing rules stay pure."""
    return build_windows(
        [
            Utterance(
                id=seg.id,
                sequence_num=int(seg.sequence_num or 0),
                start_time=float(seg.start_time or 0.0),
                end_time=float(seg.end_time or 0.0),
                text=seg.text or "",
                speaker=_speaker_name(seg),
            )
            for seg in segments
        ],
        target_chars=target_chars,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )


__all__ = [
    "Utterance",
    "Window",
    "build_windows",
    "windows_from_segments",
    "render_utterance",
    "format_timestamp",
]
