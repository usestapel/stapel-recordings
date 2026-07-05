"""Reconcile watchdog: re-drive stuck recordings, fail abandoned uploads."""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_recordings import events
from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def _age(recording, hours):
    Recording.objects.filter(pk=recording.pk).update(
        updated_at=timezone.now() - timedelta(hours=hours)
    )


def test_reconcile_re_emits_stuck_stage(make_recording):
    r = make_recording(status=RecordingStatus.TRANSCRIBING, metadata={"pipeline": {"stage_index": 1}})
    _age(r, hours=2)

    call_command("recordings_reconcile", "--once")

    from stapel_core.django.outbox.models import OutboxEvent

    rows = OutboxEvent.objects.filter(topic=events.ACTION_STAGE)
    assert rows.exists()
    import json

    payload = json.loads(rows.first().event_json)["payload"]
    assert payload["stage_index"] == 1
    assert payload["recording_id"] == str(r.id)


def test_reconcile_ignores_fresh_recordings(make_recording):
    make_recording(status=RecordingStatus.TRANSCRIBING, metadata={"pipeline": {"stage_index": 1}})
    # updated_at is fresh (now) — below the stuck threshold.
    call_command("recordings_reconcile", "--once")

    from stapel_core.django.outbox.models import OutboxEvent

    assert not OutboxEvent.objects.filter(topic=events.ACTION_STAGE).exists()


def test_reconcile_marks_abandoned_uploads(make_recording):
    r = make_recording(status=RecordingStatus.UPLOADING, file_storage_key=None)
    _age(r, hours=2)

    call_command("recordings_reconcile", "--once")

    r.refresh_from_db()
    assert r.status == RecordingStatus.ERROR
    assert r.metadata["last_error"]["reason"] == "upload_abandoned"


def test_reconcile_re_drives_custom_stage_status(make_recording):
    """A recording parked in a *custom* Stage.status (not a built-in one)
    must not hang forever — the transient set is 'anything that is neither
    pre-pipeline nor terminal'."""
    r = make_recording(status="redacting", metadata={"pipeline": {"stage_index": 2}})
    _age(r, hours=2)

    call_command("recordings_reconcile", "--once")

    from stapel_core.django.outbox.models import OutboxEvent

    rows = OutboxEvent.objects.filter(topic=events.ACTION_STAGE)
    assert rows.count() == 1
    import json

    payload = json.loads(rows.first().event_json)["payload"]
    assert payload["stage_index"] == 2
    assert payload["recording_id"] == str(r.id)


def test_reconcile_does_not_touch_terminal_or_pre_pipeline(make_recording):
    for status in (RecordingStatus.CREATED, RecordingStatus.COMPLETED,
                   RecordingStatus.ERROR, RecordingStatus.DELETED):
        r = make_recording(status=status)
        _age(r, hours=2)

    call_command("recordings_reconcile", "--once")

    from stapel_core.django.outbox.models import OutboxEvent

    assert not OutboxEvent.objects.filter(topic=events.ACTION_STAGE).exists()


def test_reconcile_emits_inside_a_transaction(make_recording, caplog):
    """The re-drive batch emits inside transaction.atomic — no
    'emit outside atomic' warning noise from stapel-core."""
    import logging

    r = make_recording(status=RecordingStatus.TRANSCRIBING, metadata={"pipeline": {"stage_index": 1}})
    _age(r, hours=2)

    with caplog.at_level(logging.WARNING, logger="stapel_core.comm.actions"):
        call_command("recordings_reconcile", "--once")

    assert "outside transaction.atomic" not in caplog.text


def test_cleanup_abandoned_is_a_conditional_update(make_recording):
    """The abandoned-upload sweep must not clobber a recording that
    finalized between the queryset snapshot and the write (conditional
    UPDATE re-checks status + missing storage key in the WHERE clause)."""
    from unittest import mock

    from stapel_recordings.management.commands.recordings_reconcile import Command

    r = make_recording(status=RecordingStatus.UPLOADING, file_storage_key=None)
    _age(r, hours=2)

    real_filter = Recording.objects.filter

    def finalize_then_filter(*args, **kwargs):
        # Simulate finalize_upload winning the race right after the sweep's
        # snapshot: the row is queued with a stored object + pipeline state.
        if kwargs.get("pk") == r.pk:
            real_filter(pk=r.pk).update(
                status=RecordingStatus.QUEUED,
                file_storage_key="recordings/ws/r/audio",
                metadata={"pipeline": {"stage_index": 0}},
            )
        return real_filter(*args, **kwargs)

    with mock.patch.object(Recording.objects, "filter", side_effect=finalize_then_filter):
        count = Command().cleanup_abandoned_uploads()

    assert count == 0
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED  # not clobbered to error
    assert r.metadata == {"pipeline": {"stage_index": 0}}
