"""i18n error keys of stapel-recordings.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_404_NOT_FOUND = "error.404.recording_not_found"
ERR_400_INVALID_STATE = "error.400.recording_invalid_state"
ERR_403_WORKSPACE_FORBIDDEN = "error.403.recording_workspace_forbidden"
ERR_413_TOO_LARGE = "error.413.recording_too_large"
ERR_415_UNSUPPORTED_MEDIA = "error.415.recording_unsupported_media"

STAPEL_RECORDINGS_ERRORS = {
    ERR_404_NOT_FOUND: "Recording not found",
    ERR_400_INVALID_STATE: "Recording is not in a valid state for this action",
    ERR_403_WORKSPACE_FORBIDDEN: "You are not a member of this workspace",
    ERR_413_TOO_LARGE: "Upload exceeds the maximum allowed size",
    ERR_415_UNSUPPORTED_MEDIA: "Upload file type is not supported",
}

register_service_errors(STAPEL_RECORDINGS_ERRORS)

__all__ = [
    "STAPEL_RECORDINGS_ERRORS",
    "ERR_404_NOT_FOUND",
    "ERR_400_INVALID_STATE",
    "ERR_403_WORKSPACE_FORBIDDEN",
    "ERR_413_TOO_LARGE",
    "ERR_415_UNSUPPORTED_MEDIA",
]
