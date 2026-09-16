"""Granting "view recordings" must not grant "read the customer's meeting".

A ModelAdmin with neither `fields` nor `exclude` renders EVERY column on the
detail page, readonly or not. So on a fleet audited 2026-09-16, "staff may
view recordings" silently meant "staff may read the AI summary of any
customer's meeting" — and because the safe answer was not expressible, the
operators were granted nothing at all and could not do their jobs.

These pin that the default is content-free, and that a host which wants more
has to say so in code rather than get it by accident.
"""
from django.contrib import admin as dj_admin

from stapel_recordings.admin import RecordingAdmin, SegmentAdmin, SpeakerAdmin
from stapel_recordings.models import Recording, Segment, Speaker


def _excluded(admin_cls, model):
    instance = admin_cls(model, dj_admin.AdminSite())
    return set(instance.get_exclude(request=None) or ())


def _rendered(admin_cls, model):
    """Columns the detail page would actually show."""
    return {f.name for f in model._meta.fields} - _excluded(admin_cls, model)


class TestARecordingDetailShowsNoContent:
    def test_the_ai_summary_is_not_rendered(self):
        assert "summary" in _excluded(RecordingAdmin, Recording)

    def test_neither_is_the_title(self):
        # Judgement call, stated out loud: metadata by schema, content by
        # privacy. "Acme acquisition, legal review" says what the meeting was
        # about as surely as the summary does.
        assert "title" in _excluded(RecordingAdmin, Recording)

    def test_nor_the_pointers_to_the_audio_and_the_transcript(self):
        excluded = _excluded(RecordingAdmin, Recording)
        assert {
            "file_storage_key",
            "normalized_storage_key",
            "transcript_storage_key",
        } <= excluded

    def test_nor_the_free_form_metadata_blob(self):
        assert "metadata" in _excluded(RecordingAdmin, Recording)

    def test_but_the_processing_facts_ARE_rendered(self):
        # Otherwise this is just "grant nothing" with extra steps: the point
        # is that an operator can answer "did it run, how long, which
        # provider, why did it fail".
        rendered = _rendered(RecordingAdmin, Recording)
        assert {
            "status",
            "duration_seconds",
            "provider_used",
            "retry_count",
            "created_at",
        } <= rendered

    def test_the_changelist_does_not_leak_what_the_detail_page_hides(self):
        # Hiding a column on the detail page and printing it in the list is
        # the same disclosure by another route.
        assert "title" not in RecordingAdmin.list_display
        assert "title" not in RecordingAdmin.search_fields


class TestTheNeighbouringTablesToo:
    def test_a_segment_is_the_transcript_and_is_not_rendered(self):
        excluded = _excluded(SegmentAdmin, Segment)
        assert {"text", "original_text", "words_json"} <= excluded
        # Timings and sequence stay: they are how a pipeline is debugged.
        assert {"start_time", "end_time", "sequence_num"} <= _rendered(
            SegmentAdmin, Segment
        )

    def test_a_speaker_name_is_a_person_who_attended(self):
        assert "display_name" in _excluded(SpeakerAdmin, Speaker)
        assert "display_name" not in SpeakerAdmin.list_display


class TestAHostCanStillDecideOtherwise:
    def test_narrowing_the_tuple_re_renders_the_field(self):
        """The escape hatch is a line of code in the host, not a side effect.

        A deployment that HAS made the decision that its staff may read
        customers' meetings subclasses and says so; what it must not be able
        to do is arrive there by granting a permission with an innocent name.
        """

        class HostRecordingAdmin(RecordingAdmin):
            CONTENT_FIELDS = ("file_storage_key",)

        rendered = _rendered(HostRecordingAdmin, Recording)
        assert "summary" in rendered
        assert "file_storage_key" not in rendered

    def test_the_default_stays_content_free_for_everyone_else(self):
        assert "summary" in _excluded(RecordingAdmin, Recording)
