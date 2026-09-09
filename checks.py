"""Django system checks for stapel-recordings configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily (a broken
*unused* dotted path must not block deploys).
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_storage_backend(app_configs, **kwargs):
    """E: the STORAGE seam must resolve to a RecordingStorage subclass."""
    from .conf import recordings_settings
    from .storage import RecordingStorage

    try:
        cls = recordings_settings.STORAGE
    except Exception as exc:
        return [checks.Error(
            f"STAPEL_RECORDINGS['STORAGE'] could not be imported: {exc}",
            id="stapel_recordings.E001",
        )]
    if not (isinstance(cls, type) and issubclass(cls, RecordingStorage)):
        return [checks.Error(
            "STAPEL_RECORDINGS['STORAGE'] must be a RecordingStorage subclass.",
            id="stapel_recordings.E002",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_multipart_part_budget(app_configs, **kwargs):
    """E: a full-size multipart upload must fit in MAX_MULTIPART_PARTS.

    ``ceil(accepted_upload_limit() / MULTIPART_PART_SIZE) <=
    MAX_MULTIPART_PARTS``, or every upload near the cap fails at start with
    ``services.MultipartMisconfigured`` — a 500 for the client, and a
    deployment fault that belongs at ``manage.py check``, not in a log.

    Against the ACCEPTED ceiling, not ``MAX_UPLOAD_BYTES``: with audio-only
    ingest the accepted one is ``MAX_CONTAINER_UPLOAD_BYTES``, eight times
    larger, and checking the smaller number would pass a deployment whose
    every real upload fails."""
    from .conf import recordings_settings
    from .services import accepted_upload_limit

    try:
        max_bytes = accepted_upload_limit()
        part_size = int(recordings_settings.MULTIPART_PART_SIZE)
        part_cap = int(recordings_settings.MAX_MULTIPART_PARTS)
        max_stored = int(recordings_settings.MAX_STORED_BYTES)
    except (TypeError, ValueError) as exc:
        return [checks.Error(
            f"STAPEL_RECORDINGS upload limits must be integers: {exc}",
            id="stapel_recordings.E005",
        )]
    if part_size <= 0 or max_bytes <= 0 or part_cap <= 0 or max_stored <= 0:
        return [checks.Error(
            "STAPEL_RECORDINGS['MAX_UPLOAD_BYTES'], ['MAX_CONTAINER_UPLOAD_BYTES'], "
            "['MAX_STORED_BYTES'], ['MULTIPART_PART_SIZE'] and "
            "['MAX_MULTIPART_PARTS'] must be positive.",
            id="stapel_recordings.E005",
        )]
    needed = (max_bytes + part_size - 1) // part_size
    if needed > part_cap:
        return [checks.Error(
            f"The accepted upload ceiling ({max_bytes}) needs {needed} parts of "
            f"MULTIPART_PART_SIZE ({part_size}) but MAX_MULTIPART_PARTS is "
            f"{part_cap}: raise MULTIPART_PART_SIZE or lower the ceiling.",
            id="stapel_recordings.E005",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_audio_extraction_tooling(app_configs, **kwargs):
    """E: audio extraction is configured, so its tools must be on this host.

    ffmpeg's presence is an environment fact, not a setting, and it used to
    be discovered the way environment facts always are: at the first upload,
    as ``NormalizeFatal('ffmpeg_missing')`` on a recording someone was
    waiting for. Everything the ``convert`` stage needs is knowable at
    ``manage.py check`` — the two binaries resolve on PATH, and the build
    carries the encoder ``AUDIO_CODEC`` names (a stripped ffmpeg without
    libopus is a normal thing to find in a slim container image, and it
    fails at the same late moment).

    Silent when the deployment does not extract audio at all
    (``passthrough_normalize``, or a host's own NORMALIZER, which shells out
    to whatever it likes)."""
    import shutil
    import subprocess

    from .conf import recordings_settings
    from .normalize import CODEC_OPUS, NormalizeFatal, audio_profile, ffmpeg_normalize

    try:
        if recordings_settings.NORMALIZER is not ffmpeg_normalize:
            return []
    except Exception:
        return []  # W004 already reports an unimportable seam

    try:
        profile = audio_profile()
    except NormalizeFatal as exc:
        return [checks.Error(
            f"STAPEL_RECORDINGS['AUDIO_CODEC'] is not one this module can "
            f"write: {exc}",
            id="stapel_recordings.E006",
        )]

    errors = []
    binaries = {
        "FFMPEG_BIN": str(recordings_settings.FFMPEG_BIN),
        "FFPROBE_BIN": str(recordings_settings.FFPROBE_BIN),
    }
    for setting, name in binaries.items():
        if shutil.which(name) is None:
            errors.append(checks.Error(
                f"STAPEL_RECORDINGS['{setting}'] = {name!r} is not on PATH, but "
                "NORMALIZER is ffmpeg_normalize — every upload will fail at the "
                "convert stage. Install ffmpeg in this image, pin an absolute "
                "path, or set NORMALIZER to a normalizer that does not need it.",
                id="stapel_recordings.E006",
            ))
    if errors or profile.codec != CODEC_OPUS:
        return errors

    # The binary is there; ask it whether this build can write the codec.
    try:
        proc = subprocess.run(
            [binaries["FFMPEG_BIN"], "-hide_banner", "-loglevel", "error", "-encoders"],
            capture_output=True,
            timeout=20,
        )
        available = b"libopus" in (proc.stdout or b"")
    except Exception as exc:
        return [checks.Error(
            f"{binaries['FFMPEG_BIN']!r} could not be asked which encoders it "
            f"has ({exc}); the convert stage runs the same binary.",
            id="stapel_recordings.E006",
        )]
    if not available:
        return [checks.Error(
            f"{binaries['FFMPEG_BIN']!r} has no libopus encoder, but "
            "STAPEL_RECORDINGS['AUDIO_CODEC'] is 'opus'. Use an ffmpeg build "
            "with libopus, or set AUDIO_CODEC = 'wav' (≈10x the stored bytes).",
            id="stapel_recordings.E006",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_audio_only_ingest_wiring(app_configs, **kwargs):
    """E: ``AUDIO_ONLY_INGEST`` is on, so the extraction must actually run.

    The policy is a promise about what is KEPT, and the accepted-upload
    ceiling is eight times the stored one *because* of it. Take away the
    ``convert`` stage or the transcoding normalizer and the promise silently
    inverts: every container is stored, at the raised ceiling, forever. That
    is a deployment that has to be stopped at ``check``, not discovered from
    a storage bill.

    (``services.accepted_upload_limit`` fails safe in the same situation and
    drops back to ``MAX_UPLOAD_BYTES``. This check is why a host finds out
    it is in that state.)"""
    from .conf import flag, recordings_settings
    from .normalize import passthrough_normalize

    if not flag("AUDIO_ONLY_INGEST"):
        return []  # the documented exception, stated deliberately

    errors = []
    if "convert" not in list(recordings_settings.PIPELINE or []):
        errors.append(checks.Error(
            "STAPEL_RECORDINGS['AUDIO_ONLY_INGEST'] is on but 'convert' is not "
            "in PIPELINE — nothing extracts the audio or deletes the uploaded "
            "container, so every upload is stored whole. Add 'convert' to "
            "PIPELINE, or set AUDIO_ONLY_INGEST = False and accept that this "
            "deployment stores what it is sent.",
            id="stapel_recordings.E007",
        ))
    try:
        normalizer = recordings_settings.NORMALIZER
    except Exception:
        return errors  # W004 already reports an unimportable seam
    if normalizer is passthrough_normalize:
        errors.append(checks.Error(
            "STAPEL_RECORDINGS['AUDIO_ONLY_INGEST'] is on but NORMALIZER is "
            "passthrough_normalize, which copies the upload through unchanged "
            "— the stored object IS the container, video and all. Use "
            "ffmpeg_normalize, or set AUDIO_ONLY_INGEST = False.",
            id="stapel_recordings.E007",
        ))
    return errors


@checks.register(checks.Tags.compatibility)
def check_pipeline_stages(app_configs, **kwargs):
    """W: every stage named in PIPELINE should resolve in the registry; the
    NORMALIZER / PIPELINE_RESOLVER seams should be importable and callable."""
    from .conf import recordings_settings
    from .stages import resolve_stages

    warnings = []
    try:
        known = set(resolve_stages().keys())
    except Exception as exc:
        return [checks.Warning(
            f"STAPEL_RECORDINGS['STAGES'] overlay could not be resolved: {exc}",
            id="stapel_recordings.W001",
        )]
    for name in recordings_settings.PIPELINE:
        if name not in known:
            warnings.append(checks.Warning(
                f"PIPELINE references stage {name!r} that is not registered "
                "(register it via register_stage or add it to STAGES).",
                id="stapel_recordings.W002",
            ))
    for key in ("NORMALIZER", "PIPELINE_RESOLVER"):
        try:
            value = getattr(recordings_settings, key)
            if not callable(value):
                warnings.append(checks.Warning(
                    f"STAPEL_RECORDINGS['{key}'] is not callable.",
                    id="stapel_recordings.W003",
                ))
        except Exception as exc:
            warnings.append(checks.Warning(
                f"STAPEL_RECORDINGS['{key}'] could not be imported: {exc}",
                id="stapel_recordings.W004",
            ))
    return warnings


@checks.register(checks.Tags.compatibility)
def check_normalizer_is_not_passthrough(app_configs, **kwargs):
    """W: ``passthrough_normalize`` means NOTHING is transcoded.

    The NORMALIZER seam is only checked for being *callable*, so pointing it
    at the bundled passthrough — the "no ffmpeg / for tests" copy-the-file
    normalizer — turns off every conversion in the pipeline and passes
    ``manage.py check`` in silence. Whatever the client uploaded is then
    what the ASR provider and every later stage opens, unconverted and
    untruncated (``max_duration_seconds``, the plan cap applied at the
    pipeline entrance, is an ffmpeg_normalize argument and does nothing
    here). That is a legitimate configuration for a stand whose uploads are
    already canonical — it is just never something to discover from a
    provider bill or a garbled transcript.
    """
    from .conf import recordings_settings
    from .normalize import passthrough_normalize

    try:
        normalizer = recordings_settings.NORMALIZER
    except Exception:
        return []  # W004 (check_pipeline_stages) already reports this
    if normalizer is not passthrough_normalize:
        return []
    return [checks.Warning(
        "STAPEL_RECORDINGS['NORMALIZER'] is passthrough_normalize — no audio "
        "is converted to the canonical STT input, and the duration cap does "
        "not apply. Uploads reach the ASR provider exactly as the client "
        "sent them. Use stapel_recordings.normalize.ffmpeg_normalize unless "
        "this stand deliberately runs without ffmpeg.",
        id="stapel_recordings.W008",
    )]


@checks.register(checks.Tags.compatibility)
def check_vector_layer(app_configs, **kwargs):
    """W: VECTOR["ENABLED"] without the opt-in vector app installed makes
    the embed stage a silent no-op — flag the half-configured state."""
    from .conf import vector_config
    from .vector import vector_app_installed

    if vector_config().get("ENABLED") and not vector_app_installed():
        return [checks.Warning(
            "STAPEL_RECORDINGS['VECTOR']['ENABLED'] is on but "
            "'stapel_recordings.vector' is not in INSTALLED_APPS — the embed "
            "stage will no-op. Install stapel-recordings[vector], add the app "
            "and run its migrations (PostgreSQL + pgvector).",
            id="stapel_recordings.W006",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_vector_app_requirements(app_configs, **kwargs):
    """E: the vector app's models use ``HnswIndex``, which Django only
    allows when ``django.contrib.postgres`` is installed.

    Django does say so itself (``postgres.E005``), but it says it about a
    model class — which reads like a library bug rather than a missing
    line in the host's INSTALLED_APPS, and it only surfaces when the host
    boots (`manage.py migrate` refused to run on a client stand,
    2026-07-25, after the documented two install steps were followed to
    the letter). This check names the fix in the module's own vocabulary,
    and the install steps in ``vector/__init__.py`` now list the app.
    """
    from django.apps import apps

    from .vector import vector_app_installed

    if not vector_app_installed():
        return []
    if apps.is_installed("django.contrib.postgres"):
        return []
    return [checks.Error(
        "'stapel_recordings.vector' needs 'django.contrib.postgres' in "
        "INSTALLED_APPS — its embedding tables carry an HNSW index, which "
        "Django refuses to build without that app (postgres.E005). Add "
        "both apps, in this order: 'django.contrib.postgres', "
        "'stapel_recordings', 'stapel_recordings.vector'.",
        id="stapel_recordings.E003",
    )]


@checks.register(checks.Tags.compatibility)
def check_reconcile_threshold(app_configs, **kwargs):
    """W: the reconcile stuck-threshold must exceed the longest legitimate
    stage duration, or the watchdog re-emits ``recording.stage`` for stages
    that are still running (duplicate deliveries piling up on the row lock)."""
    from .conf import recordings_settings

    stuck = int(recordings_settings.STUCK_THRESHOLD_SECONDS)
    longest = int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS)
    if stuck <= longest:
        return [checks.Warning(
            f"STAPEL_RECORDINGS['STUCK_THRESHOLD_SECONDS'] ({stuck}) must exceed "
            f"TRANSCRIBE_TIMEOUT_SECONDS ({longest}) — the longest built-in stage "
            "duration — or reconcile will re-drive stages that are still running. "
            "Account for slow custom stages too.",
            id="stapel_recordings.W005",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_transcribe_audio_url_ttl(app_configs, **kwargs):
    """W: the audio URL handed to the ASR provider must outlive the stage.

    With a private bucket (audit STORE-01) that presigned URL is the only
    way the provider reads the audio. A provider that starts late — queued
    behind other work, retried — fetches with a URL that has already
    expired, and the failure surfaces as an unexplained transcription error
    rather than as a configuration mistake."""
    from .conf import recordings_settings

    ttl = int(recordings_settings.TRANSCRIBE_AUDIO_URL_TTL_SECONDS)
    timeout = int(recordings_settings.TRANSCRIBE_TIMEOUT_SECONDS)
    if ttl <= timeout:
        return [checks.Warning(
            f"STAPEL_RECORDINGS['TRANSCRIBE_AUDIO_URL_TTL_SECONDS'] ({ttl}) should "
            f"exceed TRANSCRIBE_TIMEOUT_SECONDS ({timeout}) — the audio URL handed "
            "to the ASR provider must still be valid when a late-starting provider "
            "fetches it, or transcription fails on an expired signature.",
            id="stapel_recordings.W007",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_taskstore_installed(app_configs, **kwargs):
    """E: long-running work is dispatched as tasks, so the task store must be installed.

    ``transcribe`` and ``merge`` hand work off to the Task primitive
    (``stapel_core.comm.tasks``): the call returns immediately, and the stage
    resumes on ``task.completed``. The primitive keeps its state in the
    ``TaskRecord`` table, which lives in the ``stapel_core.django.taskstore``
    app.

    Without it, everything looks configured and fails LATER — deep in the
    pipeline, on the first real run, with a missing-table error. That is
    exactly the failure mode this check exists to catch at startup instead.
    """
    from django.apps import apps as django_apps

    if django_apps.is_installed("stapel_core.django.taskstore"):
        return []
    return [
        checks.Error(
            "stapel_recordings dispatches long-running work (transcription, "
            "summarization) via the Task primitive, whose table lives in "
            "'stapel_core.django.taskstore' — that app is not in "
            "INSTALLED_APPS.",
            hint=(
                "Add 'stapel_core.django.taskstore' to INSTALLED_APPS and run "
                "its migrations. Also make sure something EXECUTES tasks: "
                "STAPEL_COMM['TASK_EXECUTOR'] ('inline' — same process that "
                "consumes the event; 'celery' — a worker) and that a process "
                "with llm.* handlers is running."
            ),
            # E004, not E001: E001 is already taken by "STORAGE not importable".
            # The id is part of the public contract — it's what hosts silence
            # via SILENCED_SYSTEM_CHECKS and search for, so reusing E001 would
            # let silencing one error accidentally silence this one too.
            id="stapel_recordings.E004",
        )
    ]


#: The stapel-agent release that opened ``user_id`` / ``workspace_id`` on the
#: ``llm.*`` comm schemas. Below it those schemas are
#: ``additionalProperties: false`` against the payloads this package now
#: sends, which is a hard rejection, not a dropped field.
MIN_AGENT_VERSION = (0, 12, 0)


@checks.register(checks.Tags.compatibility)
def check_agent_version_for_identity(app_configs, **kwargs):
    """W: an in-process stapel-agent too old for the attribution fields.

    Every delegated payload carries ``user_id`` / ``workspace_id`` so the
    agent's ledger rows are attributable (``stages.identity_payload``). The
    ``llm.*`` schemas reject unknown properties, so against stapel-agent
    < 0.12.0 this is not a degraded feature — it is every transcription,
    summary, embed and question failing schema validation.

    Only answerable in a monolith, where the agent is importable here. In a
    split deployment nothing in this process can see the other side's
    version, so the floor lives in the changelog and this check stays
    silent rather than guessing. Warning rather than Error for the same
    reason a missing agent is not fatal: the host may be mid-upgrade, and a
    check that cannot see the whole system must not block a deploy.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover — stdlib since 3.8
        return []

    try:
        raw = version("stapel-agent")
    except PackageNotFoundError:
        return []  # not this process's concern — it runs elsewhere

    try:
        found = tuple(int(part) for part in raw.split(".")[:3])
    except ValueError:
        return []  # a dev/local version string — not ours to adjudicate

    if found >= MIN_AGENT_VERSION:
        return []

    wanted = ".".join(str(n) for n in MIN_AGENT_VERSION)
    return [
        checks.Warning(
            f"stapel-agent {raw} is installed, but stapel-recordings sends "
            f"'user_id'/'workspace_id' on its llm.* payloads and those "
            f"fields only exist from stapel-agent {wanted}. The llm.* "
            f"schemas set additionalProperties=false, so EVERY delegated "
            f"call (transcribe, summarize, embed, rerank, complete) will "
            f"fail schema validation, not merely lose attribution.",
            hint=(
                f"Upgrade to stapel-agent>={wanted}. The two fields are "
                "optional there, so nothing else about the upgrade is "
                "breaking."
            ),
            id="stapel_recordings.W009",
        )
    ]


@checks.register(checks.Tags.compatibility)
def check_purge_is_scheduled(app_configs, **kwargs):
    """W010: is the soft-delete purge actually going to run?

    ``PURGE_AFTER_DAYS`` is a promise to the user that a recording they
    deleted stops existing. The mechanism that keeps it is a scheduled task,
    and a task nobody schedules is indistinguishable, from the outside, from
    a retention policy that works — the rows are gone from the UI either
    way. This check is the difference, stated at boot instead of discovered
    in an audit.

    Only hosts that drive a beat schedule are checked: a host with no
    ``CELERY_BEAT_SCHEDULE`` runs the purge from its own cron or systemd
    timer, which this check cannot see and must not second-guess.
    """
    from django.conf import settings

    from .tasks import PURGE_TASK_NAME

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    if schedule is None:
        return []
    if any((entry or {}).get("task") == PURGE_TASK_NAME for entry in schedule.values()):
        return []
    return [checks.Warning(
        "Soft-deleted recordings are never purged: no CELERY_BEAT_SCHEDULE "
        f"entry runs {PURGE_TASK_NAME}, so a recording a user deleted keeps "
        "its rows, its audio and its transcript forever while "
        "STAPEL_RECORDINGS['PURGE_AFTER_DAYS'] claims otherwise.",
        hint=(
            "Add **stapel_recordings.tasks.get_recordings_beat_schedule() to "
            "CELERY_BEAT_SCHEDULE, or invoke "
            "stapel_recordings.tasks.purge_soft_deleted_recordings from your "
            "own scheduler."
        ),
        id="stapel_recordings.W010",
    )]


#: The stapel-agent release whose ``llm.transcribe`` accepts
#: ``transcript_put_url`` and answers with ``transcript_ref``. Separate
#: from MIN_AGENT_VERSION because it gates one optional behaviour, not the
#: whole delegation.
MIN_AGENT_VERSION_FOR_TRANSCRIPT_HANDOFF = (0, 22, 0)


@checks.register(checks.Tags.compatibility)
def check_agent_version_for_transcript_handoff(app_configs, **kwargs):
    """W011: an in-process stapel-agent too old for the transcript handoff.

    With ``TRANSCRIPT_HANDOFF`` on, the transcribe payload carries
    ``transcript_put_url``. The ``llm.*`` schemas are
    additionalProperties=false, so an older agent does not ignore it — it
    rejects the whole call, and every transcription fails.

    Same limits as W009: answerable only where the agent is importable. In
    a split deployment the floor is a deploy-order fact — upgrade the agent
    service BEFORE the recordings service — and it lives in the changelog.
    """
    from .conf import recordings_settings

    value = recordings_settings.TRANSCRIPT_HANDOFF
    if isinstance(value, str) and value.lower() == "auto":
        try:
            from .storage import get_storage

            enabled = bool(getattr(get_storage(), "signs_put_urls", False))
        except Exception:
            return []  # a storage misconfiguration is W00x's to report
    else:
        enabled = bool(value)
    if not enabled:
        return []

    from importlib.metadata import PackageNotFoundError, version

    try:
        raw = version("stapel-agent")
    except PackageNotFoundError:
        return []  # it runs elsewhere — deploy order is the changelog's job

    try:
        found = tuple(int(part) for part in raw.split(".")[:3])
    except ValueError:
        return []

    if found >= MIN_AGENT_VERSION_FOR_TRANSCRIPT_HANDOFF:
        return []

    wanted = ".".join(str(n) for n in MIN_AGENT_VERSION_FOR_TRANSCRIPT_HANDOFF)
    return [checks.Warning(
        f"stapel-agent {raw} is installed and STAPEL_RECORDINGS"
        f"['TRANSCRIPT_HANDOFF'] is on, so llm.transcribe payloads carry "
        f"'transcript_put_url' — a field that only exists from stapel-agent "
        f"{wanted}. The llm.* schemas set additionalProperties=false, so "
        f"every transcription fails schema validation.",
        hint=(
            f"Upgrade to stapel-agent>={wanted}, or set "
            "STAPEL_RECORDINGS['TRANSCRIPT_HANDOFF'] = False to keep the "
            "transcript inline (only safe where no message broker sits "
            "between the two services — a long meeting's transcript does "
            "not fit one broker message)."
        ),
        id="stapel_recordings.W011",
    )]
