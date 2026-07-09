"""Open source-type registry (G9 extension point).

The recording *source kind* — where the audio came from — is not a closed
code enum. :class:`stapel_recordings.models.SourceType` provides the four
default kinds (``meet`` / ``dictaphone`` / ``upload`` / ``other``); a host
adds its own (``zoom`` / ``teams`` / ``phone`` …) by overlaying
``STAPEL_RECORDINGS["SOURCE_TYPES"]`` — merged OVER the built-ins, the same
merge-over-builtins idiom as the ``STAGES`` overlay and
``notifications.TYPES``. No fork, no migration (``Recording.source_type`` is
a free ``CharField``; the enum only supplies the default set + admin labels).

    STAPEL_RECORDINGS = {
        "SOURCE_TYPES": {"zoom": "Zoom", "teams": "Microsoft Teams"},
    }

Resolution is lazy (read at call time via ``recordings_settings``) so a host
can flip it per-test with ``override_settings``.
"""
from __future__ import annotations

from .conf import recordings_settings

#: Built-in source kinds, derived from the model enum so the two never drift.
#: This is the base the ``SOURCE_TYPES`` overlay merges over.
def _default_source_types() -> dict[str, str]:
    from .models import SourceType

    return {choice.value: str(choice.label) for choice in SourceType}


DEFAULT_SOURCE_TYPES: dict[str, str] = _default_source_types()


def resolve_source_types() -> dict[str, str]:
    """Effective ``{key: label}`` map: the built-ins with the settings
    ``SOURCE_TYPES`` overlay merged over them (overlay wins on key clash;
    a host cannot *remove* a built-in, only relabel or extend)."""
    overlay = recordings_settings.SOURCE_TYPES or {}
    return {**DEFAULT_SOURCE_TYPES, **overlay}


def is_valid_source_type(name: str) -> bool:
    """True iff *name* is a registered source kind (built-in or overlaid)."""
    return bool(name) and name in resolve_source_types()


def registered_source_types() -> list[str]:
    """Sorted list of every registered source-kind key."""
    return sorted(resolve_source_types())


__all__ = [
    "DEFAULT_SOURCE_TYPES",
    "resolve_source_types",
    "is_valid_source_type",
    "registered_source_types",
]
