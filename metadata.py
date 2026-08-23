"""The client/server split on a recording's JSON (audit REC-01).

``Recording.metadata`` is the client's; ``Recording.workflow_state`` is the
server's. The audit found the two fused: one dict carried the pipeline's
start marker, its completed-stage cursor and the stage ``ctx``, while the
product exposed that same dict to a client PATCH. Nobody wrote that on
purpose — it is what a single "metadata" field grows into, because every
new piece of server state has somewhere obvious to go and nothing says no.

This module is the "nothing says no" part, for the half that cannot be
enforced by the schema alone:

- :func:`sanitize_user_metadata` rejects reserved keys **at any depth**.
  Depth matters: a host that stores ``{"import": {"pipeline": {...}}}``
  today is fine, but a host that later flattens or merges that structure
  re-creates the confusion, and a nested reserved key is exactly what such
  a merge would promote.
- :class:`UserMetadataField` is the serializer field to expose on a write
  endpoint, so the check lives in the contract rather than in a view body
  that the next endpoint will not copy.
- :func:`set_user_metadata` is the write path itself: it saves the client's
  half and nothing else, so a client write can never touch workflow state
  even by accident.

Reserved keys are not only refused on the way IN: :func:`set_user_metadata`
carries the ones already stored across a write, because a whole-document
replace deletes a server receipt just as effectively as overwriting it.

Hosts reserve their own keys through ``RESERVED_METADATA_KEYS`` — a billing
waiver flag, an entitlement stamp, anything a server decision reads. That is
a setting rather than a fork because the keys are the host's vocabulary,
while the rule ("a server decision is never read from a client-writable
field") is this module's.
"""
from __future__ import annotations

from rest_framework import serializers

from .conf import recordings_settings

#: Keys this module owns. Reserved everywhere in ``metadata``, at any depth.
#:
#: ``derived`` / ``staleness`` are the receipt that says which transcript a
#: derived artifact (today: the summary) was built from — written by the
#: server when the artifact is produced, read to answer "is this summary
#: still current". They live in ``metadata`` rather than ``workflow_state``
#: because that answer crosses the seam: hosts already serialize it onto the
#: wire. Reserving them is what keeps them server-owned in a client column —
#: a client can neither hand in a version key (forging freshness) nor drop
#: one by writing metadata (see :func:`set_user_metadata`).
LIBRARY_RESERVED_KEYS = frozenset(
    {
        "pipeline",
        "workflow_state",
        "last_error",
        "recovered_error",
        "derived",
        "staleness",
    }
)


class ReservedMetadataKey(ValueError):
    """A client-supplied metadata document used a reserved key."""

    def __init__(self, key: str, path: str):
        super().__init__(f"reserved metadata key {key!r} at {path or '<root>'}")
        self.key = key
        self.path = path


def reserved_keys() -> frozenset:
    """Library-reserved keys plus the host's ``RESERVED_METADATA_KEYS``."""
    host = recordings_settings.RESERVED_METADATA_KEYS or ()
    return LIBRARY_RESERVED_KEYS | {str(k) for k in host}


def sanitize_user_metadata(value, *, path: str = ""):
    """Return *value* if it is acceptable client metadata, else raise
    :class:`ReservedMetadataKey`.

    Recursive through dicts and lists — a reserved key is reserved wherever
    it appears, not only at the top level."""
    banned = reserved_keys()
    return _walk(value, banned, path)


def _walk(value, banned, path):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in banned:
                raise ReservedMetadataKey(str(key), path)
            _walk(item, banned, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(item, banned, f"{path}[{index}]")
    return value


class UserMetadataField(serializers.JSONField):
    """A ``metadata`` field that refuses reserved keys.

    Expose this instead of a bare ``JSONField`` on any endpoint that lets a
    client write recording metadata."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            return sanitize_user_metadata(value)
        except ReservedMetadataKey as exc:
            raise serializers.ValidationError(str(exc)) from exc  # noqa: R002


def set_user_metadata(recording, value) -> None:
    """Validate and store the client's half of a recording's JSON.

    The write path a host's PATCH endpoint should call: it saves
    ``metadata`` alone, so no client write can reach workflow state even if
    the caller hands over a whole model instance.

    Reserved keys already present are CARRIED OVER rather than replaced.
    Refusing to let a client *write* them was only half the guard: a metadata
    write is a whole-document replace, so a client that submitted a document
    without them deleted the server's receipts — and losing the summary's
    version key reads exactly like "this summary is current", the one lie the
    receipt exists to prevent."""
    incoming = sanitize_user_metadata(value)
    banned = reserved_keys()
    preserved = {
        k: v for k, v in (recording.metadata or {}).items() if str(k) in banned
    }
    recording.metadata = {**preserved, **(incoming or {})}
    recording.save(update_fields=["metadata", "updated_at"])


__all__ = [
    "LIBRARY_RESERVED_KEYS",
    "ReservedMetadataKey",
    "UserMetadataField",
    "reserved_keys",
    "sanitize_user_metadata",
    "set_user_metadata",
]
