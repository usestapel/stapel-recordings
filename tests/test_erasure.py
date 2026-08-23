"""Subject-scoped erasure: the four subjects, the receipts, the purge.

What is being pinned here is the protocol, not the SQL: an owner that erases
but never receipts is indistinguishable from one that was never deployed,
and an owner that answers the probe from somewhere other than the erasure
subscriber proves only that a container is running.
"""
import json
import uuid

import pytest
from django.test import override_settings

from stapel_recordings.erasure import (
    SUBJECT_ACCOUNT,
    SUBJECT_MEETING,
    SUBJECT_RECORDING,
    SUBJECT_TYPES,
    SUBJECT_WORKSPACE,
    erase,
)
from stapel_recordings.models import Recording, RecordingStatus, Segment, Speaker

pytestmark = pytest.mark.django_db


def _stored(make_recording, **kwargs):
    """A completed recording whose three objects exist in FakeStorage."""
    from stapel_recordings.storage import get_storage

    rid = uuid.uuid4()
    recording = make_recording(
        id=rid,
        status=RecordingStatus.COMPLETED,
        file_storage_key=f"recordings/{rid}/audio",
        normalized_storage_key=f"recordings/{rid}/audio.normalized.wav",
        transcript_storage_key=f"recordings/{rid}/transcript.json",
        summary="hi",
        **kwargs,
    )
    storage = get_storage()
    for key in (
        recording.file_storage_key,
        recording.normalized_storage_key,
        recording.transcript_storage_key,
    ):
        storage.put_bytes(key, b"x")
    return recording


def _with_transcript(recording):
    speaker = Speaker.objects.create(recording=recording, label="speaker_0")
    Segment.objects.create(
        recording=recording, speaker=speaker, sequence_num=0,
        start_time=0.0, end_time=1.0, text="hello",
    )
    return recording


def _objects_gone(recording):
    from stapel_recordings.storage import get_storage

    storage = get_storage()
    return not any(
        storage.head_object(key)[0]
        for key in (
            recording.file_storage_key,
            recording.normalized_storage_key,
            recording.transcript_storage_key,
        )
    )


# ── the four subject types ────────────────────────────────────────────

def test_erase_account(use_fakes, make_recording, user):
    mine = _with_transcript(_stored(make_recording))
    counts = erase(SUBJECT_ACCOUNT, user.id)

    assert counts["recordings"] == 1
    assert counts["segments"] == 1 and counts["speakers"] == 1
    assert counts["storage_objects"] == 3
    assert not Recording.objects.filter(owner_id=user.id).exists()
    assert _objects_gone(mine)


def test_erase_workspace_leaves_other_workspaces(use_fakes, make_recording):
    workspace = uuid.uuid4()
    mine = _stored(make_recording, workspace_id=workspace)
    other = _stored(make_recording, workspace_id=uuid.uuid4())

    counts = erase(SUBJECT_WORKSPACE, workspace)

    assert counts["recordings"] == 1
    assert not Recording.objects.filter(pk=mine.pk).exists()
    assert Recording.objects.filter(pk=other.pk).exists()
    assert not _objects_gone(other)


def test_erase_recording(use_fakes, make_recording):
    target = _with_transcript(_stored(make_recording))
    keeper = _stored(make_recording)

    counts = erase(SUBJECT_RECORDING, target.id)

    assert counts["recordings"] == 1
    assert list(Recording.objects.values_list("pk", flat=True)) == [keeper.pk]
    assert _objects_gone(target)


def test_erase_meeting_matches_the_recording_id(use_fakes, make_recording):
    """A recording IS the meeting where the host has no separate entity —
    transcript_schema already numbers transcripts by meeting_id=recording.id."""
    target = _stored(make_recording)

    counts = erase(SUBJECT_MEETING, target.id)

    assert counts["recordings"] == 1
    assert not Recording.objects.filter(pk=target.pk).exists()


def test_erase_meeting_matches_the_metadata_link(use_fakes, make_recording):
    """A host with its own meeting object tags recordings with meeting_id;
    all of them go, and nothing else does."""
    meeting = str(uuid.uuid4())
    first = _stored(make_recording, metadata={"meeting_id": meeting})
    second = _stored(make_recording, metadata={"meeting_id": meeting})
    unrelated = _stored(make_recording, metadata={"meeting_id": str(uuid.uuid4())})

    counts = erase(SUBJECT_MEETING, meeting)

    assert counts["recordings"] == 2
    assert not Recording.objects.filter(pk__in=[first.pk, second.pk]).exists()
    assert Recording.objects.filter(pk=unrelated.pk).exists()


def test_erase_meeting_is_scoped_by_workspace_when_stated(use_fakes, make_recording):
    meeting = "external-meeting-7"
    workspace = uuid.uuid4()
    mine = _stored(make_recording, workspace_id=workspace, metadata={"meeting_id": meeting})
    theirs = _stored(
        make_recording, workspace_id=uuid.uuid4(), metadata={"meeting_id": meeting}
    )

    counts = erase(SUBJECT_MEETING, meeting, workspace_id=workspace)

    assert counts["recordings"] == 1
    assert not Recording.objects.filter(pk=mine.pk).exists()
    assert Recording.objects.filter(pk=theirs.pk).exists()


def test_unknown_subject_erases_nothing(use_fakes, make_recording):
    kept = _stored(make_recording)

    assert erase("document", str(kept.id)) == {}
    assert Recording.objects.filter(pk=kept.pk).exists()


def test_malformed_subject_key_is_a_zero_count_not_a_crash(use_fakes, make_recording):
    kept = _stored(make_recording)

    assert erase(SUBJECT_RECORDING, "not-a-uuid") == {}
    assert Recording.objects.filter(pk=kept.pk).exists()


# ── idempotency ───────────────────────────────────────────────────────

def test_second_erase_removes_nothing_and_still_receipts(
    use_fakes, make_recording, user, drain
):
    from stapel_core.comm import emit

    _with_transcript(_stored(make_recording))
    correlation = str(uuid.uuid4())

    for _ in range(2):
        emit("gdpr.erasure.requested", {
            "request_id": 1,
            "correlation_id": correlation,
            "subject_type": SUBJECT_ACCOUNT,
            "subject_key": str(user.id),
        })
        drain()

    receipts = _receipts()
    assert len(receipts) == 2, "a redelivery must still confirm — silence times out"
    assert receipts[0]["counts"]["recordings"] == 1
    assert receipts[1]["counts"] == {}, "nothing left to remove the second time"
    assert not Recording.objects.exists()


# ── the subscriber: receipts and the probe, from the same module ──────

def _receipts():
    from stapel_core.django.outbox.models import OutboxEvent

    return [
        json.loads(row.event_json)["payload"]
        for row in OutboxEvent.objects.filter(topic="gdpr.section.erased")
    ]


@pytest.mark.parametrize("subject_type", SUBJECT_TYPES)
def test_erasure_requested_erases_and_receipts_every_claimed_subject(
    use_fakes, make_recording, user, drain, subject_type
):
    from stapel_core.comm import emit

    workspace = uuid.uuid4()
    recording = _stored(make_recording, workspace_id=workspace)
    subject_key = {
        SUBJECT_ACCOUNT: str(user.id),
        SUBJECT_WORKSPACE: str(workspace),
        SUBJECT_MEETING: str(recording.id),
        SUBJECT_RECORDING: str(recording.id),
    }[subject_type]
    correlation = str(uuid.uuid4())

    emit("gdpr.erasure.requested", {
        "request_id": 7,
        "correlation_id": correlation,
        "subject_type": subject_type,
        "subject_key": subject_key,
    })
    drain()

    assert not Recording.objects.exists()
    assert _objects_gone(recording)
    receipt = _receipts()[0]
    assert receipt["owner"] == "recordings"
    assert receipt["correlation_id"] == correlation
    assert receipt["subject_type"] == subject_type
    assert receipt["subject_key"] == subject_key
    assert receipt["counts"]["recordings"] == 1


def test_unclaimed_subject_type_is_not_receipted(use_fakes, make_recording, drain):
    """Receipting a subject this module does not claim would invent an
    answer for a part the orchestrator never opened."""
    from stapel_core.comm import emit

    _stored(make_recording)
    emit("gdpr.erasure.requested", {
        "request_id": 8,
        "correlation_id": str(uuid.uuid4()),
        "subject_type": "document",
        "subject_key": str(uuid.uuid4()),
    })
    drain()

    assert Recording.objects.count() == 1
    assert _receipts() == []


def test_probe_is_answered_from_the_erasure_subscriber(use_fakes, drain):
    from stapel_core.comm import emit
    from stapel_core.django.outbox.models import OutboxEvent

    import stapel_recordings.actions as actions

    # Co-location is the evidence the protocol asks for: both handlers are
    # in the module that erases, so an answer cannot come from a process
    # that merely imported the models.
    assert actions.handle_owner_probe.__module__ == actions.handle_erasure_requested.__module__

    correlation = str(uuid.uuid4())
    emit("gdpr.owner.probe", {"correlation_id": correlation})
    drain()

    alive = [
        json.loads(row.event_json)["payload"]
        for row in OutboxEvent.objects.filter(topic="gdpr.owner.alive")
    ]
    assert alive == [{
        "owner": "recordings",
        "subject_types": list(SUBJECT_TYPES),
        "correlation_id": correlation,
    }]


def test_declared_owner_matches_the_gdpr_provider_section():
    from stapel_recordings.erasure import OWNER
    from stapel_recordings.gdpr import RecordingsGDPRProvider

    assert OWNER == RecordingsGDPRProvider.section


def test_user_deleted_still_erases_and_now_receipts(use_fakes, make_recording, user, drain):
    """The deprecated action keeps working for one minor — through the same
    erase() the erasure handler runs."""
    from stapel_core.comm import emit

    correlation = str(uuid.uuid4())
    _stored(make_recording)
    emit("user.deleted", {"user_id": str(user.id), "correlation_id": correlation})
    drain()

    assert not Recording.objects.filter(owner_id=user.id).exists()
    receipt = _receipts()[0]
    assert receipt["subject_type"] == SUBJECT_ACCOUNT
    assert receipt["correlation_id"] == correlation
    assert receipt["counts"]["recordings"] == 1


# ── the scheduled purge ───────────────────────────────────────────────

def _purge_settings(**extra):
    return override_settings(STAPEL_RECORDINGS={
        "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
        "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
        "ERASURE_CLIENT": "stapel_recordings.tests.fakes.FakeErasureClient",
        **extra,
    })


def _age(recording, days):
    from datetime import timedelta

    from django.utils import timezone

    Recording.objects.filter(pk=recording.pk).update(
        deleted_at=timezone.now() - timedelta(days=days)
    )
    return recording


def test_purge_requests_erasure_only_for_aged_rows(use_fakes, make_recording):
    from stapel_recordings.tasks import purge_soft_deleted_recordings
    from stapel_recordings.tests import fakes

    aged = _age(_stored(make_recording), days=31)
    _age(_stored(make_recording), days=3)      # inside the window
    _stored(make_recording)                    # never deleted

    with _purge_settings():
        result = purge_soft_deleted_recordings()

    assert result == {"aged": 1, "requested": 1, "already_open": 0, "skipped": 0}
    assert fakes.ERASURE_REQUESTS == [
        (SUBJECT_RECORDING, str(aged.id), str(aged.workspace_id))
    ]
    # The purge asks; the erasure subscriber is what destroys.
    assert Recording.objects.count() == 3


def test_purge_honours_a_shorter_window(use_fakes, make_recording):
    from stapel_recordings.tasks import purge_soft_deleted_recordings
    from stapel_recordings.tests import fakes

    recent = _age(_stored(make_recording), days=3)

    with _purge_settings(PURGE_AFTER_DAYS=1):
        result = purge_soft_deleted_recordings()

    assert result["requested"] == 1
    assert fakes.ERASURE_REQUESTS[0][1] == str(recent.id)


def test_purge_does_not_re_ask_while_an_erasure_is_open(use_fakes, make_recording):
    from stapel_recordings.tasks import purge_soft_deleted_recordings
    from stapel_recordings.tests import fakes

    aged = _age(_stored(make_recording), days=40)
    fakes.OPEN_ERASURES.add((SUBJECT_RECORDING, str(aged.id)))

    with _purge_settings():
        result = purge_soft_deleted_recordings()

    assert result == {"aged": 1, "requested": 0, "already_open": 1, "skipped": 0}
    assert fakes.ERASURE_REQUESTS == []


def test_purge_without_a_gdpr_client_reports_instead_of_deleting(use_fakes, make_recording):
    from stapel_recordings.tasks import purge_soft_deleted_recordings

    aged = _age(_stored(make_recording), days=90)

    with override_settings(STAPEL_RECORDINGS={
        "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
        "ERASURE_CLIENT": "stapel_recordings.tests.fakes.UnavailableErasureClient",
    }):
        result = purge_soft_deleted_recordings()

    assert result == {"aged": 1, "requested": 0, "already_open": 0, "skipped": 1}
    assert Recording.objects.filter(pk=aged.pk).exists()


def test_beat_schedule_points_at_the_purge_task():
    from stapel_recordings.tasks import PURGE_TASK_NAME, get_recordings_beat_schedule

    entries = list(get_recordings_beat_schedule().values())
    assert [e["task"] for e in entries] == [PURGE_TASK_NAME]


# ── the check that says the purge will never run ──────────────────────

def _w010():
    from stapel_recordings.checks import check_purge_is_scheduled

    return [w.id for w in check_purge_is_scheduled(None)]


def test_check_fires_when_a_beat_schedule_runs_everything_but_the_purge():
    with override_settings(CELERY_BEAT_SCHEDULE={
        "something-else": {"task": "stapel_gdpr.tasks.probe_data_owners"},
    }):
        assert _w010() == ["stapel_recordings.W010"]


def test_check_is_silent_once_the_schedule_is_registered():
    from stapel_recordings.tasks import get_recordings_beat_schedule

    with override_settings(CELERY_BEAT_SCHEDULE=get_recordings_beat_schedule()):
        assert _w010() == []


def test_check_does_not_second_guess_a_host_without_a_beat_schedule(settings):
    if hasattr(settings, "CELERY_BEAT_SCHEDULE"):  # pragma: no cover
        del settings.CELERY_BEAT_SCHEDULE
    assert _w010() == []
