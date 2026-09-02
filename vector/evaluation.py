"""Offline retrieval evaluation — recall@k and MRR over a labeled question set.

Every knob in the search layer (which arm, which fusion weights, which
chunking scheme, which FTS query shape, whether a reranker earns its
latency) is a ranking question, and a ranking question cannot be settled by
reading the diff or by trying three queries by hand. Until there is a
number, "better" is an opinion and every such change ships on faith. This
module is where the number comes from.

**Labels are time spans, not row ids.** A question is labeled with
``(recording_id, start, end)`` — "the answer is in this recording between
1:04 and 1:48". A hit counts as relevant when its segment's time range
overlaps a labeled span of the same recording. Ids would have been easier
and would have been wrong: re-chunking the index changes which row carries
a passage, so an id-labeled set silently stops measuring the moment the
thing it exists to measure changes. A clock reading survives re-embedding,
re-chunking, even re-transcription.

**Metrics.** Per question over the ranked hit list:

- ``recall@k`` — 1.0 if any relevant hit appears in the top *k*, else 0.0.
  Set-level recall (what fraction of relevant spans were found) is the
  wrong question for a QA pipeline that reads the top few excerpts: one
  good excerpt is enough to answer from, and finding a second copy of the
  same fact is worth nothing.
- ``MRR`` — ``1 / rank`` of the FIRST relevant hit, 0 when none. This is
  the one that moves when ranking (as opposed to retrieval) changes, and
  the one to watch before deciding a reranker is needed.

The suite reports both, per language and overall, and prints per-question
rows so a regression can be traced to the question that caused it rather
than to a moved average.

Nothing here calls a model except through the ordinary search path, so an
eval run measures exactly what production does — the same code, the same
settings, the same embedder.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Span:
    """A labeled answer location: a time range inside one recording."""

    recording_id: str
    start: float = 0.0
    end: float = 0.0

    def covers(self, recording_id: str, start: float, end: float) -> bool:
        """True when [start, end] overlaps this span in this recording.

        A zero-length label (``start == end == 0``) means "anywhere in this
        recording" — the coarse label, useful for a short recording that is
        about one thing."""
        if str(recording_id) != str(self.recording_id):
            return False
        if self.end <= self.start:
            return True
        return start <= self.end and end >= self.start


@dataclass(frozen=True)
class Question:
    """One labeled query."""

    id: str
    query: str
    relevant: list = field(default_factory=list)
    lang: str = ""
    workspace_id: object = None
    note: str = ""


@dataclass(frozen=True)
class QuestionResult:
    """What one question scored, and where its first relevant hit landed."""

    question: Question
    first_relevant_rank: int | None
    n_hits: int
    top_snippet: str = ""

    def recall_at(self, k: int) -> float:
        rank = self.first_relevant_rank
        return 1.0 if rank is not None and rank <= k else 0.0

    @property
    def reciprocal_rank(self) -> float:
        rank = self.first_relevant_rank
        return 1.0 / rank if rank else 0.0


def load_questions(path: str) -> list[Question]:
    """Read a labeled question set.

    Format (JSON)::

        {"name": "...",
         "questions": [
           {"id": "q01", "lang": "ru", "query": "...",
            "workspace_id": "<uuid>",
            "relevant": [{"recording_id": "<uuid>", "start": 64, "end": 108}],
            "note": "why this is the answer (for the human, not the metric)"}
         ]}
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    raw = payload["questions"] if isinstance(payload, dict) else payload
    questions = []
    for item in raw:
        questions.append(
            Question(
                id=str(item["id"]),
                query=str(item["query"]),
                lang=str(item.get("lang") or ""),
                workspace_id=item.get("workspace_id"),
                note=str(item.get("note") or ""),
                relevant=[
                    Span(
                        recording_id=str(span["recording_id"]),
                        start=float(span.get("start") or 0.0),
                        end=float(span.get("end") or 0.0),
                    )
                    for span in item.get("relevant") or []
                ],
            )
        )
    if not questions:
        raise ValueError(f"{path}: no questions in the set")
    unlabeled = [q.id for q in questions if not q.relevant]
    if unlabeled:
        raise ValueError(
            f"{path}: questions with no labeled answer span: "
            f"{', '.join(unlabeled)} — an unlabeled question scores 0 for "
            "every configuration and only drags the average"
        )
    return questions


def _hit_locations(hits) -> list[tuple[str, float, float]]:
    """``(recording_id, start_time, end_time)`` per hit, in rank order.

    A hit names a segment; a window hit names the segment it is anchored
    at and covers ``span`` of them, so the range asked about is the range
    the row actually stands for."""
    from stapel_recordings.models import Segment

    ids = [h.segment_id for h in hits]
    if not ids:
        return []
    rows = {
        row["id"]: row
        for row in Segment.objects.filter(id__in=ids).values(
            "id", "recording_id", "sequence_num", "start_time", "end_time"
        )
    }
    spans = {}
    try:
        from stapel_recordings.conf import vector_config
        from stapel_recordings.vector.models import SegmentEmbedding

        # ONLY the scheme search is actually reading. Without this filter,
        # building a window index silently widens the time range credited
        # to every segment-scheme hit that happens to also be a window
        # anchor — the instrument would report the new scheme's coverage
        # for the old scheme's results and make the comparison meaningless.
        # The widest span anchored there: several windows can share an
        # anchor, a SearchHit names only the segment, and crediting the
        # narrower one would score a hit the retriever did not make.
        for seg_id, span in SegmentEmbedding.objects.filter(
            segment_id__in=ids,
            scheme=str(vector_config().get("SEGMENT_SCHEME") or "segment"),
        ).exclude(span=1).values_list("segment_id", "span"):
            spans[seg_id] = max(spans.get(seg_id, 1), int(span))
    except Exception as exc:  # pragma: no cover - vector app not installed
        logger.debug("no span information available: %s", exc)

    out = []
    for hit in hits:
        row = rows.get(hit.segment_id)
        if row is None:
            continue
        end = float(row["end_time"] or 0.0)
        span = int(spans.get(hit.segment_id) or 1)
        if span > 1:
            # The window runs to the end of the last utterance it covers.
            last = (
                Segment.objects.filter(
                    recording_id=row["recording_id"],
                    sequence_num__gte=row["sequence_num"],
                    sequence_num__lt=row["sequence_num"] + span,
                )
                .order_by("-sequence_num")
                .values_list("end_time", flat=True)
                .first()
            )
            end = float(last or end)
        out.append((str(row["recording_id"]), float(row["start_time"] or 0.0), end))
    return out


def evaluate_question(question: Question, *, mode: str, limit: int) -> QuestionResult:
    """Run one question through the ordinary search path and locate its
    first relevant hit."""
    from .search import search_recordings

    hits = search_recordings(
        question.query,
        mode=mode,
        limit=limit,
        workspace_id=question.workspace_id,
    )
    first = None
    for rank, (rec_id, start, end) in enumerate(_hit_locations(hits), start=1):
        if any(span.covers(rec_id, start, end) for span in question.relevant):
            first = rank
            break
    return QuestionResult(
        question=question,
        first_relevant_rank=first,
        n_hits=len(hits),
        top_snippet=hits[0].snippet if hits else "",
    )


def summarize(results: list[QuestionResult], *, ks=(1, 5, 10)) -> dict:
    """Aggregate per-question results into the report's numbers."""

    def block(subset: list[QuestionResult]) -> dict:
        if not subset:
            return {"n": 0}
        out: dict = {"n": len(subset)}
        for k in ks:
            out[f"recall@{k}"] = sum(r.recall_at(k) for r in subset) / len(subset)
        out["mrr"] = sum(r.reciprocal_rank for r in subset) / len(subset)
        out["found"] = sum(1 for r in subset if r.first_relevant_rank)
        return out

    summary = {"overall": block(results), "by_lang": {}}
    for lang in sorted({r.question.lang for r in results if r.question.lang}):
        summary["by_lang"][lang] = block(
            [r for r in results if r.question.lang == lang]
        )
    return summary


def run_evaluation(questions: list[Question], *, mode: str = "hybrid", limit: int = 10):
    """Evaluate every question; returns ``(results, summary)``."""
    results = [
        evaluate_question(q, mode=mode, limit=limit) for q in questions
    ]
    return results, summarize(results)


def format_report(results: list[QuestionResult], summary: dict, *, label: str) -> str:
    """A plain-text report: the aggregate, then every question, so a
    regression points at the question that caused it."""
    lines = [f"=== {label} ===", ""]
    overall = summary["overall"]
    lines.append(
        f"  overall  n={overall['n']}  found={overall.get('found', 0)}  "
        + "  ".join(
            f"{key}={overall[key]:.3f}"
            for key in sorted(overall)
            if key.startswith("recall@") or key == "mrr"
        )
    )
    for lang, block in summary["by_lang"].items():
        lines.append(
            f"  {lang:<8} n={block['n']}  found={block.get('found', 0)}  "
            + "  ".join(
                f"{key}={block[key]:.3f}"
                for key in sorted(block)
                if key.startswith("recall@") or key == "mrr"
            )
        )
    lines.append("")
    lines.append(f"  {'question':<10} {'lang':<5} {'rank':>5} {'hits':>5}  query")
    for result in results:
        rank = result.first_relevant_rank
        lines.append(
            f"  {result.question.id:<10} {result.question.lang:<5} "
            f"{(str(rank) if rank else '—'):>5} {result.n_hits:>5}  "
            f"{result.question.query[:56]}"
        )
    return "\n".join(lines)


__all__ = [
    "Span",
    "Question",
    "QuestionResult",
    "load_questions",
    "evaluate_question",
    "run_evaluation",
    "summarize",
    "format_report",
]
