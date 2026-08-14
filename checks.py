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
    boots (`manage.py migrate` refused to run on the ironmemo stand,
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
