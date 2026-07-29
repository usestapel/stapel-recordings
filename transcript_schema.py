"""Unified transcript schema.

Single canonical JSON for a completed recording, built from the persisted
Segment/Speaker rows (``from_db_segments``) and stored via the STORAGE
seam at ``<prefix>/<id>/transcript.json``. ``render_markdown`` /
``build_summary_input`` prepare LLM-ready views for the summarize step
(which delegates to stapel-agent's ``llm.summarize``).

The provider-facing ``from_normalized`` mapper is dropped because STT now
lives in stapel-agent — recordings persists Segment rows directly from
the ``llm.transcribe`` result dict (see ``stages.TranscribeStage``).

Invariants (checked by ``run_qa``): monotonic start/end ms; max end <=
duration (+tolerance); >=1 speaker when diarization requested.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from stapel_core.hashing import canonical_hash

SCHEMA_VERSION = "1.0"
PIPELINE_VERSION = "2.0.0"


# ─── Schema dataclasses ────────────────────────────────────────────────


@dataclass
class UnifiedWord:
    w: str
    start_ms: int
    end_ms: int
    conf: Optional[float] = None


@dataclass
class UnifiedSegment:
    id: str
    start_ms: int
    end_ms: int
    speaker_id: Optional[str]
    text: str
    words: list[UnifiedWord] = field(default_factory=list)
    lang: Optional[str] = None


@dataclass
class UnifiedSpeaker:
    speaker_id: str
    db_id: Optional[str] = None
    name: Optional[str] = None
    color: Optional[str] = None


@dataclass
class LanguageMeta:
    routed: Optional[str]
    detected: Optional[str]
    path: str  # "A" (auto-detect) or "B" (user-selected)


@dataclass
class EngineMeta:
    asr_model_id: str
    pipeline_version: str
    fallback_used: bool


@dataclass
class QAResult:
    passed: bool
    checks: dict = field(default_factory=dict)


@dataclass
class UnifiedTranscript:
    schema_version: str
    meeting_id: str
    duration_ms: int
    language: LanguageMeta
    engine: EngineMeta
    segments: list[UnifiedSegment]
    speakers: list[UnifiedSpeaker]
    qa: QAResult

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ─── Builder from DB rows ──────────────────────────────────────────────


def from_db_segments(recording) -> UnifiedTranscript:
    """Build a UnifiedTranscript from persisted Segment/Speaker rows."""
    segments_qs = list(recording.segments.select_related("speaker").order_by("sequence_num"))
    # Explicit ORDER BY even though Speaker.Meta.ordering already supplies one:
    # the positional ``spk_N`` ids assigned below are part of the transcript's
    # identity, so the ordering they depend on is stated where it is relied upon
    # rather than inherited silently from a model two files away.
    speakers_qs = list(recording.speakers.order_by("label", "id"))

    pk_to_spk_id: dict[str, str] = {}
    unified_speakers: list[UnifiedSpeaker] = []
    for idx, sp in enumerate(speakers_qs):
        spk_id = f"spk_{idx}"
        pk_to_spk_id[str(sp.id)] = spk_id
        unified_speakers.append(
            UnifiedSpeaker(
                speaker_id=spk_id,
                db_id=str(sp.id),
                name=sp.display_name,
                color=sp.color,
            )
        )

    duration_ms = int(round((recording.duration_seconds or 0) * 1000))
    lang = recording.language

    segments: list[UnifiedSegment] = []
    for seg in segments_qs:
        spk_id = pk_to_spk_id.get(str(seg.speaker_id)) if seg.speaker_id else None
        start_ms = _to_ms(seg.start_time)
        end_ms = _to_ms(seg.end_time)
        raw_words = getattr(seg, "words_json", None) or []
        utt_words = [
            UnifiedWord(
                w=wd.get("w", ""),
                start_ms=wd.get("start_ms", start_ms),
                end_ms=wd.get("end_ms", end_ms),
                conf=wd.get("conf"),
            )
            for wd in raw_words
        ]
        segments.append(
            UnifiedSegment(
                id=f"seg_{seg.sequence_num:04d}",
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id=spk_id,
                text=seg.text,
                words=utt_words,
                lang=seg.language or lang,
            )
        )

    metadata = recording.metadata or {}
    lang_meta = LanguageMeta(
        routed=lang,
        detected=lang,
        path="B" if metadata.get("user_selected_language") else "A",
    )
    engine_meta = EngineMeta(
        asr_model_id=recording.provider_used or "unknown",
        pipeline_version=PIPELINE_VERSION,
        fallback_used=bool(recording.fallback_used),
    )
    qa_result = run_qa(
        segments=segments,
        speakers=unified_speakers,
        duration_ms=duration_ms,
        diarization_requested=recording.diarization_enabled,
    )

    return UnifiedTranscript(
        schema_version=SCHEMA_VERSION,
        meeting_id=str(recording.id),
        duration_ms=duration_ms,
        language=lang_meta,
        engine=engine_meta,
        segments=segments,
        speakers=unified_speakers,
        qa=qa_result,
    )


# ─── QA invariants ─────────────────────────────────────────────────────


def run_qa(
    *,
    segments: list[UnifiedSegment],
    speakers: list[UnifiedSpeaker],
    duration_ms: int,
    diarization_requested: bool,
) -> QAResult:
    checks: dict[str, str] = {}
    passed = True

    prev_end = -1
    mono_fail = None
    for seg in segments:
        if seg.start_ms < prev_end - 100:
            mono_fail = f"seg {seg.id}: start_ms={seg.start_ms} < prev_end={prev_end}"
            break
        prev_end = seg.end_ms
    if mono_fail:
        checks["monotonicity"] = f"FAIL: {mono_fail}"
        passed = False
    else:
        checks["monotonicity"] = "PASS"

    if segments and duration_ms > 0:
        max_end = max(s.end_ms for s in segments)
        if max_end > duration_ms + 2000:
            checks["max_end_in_bounds"] = f"FAIL: max_end={max_end} > duration={duration_ms}"
            passed = False
        else:
            checks["max_end_in_bounds"] = "PASS"
    else:
        checks["max_end_in_bounds"] = "SKIP"

    if diarization_requested:
        if len(speakers) < 1:
            checks["diarization_present"] = "FAIL: 0 speakers (diarization requested)"
            passed = False
        else:
            checks["diarization_present"] = f"PASS: {len(speakers)}"
    else:
        checks["diarization_present"] = "SKIP"

    checks["segments_present"] = f"PASS: {len(segments)}" if segments else "WARN: 0 segments"
    return QAResult(passed=passed, checks=checks)


# ─── LLM-ready views ───────────────────────────────────────────────────


def render_markdown(transcript: UnifiedTranscript) -> str:
    """Render as Markdown suitable as ``llm.summarize`` text input."""
    spk_names = {sp.speaker_id: (sp.name or sp.speaker_id) for sp in transcript.speakers}
    duration_str = _format_ms(transcript.duration_ms)
    lang = transcript.language.detected or transcript.language.routed or "?"
    lines = [
        "# Transcript",
        f"Duration: {duration_str} | Language: {lang} | Speakers: {len(transcript.speakers)}",
        "",
    ]
    for seg in transcript.segments:
        spk = spk_names.get(seg.speaker_id or "", seg.speaker_id or "Unknown")
        lines.append(f"[{_format_ms(seg.start_ms)}] {spk}: {seg.text}")
    return "\n".join(lines)


def build_summary_input(
    transcript: UnifiedTranscript,
    *,
    tokens_per_chunk: int = 15_000,
    overlap_segments: int = 1,
) -> dict:
    """Chunk the transcript for map-reduce summarization (~4 chars/token),
    each chunk carrying a ``seg_id -> start_ms`` anchor map."""
    max_chars = tokens_per_chunk * 4
    spk_names = {sp.speaker_id: (sp.name or sp.speaker_id) for sp in transcript.speakers}

    chunks: list[dict] = []
    buf_segs: list[UnifiedSegment] = []
    buf_chars = 0

    def flush(segs: list[UnifiedSegment]) -> None:
        if not segs:
            return
        anchors: dict[str, int] = {}
        lines: list[str] = []
        for s in segs:
            spk = spk_names.get(s.speaker_id or "", s.speaker_id or "Unknown")
            lines.append(f"[{_format_ms(s.start_ms)}] {spk}: {s.text}")
            anchors[s.id] = s.start_ms
        chunks.append({"text": "\n".join(lines), "anchors": anchors})

    for seg in transcript.segments:
        seg_chars = len(seg.text) + 30
        if buf_chars + seg_chars > max_chars and buf_segs:
            flush(buf_segs)
            buf_segs = buf_segs[-overlap_segments:] if overlap_segments else []
            buf_chars = sum(len(s.text) + 30 for s in buf_segs)
        buf_segs.append(seg)
        buf_chars += seg_chars
    flush(buf_segs)

    return {
        "meta": {
            "meeting_id": transcript.meeting_id,
            "schema_version": transcript.schema_version,
            "language": transcript.language.routed,
            "duration_ms": transcript.duration_ms,
            "speakers": [{"speaker_id": s.speaker_id, "name": s.name} for s in transcript.speakers],
            "total_segments": len(transcript.segments),
            "chunks_count": len(chunks),
        },
        "chunks": chunks,
    }


# ─── Version key ───────────────────────────────────────────────────────
#
# Derived work needs to say which transcript it was built from: a summary, an
# LLM extraction whose evidence anchors point at turn indices, a user's edit
# log. ``updated_at`` cannot answer that (it moves when nothing meaningful
# changed) and a revision counter cannot either. A hash of the content can —
# but only if "the content" is decided deliberately.
#
# The criterion here is: **content is what the model saw, plus what an anchor
# indexes into.** Everything the transcript carries for other reasons —
# provenance, quality verdicts, colours, the recording's own bookkeeping — is
# not content, because changing it does not move a single turn and must not
# invalidate a single derived artifact.
#
# Getting this wrong is not a loud failure. Fold ``updated_at`` into the hash
# and the key changes on every save, so every summary and every user
# correction reads as stale forever, with nothing in the logs to say why. Fold
# in ``title`` and renaming a meeting throws away its extraction. That is the
# exact shape of the bug this classification exists to prevent, and it is why
# both halves are listed explicitly below: a field added to the schema with no
# decision recorded fails ``test_version_key`` rather than defaulting into
# whichever half the author happened not to think about.

#: Scalars of UnifiedTranscript that are content.
_CONTENT_TRANSCRIPT_SCALARS = frozenset({"schema_version", "meeting_id", "duration_ms"})

#: Fields of UnifiedTranscript that are content but need their own projection.
_CONTENT_TRANSCRIPT_COMPOSITES = frozenset({"language", "speakers", "segments"})

#: Fields of UnifiedTranscript deliberately outside the key.
#: ``engine`` — which ASR produced this. Re-running a different provider yields
#:   different segments, so a real re-transcription already changes the key
#:   through them; the label alone is provenance.
#: ``qa`` — a verdict *about* the transcript, computed from it. Hashing it
#:   would let a change in the QA rules invalidate untouched transcripts.
_NON_CONTENT_TRANSCRIPT = frozenset({"engine", "qa"})

#: ``path`` records *how* the language was decided ("A" auto / "B" user), which
#: never reaches the model; ``routed``/``detected`` are rendered in the header.
_CONTENT_LANGUAGE = frozenset({"routed", "detected"})
_NON_CONTENT_LANGUAGE = frozenset({"path"})

#: ``name`` is content because it is rendered in place of the label — and
#: because renaming a speaker is precisely the kind of user edit that must
#: invalidate a summary quoting them. ``color`` is presentation; ``db_id`` is a
#: join key that never reaches the model.
_CONTENT_SPEAKER = frozenset({"speaker_id", "name"})
_NON_CONTENT_SPEAKER = frozenset({"db_id", "color"})

#: ``words`` is excluded: the word grid is never rendered and no anchor points
#: into it, so re-aligning words leaves every turn anchor valid.
#: TODO(word-level edits): a future split-segment edit operates on the word
#: grid; when that ships, ``words`` becomes content and the key changes for
#: every transcript that has one — a migration, not a patch.
#: ``lang`` is per-segment detection metadata, not rendered.
_CONTENT_SEGMENT = frozenset({"id", "start_ms", "end_ms", "speaker_id", "text"})
_NON_CONTENT_SEGMENT = frozenset({"words", "lang"})


def _project(obj, field_names: frozenset) -> dict:
    """Pick exactly ``field_names`` off ``obj``, key-sorted.

    Driven by the same frozensets the guard test checks, so the projection and
    the classification cannot drift apart — a field marked as content that the
    builder forgot is not a possible state.
    """
    return {name: getattr(obj, name) for name in sorted(field_names)}


def transcript_content(transcript: UnifiedTranscript) -> dict:
    """The content projection of ``transcript`` — the input to its version key.

    Public because a digest tells you *that* two transcripts differ and nothing
    about *where*; when a key changes unexpectedly, diffing two projections is
    how you find out which turn moved.
    """
    return {
        **_project(transcript, _CONTENT_TRANSCRIPT_SCALARS),
        "language": _project(transcript.language, _CONTENT_LANGUAGE),
        "speakers": [_project(s, _CONTENT_SPEAKER) for s in transcript.speakers],
        "segments": [_project(s, _CONTENT_SEGMENT) for s in transcript.segments],
    }


def transcript_hash(transcript: UnifiedTranscript) -> str:
    """Stable ``sha256:<hex>`` identifying this transcript's content.

    Equal keys mean derived work is still about this transcript. Unequal means
    it is not, and must be recomputed or flagged — a deterministic comparison,
    never a heuristic about how much changed.
    """
    return canonical_hash(transcript_content(transcript))


# ─── Helpers ───────────────────────────────────────────────────────────


def _to_ms(seconds: float) -> int:
    return int(round(float(seconds or 0) * 1000))


def _format_ms(ms: int) -> str:
    total_sec = ms // 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}"


__all__ = [
    "SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "UnifiedTranscript",
    "UnifiedSegment",
    "UnifiedSpeaker",
    "UnifiedWord",
    "from_db_segments",
    "run_qa",
    "render_markdown",
    "build_summary_input",
    "transcript_content",
    "transcript_hash",
]
