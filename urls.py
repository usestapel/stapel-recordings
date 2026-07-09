"""URL patterns — no global prefix here, the host project mounts them:

    path("recordings/", include("stapel_recordings.urls"))
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    FinalizeUploadView,
    RecordingDetailView,
    RecordingListCreateView,
    ReprocessRecordingView,
)

urlpatterns = [
    path("api/recordings", RecordingListCreateView.as_view(), name="recordings-list-create"),
    path("api/recordings/<uuid:recording_id>", RecordingDetailView.as_view(), name="recordings-detail"),
    path("api/recordings/<uuid:recording_id>/finalize", FinalizeUploadView.as_view(), name="recordings-finalize"),
    path("api/recordings/<uuid:recording_id>/reprocess", ReprocessRecordingView.as_view(), name="recordings-reprocess"),
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
