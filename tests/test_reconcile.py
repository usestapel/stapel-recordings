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
