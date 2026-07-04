"""Read-only admin for the recordings journal models."""
from django.contrib import admin

from .models import Job, Recording, Segment, Speaker, UploadSession


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Recording)
class RecordingAdmin(_ReadOnlyAdmin):
    list_display = ("id", "title", "status", "workspace_id", "provider_used", "created_at")
    list_filter = ("status", "source_type")
    search_fields = ("id", "title", "workspace_id")


@admin.register(Speaker)
class SpeakerAdmin(_ReadOnlyAdmin):
    list_display = ("id", "recording", "label", "display_name")


@admin.register(Segment)
class SegmentAdmin(_ReadOnlyAdmin):
    list_display = ("id", "recording", "sequence_num", "start_time", "end_time")


@admin.register(UploadSession)
class UploadSessionAdmin(_ReadOnlyAdmin):
    list_display = ("id", "recording", "is_multipart", "finalized_at", "expires_at")


@admin.register(Job)
class JobAdmin(_ReadOnlyAdmin):
    list_display = ("id", "recording", "type", "status", "progress_percent")
    list_filter = ("type", "status")
