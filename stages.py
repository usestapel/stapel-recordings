"""Pipeline stages + the open stage registry.

A stage is a swappable unit of pipeline work with a small contract:

    class Stage:
        name: str            # registry key
        status: str          # RecordingStatus set by the driver while running
        def run(self, recording, ctx: dict) -> dict: ...

``run`` does the work (mutating/saving the recording as needed) and returns
the context dict passed to the next stage. It raises :class:`StageRetryable`
(transient — the driver counts the attempt and lets reconcile re-drive) or
:class:`StageFatal` (bad input — straight to DLQ). Stages MUST be idempotent
(guard on status / persisted keys) because delivery is at-least-once.

The four built-ins — ``convert``, ``transcribe``, ``diarize``, ``merge`` —
are registered here. Hosts customize the pipeline three ways, all
fork-free:

1. Reorder / subset / extend the stage list: ``STAPEL_RECORDINGS["PIPELINE"]``
   (or a ``PIPELINE_RESOLVER`` for runtime/per-recording lists).
2. Replace or remove a built-in, or add a new named stage, via the
   ``STAPEL_RECORDINGS["STAGES"]`` overlay (``{name: dotted-path | None}``)
   — merge-over-builtins, the same semantics as the other Stapel registries.
3. Register a stage at runtime: ``register_stage("redact_pii", handler)``.

``transcribe`` and ``summarize`` (inside ``merge``) delegate to stapel-agent
via the ``llm.transcribe`` / ``llm.summarize`` comm Functions — recordings
does NOT implement STT or summarization. ``diarize`` is a no-op by default
because diarization is returned inline by ``llm.transcribe``; it stays in
the pipeline so hosts can swap in a real diarizer without touching the list.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Callable

from .conf import recordings_settings
from .models import RecordingStatus, Segment, Speaker
from .storage import get_storage

logger = logging.getLogger(__name__)


# ─── Stage contract + signals ──────────────────────────────────────────


class StageError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class StageRetryable(StageError):
    """Transient failure — count the attempt, retry / reconcile."""


class StageFatal(StageError):
    """Permanent failure — DLQ, no retry."""


class Stage:
    """Base class. Subclass and implement :meth:`run`, or register any
    ``callable(recording, ctx) -> ctx`` — it is adapted automatically."""

    name: str = ""
    status: str = ""

    def run(self, recording, ctx: dict) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


class _CallableStage(Stage):
    """Adapter so a plain function can be registered as a stage."""

    def __init__(self, func: Callable, *, name: str = "", status: str = ""):
        self._func = func
        self.name = name or getattr(func, "__name__", "")
        self.status = status

    def run(self, recording, ctx: dict) -> dict:
        result = self._func(recording, ctx)
        return result if isinstance(result, dict) else ctx


# ─── Built-in stages ───────────────────────────────────────────────────


def _key(recording, suffix: str) -> str:
    prefix = recordings_settings.STORAGE_PREFIX.strip("/")
    return f"{prefix}/{recording.workspace_id}/{recording.id}/{suffix}"


class ConvertStage(Stage):
    """Normalize the uploaded media to canonical STT input via the
    NORMALIZER seam; store it, drop the raw original."""

    name = "convert"
    status = RecordingStatus.NORMALIZING

    def run(self, recording, ctx):
        from .normalize import NormalizeFatal

        if recording.normalized_storage_key:
            return ctx  # idempotent: already converted
        if not recording.file_storage_key:
            raise StageFatal("missing_raw_storage_key")

        storage = get_storage()
        normalizer = recordings_settings.NORMALIZER
        workdir = tempfile.mkdtemp(prefix="rec-convert-")
        raw_path = os.path.join(workdir, "raw.bin")
        out_path = os.path.join(workdir, "normalized.wav")
        try:
            try:
                storage.download_to_file(recording.file_storage_key, raw_path)
            except Exception as exc:  # transient object-store failure
                raise StageRetryable("download_failed", str(exc)) from exc

            try:
                duration = normalizer(raw_path, out_path)
            except NormalizeFatal as exc:
                raise StageFatal(exc.reason, exc.detail) from exc

            normalized_key = _key(recording, "audio.normalized.wav")
            if normalized_key == recording.file_storage_key:
                raise StageFatal("key_collision", normalized_key)
            try:
                storage.upload_from_file(normalized_key, out_path, content_type="audio/wav")
            except Exception as exc:
                raise StageRetryable("upload_failed", str(exc)) from exc

            recording.normalized_storage_key = normalized_key
            if duration:
                recording.duration_seconds = duration
            recording.save(update_fields=["normalized_storage_key", "duration_seconds", "updated_at"])

            try:
                storage.delete_object(recording.file_storage_key)
            except Exception:
                logger.warning("convert: could not delete raw for %s", recording.id, exc_info=True)
            return ctx
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class TranscribeStage(Stage):
    """Call ``llm.transcribe`` (stapel-agent) and persist Speaker/Segment
    rows. STT provider selection/fallback lives in the agent."""

    name = "transcribe"
    status = RecordingStatus.TRANSCRIBING

    def run(self, recording, ctx):
        from stapel_core.comm import call
        from stapel_core.comm.exceptions import CommError

        if recording.segments.exists():
            return ctx  # idempotent: already transcribed

        storage_key = recording.normalized_storage_key or recording.file_storage_key
        if not storage_key:
            raise StageFatal("no_storage_key")

        storage = get_storage()
        audio_url = storage.presigned_get_url(storage_key, expires_seconds=3600)

        payload = {
            "audio_url": audio_url,
            "diarization": bool(recording.diarization_enabled),
            "timeout_seconds": int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS),
        }
        if recording.language:
            payload["language"] = recording.language
        provider = recording.provider_override
        if provider:
            payload["provider"] = provider

        try:
            result = call("llm.transcribe", payload)
        except CommError as exc:
            raise StageRetryable("transcribe_call_failed", str(exc)) from exc

        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (result or {}).get("reason", "transcribe_failed") if isinstance(result, dict) else "transcribe_failed"
            raise StageRetryable("transcribe_failed", str(reason))

        _persist_transcript(
            recording,
            result.get("transcript") or {},
            provider_used=result.get("provider_used"),
            fallback_used=bool(result.get("fallback_used")),
        )
        return ctx


class DiarizeStage(Stage):
    """No-op by default: diarization is returned inline by ``llm.transcribe``.
    Kept in the pipeline so hosts can swap in a dedicated diarizer without
    editing the stage list."""

    name = "diarize"
    status = RecordingStatus.DIARIZING

    def run(self, recording, ctx):
        return ctx


class MergeStage(Stage):
    """Finalize: build + store the unified transcript JSON and (optionally)
    the summary via ``llm.summarize`` (stapel-agent)."""

    name = "merge"
    status = RecordingStatus.MERGING

    def run(self, recording, ctx):
        from . import transcript_schema

        if recording.transcript_storage_key:
            return ctx  # idempotent

        transcript = transcript_schema.from_db_segments(recording)
        storage = get_storage()
        key = _key(recording, "transcript.json")
        try:
            storage.put_bytes(
                key, transcript.to_json().encode("utf-8"), content_type="application/json"
            )
        except Exception as exc:
            raise StageRetryable("transcript_store_failed", str(exc)) from exc
        recording.transcript_storage_key = key

        if recordings_settings.SUMMARIZE_ENABLED and transcript.segments:
            summary = _summarize(recording, transcript)
            if summary is not None:
                recording.summary = summary

        recording.save(update_fields=["transcript_storage_key", "summary", "updated_at"])
        return ctx


# ─── stage helpers ─────────────────────────────────────────────────────


def _summarize(recording, transcript) -> str | None:
    """Best-effort call to ``llm.summarize``. A summary failure must not
    fail the recording — the transcript is the primary artifact."""
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import CommError

    from . import transcript_schema

    payload = {"text": transcript_schema.render_markdown(transcript), "model": recordings_settings.SUMMARIZE_MODEL}
    if recording.language:
        payload["language"] = recording.language
    try:
        result = call("llm.summarize", payload)
    except CommError:
        logger.warning("merge: summarize call failed for %s", recording.id, exc_info=True)
        return None
    if isinstance(result, dict) and result.get("status") == "ok":
        return result.get("summary")
    logger.warning("merge: summarize returned failure for %s", recording.id)
    return None


def _persist_transcript(recording, transcript: dict, *, provider_used, fallback_used) -> None:
    """Write Speaker/Segment rows from an ``llm.transcribe`` result dict and
    denormalize counters onto the Recording."""
    from django.db import transaction

    words = transcript.get("words") or []
    utterances = transcript.get("utterances") or _utterances_from_words(words)
    speakers_detected = transcript.get("speakers_detected") or []
    language = transcript.get("language")
    duration = transcript.get("duration_seconds")

    with transaction.atomic():
        speaker_map: dict[str, Speaker] = {}
        for idx, label in enumerate(speakers_detected):
            speaker_map[label] = Speaker.objects.create(
                recording=recording, label=label, color=Speaker.color_for_index(idx)
            )

        objs = []
        word_count = 0
        for idx, utt in enumerate(utterances):
            indexes = utt.get("word_indexes") or []
            n_words = len(indexes) or len(utt.get("text", "").split())
            word_count += n_words
            words_json = []
            for wi in indexes:
                if 0 <= wi < len(words):
                    w = words[wi]
                    words_json.append({
                        "w": w.get("text", ""),
                        "start_ms": int(round(float(w.get("start") or 0) * 1000)),
                        "end_ms": int(round(float(w.get("end") or 0) * 1000)),
                        "conf": w.get("confidence"),
                    })
            speaker_label = utt.get("speaker")
            objs.append(Segment(
                recording=recording,
                speaker=speaker_map.get(speaker_label) if speaker_label else None,
                sequence_num=idx,
                start_time=float(utt.get("start") or 0),
                end_time=float(utt.get("end") or 0),
                text=utt.get("text", ""),
                confidence=utt.get("confidence"),
                word_count=n_words,
                language=language,
                words_json=words_json,
            ))
        Segment.objects.bulk_create(objs)

        recording.language = language or recording.language
        if duration:
            recording.duration_seconds = duration
        recording.segments_count = len(objs)
        recording.speakers_count = len(speaker_map)
        recording.word_count = word_count
        recording.provider_used = provider_used
        recording.fallback_used = fallback_used
        recording.save(update_fields=[
            "language", "duration_seconds", "segments_count", "speakers_count",
            "word_count", "provider_used", "fallback_used", "updated_at",
        ])


def _utterances_from_words(words: list[dict]) -> list[dict]:
    """Group consecutive same-speaker words into utterance dicts (for
    providers that ship word-level output only)."""
    if not words:
        return []
    grouped: list[dict] = []
    buf_text: list[str] = []
    buf_idx: list[int] = []
    buf_start = words[0].get("start") or 0
    buf_end = words[0].get("end") or 0
    buf_speaker = words[0].get("speaker")

    def flush():
        grouped.append({
            "text": " ".join(buf_text).strip(),
            "start": buf_start,
            "end": buf_end,
            "speaker": buf_speaker,
            "word_indexes": list(buf_idx),
        })

    for i, w in enumerate(words):
        if w.get("speaker") != buf_speaker and buf_text:
            flush()
            buf_text, buf_idx = [], []
            buf_start = w.get("start") or 0
            buf_speaker = w.get("speaker")
        buf_text.append(w.get("text", ""))
        buf_idx.append(i)
        buf_end = w.get("end") or 0
    flush()
    return grouped


# ─── Registry (merge-over-builtins) ────────────────────────────────────

BUILTIN_STAGES: dict[str, object] = {
    "convert": ConvertStage,
    "transcribe": TranscribeStage,
    "diarize": DiarizeStage,
    "merge": MergeStage,
}

_runtime_stages: dict[str, object] = {}


def register_stage(name: str, handler) -> None:
    """Register/replace a stage at runtime. ``handler`` is a Stage class,
    a Stage instance, or a ``callable(recording, ctx) -> ctx``. Pass
    ``None`` to remove a built-in. Merge-over-builtins, like the other
    Stapel open registries."""
    _runtime_stages[name] = handler


def unregister_stage(name: str) -> None:
    _runtime_stages.pop(name, None)


def reset_runtime_stages() -> None:
    """Tests only."""
    _runtime_stages.clear()


def resolve_stages() -> dict[str, object]:
    """Merged stage map: built-ins, then the STAGES setting overlay
    (dotted-paths; ``None`` removes), then runtime registrations."""
    from django.utils.module_loading import import_string

    merged: dict[str, object] = dict(BUILTIN_STAGES)
    overlay = recordings_settings.STAGES or {}
    for name, path in overlay.items():
        if path is None:
            merged.pop(name, None)
        else:
            merged[name] = import_string(path) if isinstance(path, str) else path
    for name, handler in _runtime_stages.items():
        if handler is None:
            merged.pop(name, None)
        else:
            merged[name] = handler
    return merged


def get_stage(name: str) -> Stage:
    """Resolve *name* to a ready-to-run Stage instance."""
    stages = resolve_stages()
    if name not in stages:
        raise KeyError(f"stage {name!r} is not registered")
    return _as_stage(name, stages[name])


def _as_stage(name: str, obj) -> Stage:
    if isinstance(obj, Stage):
        return obj
    if isinstance(obj, type) and issubclass(obj, Stage):
        return obj()
    if callable(obj):
        return _CallableStage(obj, name=name)
    raise TypeError(f"stage {name!r} handler is not a Stage/class/callable: {obj!r}")


__all__ = [
    "Stage",
    "StageError",
    "StageRetryable",
    "StageFatal",
    "ConvertStage",
    "TranscribeStage",
    "DiarizeStage",
    "MergeStage",
    "BUILTIN_STAGES",
    "register_stage",
    "unregister_stage",
    "reset_runtime_stages",
    "resolve_stages",
    "get_stage",
]
