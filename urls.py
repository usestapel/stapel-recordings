"""URL patterns — no global prefix here, the host project mounts them:

    path("recordings/", include("stapel_recordings.urls"))
"""
from django.urls import path

from .views import FinalizeUploadView, RecordingDetailView, RecordingListCreateView

urlpatterns = [
    path("api/recordings", RecordingListCreateView.as_view(), name="recordings-list-create"),
    path("api/recordings/<uuid:recording_id>", RecordingDetailView.as_view(), name="recordings-detail"),
    path("api/recordings/<uuid:recording_id>/finalize", FinalizeUploadView.as_view(), name="recordings-finalize"),
]
