"""Opaque resource keys (G4).

The workspace-scoped list surface returns recordings that may belong to
*other* members of the workspace. Rather than leak internal identifiers or
storage paths across owners, each recording carries an **opaque**
``resource_key`` in the API payload: a signed, tamper-evident handle a
client passes back to reference the recording, with no parseable structure.

It is a pure function of the recording id (stable across requests) signed
with the project ``SECRET_KEY`` via :mod:`django.core.signing` — so a
client cannot forge one or read the underlying UUID out of it, and the
server can resolve it back with :func:`resolve_resource_key`.
"""
from __future__ import annotations

from typing import Optional

from django.core import signing

_SALT = "stapel_recordings.resource_key"


def resource_key(recording) -> str:
    """Opaque, tamper-evident handle for *recording* (stable per id)."""
    return signing.dumps(str(recording.id), salt=_SALT)


def resolve_resource_key(token: str) -> Optional[str]:
    """Recover the recording id from a ``resource_key`` token, or ``None``
    if the token is missing/forged/corrupt."""
    if not token:
        return None
    try:
        return signing.loads(token, salt=_SALT)
    except signing.BadSignature:
        return None


__all__ = ["resource_key", "resolve_resource_key"]
