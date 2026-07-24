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
