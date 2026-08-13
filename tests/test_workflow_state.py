"""Client metadata vs server workflow state (audit REC-01).

The finding: one JSON dict carried the pipeline's start marker, its
completed-stage cursor and the stage ``ctx``, while the product exposed that
same dict to a client PATCH. These tests pin the separation — a client can
write whatever it likes into ``metadata`` and the pipeline neither reads it
nor is influenced by it — and the reserved-key guard that stops the two from
being re-merged by the next host.
"""
import pytest

from stapel_recordings import pipeline
from stapel_recordings.metadata import (
    ReservedMetadataKey,
    UserMetadataField,
    sanitize_user_metadata,
    set_user_metadata,
)
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


# ── the pipeline does not read the client's field ────────────────────────


def test_client_metadata_cannot_forge_the_completed_cursor(ready_recording, stub_transcribe):
    """A client-planted cursor must not skip stages.

    The forged dict claims every stage is done at index 99 — under the old
    single-field design that is exactly what the driver read."""
    r = ready_recording
    Recording.objects.filter(pk=r.pk).update(
        metadata={
            "pipeline": {
                "completed": ["convert", "transcribe", "diarize", "merge", "embed"],
                "completed_index": 99,
                "ctx": {"normalized_key": "attacker://payload"},
            }
        }
    )
    pipeline.start_pipeline(str(r.id))
    pipeline.run_stage(str(r.id), 0)

    r.refresh_from_db()
    assert r.workflow_state["pipeline"]["completed"] == ["convert"]
    assert r.workflow_state["pipeline"]["completed_index"] == 0
    # The client's dict is untouched, and unread.
    assert r.metadata["pipeline"]["completed_index"] == 99


def test_client_metadata_cannot_suppress_the_start_marker(ready_recording):
    r = ready_recording
    Recording.objects.filter(pk=r.pk).update(metadata={"pipeline": {"started_at": "1999-01-01"}})
    pipeline.start_pipeline(str(r.id))
    r.refresh_from_db()
    assert r.workflow_state["pipeline"]["started_at"] > "2000"


def test_client_metadata_cannot_inject_stage_ctx(ready_recording, stub_transcribe):
    r = ready_recording
    Recording.objects.filter(pk=r.pk).update(
        metadata={"pipeline": {"ctx": {"normalized_key": "attacker://payload"}}}
    )
    pipeline.start_pipeline(str(r.id))
    pipeline.run_stage(str(r.id), 0)
    r.refresh_from_db()
    ctx = r.workflow_state["pipeline"].get("ctx") or {}
    assert ctx.get("normalized_key") != "attacker://payload"


def test_pipeline_writes_never_land_in_client_metadata(ready_recording, stub_transcribe):
    r = ready_recording
    set_user_metadata(r, {"note": "user typed this"})
    pipeline.start_pipeline(str(r.id))
    pipeline.run_stage(str(r.id), 0)
    r.refresh_from_db()
    assert r.metadata == {"note": "user typed this"}
    assert "pipeline" in r.workflow_state


def test_error_markers_are_server_state(ready_recording, make_recording):
    from stapel_recordings.stages import StageFatal, register_stage

    def boom(recording, ctx):
        raise StageFatal("bad_input", "nope")

    register_stage("convert", boom)
    r = ready_recording
    pipeline.start_pipeline(str(r.id))
    pipeline.run_stage(str(r.id), 0)
    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert r.workflow_state["last_error"]["reason"] == "bad_input"
    assert "last_error" not in r.metadata


# ── the reserved-key guard ───────────────────────────────────────────────


def test_reserved_keys_are_rejected_at_any_depth():
    assert sanitize_user_metadata({"note": "fine", "tags": ["a", "b"]})
    for bad in (
        {"pipeline": {}},
        {"last_error": {"stage": "x"}},
        {"recovered_error": 1},
        {"a": {"b": {"pipeline": {"completed_index": 9}}}},
        {"items": [{"ok": 1}, {"pipeline": {}}]},
    ):
        with pytest.raises(ReservedMetadataKey):
            sanitize_user_metadata(bad)


def test_host_can_reserve_its_own_keys(settings):
    from django.test import override_settings

    with override_settings(STAPEL_RECORDINGS={"RESERVED_METADATA_KEYS": ["free_cap_waived"]}):
        with pytest.raises(ReservedMetadataKey):
            sanitize_user_metadata({"billing": {"free_cap_waived": True}})


def test_serializer_field_rejects_reserved_keys():
    from rest_framework import serializers

    class Req(serializers.Serializer):
        metadata = UserMetadataField()

    ok = Req(data={"metadata": {"note": "hi"}})
    assert ok.is_valid(), ok.errors
    bad = Req(data={"metadata": {"pipeline": {"completed_index": 5}}})
    assert not bad.is_valid()
    assert "pipeline" in str(bad.errors)


def test_set_user_metadata_writes_only_the_client_field(make_recording):
    r = make_recording()
    r.workflow_state = {"pipeline": {"completed_index": 2}}
    r.save(update_fields=["workflow_state"])
    set_user_metadata(r, {"note": "n"})
    r.refresh_from_db()
    assert r.metadata == {"note": "n"}
    assert r.workflow_state == {"pipeline": {"completed_index": 2}}
    with pytest.raises(ReservedMetadataKey):
        set_user_metadata(r, {"pipeline": {"completed_index": 99}})


# ── reprocess keeps the finished run recoverable (REC-03) ────────────────


def test_reprocess_snapshots_the_previous_run(make_recording):
    r = make_recording(
        status=RecordingStatus.COMPLETED,
        transcript_storage_key="recordings/x/transcript.json",
        segments_count=12,
    )
    assert pipeline.reprocess_recording(str(r.id)) is True
    r.refresh_from_db()
    previous = r.workflow_state["previous_run"]
    assert previous["transcript_storage_key"] == "recordings/x/transcript.json"
    assert previous["segments_count"] == 12
    # And the pointer itself is untouched — the module deletes nothing.
    assert r.transcript_storage_key == "recordings/x/transcript.json"


def test_refused_reprocess_writes_nothing(make_recording):
    r = make_recording(status=RecordingStatus.QUEUED, transcript_storage_key="k")
    assert pipeline.reprocess_recording(str(r.id)) is False
    r.refresh_from_db()
    assert r.workflow_state == {}
    assert r.transcript_storage_key == "k"


# ── the data migration ───────────────────────────────────────────────────


def test_migration_moves_state_and_reverses(make_recording):
    """0004's RunPython, exercised on real rows.

    Adding the column is half the fix — the rows already in the database
    carry their cursor in ``metadata``, where the driver no longer looks."""
    from django.apps import apps as django_apps

    from importlib import import_module

    migration = import_module(
        "stapel_recordings.migrations.0004_recording_workflow_state"
    )
    move_state_out_of_metadata = migration.move_state_out_of_metadata
    fold_state_back_into_metadata = migration.fold_state_back_into_metadata

    r = make_recording()
    Recording.objects.filter(pk=r.pk).update(
        metadata={
            "note": "user typed this",
            "pipeline": {"completed": ["convert"], "completed_index": 0},
            "last_error": {"stage": "convert"},
        }
    )

    move_state_out_of_metadata(django_apps, None)
    r.refresh_from_db()
    assert r.metadata == {"note": "user typed this"}
    assert r.workflow_state["pipeline"]["completed"] == ["convert"]
    assert r.workflow_state["last_error"]["stage"] == "convert"

    fold_state_back_into_metadata(django_apps, None)
    r.refresh_from_db()
    assert r.metadata["pipeline"]["completed"] == ["convert"]
    assert r.metadata["note"] == "user typed this"
