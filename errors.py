"""i18n error keys of stapel-recordings.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_404_NOT_FOUND = "error.404.recording_not_found"
ERR_400_INVALID_STATE = "error.400.recording_invalid_state"
ERR_413_TOO_LARGE = "error.413.recording_too_large"

STAPEL_RECORDINGS_ERRORS = {
    ERR_404_NOT_FOUND: "Recording not found",
    ERR_400_INVALID_STATE: "Recording is not in a valid state for this action",
    ERR_413_TOO_LARGE: "Upload exceeds the maximum allowed size",
}

register_service_errors(STAPEL_RECORDINGS_ERRORS)

__all__ = [
    "STAPEL_RECORDINGS_ERRORS",
    "ERR_404_NOT_FOUND",
    "ERR_400_INVALID_STATE",
    "ERR_413_TOO_LARGE",
]
