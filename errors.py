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
# The upload's bytes could not be CHECKED (the storage backend cannot serve a
# ranged read), so they are not accepted. The deployment's fault, not the
# caller's — hence 5xx, and hence not 415, which would blame the file.
ERR_503_UPLOAD_UNVERIFIABLE = "error.503.recording_upload_unverifiable"
# Re-summary. Same split as media above: 409 is the CALLER's state (there is
# no transcript to summarize yet), 503 is the DEPLOYMENT's (summaries are
# switched off, or the task bus refused the submission). A caller told 409
# should wait for the pipeline; a caller told 503 should stop asking.
ERR_409_NO_TRANSCRIPT = "error.409.recording_no_transcript"
ERR_503_SUMMARIZE_UNAVAILABLE = "error.503.recording_summarize_unavailable"
# Object-policy refusals that are NOT "no such recording" (0.18.0). A host
# policy answers with a :class:`~stapel_recordings.policy.PolicyDecision`
# carrying its OWN key — these two are the fallbacks for a decision that
# named a status and left the key out, so the envelope never carries a key
# that contradicts its status. 402 is the one that pays for itself: a
# re-summary refused for an empty balance must be distinguishable from one
# refused because the recording is not yours, or the UI cannot offer a
# top-up.
ERR_402_PAYMENT_REQUIRED = "error.402.recording_payment_required"
ERR_403_ACTION_DENIED = "error.403.recording_action_denied"

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
    ERR_503_UPLOAD_UNVERIFIABLE: "Upload could not be verified",
    ERR_409_NO_TRANSCRIPT: "This recording has no transcript to summarize yet",
    ERR_503_SUMMARIZE_UNAVAILABLE: "Summaries are not available",
    ERR_402_PAYMENT_REQUIRED: "This action requires available credit",
    ERR_403_ACTION_DENIED: "You are not allowed to do that with this recording",
}

#: Fallback key per status for an object-policy refusal that named a status
#: but no error key. Anything not listed here answers
#: :data:`ERR_403_ACTION_DENIED` — a refusal whose status this module cannot
#: name is still a refusal, and it keeps the host's status rather than being
#: rewritten into a 404 the host did not ask for.
POLICY_DENIAL_CODES = {
    402: ERR_402_PAYMENT_REQUIRED,
    403: ERR_403_ACTION_DENIED,
    404: ERR_404_NOT_FOUND,
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
    "ERR_503_UPLOAD_UNVERIFIABLE",
    "ERR_409_NO_TRANSCRIPT",
    "ERR_503_SUMMARIZE_UNAVAILABLE",
    "ERR_402_PAYMENT_REQUIRED",
    "ERR_403_ACTION_DENIED",
    "POLICY_DENIAL_CODES",
]
