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

The five built-ins — ``convert``, ``transcribe``, ``diarize``, ``merge``,
``embed`` — are registered here. Hosts customize the pipeline three ways,
all fork-free:

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
``embed`` follows the same pattern: a no-op unless the opt-in
``stapel_recordings.vector`` app is installed AND ``VECTOR["ENABLED"]`` is
on — then it delegates to ``llm.embed`` (stapel-agent) and persists segment
/ summary embeddings for the hybrid search service (``vector/search.py``).

WHO THE WORK IS FOR TRAVELS WITH IT
-----------------------------------
Every delegated payload carries ``user_id`` / ``workspace_id`` from the
recording being processed. Not as telemetry decoration: the agent writes one
ledger row per provider call, and those rows were the only place the money
was visible while being unattributable — a pipeline stage is nobody's
request, so the id had to come from the row it is working on. ``Recording``
already knows both, which is why this needs no new plumbing from the host
and no per-call-site argument.

Requires **stapel-agent >= 0.12.0**, the release that opened those two
optional fields on the ``llm.*`` schemas. Those schemas are
``additionalProperties: false``, so an older agent REJECTS the payload
outright rather than ignoring the extra keys — ``checks.W009`` says so at
boot when it can see the agent's version.
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


class StageAwaiting(StageError):
    """Work was SUBMITTED, the stage is waiting — this is not a failure.

    A third outcome alongside Retryable and Fatal. The stage handed
    long-running work to the Task primitive (``stapel_core.comm.tasks``) and
    returns control immediately: the driver remembers ``task_id``, leaves
    the stage incomplete, and does NOT count this as an attempt. Resumption
    arrives as a ``task.completed`` Action.

    Why: long-running work used to be a synchronous Function call, with the
    caller holding a worker and no reliable way to know how long to wait. A
    queue breaks that model entirely — if no worker is free, a synchronous
    call must either lie with a timeout or hang.

    With a task, state is observable: ``status(task_id)`` returns
    pending/running/done/failed and an attempt count, so the frontend has
    something to show — "queued", not a spinner with no promises.
    """

    def __init__(self, task_id: str, kind: str):
        super().__init__("awaiting_task", f"{kind}:{task_id}")
        self.task_id = task_id
        self.kind = kind


class Stage:
    """Base class. Subclass and implement :meth:`run`, or register any
    ``callable(recording, ctx) -> ctx`` — it is adapted automatically.

    A stage that hands work to the Task primitive splits in two:
    :meth:`run` submits the task and raises :class:`StageAwaiting`, while
    :meth:`resume` receives the result once it arrives. ``resume`` is
    deliberately not abstract — stages without long-running work simply
    don't override it.
    """

    name: str = ""
    status: str = ""

    def run(self, recording, ctx: dict) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def resume(self, recording, ctx: dict, result) -> dict:
        """Complete the stage from a task's result.

        Called by the driver on ``task.completed`` for a stage that
        previously raised :class:`StageAwaiting`. Not overriding this is a
        programmer error: a stage that submits a task must know how to
        accept its result.
        """
        raise NotImplementedError(
            f"stage {self.name!r} submitted a task but cannot accept its result"
        )


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


def identity_fields(user_id=None, workspace_id=None) -> dict:
    """The ``{user_id?, workspace_id?}`` block the ``llm.*`` schemas accept.

    Keys are OMITTED rather than sent as null when absent: the schemas type
    both as strings, and they are ``additionalProperties: false``, so a null
    would be rejected where a missing key is fine. Ids are stringified
    because hosts number their subjects differently (int pk here, UUID
    workspace) and the ledger columns are text.

    One constructor so the shape cannot drift between the pipeline, the
    vector layer and whatever a host adds next.
    """
    fields = {}
    if user_id is not None:
        fields["user_id"] = str(user_id)
    if workspace_id is not None:
        fields["workspace_id"] = str(workspace_id)
    return fields


def identity_payload(recording) -> dict:
    """Who a delegated AI call is for, read off the recording.

    The agent's ledger has one row per provider call and, until it could be
    told, no way to attribute any of them: a pipeline stage runs on a queue
    long after the request that created the recording, so there is no
    "current user" to read. The recording itself is the only thing that
    knows, and it knows both.

    Public, not private: a host that adds its own stage delegating to
    ``llm.*`` should attribute its calls the same way, and a second
    hand-rolled version of this would be the drift.
    """
    return identity_fields(
        getattr(recording, "owner_id", None),
        getattr(recording, "workspace_id", None),
    )


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


def submit_task(kind, payload, *, recording, deadline_seconds=None, max_attempts=3):
    """Submit long-running work as a task and return control immediately.

    Returns a result ONLY when the deployment dispatches tasks synchronously
    (``STAPEL_COMM["TASK_DISPATCH"]="inline"``: brokerless monolith, tests,
    scripts) and the task already reached ``done`` inside ``start()``.
    Otherwise raises :class:`StageAwaiting`, and the stage continues in
    :meth:`Stage.resume` on ``task.completed``.

    ``correlation_id`` is the recording id: resume uses it to find which
    stage to complete. It's also the event partition key, so a recording's
    events stay in order.
    """
    from stapel_core.comm import start, status
    from stapel_core.comm.exceptions import CommError

    try:
        task_id = start(
            kind,
            payload,
            correlation_id=str(recording.id),
            deadline_seconds=deadline_seconds,
            max_attempts=max_attempts,
        )
    except CommError as exc:
        # Couldn't even SUBMIT the task — a bus availability issue, not a
        # work failure; retryable.
        raise StageRetryable(f"{kind}_submit_failed", str(exc)) from exc

    snapshot = status(task_id)
    if snapshot.state == "done":
        return snapshot.result
    if snapshot.state == "failed":
        raise StageRetryable(f"{kind}_failed", snapshot.error or "task failed")
    raise StageAwaiting(task_id, kind)


class TranscribeStage(Stage):
    """Hand transcription to the ``llm.transcribe`` task (stapel-agent) and
    persist Speaker/Segment from its result. STT provider choice and
    fallback live in the agent."""

    name = "transcribe"
    status = RecordingStatus.TRANSCRIBING

    def run(self, recording, ctx):
        if recording.segments.exists():
            return ctx  # idempotent: already transcribed

        storage_key = recording.normalized_storage_key or recording.file_storage_key
        if not storage_key:
            raise StageFatal("no_storage_key")

        storage = get_storage()
        # With a private bucket this presigned URL is the ONLY way the ASR
        # provider reads the audio, so its TTL is configuration, not a
        # literal: it has to outlive TRANSCRIBE_TIMEOUT_SECONDS (a provider
        # that starts late must still be able to fetch). W007 warns if it
        # does not.
        audio_url = storage.presigned_get_url(
            storage_key,
            expires_seconds=int(recordings_settings.TRANSCRIBE_AUDIO_URL_TTL_SECONDS),
        )

        payload = {
            "audio_url": audio_url,
            "diarization": bool(recording.diarization_enabled),
            "timeout_seconds": int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS),
            **identity_payload(recording),
        }
        if recording.language:
            payload["language"] = recording.language
        provider = recording.provider_override
        if provider:
            payload["provider"] = provider

        # A TASK, NOT A SYNCHRONOUS CALL. Transcription takes minutes, or
        # arbitrarily longer under busy workers; holding a worker to wait
        # for it is unfair to both the system and the person watching.
        # `submit_task` returns control right away, and the stage completes
        # in :meth:`resume` once the result arrives.
        result = submit_task(
            "llm.transcribe",
            payload,
            recording=recording,
            deadline_seconds=int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS),
        )
        # Reached only under synchronous dispatch (TASK_DISPATCH="inline" —
        # brokerless monolith, tests, scripts): the task already completed
        # by the time start() returns.
        return self.resume(recording, ctx, result)

    def resume(self, recording, ctx, result):
        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (
                (result or {}).get("reason", "transcribe_failed")
                if isinstance(result, dict) else "transcribe_failed"
            )
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

        if recording.transcript_storage_key and recording.summary:
            return ctx  # idempotent

        transcript = transcript_schema.from_db_segments(recording)
        if not recording.transcript_storage_key:
            storage = get_storage()
            key = _key(recording, "transcript.json")
            try:
                storage.put_bytes(
                    key, transcript.to_json().encode("utf-8"), content_type="application/json"
                )
            except Exception as exc:
                raise StageRetryable("transcript_store_failed", str(exc)) from exc
            recording.transcript_storage_key = key
            recording.save(update_fields=["transcript_storage_key", "updated_at"])

        if not (recordings_settings.SUMMARIZE_ENABLED and transcript.segments):
            return ctx

        # The transcript is already saved — it's the main artifact. The
        # summary runs as a SEPARATE task: it can take tens of seconds, and
        # there's no reason to hold a worker for it.
        result = submit_task(
            "llm.summarize",
            _summarize_payload(recording, transcript),
            recording=recording,
            deadline_seconds=int(recordings_settings.SUMMARIZE_TIMEOUT_SECONDS),
        )
        return self.resume(recording, ctx, result)

    def resume(self, recording, ctx, result):
        # The summary is best-effort: the transcript already exists, and the
        # recording must not fail because of it. But staying SILENT about a
        # failure is also wrong — that used to be exactly what happened.
        summary = summary_from_result(result)
        if summary is None:
            logger.warning(
                "merge: summary for %s not produced: %.200s", recording.id, result
            )
        else:
            store_summary(recording, summary)
        return ctx


class EmbedStage(Stage):
    """Persist vector embeddings for the finished transcript (opt-in).

    No-op — exactly the DiarizeStage pattern — unless BOTH hold:

    - the opt-in vector app (``stapel_recordings.vector``) is installed
      (INSTALLED_APPS; needs the ``[vector]`` extra + postgres/pgvector);
    - ``STAPEL_RECORDINGS["VECTOR"]["ENABLED"]`` is True (default False).

    So the stage can sit in the default pipeline at zero cost for hosts
    that don't want vectors. When active it batches segment texts (and the
    chunked summary) through ``llm.embed`` (stapel-agent) and upserts
    ``SegmentEmbedding`` / ``RecordingEmbedding`` rows. Idempotent /
    retry-safe: rows are keyed by a content hash, so a redelivery or a
    partially-failed run only embeds what is missing. ``status`` is empty
    on purpose — no new RecordingStatus enum value, the driver keeps the
    previous status while embed runs (zero migration burden on the base
    app)."""

    name = "embed"
    status = ""

    def run(self, recording, ctx):
        from .conf import vector_config
        from .vector import vector_app_installed

        if not (vector_app_installed() and vector_config()["ENABLED"]):
            return ctx  # opt-in layer absent/off — no-op, like diarize

        from .vector.embedding import embed_recording

        embed_recording(recording)
        return ctx


# ─── stage helpers ─────────────────────────────────────────────────────


def _summarize_payload(recording, transcript) -> dict:
    """Payload for ``llm.summarize``.

    Language comes from the recording, or from what STT detected when
    ``language_mode="auto"`` (filled in during the transcribe stage).
    Without it the model picks its own language, which has produced a
    summary in the wrong language for the conversation — about as useless
    as no summary at all.
    """
    from . import transcript_schema

    payload = {
        "text": transcript_schema.render_markdown(transcript),
        "model": recordings_settings.SUMMARIZE_MODEL,
        **identity_payload(recording),
    }
    language = recording.language or getattr(transcript, "language", "") or ""
    if language:
        payload["language"] = language
    return payload


# ─── the summary as a derived artifact ─────────────────────────────────
#
# A summary is not a fact about the recording, it is a fact about the
# TRANSCRIPT IT WAS BUILT FROM — and transcripts get edited (a speaker
# renamed, a turn reassigned, a word corrected). So storing a summary means
# storing two things: the text, and which transcript produced it. With only
# the text, "is this summary still current" has no answer that is not a
# guess, and the guess a product makes is always "yes".
#
# The version key is ``transcript_schema.transcript_hash`` — the same
# deterministic hash the canonical transcript already has — recorded under
# ``metadata["derived"]["summary"]["transcript_hash"]``. Staleness is then
# COMPUTED (recorded key != current key), not remembered: an edit path that
# never heard of staleness still invalidates the summary, because it changed
# the transcript. The older boolean flag (``metadata["staleness"]["summary"]``)
# is cleared here for readers that only know about the flag.
#
# ``metadata`` and not ``workflow_state`` (audit REC-01 splits them) because
# this receipt is read across the seam — a host's wire serializer answers
# "summary_stale" from it, and consumers of this convention already exist. It
# is a server-written value living in the client's column, which is exactly
# what ``metadata.LIBRARY_RESERVED_KEYS`` is for: both keys are reserved, so
# a client can neither forge freshness nor lose it in a metadata write.

#: Derived-artifact kind for the narrative summary (the key under
#: ``metadata["derived"]`` / ``metadata["staleness"]``).
DERIVED_SUMMARY = "summary"


def summary_from_result(result):
    """The summary text out of an ``llm.summarize`` result, or ``None``.

    One reader for the delegated call's answer, because there are now two
    callers (the ``merge`` stage and the standalone re-summary) and "what
    counts as a produced summary" must not become two slightly different
    truthiness checks.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    summary = result.get("summary")
    return summary if summary else None


def store_summary(recording, summary: str, *, transcript=None) -> None:
    """Persist a produced summary AND pin it to the transcript behind it.

    The sanctioned write: setting ``recording.summary`` alone leaves the
    version key pointing at whatever transcript the PREVIOUS summary was
    built from, so a freshly regenerated summary keeps reading as stale
    forever — the flag says fresh, the key says stale, and the key wins.

    *transcript* is an already-built :class:`~stapel_recordings.transcript_schema.UnifiedTranscript`
    when the caller has one (the merge stage does); otherwise it is rebuilt
    from the persisted rows.
    """
    from . import transcript_schema

    if transcript is None:
        transcript = transcript_schema.from_db_segments(recording)

    metadata = dict(recording.metadata or {})
    derived = dict(metadata.get("derived") or {})
    derived[DERIVED_SUMMARY] = {
        "transcript_hash": transcript_schema.transcript_hash(transcript)
    }
    metadata["derived"] = derived
    staleness = dict(metadata.get("staleness") or {})
    staleness.pop(DERIVED_SUMMARY, None)
    if staleness:
        metadata["staleness"] = staleness
    else:
        metadata.pop("staleness", None)

    recording.summary = summary
    recording.metadata = metadata
    recording.save(update_fields=["summary", "metadata", "updated_at"])


# ─── summarize-only: one recording, no STT, no diarize ─────────────────
#
# The pipeline driver cannot do this job. It refuses to touch a recording in
# a terminal status (``completed`` is terminal), it walks a stage LIST from a
# cursor, and its only "run it again" transition is ``reprocess_recording``,
# which re-runs every stage from zero — a new transcription, a new
# diarization and a new bill for a recording whose transcript is already
# correct and was probably just corrected BY HAND. That is why the only
# regenerate path a product ended up with was staff-only: the cheap version
# of it did not exist.
#
# So the re-summary is its own small runner, not a pipeline entry. It reuses
# the exact ``llm.summarize`` call and the exact storage write the merge
# stage uses (``_summarize_payload`` / :func:`store_summary`) — one summarize
# implementation, two ways in — and tracks its own life on the module's
# ``Job`` ledger rather than on the pipeline cursor, so it can never move a
# recording's status or disturb a run in flight.


class ResummarizeRefused(Exception):
    """Base: this recording cannot be re-summarized right now."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NoTranscriptToSummarize(ResummarizeRefused):
    """The caller's state: there is nothing to summarize yet (409)."""


class SummarizationUnavailable(ResummarizeRefused):
    """The deployment's state: summarization is off or unreachable (503)."""


def _inflight_summarize_jobs(recording_id):
    from .models import Job, JobStatus, JobType

    return (
        Job.objects.select_for_update()
        .filter(
            recording_id=recording_id,
            type=JobType.SUMMARIZE,
            status__in=(JobStatus.QUEUED, JobStatus.PROCESSING),
        )
        .order_by("queued_at")
    )


def start_resummarize(recording, *, user=None):
    """Re-run summarization for ONE recording. Returns ``(job, started)``.

    ``started`` is False when an identical run is already in flight: a second
    request joins the first job instead of paying for a second summary. That
    is the whole idempotency contract — a double-clicked button, a retried
    POST and a client that lost the response all converge on one Job and one
    delegated call.

    Raises :class:`NoTranscriptToSummarize` when the recording has no
    transcript to summarize (nothing has been transcribed, or the merge stage
    has not stored one yet) and :class:`SummarizationUnavailable` when the
    deployment has summaries switched off or the task bus refused the
    submission.

    The recording's ``status`` is never touched: a re-summary is work ABOUT a
    finished recording, and moving a completed recording back into a
    processing status would tell every listing and every client that the
    transcript is in doubt when it is not.
    """
    from django.db import transaction
    from django.utils import timezone

    from . import transcript_schema
    from .conf import flag
    from .models import Job, JobStatus, JobType, Recording

    if not flag("SUMMARIZE_ENABLED"):
        raise SummarizationUnavailable("summarize_disabled")

    with transaction.atomic():
        locked = Recording.objects.select_for_update().filter(pk=recording.pk).first()
        if locked is None:
            raise NoTranscriptToSummarize("recording_gone")

        job = _inflight_summarize_jobs(locked.pk).first()
        if job is not None:
            return job, False

        transcript = transcript_schema.from_db_segments(locked)
        if not locked.transcript_storage_key or not transcript.segments:
            # Both halves matter: rows without a stored transcript means the
            # pipeline has not merged yet, and a stored transcript with no
            # segments is an empty conversation. Either way there is nothing
            # to summarize, and submitting the call anyway would bill for a
            # summary of nothing.
            raise NoTranscriptToSummarize("no_transcript")

        job = Job.objects.create(
            workspace_id=locked.workspace_id,
            owner=user if getattr(user, "is_authenticated", False) else locked.owner,
            recording=locked,
            type=JobType.SUMMARIZE,
            status=JobStatus.QUEUED,
            current_step="summarize",
        )

        try:
            result = submit_task(
                "llm.summarize",
                _summarize_payload(locked, transcript),
                recording=locked,
                deadline_seconds=int(recordings_settings.SUMMARIZE_TIMEOUT_SECONDS),
            )
        except StageAwaiting as exc:
            # The normal production path: the work is queued, the request
            # returns, and `resume_resummarize` finishes the job when the
            # result arrives.
            job.status = JobStatus.PROCESSING
            job.started_at = timezone.now()
            job.options = {**(job.options or {}), "task_id": str(exc.task_id)}
            job.save(update_fields=["status", "started_at", "options"])
            return job, True
        except StageRetryable as exc:
            # Could not even SUBMIT — the bus, not the recording. Raising
            # rolls the whole block back, Job row included, which is the
            # honest outcome: nothing was queued, nothing was spent, and a
            # "failed" job row would put a run in the user's history that
            # never existed. The 503 is the whole answer.
            raise SummarizationUnavailable(exc.reason, exc.detail) from exc

        # Inline task dispatch (brokerless monolith / tests): the result is
        # already here, so the job completes inside the request.
        _apply_summary_result(locked, job, result)
        return job, True


def resume_resummarize(recording_id, task_id, result) -> bool:
    """Finish a standalone re-summary from its ``llm.summarize`` result.

    Returns True when *task_id* belonged to a re-summary job (and was
    applied), False when it did not — the caller then routes the result to
    the pipeline driver. Delivery is at-least-once, so a redelivery finds the
    job already completed and answers False rather than storing twice.
    """
    from django.db import transaction

    from .models import Recording

    with transaction.atomic():
        recording = (
            Recording.objects.select_for_update().filter(pk=recording_id).first()
        )
        if recording is None:
            return False
        job = _job_awaiting(recording.pk, task_id)
        if job is None:
            return False
        _apply_summary_result(recording, job, result)
        return True


def fail_resummarize(recording_id, task_id, error: str) -> bool:
    """The task a re-summary was waiting on failed for good. True if handled.

    No retry: the Task primitive already exhausted its own attempts. The
    recording keeps its previous summary and its previous version key, so it
    still reads as stale — which is the truth, and the reason no
    ``recording.resummarized`` event leaves here.
    """
    from django.db import transaction

    with transaction.atomic():
        job = _job_awaiting(recording_id, task_id)
        if job is None:
            return False
        _fail_job(job, "summarize_task_failed", error)
        return True


def _job_awaiting(recording_id, task_id):
    """The in-flight re-summary job waiting on *task_id*, if any.

    Matched in Python over the (few) in-flight rows rather than with a JSON
    key lookup: this runs on every ``task.completed`` in the process, and the
    query has to behave identically on every database the fleet supports.
    """
    return next(
        (
            job
            for job in _inflight_summarize_jobs(recording_id)
            if str((job.options or {}).get("task_id") or "") == str(task_id)
        ),
        None,
    )


def _apply_summary_result(recording, job, result) -> bool:
    """Store a delegated summary against *job*; emit the public receipt."""
    from django.utils import timezone

    from . import events
    from .models import JobStatus

    summary = summary_from_result(result)
    if summary is None:
        logger.warning(
            "resummarize: summary for %s not produced: %.200s", recording.id, result
        )
        _fail_job(job, "summary_not_produced", str(result)[:500])
        return False

    store_summary(recording, summary)
    job.status = JobStatus.COMPLETED
    job.progress_percent = 100
    job.completed_at = timezone.now()
    job.result = {"summary_chars": len(summary)}
    job.save(update_fields=["status", "progress_percent", "completed_at", "result"])
    # Emitted INSIDE the same transaction as the write (outbox discipline):
    # a host that debits for this must never be told about a summary that
    # rolled back, and must always be told about one that did not.
    events.emit_resummarized(recording, job_id=job.id, user_id=job.owner_id)  # emit-check: ok — every caller (start_resummarize / resume_resummarize) holds the atomic block
    return True


def _fail_job(job, reason: str, detail=None) -> None:
    from django.utils import timezone

    from .models import JobStatus

    job.status = JobStatus.FAILED
    job.error = {"reason": reason, "detail": detail}
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error", "completed_at"])


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
    "embed": EmbedStage,  # after merge — embeds the finished transcript
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
    """Resolve *name* to a ready-to-run Stage instance.

    Lazy: only the requested stage's handler is imported (same precedence as
    :func:`resolve_stages` — runtime > STAGES overlay > built-ins). A broken
    dotted-path elsewhere in the STAGES overlay therefore only affects
    pipelines that actually include that stage, instead of failing every
    recording at once. A bad path raises ``ImportError`` (the driver DLQs
    the recording as ``unresolvable_stage``)."""
    from django.utils.module_loading import import_string

    if name in _runtime_stages:
        handler = _runtime_stages[name]
        if handler is None:
            raise KeyError(f"stage {name!r} was removed by a runtime registration")
        return _as_stage(name, handler)

    overlay = recordings_settings.STAGES or {}
    if name in overlay:
        path = overlay[name]
        if path is None:
            raise KeyError(f"stage {name!r} was removed via the STAGES overlay")
        handler = import_string(path) if isinstance(path, str) else path
        return _as_stage(name, handler)

    if name in BUILTIN_STAGES:
        return _as_stage(name, BUILTIN_STAGES[name])
    raise KeyError(f"stage {name!r} is not registered")


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
    "ResummarizeRefused",
    "NoTranscriptToSummarize",
    "SummarizationUnavailable",
    "DERIVED_SUMMARY",
    "summary_from_result",
    "store_summary",
    "start_resummarize",
    "resume_resummarize",
    "fail_resummarize",
    "ConvertStage",
    "TranscribeStage",
    "DiarizeStage",
    "MergeStage",
    "EmbedStage",
    "BUILTIN_STAGES",
    "register_stage",
    "unregister_stage",
    "reset_runtime_stages",
    "resolve_stages",
    "get_stage",
]
