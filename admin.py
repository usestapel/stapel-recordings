"""Admin for stapel-recordings.

``Recording``/``Speaker``/``Segment`` are undecorated business tables (the
transcript data itself) but kept read-only here as this module's own
pre-existing choice, independent of the ``@access`` category rollout below.
``UploadSession`` and ``Job`` are decorated ``@access.ops`` (admin-suite
AS-5) — pure machinery with no staff add/change/delete workflow — so their
admins subclass ``StapelModelAdmin``, which enforces the read-only-even-for-
superuser lockout and the HIGH-clearance view gate from the declaration.
"""
from django.contrib import admin
from stapel_core.django.admin.base import StapelModelAdmin

from .models import Job, Recording, Segment, Speaker, UploadSession


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Read-only, and — since 0.26.0 — content-free by default.

    Read-only was never the whole question. A ModelAdmin with no `fields` or
    `exclude` renders EVERY column on the detail page, readonly or not, so
    "staff may view recordings" silently meant "staff may read what the
    meeting was about". A fleet audited on 2026-09-16 wanted to grant its
    operators status and metadata so they could answer "did this run"; it
    could not, because `view_recording` also rendered `summary` — the AI
    summary of a customer's meeting — and the permission set had no way to
    say otherwise. The safe answer was not expressible, so the honest thing
    was to grant nothing, and nothing is what staff got.

    ``CONTENT_FIELDS`` is the list of columns that carry what a meeting was
    ABOUT rather than how it was PROCESSED. They are excluded from the detail
    view by default. A deployment that has actually made the decision that
    its staff may read customers' meetings subclasses and narrows the tuple —
    which is a line of code in that host, reviewable, instead of a silent
    consequence of a permission name.
    """

    #: Overridden per admin below.
    CONTENT_FIELDS: tuple[str, ...] = ()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_exclude(self, request, obj=None):
        # `get_exclude` rather than the `exclude` attribute so a subclass can
        # decide per request (per workspace, per clearance) without
        # reimplementing the default.
        return tuple(self.CONTENT_FIELDS) or None


@admin.register(Recording)
class RecordingAdmin(_ReadOnlyAdmin):
    # `title` is in here and not in list_display, which is the judgement call
    # worth arguing with: it is metadata by schema and content by privacy —
    # "Acme acquisition, legal review" tells you what the meeting was about
    # as surely as the summary does. The id and the workspace are the handles
    # support actually needs; the storage keys are pointers to the audio and
    # the transcript, so they are content too, and `metadata` is free-form
    # JSON that has held anything anyone ever put there.
    CONTENT_FIELDS = (
        "title",
        "summary",
        "metadata",
        "file_storage_key",
        "normalized_storage_key",
        "transcript_storage_key",
    )
    list_display = (
        "id", "status", "workspace_id", "duration_seconds", "provider_used", "created_at",
    )
    list_filter = ("status", "source_type")
    # Searching BY title would put the titles on screen in the results, which
    # is the same disclosure by another route.
    search_fields = ("id", "workspace_id")


@admin.register(Speaker)
class SpeakerAdmin(_ReadOnlyAdmin):
    # A speaker's display_name is a person who attended the meeting.
    CONTENT_FIELDS = ("display_name",)
    list_display = ("id", "recording", "label", "segment_count")


@admin.register(Segment)
class SegmentAdmin(_ReadOnlyAdmin):
    # A segment IS the transcript: `text` is what was said, `words_json`
    # is the same thing with timings on it.
    CONTENT_FIELDS = ("text", "original_text", "words_json")
    list_display = ("id", "recording", "sequence_num", "start_time", "end_time")


@admin.register(UploadSession)
class UploadSessionAdmin(StapelModelAdmin):
    list_display = ("id", "recording", "is_multipart", "finalized_at", "expires_at")


@admin.register(Job)
class JobAdmin(StapelModelAdmin):
    list_display = ("id", "recording", "type", "status", "progress_percent")
    list_filter = ("type", "status")
