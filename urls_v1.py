"""v1 URL set for stapel-recordings (api-versioning.md §2, §6).

No global prefix here — the root ``urls.py`` mounts this module under
``api/v1/`` and the host mounts that under ``recordings/``:

    path("recordings/", include("stapel_recordings.urls"))   # -> /recordings/api/v1/...
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    FinalizeUploadView,
    RecordingDetailView,
    RecordingListCreateView,
    RecordingMediaView,
    ReprocessRecordingView,
    ResummarizeRecordingView,
    SharedRecordingMediaView,
    SharedRecordingView,
    ShareUnlockView,
)

urlpatterns = [
    path("recordings", RecordingListCreateView.as_view(), name="recordings-list-create"),
    path("recordings/<uuid:recording_id>", RecordingDetailView.as_view(), name="recordings-detail"),
    path("recordings/<uuid:recording_id>/finalize", FinalizeUploadView.as_view(), name="recordings-finalize"),
    path("recordings/<uuid:recording_id>/reprocess", ReprocessRecordingView.as_view(), name="recordings-reprocess"),
    # Authorized media delivery (audit STORE-01): the ONLY sanctioned way a
    # client reaches the bytes. Everything else — a key pasted into a public
    # bucket URL, a proxy in front of the store — is delivery without an
    # authorization decision.
    # The cheap regenerate: summary only, no STT/diarize re-run. A sibling of
    # /reprocess rather than a flag on it — they differ in cost, in authority
    # and in what they touch, and one endpoint with a "just the summary"
    # switch would hide all three behind a request body.
    path("recordings/<uuid:recording_id>/resummarize", ResummarizeRecordingView.as_view(), name="recordings-resummarize"),
    path("recordings/<uuid:recording_id>/media", RecordingMediaView.as_view(), name="recordings-media"),
    # Public share surface. The link token is a path segment because it IS
    # the credential the route resolves; unlock tokens travel in a header.
    path("shares/<str:link_token>", SharedRecordingView.as_view(), name="recordings-share-detail"),
    path("shares/<str:link_token>/unlock", ShareUnlockView.as_view(), name="recordings-share-unlock"),
    path("shares/<str:link_token>/media", SharedRecordingMediaView.as_view(), name="recordings-share-media"),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on,
    and disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): recordings has no per-method
#: config gates (SUMMARIZE_ENABLED gates pipeline behavior, not endpoints;
#: the seams swap strategies) — the whole URL surface is a single always-on
#: block. Declared as a registry entry (rather than left implicit) so the
#: capabilities.json emitter has a uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'recordings.api': GateEntry('recordings.api', (), tuple(urlpatterns)),
}
