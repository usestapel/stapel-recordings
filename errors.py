"""i18n error keys of stapel-recordings.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_404_NOT_FOUND = "error.404.recording_not_found"
ERR_400_INVALID_STATE = "error.400.recording_invalid_state"
ERR_403_WORKSPACE_FORBIDDEN = "error.403.recording_workspace_forbidden"
ERR_409_INVALID_STATE = "error.409.recording_invalid_state"
ERR_413_TOO_LARGE = "error.413.recording_too_large"
ERR_415_UNSUPPORTED_MEDIA = "error.415.recording_unsupported_media"
# Share links. Unknown / revoked / expired / deleted all answer the same
# 404: telling them apart tells a probing client which guessed links exist.
ERR_404_SHARE_NOT_FOUND = "error.404.share_not_found"
ERR_401_SHARE_PASSCODE_REQUIRED = "error.401.share_passcode_required"
ERR_403_SHARE_PERMISSION_DENIED = "error.403.share_permission_denied"
ERR_429_SHARE_THROTTLED = "error.429.share_unlock_throttled"
# Authorized media delivery. "Not stored" is the caller's state (no object
# yet); "unavailable" is the deployment's (the storage backend cannot mint an
# expiring URL, and this module will not substitute a permanent one).
ERR_409_MEDIA_NOT_STORED = "error.409.recording_media_not_stored"
ERR_503_MEDIA_UNAVAILABLE = "error.503.recording_media_unavailable"

STAPEL_RECORDINGS_ERRORS = {
    ERR_404_NOT_FOUND: "Recording not found",
    ERR_400_INVALID_STATE: "Recording is not in a valid state for this action",
    ERR_403_WORKSPACE_FORBIDDEN: "You are not a member of this workspace",
    ERR_409_INVALID_STATE: "Recording is not in a valid state for this action",
    ERR_413_TOO_LARGE: "Upload exceeds the maximum allowed size",
    ERR_415_UNSUPPORTED_MEDIA: "Upload file type is not supported",
    ERR_404_SHARE_NOT_FOUND: "Share link not found",
    ERR_401_SHARE_PASSCODE_REQUIRED: "This share link requires a passcode",
    ERR_403_SHARE_PERMISSION_DENIED: "This share link does not grant that",
    ERR_429_SHARE_THROTTLED: "Too many attempts — try again later",
    ERR_409_MEDIA_NOT_STORED: "This recording has no media file",
    ERR_503_MEDIA_UNAVAILABLE: "Media delivery is not available",
}

register_service_errors(STAPEL_RECORDINGS_ERRORS)

__all__ = [
    "STAPEL_RECORDINGS_ERRORS",
    "ERR_404_NOT_FOUND",
    "ERR_400_INVALID_STATE",
    "ERR_403_WORKSPACE_FORBIDDEN",
    "ERR_409_INVALID_STATE",
    "ERR_413_TOO_LARGE",
    "ERR_415_UNSUPPORTED_MEDIA",
    "ERR_404_SHARE_NOT_FOUND",
    "ERR_401_SHARE_PASSCODE_REQUIRED",
    "ERR_403_SHARE_PERMISSION_DENIED",
    "ERR_429_SHARE_THROTTLED",
    "ERR_409_MEDIA_NOT_STORED",
    "ERR_503_MEDIA_UNAVAILABLE",
]
