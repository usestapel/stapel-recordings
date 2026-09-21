"""Pipeline stages + the open stage registry.

A stage is a swappable unit of pipeline work with a small contract:

    class Stage:
        name: str            # registry key
        status: str          # RecordingStatus set by the driver while running
        def run(self, recording, ctx: dict) -> dict: ...

``run`` does the work (mutating/saving the recording as needed) and returns
the context dict passed to the next stage. It raises :class:`StageRetryable`
(transient — the driver counts the attempt and lets reconcile re-drive),
:class:`StageFatal` (bad input — straight to DLQ) or
:class:`StageNeedsPayment` (the wallet cannot buy this — the recording is
parked in the ``needs_payment`` STATUS, no failure event goes out, and a
host resumes it with ``pipeline.resume_after_payment`` once it is paid for).
Stages MUST be idempotent (guard on status / persisted keys) because
delivery is at-least-once.

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

import hashlib
import json
import logging
import os
import shutil
import tempfile
from typing import Callable

from .conf import recordings_settings
from .models import RecordingStatus, Segment, Speaker
from .storage import get_storage

logger = logging.getLogger(__name__)

#: Payload key carrying one task's call budget to this package's task
#: delegate (``task_delegates``), which pops it before the Function call.
#: It is OURS, not the agent's: how long our executor waits is a property
#: of this pipeline's recording, and the agent's contract has no field for
#: it (nor should it — ``additionalProperties: false`` would refuse it).
TASK_TIMEOUT_KEY = "task_timeout_seconds"


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


class StageNeedsPayment(StageError):
    """The wallet cannot buy the work this stage is about to do.

    Same shape as :class:`StageFatal` (``reason`` + optional ``detail``) and
    equally terminal for event deliveries, but it is a STATUS, not an error:
    the driver parks the recording in ``needs_payment``, publishes
    ``recording.needs_payment`` instead of ``recording.failed``, and keeps
    the pipeline cursor and the run identity. A retry cannot fix it — money
    can — so the only way back in is ``pipeline.resume_after_payment``,
    called by the host once the balance is there.

    ``reason`` is the machine-readable code a UI branches on
    (``insufficient_credits``, ``free_minutes_exhausted``); ``detail`` is for
    logs and may carry balance internals, so it does not reach a client.
    """


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

    def input_fingerprint(self, recording, ctx: dict):  # noqa: ARG002
        """What this stage is about to read, as a short stable string.

        The identity of the INPUT plus every PARAMETER that changes the
        answer — a content hash of the object, the provider, the language,
        a trim. The driver records it beside the completion, and the
        stage's result is reused only while the two still agree
        (:mod:`stapel_recordings.checkpoints`). So this is the sentence that
        decides whether a re-run is free or paid, and getting it wrong is
        expensive in both directions: too narrow and a changed input is
        served the old answer, too broad and every delivery re-buys the
        stage.

        Two requirements. It must be DETERMINISTIC for an unchanged input —
        no timestamps, no uuids, no dict ordering — because a value that
        differs from itself asks for a re-run on every delivery. And it must
        be CHEAP: it is read on every pass of the driver, so it reads fields
        the row already carries and never downloads an object to hash it
        (the convert stage hashes the audio once, while the file is local,
        exactly so nobody has to).

        ``None`` (the default) means "I do not declare one", and the stage
        keeps the behaviour it had before checkpoints existed: its own
        artifact guard, reused whatever the input.
        """
        return None

    def is_stale(self, recording, ctx: dict) -> bool:
        """Was this stage's persisted artifact computed from another input?

        What a stage's own idempotence guard must ask before returning
        early. ``if the artifact exists: return`` is the cheap half of the
        question and it is the half that hands a paying customer the
        previous answer; this is the other half.
        """
        from . import checkpoints

        return checkpoints.is_stale(
            recording, self.name, self.input_fingerprint(recording, ctx)
        )

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


def _file_content_hash(path: str) -> str:
    """``sha256:<hex>`` of the file at *path*, read in chunks.

    Chunked because this runs on a two-and-a-half-hour meeting's audio and
    a pipeline worker is not entitled to hold it all in memory to compute
    64 characters. The prefix names the algorithm, because the value
    crosses a module boundary (it is handed to ``llm.transcribe``) and a
    bare hex string is the kind of value that gets silently re-hashed.
    """
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


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
    """Extract the upload's audio track at the configured profile, store
    that one object, and delete the container it came from.

    This is where "we are an audio service, not a video host" stops being a
    sentence in the docs: the working copy of the source lives in a temp
    directory that ``finally`` removes on success and on every failure
    path, the object store keeps the extracted mono audio and nothing else,
    and ``file_storage_key`` is CLEARED, so no later read can hand back a
    container that is no longer there.

    Purging the source is not best-effort. A delete that fails is
    :class:`StageRetryable`, and the purge runs again on the idempotent
    re-entry — a warning in a log is not a deletion, and the container
    ceiling (``MAX_CONTAINER_UPLOAD_BYTES``, eight times the stored one) was
    raised on the promise that the container goes away.
    """

    name = "convert"
    status = RecordingStatus.NORMALIZING

    def input_fingerprint(self, recording, ctx):
        """The uploaded object and the profile it is converted at.

        The source's identity is its key plus the byte count the upload
        finalizer verified — the two facts the row already holds about it.
        Not a content hash: the container is the thing we refuse to
        download twice, and a key that is reused for different bytes is not
        a thing this module's upload sessions can produce.

        The audio profile is in the fingerprint because it is a parameter
        of the conversion: change the codec or the sample rate and the
        stored object is genuinely a different object, even from the same
        upload. A host that converts under further parameters of its own —
        a trim is the live example — extends this rather than replacing it.
        """
        from .normalize import audio_profile

        key = recording.file_storage_key or ""
        if not key:
            return None
        size = getattr(recording, "file_size_bytes", None)
        try:
            profile = audio_profile().ext
        except Exception:  # noqa: BLE001 — a fingerprint never fails a stage
            profile = "?"
        return f"src={key}|bytes={size}|profile={profile}"

    def keep_source(self, recording, ctx) -> bool:  # noqa: ARG002
        """Should the uploaded container survive this conversion?

        False here: we are an audio service, the extracted audio is the
        artifact, and the container is deleted (see the class docstring and
        the upload ceiling that was raised on that promise).

        A HOOK because one host already needs the opposite and had to reach
        for the worst available tool to get it. Their case: a recording
        trimmed to a free allowance, where the rest of the meeting is a sale
        away — deleting the source turns "pay to hear the rest" into "upload
        it again". They got there by swapping the storage backend's
        ``delete_object`` for a no-op around this call, which kept the OBJECT
        and did not stop the purge from clearing ``file_storage_key``: the
        bytes stayed in the bucket and the row forgot where they were, so
        the paid re-run had nothing to convert. The purge is one decision,
        so it gets one switch.

        Called AFTER the conversion, so the answer may depend on what the
        conversion turned out to do. Returning True keeps the object and
        the pointer; the host drops both itself once it knows it does not
        need them.
        """
        return False

    def run(self, recording, ctx):
        from .normalize import NormalizeFatal, NormalizePaymentRequired, audio_profile

        if recording.normalized_storage_key and not self.is_stale(recording, ctx):
            # Idempotent, but NOT a bare return: a re-drive after a failed
            # purge is the retry that finally removes the container.
            self._purge_source(recording, ctx)
            return ctx
        if not recording.file_storage_key:
            raise StageFatal("missing_raw_storage_key")

        storage = get_storage()
        normalizer = recordings_settings.NORMALIZER
        try:
            profile = audio_profile()
        # NeedsPayment BEFORE Fatal: it is a subclass, and an `except
        # NormalizeFatal` reached first would swallow it into a DLQ.
        except NormalizePaymentRequired as exc:
            raise StageNeedsPayment(exc.reason, exc.detail) from exc
        except NormalizeFatal as exc:
            raise StageFatal(exc.reason, exc.detail) from exc
        workdir = tempfile.mkdtemp(prefix="rec-convert-")
        raw_path = os.path.join(workdir, "raw.bin")
        out_path = os.path.join(workdir, f"normalized{profile.ext}")
        try:
            try:
                storage.download_to_file(recording.file_storage_key, raw_path)
            except Exception as exc:  # transient object-store failure
                raise StageRetryable("download_failed", str(exc)) from exc

            try:
                duration = normalizer(raw_path, out_path)
            # The host's affordability gate speaks here: NeedsPayment first,
            # or the base-class handler below turns "top up" into "failed".
            except NormalizePaymentRequired as exc:
                raise StageNeedsPayment(exc.reason, exc.detail) from exc
            except NormalizeFatal as exc:
                raise StageFatal(exc.reason, exc.detail) from exc

            # The stored-object ceiling, enforced on the only object that
            # will still exist a minute from now. Fatal, not retryable:
            # re-running produces the same bytes.
            stored_bytes = os.path.getsize(out_path)
            max_stored = int(recordings_settings.MAX_STORED_BYTES)
            if max_stored > 0 and stored_bytes > max_stored:
                raise StageFatal(
                    "stored_audio_too_large",
                    f"{stored_bytes} bytes of extracted audio exceeds "
                    f"MAX_STORED_BYTES ({max_stored})",
                )

            normalized_key = _key(recording, f"audio.normalized{profile.ext}")
            if normalized_key == recording.file_storage_key:
                raise StageFatal("key_collision", normalized_key)
            try:
                storage.upload_from_file(
                    normalized_key, out_path, content_type=profile.content_type
                )
            except Exception as exc:
                raise StageRetryable("upload_failed", str(exc)) from exc

            recording.normalized_storage_key = normalized_key
            recording.stored_size_bytes = stored_bytes
            if duration:
                recording.duration_seconds = duration
            # The identity of the audio, computed HERE — the one moment
            # the file is local and already open. Everything downstream
            # (the agent's checkpoint, which is what makes a retry of a
            # priced transcription free) keys on this string, and any
            # other place to compute it would mean downloading the object
            # again.
            #
            # It lives in ``workflow_state``, not ``metadata``: REC-01 —
            # metadata is the CLIENT's half of this row and a client PATCH
            # must never be able to write a value a server decision reads.
            # Here that is not a style rule: the hash IS the agent's
            # checkpoint key, so a client that could set it could name
            # another recording's hash and be handed that recording's paid
            # transcript. (The key is reserved in ``metadata`` as well, so
            # a host that populates it there — the documented alternative
            # source below — still cannot take it from a client.)
            recording.workflow_state = {
                **(recording.workflow_state or {}),
                "audio_content_hash": _file_content_hash(out_path),
            }
            recording.save(update_fields=[
                "normalized_storage_key", "stored_size_bytes",
                "duration_seconds", "workflow_state", "updated_at",
            ])

            self._purge_source(recording, ctx)
            return ctx
        finally:
            # Success, StageFatal, StageRetryable, a killed worker's
            # SystemExit — the source's working copy does not outlive this
            # call, and it is the only place on disk it ever existed.
            shutil.rmtree(workdir, ignore_errors=True)

    def _purge_source(self, recording, ctx=None) -> None:
        """Delete the uploaded container and forget its key.

        A no-op once the key is gone, so re-entry is free. With
        ``AUDIO_ONLY_INGEST`` off the upload IS the stored object and there
        is nothing to purge — that is the documented exception, and the
        accepted-upload ceiling drops accordingly
        (``services.accepted_upload_limit``).

        Also a no-op when :meth:`keep_source` claims the container: OBJECT
        AND POINTER TOGETHER. Keeping the bytes while clearing the key
        would leave a recording that cannot be reprocessed and an object no
        retention policy can reach.
        """
        from .normalize import audio_only_ingest_active

        key = recording.file_storage_key
        if not key or key == recording.normalized_storage_key:
            return
        if not audio_only_ingest_active():
            return
        if self.keep_source(recording, ctx or {}):
            logger.info(
                "convert: source container of recording %s kept on request", recording.id
            )
            return
        try:
            get_storage().delete_object(key)
        except Exception as exc:
            logger.warning(
                "convert: could not delete the source container for %s", recording.id,
                exc_info=True,
            )
            raise StageRetryable("source_purge_failed", str(exc)) from exc
        recording.file_storage_key = None
        recording.save(update_fields=["file_storage_key", "updated_at"])


def stage_dedupe_key(recording, stage: str) -> str:
    """The identity of one unit of work: this recording, this object,
    this stage.

    ``stapel_core.comm.start`` coalesces on it — while a task with this
    key is PENDING or RUNNING, a second ``start()`` returns the FIRST
    task's id and creates nothing. That is the half of the duplicate-spend
    defect this module owns: production measured ONE 148-minute recording
    transcribed six times, as TWO tasks of three attempts each, and the
    column that would have collapsed the two tasks into one existed and
    was empty on every row ever written.

    The storage key is in the key, not just the recording id, because the
    OBJECT is what gets transcribed: a recording re-converted to a new
    normalized key is genuinely new work, and must not be coalesced into
    the task that is still transcribing the old one.

    Core releases the key at DONE/FAILED — it deduplicates work IN
    FLIGHT and does not claim to remember forever. Remembering forever is
    the agent checkpoint's job (a paid answer, stored under its input);
    these two together are why a retry is now free rather than merely
    rarer.
    """
    storage_key = recording.normalized_storage_key or recording.file_storage_key or ""
    return f"{recording.id}:{storage_key}:{stage}"


def stage_input_dedupe_key(recording, stage) -> str:
    """The identity of one unit of work, by INPUT rather than by object path.

    :func:`stage_dedupe_key` reads the storage key, which is stable across a
    re-conversion — the normalized audio is written back to the same path.
    That is right for the question it was written for (two deliveries racing
    over the same object) and wrong for the one that costs money: a second
    click on "process in full" while the first is still transcribing is the
    SAME work and must coalesce, while the same recording re-converted from
    a longer source is DIFFERENT work and must not.

    So when the stage declares an input fingerprint, that is what the key
    carries. When it does not, this falls back to the object path and
    nothing changes for that stage.
    """
    name = getattr(stage, "name", stage) or ""
    fingerprint = None
    if not isinstance(stage, str):
        try:
            fingerprint = stage.input_fingerprint(recording, {})
        except Exception:  # noqa: BLE001 — a dedupe key never fails a stage
            fingerprint = None
    if not fingerprint:
        return stage_dedupe_key(recording, name)
    digest = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:32]
    return f"{recording.id}:{name}:{digest}"


def transcribe_attempt_ceiling() -> int:
    """The MOST provider calls one recording's transcribe stage can cause.

    The two retry ladders MULTIPLY — that is the arithmetic nobody did
    before production did it for us:

        stage retries (MAX_STAGE_RETRIES)  ×  task attempts (max_attempts)

    3 × 3 was the shipped configuration, and six of those nine were
    actually spent on one recording. With ``TRANSCRIBE_TASK_MAX_ATTEMPTS``
    at 1 the product is 3, and every one of those 3 after the first is
    served from the agent's checkpoint at no charge.

    A checks-time gate reads this (W0xx is not claimed here; the number is
    asserted in tests/test_retry_ceiling.py), so raising either setting
    without meaning to raise the ceiling fails loudly.
    """
    return int(recordings_settings.MAX_STAGE_RETRIES) * int(
        recordings_settings.TRANSCRIBE_TASK_MAX_ATTEMPTS
    )


def summarize_attempt_ceiling() -> int:
    """The MOST llm.summarize calls one recording's merge stage can cause.

    Same arithmetic as :func:`transcribe_attempt_ceiling`, stated for the
    same reason. Every call after the first is served from stapel-agent's
    checkpoint — per map-reduce PART — because
    :func:`_summarize_payload` passes the transcript hash as the
    idempotency key.
    """
    return int(recordings_settings.MAX_STAGE_RETRIES) * int(
        recordings_settings.SUMMARIZE_TASK_MAX_ATTEMPTS
    )


def summarize_budget_seconds(recording, transcript=None) -> int:
    """How long ONE llm.summarize call may take, for THIS recording.

    A constant cannot bound work that grows with the meeting: the call
    is a map-reduce over the transcript, and 300 seconds that comfortably
    covers twenty minutes cannot cover four hours. The audio's own
    duration is the honest input — it is known before the call, it is
    what the transcript's size follows from, and it needs no guess about
    tokens.
    """
    base = int(recordings_settings.SUMMARIZE_TIMEOUT_SECONDS)
    per_hour = int(recordings_settings.SUMMARIZE_SECONDS_PER_HOUR)
    ceiling = int(recordings_settings.SUMMARIZE_TIMEOUT_MAX_SECONDS)

    seconds = getattr(recording, "duration_seconds", None)
    if not seconds and transcript is not None:
        seconds = getattr(transcript, "duration_seconds", None)
    try:
        hours = max(float(seconds or 0.0), 0.0) / 3600.0
    except (TypeError, ValueError):
        hours = 0.0
    return int(max(base, min(base + hours * per_hour, ceiling)))


def task_deadline_seconds(budget_seconds: int, max_attempts: int) -> int:
    """The deadline a task needs to be able to USE the attempts it declares.

    The merge stage set its deadline to exactly one attempt's budget
    while declaring three attempts. The first timeout therefore left the
    row past its deadline, the 60-second sweep failed it with "deadline
    exceeded", and attempts two and three never existed — a retry ladder
    that could not be climbed (a client stand, 2026-09-13).
    """
    attempts = max(int(max_attempts), 1)
    headroom = int(recordings_settings.TASK_DEADLINE_HEADROOM_SECONDS)
    return int(budget_seconds) * attempts + headroom


def submit_task(
    kind,
    payload,
    *,
    recording,
    deadline_seconds=None,
    max_attempts=3,
    dedupe_key=None,
    stage=None,
):
    """Submit long-running work as a task and return control immediately.

    Returns a result ONLY when the deployment dispatches tasks synchronously
    (``STAPEL_COMM["TASK_DISPATCH"]="inline"``: brokerless monolith, tests,
    scripts) and the task already reached ``done`` inside ``start()``.
    Otherwise raises :class:`StageAwaiting`, and the stage continues in
    :meth:`Stage.resume` on ``task.completed``.

    ``correlation_id`` is the recording id: resume uses it to find which
    stage to complete. It's also the event partition key, so a recording's
    events stay in order.

    ``dedupe_key`` is derived from *stage* when not given explicitly (see
    :func:`stage_input_dedupe_key`) — EVERY submission carries one, because
    a submission without one is a submission that a redelivered message
    duplicates. Pass the stage OBJECT rather than its name where the stage
    declares an input fingerprint: the key is then the identity of the
    work, so two clicks on the same re-run coalesce and a re-run over a
    changed input does not.
    """
    from stapel_core.comm import start, status
    from stapel_core.comm.exceptions import CommError

    if dedupe_key is None:
        dedupe_key = stage_input_dedupe_key(recording, stage or kind)

    try:
        task_id = start(
            kind,
            payload,
            correlation_id=str(recording.id),
            deadline_seconds=deadline_seconds,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
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

    def input_fingerprint(self, recording, ctx):  # noqa: ARG002
        """The audio's content hash and everything else the provider is told.

        The hash is the one ConvertStage took while the normalized file was
        still on local disk, so this costs a dict lookup. It is also what
        makes this stage's checkpoint correct rather than merely present:
        the storage KEY of the normalized audio does not change when the
        audio does (the object is written to the same path), so a
        key-shaped fingerprint would call a re-converted two-hour meeting
        identical to the ten minutes it replaced.

        The rest are the parameters that change the answer for the same
        bytes: which provider, how the language was chosen, whether
        diarization was asked for.

        ``language`` itself is deliberately NOT in here, though it is an
        input: this stage WRITES it back with what the provider detected,
        so a fingerprint carrying it would differ from itself the moment
        the stage succeeded, and the next delivery would re-buy the
        transcription for ever. ``language_mode`` is the parameter that was
        actually asked for, and it is the one the stage does not touch. A
        fingerprint may only read what its own stage leaves alone.
        """
        content_hash = (recording.workflow_state or {}).get("audio_content_hash") or (
            recording.metadata or {}
        ).get("audio_content_hash")
        if not content_hash:
            # Nothing cheap identifies the audio. Say so rather than
            # fingerprinting the storage key, which would compare equal
            # across a re-conversion and quietly authorise reuse.
            return None
        return (
            f"audio={content_hash}"
            f"|provider={recording.provider_override or ''}"
            f"|language_mode={recording.language_mode or ''}"
            f"|diarization={bool(recording.diarization_enabled)}"
        )

    def build_payload(self, recording) -> dict:
        """The ``llm.transcribe`` payload for *recording*.

        A HOOK, not an implementation detail. Hosts that need one more
        field (vocabulary biasing is the live example) used to copy this
        body into a subclass and keep the copy in lockstep by hand — which
        is how a fix to the module's stage silently misses the host's
        pipeline. Override or extend this instead; ``run`` and ``resume``
        stay the module's.
        """
        storage_key = recording.normalized_storage_key or recording.file_storage_key
        if not storage_key:
            raise StageFatal("no_storage_key")

        storage = get_storage()
        # With a private bucket this presigned URL is the ONLY way the ASR
        # provider reads the audio, so its TTL is configuration, not a
        # literal: it has to outlive TRANSCRIBE_TIMEOUT_SECONDS (a provider
        # that starts late must still be able to fetch). W007 warns if it
        # does not.
        ttl = int(recordings_settings.TRANSCRIBE_AUDIO_URL_TTL_SECONDS)
        payload = {
            "audio_url": storage.presigned_get_url(storage_key, expires_seconds=ttl),
            "diarization": bool(recording.diarization_enabled),
            "timeout_seconds": int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS),
            **identity_payload(recording),
        }

        # THE IDENTITY OF THE MEDIA, and HOW MUCH OF IT THERE IS. Both are
        # facts this side already owns and the agent cannot cheaply
        # recover from a presigned URL:
        #
        # * the content hash is the agent's checkpoint key (stapel-agent
        #   >=0.24.0). Without it a retry of this stage — including the
        #   one that fires when the transcript handoff fails, downstream
        #   of the money — pays the provider a second time.
        # * the duration is what the agent's ledger METERS. Several STT
        #   adapters report the last word's end timestamp instead, so an
        #   empty transcript meters as zero while the invoice counts it in
        #   full. ConvertStage measured this file; the agent would have to
        #   download it again to measure it itself.
        # Two sources, server-written first: ConvertStage's own hash of the
        # normalized object, else a host that computed one at ingest and
        # stored it on the row (the reserved metadata key). Never a third
        # place, and never computed here — the object is in the bucket by
        # now and downloading it to hash it would cost the transfer the
        # hash exists to avoid.
        content_hash = (recording.workflow_state or {}).get(
            "audio_content_hash"
        ) or (recording.metadata or {}).get("audio_content_hash")
        if content_hash:
            payload["audio_content_hash"] = str(content_hash)
        if recording.duration_seconds:
            payload["audio_duration_ms"] = int(
                round(float(recording.duration_seconds) * 1000)
            )
        if recording.language:
            payload["language"] = recording.language
        provider = recording.provider_override
        if provider:
            payload["provider"] = provider

        # THE ANSWER COMES BACK BY REFERENCE TOO. The audio has always
        # travelled as a URL; the transcript travelled as bulk, and a 2h28m
        # meeting's transcript is 8.6 MB against a broker that carries 8
        # (owner's stand, 2026-09-09: two recordings of the same meeting
        # lost). So the agent is handed a presigned PUT and writes the
        # transcript into this recording's own prefix, where the pipeline
        # was going to keep it anyway.
        #
        # Asked for whenever the storage backend can sign a PUT — a
        # deployment fact, read once, never a judgement about this
        # recording's size. A backend that cannot sign is a backend with no
        # broker in front of it (see storage.signs_put_urls).
        if transcript_handoff_enabled():
            key = handoff_key(recording)
            payload["transcript_put_url"] = storage.presigned_put_url(
                key, expires_seconds=ttl, content_type="application/json"
            )
            payload["transcript_key"] = key
        return payload

    def run(self, recording, ctx):
        if recording.segments.exists() and not self.is_stale(recording, ctx):
            return ctx  # idempotent: already transcribed, from THIS audio

        # BEFORE PAYING AGAIN, LOOK WHERE THE LAST ANSWER WOULD BE.
        #
        # The handoff object is written by the provider BEFORE it replies,
        # and at a key THIS side chose — so a transcript whose reply was
        # lost is not lost, it is unread. Measured, 2026-09-20: a 2h28m
        # meeting was transcribed, the 8 779 798 bytes landed in our bucket
        # at 22:06:01, and the small reply naming them was published into a
        # NATS inbox that had died with a restarted consumer. The task then
        # failed `deadline_exceeded` and the only way back was to buy the
        # transcription a second time.
        stranded = stranded_handoff(recording)
        if stranded is not None:
            return self.resume(recording, ctx, stranded)

        payload = self.build_payload(recording)

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
            # ONE task attempt for the priced call (see
            # TRANSCRIBE_TASK_MAX_ATTEMPTS), and a dedupe key so a
            # redelivered stage event joins the transcription already in
            # flight instead of starting a second one. Together with
            # MAX_STAGE_RETRIES this bounds the recording at
            # transcribe_attempt_ceiling() calls, of which at most the
            # first is paid for.
            max_attempts=int(recordings_settings.TRANSCRIBE_TASK_MAX_ATTEMPTS),
            stage=self,
        )
        # Reached only under synchronous dispatch (TASK_DISPATCH="inline" —
        # brokerless monolith, tests, scripts): the task already completed
        # by the time start() returns.
        return self.resume(recording, ctx, result)

    def resume(self, recording, ctx, result):
        # Idempotent, like run(): task.completed is at-least-once, and a
        # redelivery must not re-read a handoff object this stage has
        # already consumed and deleted. "Already transcribed" is again the
        # narrow claim — transcribed FROM THIS AUDIO; segments left by an
        # earlier, different input are what this result replaces.
        if recording.segments.exists() and not self.is_stale(recording, ctx):
            return ctx
        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (
                (result or {}).get("reason", "transcribe_failed")
                if isinstance(result, dict) else "transcribe_failed"
            )
            raise StageRetryable("transcribe_failed", str(reason))

        transcript = transcript_from_result(result)
        _persist_transcript(
            recording,
            transcript,
            provider_used=result.get("provider_used"),
            fallback_used=bool(result.get("fallback_used")),
        )
        # A POSTBOX, NOT AN ARTIFACT. transcript.raw.json exists to get the
        # answer off the wire; the transcript's permanent home is the
        # Segment rows and the unified transcript.json MergeStage writes.
        # Leaving it would put a second verbatim copy of a private meeting
        # in the bucket forever — under no field of the row, so erasure
        # would never find it and no retention would ever reach it.
        # Deleted only after the rows are committed, so a failure above
        # leaves it for the retry.
        _discard_handoff(recording, result)
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

    def input_fingerprint(self, recording, ctx):  # noqa: ARG002
        """The transcript this stage merges and summarizes.

        Its own content, not the stage before it: the segments are
        editable after the pipeline is done (a word corrected, a turn
        reassigned), and the artifacts built here are artifacts OF THAT
        CONTENT. The hash is the canonical transcript hash — the same key
        the summary is pinned to and the one a host's staleness flag
        already reads, so one number decides freshness everywhere instead
        of two that can disagree.
        """
        from . import transcript_schema

        if not recording.segments_count:
            return None
        return "transcript=" + transcript_schema.transcript_hash(
            transcript_schema.from_db_segments(recording)
        )

    def run(self, recording, ctx):
        from . import transcript_schema

        transcript = transcript_schema.from_db_segments(recording)
        current = transcript_schema.transcript_hash(transcript)

        # The stored transcript is an artifact OF the segments, so "does it
        # exist" is not the question — "is it the one these segments make"
        # is. A re-run after a re-transcription finds the key populated and
        # the content a meeting old.
        stored = _recorded_derived_hash(recording, DERIVED_TRANSCRIPT)
        if not recording.transcript_storage_key or stored != current:
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
            _record_derived_hash(recording, DERIVED_TRANSCRIPT, current)

        if recording.summary and _recorded_derived_hash(recording, DERIVED_SUMMARY) == current:
            return ctx  # the summary already belongs to THIS transcript

        if not (recordings_settings.SUMMARIZE_ENABLED and transcript.segments):
            return ctx

        # The transcript is already saved — it's the main artifact. The
        # summary runs as a SEPARATE task: it can take tens of seconds, and
        # there's no reason to hold a worker for it.
        budget = summarize_budget_seconds(recording, transcript)
        attempts = int(recordings_settings.SUMMARIZE_TASK_MAX_ATTEMPTS)
        result = submit_task(
            "llm.summarize",
            _summarize_payload(recording, transcript, budget_seconds=budget),
            recording=recording,
            # Sized to the meeting, and big enough for the attempts this
            # declares — see task_deadline_seconds() for what happened
            # when the two were one number.
            deadline_seconds=task_deadline_seconds(budget, attempts),
            max_attempts=attempts,
            stage=self,
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


def _summarize_payload(recording, transcript, *, budget_seconds=None) -> dict:
    """Payload for ``llm.summarize``.

    Language comes from the recording, or from what STT detected when
    ``language_mode="auto"`` (filled in during the transcribe stage).
    Without it the model picks its own language, which has produced a
    summary in the wrong language for the conversation — about as useless
    as no summary at all.

    ``idempotency_key`` is the transcript's own hash, which makes a
    retried summary free: stapel-agent checkpoints each part of the
    map-reduce under it, so the second attempt re-buys nothing the first
    one already paid for, and an EDITED transcript is a different key and
    therefore a real new summary.

    ``task_timeout_seconds`` is read by this package's task delegate and
    stripped before the call — it is how long this particular meeting's
    summary may take, and the agent's contract has no business carrying
    our executor's budget.
    """
    from . import transcript_schema

    payload = {
        "text": transcript_schema.render_markdown(transcript),
        "model": recordings_settings.SUMMARIZE_MODEL,
        "idempotency_key": (
            f"summary:{transcript_schema.transcript_hash(transcript)}"
        ),
        **identity_payload(recording),
    }
    if budget_seconds:
        payload[TASK_TIMEOUT_KEY] = int(budget_seconds)
    payload_language = _transcript_language(recording, transcript)
    if payload_language:
        payload["language"] = payload_language
    return payload


def _transcript_language(recording, transcript) -> str:
    """The language tag to summarize in — always a string, never a struct.

    ``UnifiedTranscript.language`` is a :class:`~stapel_recordings.transcript_schema.LanguageMeta`
    (routed / detected / path), not a tag. Falling back to it whole put a
    dataclass into a task payload that has to be JSON, so a recording with no
    ``language`` of its own failed to submit at all — which is exactly the
    ``language_mode="auto"`` case the fallback was written for.
    """
    meta = getattr(transcript, "language", None)
    return (
        recording.language
        or getattr(meta, "routed", None)
        or getattr(meta, "detected", None)
        or ""
    )


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

#: Derived-artifact kind for the stored unified transcript JSON. The same
#: convention as the summary, and for the same reason: the object at
#: ``transcript_storage_key`` is a rendering of the segments at one moment,
#: and "the key is set" says nothing about WHICH moment. Recorded so a
#: re-transcription (or an edit) is followed by a rewrite instead of by a
#: stored transcript that quietly disagrees with the rows it came from.
DERIVED_TRANSCRIPT = "transcript"


def _recorded_derived_hash(recording, kind: str):
    """Which transcript version *kind* was built from, or ``None``."""
    derived = (recording.metadata or {}).get("derived") or {}
    value = (derived.get(kind) or {}).get("transcript_hash")
    return str(value) if value else None


def _record_derived_hash(recording, kind: str, transcript_hash: str) -> None:
    """Pin *kind* to the transcript version it was just built from."""
    metadata = dict(recording.metadata or {})
    derived = dict(metadata.get("derived") or {})
    derived[kind] = {**(derived.get(kind) or {}), "transcript_hash": str(transcript_hash)}
    metadata["derived"] = derived
    recording.metadata = metadata
    recording.save(update_fields=["metadata", "updated_at"])


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
                stage="summarize",
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


def transcript_handoff_enabled() -> bool:
    """Whether ``llm.transcribe`` is asked to write the transcript to storage.

    ``TRANSCRIPT_HANDOFF`` is "auto" (default), True or False. "auto" means
    "whenever the storage backend can actually sign a PUT" — asking for a
    handoff a backend cannot honour would hand the agent a URL that reads
    like an upload target and is not one (``DjangoStorageBackend`` returns
    the SERVED url; see ``storage.signs_put_urls``).

    That is not the size test in disguise. It is read from configuration,
    the same answer for every recording in the deployment, and the
    deployments it turns off are exactly the ones with no broker between
    the two services and therefore no ceiling to hit.
    """
    value = recordings_settings.TRANSCRIPT_HANDOFF
    if isinstance(value, str) and value.lower() == "auto":
        return bool(getattr(get_storage(), "signs_put_urls", False))
    return bool(value)


def handoff_key(recording) -> str:
    """Where ``llm.transcribe`` is asked to write the transcript.

    One function, because two places need the same answer: the payload that
    asks for it, and the erasure sweep that has to be able to delete it for
    a recording that never got past this stage.
    """
    return _key(recording, "transcript.raw.json")


def stranded_handoff(recording) -> dict | None:
    """An ``llm.transcribe`` result whose reply never arrived, or None.

    The handoff is a claim check this side addressed: ``handoff_key`` is
    derived from the recording, the provider writes the bytes there before
    it answers, and only the small envelope naming them travels the wire.
    So an object sitting at that key with no segments to show for it means
    exactly one thing — the transcription happened, was paid for, and the
    answer did not get home.

    Adopted ONLY when the recording has no segments at all. If it has stale
    ones the audio has changed underneath, and a handoff from the previous
    audio is the wrong transcript, not a free one.

    Never raises: absent is the ordinary answer, and every first attempt
    takes that path.
    """
    if not transcript_handoff_enabled():
        return None
    if recording.segments.exists():
        return None
    key = handoff_key(recording)
    try:
        data = get_storage().get_bytes(key)
    except Exception:
        return None
    if not data:
        return None
    logger.warning(
        "transcribe: recording %s has no segments but a finished transcript "
        "of %d bytes is waiting at %s — adopting it instead of transcribing "
        "again. The provider wrote it and its reply was lost; this is paid "
        "work being collected, not repeated.",
        recording.id, len(data), key,
    )
    return {
        "status": "ok",
        "transcript_ref": {"key": key, "bytes": len(data)},
        "recovered_handoff": True,
    }


def _discard_handoff(recording, result: dict) -> None:
    """Drop the handoff object once its contents are persisted."""
    ref = result.get("transcript_ref")
    key = str((ref or {}).get("key") or "") if isinstance(ref, dict) else ""
    if not key:
        return
    try:
        get_storage().delete_object(key)
    except Exception:
        # Not the caller's problem — the transcript is in the database. A
        # leftover is swept by erasure (which derives the same key) and by
        # the bucket's own retention.
        logger.warning(
            "transcribe: could not discard the handoff object %s", key, exc_info=True
        )


def transcript_from_result(result: dict) -> dict:
    """The transcript dict out of an ``llm.transcribe`` result, either shape.

    ``transcript_ref`` (the agent wrote it to our own bucket) or
    ``transcript`` (inline). ONE function so that every reader — this
    module's stage and any host stage that adds a field to the payload —
    gets both shapes from the same place instead of growing its own branch.
    """
    ref = result.get("transcript_ref")
    if not isinstance(ref, dict):
        return result.get("transcript") or {}

    key = str(ref.get("key") or "")
    if not key:
        raise StageRetryable(
            "transcript_ref_incomplete",
            "llm.transcribe answered with a reference carrying no key",
        )
    try:
        data = get_storage().get_bytes(key)
    except Exception as exc:
        raise StageRetryable("transcript_fetch_failed", f"{key}: {exc}") from exc

    expected = ref.get("bytes")
    if isinstance(expected, int) and len(data) != expected:
        # A short read is the failure this shape could plausibly hide, and
        # it would surface as a recording missing its last hour rather than
        # as an error. The producer already counted the bytes; check them.
        raise StageRetryable(
            "transcript_truncated",
            f"{key}: read {len(data)} bytes, the agent wrote {expected}",
        )
    try:
        transcript = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise StageRetryable("transcript_unreadable", f"{key}: {exc}") from exc
    if not isinstance(transcript, dict):
        raise StageRetryable("transcript_unreadable", f"{key}: not a JSON object")
    return transcript


def _persist_transcript(recording, transcript: dict, *, provider_used, fallback_used) -> None:
    """Write Speaker/Segment rows from an ``llm.transcribe`` result dict and
    denormalize counters onto the Recording.

    REPLACES, and replaces ATOMICALLY. A recording can be transcribed more
    than once — a re-conversion of a longer source, a provider changed, a
    reprocess — and the rows of the previous transcript are not a base to
    add to: appending would interleave two transcripts of the same meeting
    into one unreadable list, with duplicated sequence numbers and a word
    count that belongs to neither.

    The delete and the insert are one transaction, so the change is visible
    to readers only as a whole: the user keeps seeing the previous
    transcript — every segment of it — until the moment the new one is
    complete, and never a half-emptied one. That is also why the delete
    happens HERE, at the end of the paid work, rather than when the re-run
    was scheduled: a run cleared up front leaves the customer staring at an
    empty meeting for as long as the transcription takes, and at nothing at
    all if it fails.
    """
    from django.db import transaction

    words = transcript.get("words") or []
    utterances = transcript.get("utterances") or _utterances_from_words(words)
    speakers_detected = transcript.get("speakers_detected") or []
    language = transcript.get("language")
    duration = transcript.get("duration_seconds")

    with transaction.atomic():
        # Segments first: Speaker is their FK target, and a speaker deleted
        # while a segment still points at it nulls that segment's speaker
        # instead of removing it — the old transcript would survive as rows
        # with no voice attached to them.
        recording.segments.all().delete()
        recording.speakers.all().delete()

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


#: The safety net's cut rule, kept numerically identical to
#: ``stapel_agent.stt.segmentation`` (whose defaults were derived from
#: 94 608 real word gaps: p95 = 0.60s, p97 = 0.88s). Duplicated rather than
#: imported because recordings does not depend on the agent — the two talk
#: over comm — and a transcript that arrives with no utterances must not be
#: segmented by a different rule than one that arrives with them.
UTTERANCE_GAP_SECONDS = 0.65
UTTERANCE_MAX_SECONDS = 30.0
UTTERANCE_MAX_CHARS = 500
UTTERANCE_MIN_SECONDS = 1.5
UTTERANCE_MIN_WORDS = 4
SENTENCE_ENDINGS = (".", "?", "!", "\u2026", "\u3002", "\uff1f", "\uff01", "\uff0e")


def _utterances_from_words(words: list[dict]) -> list[dict]:
    """Group words into utterance dicts on speaker, pause, sentence and size.

    The fallback for a transcript that carries words but no utterances —
    which is what every provider sends with diarization off. It used to cut
    on the speaker changing and nothing else, so exactly that case produced
    ONE utterance covering the whole meeting: on the owner's stand, 24 of 83
    completed recordings render as a single segment, one of them a ten-minute
    meeting as a single 8592-character turn.

    A segment is what a timestamp anchors to, so this is also what decides
    whether a citation can point anywhere inside a long recording.
    """
    if not words:
        return []
    grouped: list[dict] = []
    buf_text: list[str] = []
    buf_idx: list[int] = []
    buf_start = words[0].get("start") or 0
    buf_end = words[0].get("end") or 0
    buf_speaker = words[0].get("speaker")

    def flush():
        text = " ".join(buf_text).strip()
        if not text:
            return
        grouped.append({
            "text": text,
            "start": buf_start,
            "end": buf_end,
            "speaker": buf_speaker,
            "word_indexes": list(buf_idx),
        })

    for i, w in enumerate(words):
        start = float(w.get("start") or 0)
        if buf_text:
            span = float(buf_end) - float(buf_start)
            chars = sum(len(t) + 1 for t in buf_text)
            cut = w.get("speaker") != buf_speaker
            if not cut and (span >= UTTERANCE_MAX_SECONDS or chars >= UTTERANCE_MAX_CHARS):
                cut = True
            if not cut and (span >= UTTERANCE_MIN_SECONDS or len(buf_text) >= UTTERANCE_MIN_WORDS):
                gap = start - float(words[i - 1].get("end") or 0)
                prev_text = (words[i - 1].get("text") or "").rstrip()
                if gap >= UTTERANCE_GAP_SECONDS or prev_text.endswith(SENTENCE_ENDINGS):
                    cut = True
            if cut:
                flush()
                buf_text, buf_idx = [], []
                buf_start = start
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
    "transcript_handoff_enabled",
    "handoff_key",
    "transcript_from_result",
    "StageError",
    "StageRetryable",
    "StageFatal",
    "StageNeedsPayment",
    "ResummarizeRefused",
    "NoTranscriptToSummarize",
    "SummarizationUnavailable",
    "DERIVED_SUMMARY",
    "DERIVED_TRANSCRIPT",
    "stage_dedupe_key",
    "stage_input_dedupe_key",
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
