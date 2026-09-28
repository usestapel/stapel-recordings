"""v1 URL set for stapel-recordings (api-versioning.md §2, §6).

No global prefix here — the root ``urls.py`` mounts this module under
``api/v1/`` and the host mounts that under ``recordings/``:

    path("recordings/", include("stapel_recordings.urls"))   # -> /recordings/api/v1/...
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    FinalizeUploadView,
    MultipartAbortView,
    MultipartCompleteView,
    MultipartPartsView,
    MultipartStartView,
    RecordingDetailView,
    RecordingListCreateView,
    RecordingMediaView,
    RecordingTranscriptView,
    ReprocessRecordingView,
    ResummarizeRecordingView,
    SharedRecordingMediaView,
    SharedRecordingView,
    ShareUnlockView,
    UploadLimitsView,
    UploadLookupView,
)

urlpatterns = [
    path("recordings", RecordingListCreateView.as_view(), name="recordings-list-create"),
    # Read the ceilings before uploading. A literal segment, listed before
    # the <uuid> routes it can never collide with.
    path("recordings/upload-limits", UploadLimitsView.as_view(), name="recordings-upload-limits"),
    # Is this file (by fingerprint) already here, or resumable?
    path("recordings/uploads/lookup", UploadLookupView.as_view(), name="recordings-upload-lookup"),
    path("recordings/<uuid:recording_id>", RecordingDetailView.as_view(), name="recordings-detail"),
    path("recordings/<uuid:recording_id>/finalize", FinalizeUploadView.as_view(), name="recordings-finalize"),
    # Multipart: start, mint/manifest, complete, abort. A start with a
    # fingerprint is a verified upload (parts bound to their SHA-256).
    path("recordings/<uuid:recording_id>/multipart", MultipartStartView.as_view(), name="recordings-multipart-start"),
    path(
        "recordings/<uuid:recording_id>/multipart/<uuid:upload_id>/parts",
        MultipartPartsView.as_view(),
        name="recordings-multipart-parts",
    ),
    path(
        "recordings/<uuid:recording_id>/multipart/<uuid:upload_id>/complete",
        MultipartCompleteView.as_view(),
        name="recordings-multipart-complete",
    ),
    path(
        "recordings/<uuid:recording_id>/multipart/<uuid:upload_id>/abort",
        MultipartAbortView.as_view(),
        name="recordings-multipart-abort",
    ),
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
    # The owner's own transcript. Before this route, speaker-attributed
    # segments left the module only through a public share link — an owner
    # had to publish a recording to read it.
    path(
        "recordings/<uuid:recording_id>/transcript",
        RecordingTranscriptView.as_view(),
        name="recordings-transcript",
    ),
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
