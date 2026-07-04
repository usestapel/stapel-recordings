"""Unified transcript schema.

Single canonical JSON for a completed recording, built from the persisted
Segment/Speaker rows (``from_db_segments``) and stored via the STORAGE
seam at ``<prefix>/<id>/transcript.json``. ``render_markdown`` /
``build_summary_input`` prepare LLM-ready views for the summarize step
(which delegates to stapel-agent's ``llm.summarize``).

Ported from the legacy recordings service ``recordings/transcript_schema.py``; the
provider-facing ``from_normalized`` mapper is dropped because STT now
lives in stapel-agent — recordings persists Segment rows directly from
the ``llm.transcribe`` result dict (see ``stages.TranscribeStage``).

Invariants (checked by ``run_qa``): monotonic start/end ms; max end <=
duration (+tolerance); >=1 speaker when diarization requested.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

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
    speakers_qs = list(recording.speakers.all())

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
]
