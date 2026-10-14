"""The same owner uploading the same file again (duplicates.py)."""
import json
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_recordings import chunked, duplicates, events, services
from stapel_recordings.models import Recording, RecordingStatus, UploadSession
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db

PART = 4
DATA = b"0123456789"
FP = chunked.fingerprint_bytes(DATA, PART)[0]


@pytest.fixture
def small_parts():
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "MULTIPART_PART_SIZE": PART,
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        yield
        storage.reset_storage_cache()


def _uploaded(make_recording, **kwargs):
    """A recording whose verified upload of DATA finished."""
    r = make_recording(status=RecordingStatus.CREATED, **kwargs)
    session, _, _ = services.start_multipart_upload(
        recording=r, file_size_bytes=len(DATA), filename="take.mp3", fingerprint=FP
    )
    for n, chunk in enumerate([DATA[i:i + PART] for i in range(0, len(DATA), PART)], 1):
        import base64
        import hashlib

        fakes.put_part(
            session.multipart_upload_id, n, chunk,
            sha256_b64=base64.b64encode(hashlib.sha256(chunk).digest()).decode(),
        )
    services.finalize_upload(session=session, parts=[])
    r.refresh_from_db()
    return r


def _start(recording, **kw):
    return services.start_multipart_upload(
        recording=recording, file_size_bytes=len(DATA), filename="take.mp3",
        fingerprint=FP, **kw,
    )


# ── refusal at upload ────────────────────────────────────────────────────


def test_second_start_of_the_same_file_is_409_naming_the_first(small_parts, make_recording):
    first = _uploaded(make_recording)
    second = make_recording(status=RecordingStatus.CREATED, workspace_id=first.workspace_id)
    with pytest.raises(duplicates.UploadDuplicate) as exc:
        _start(second)
    assert exc.value.http_status == 409
    assert exc.value.error_key == "error.409.recording_upload_duplicate"
    assert exc.value.error_params == {"recording_id": str(first.pk)}
    # Refused before any storage state exists.
    assert not UploadSession.objects.filter(recording=second).exists()


def test_the_match_crosses_workspaces_of_the_same_owner(small_parts, make_recording):
    first = _uploaded(make_recording)
    other_ws = make_recording(status=RecordingStatus.CREATED, workspace_id=uuid.uuid4())
    assert other_ws.workspace_id != first.workspace_id
    with pytest.raises(duplicates.UploadDuplicate):
        _start(other_ws)


def test_another_owner_is_not_matched(small_parts, make_recording):
    from django.contrib.auth import get_user_model

    _uploaded(make_recording)
    stranger = get_user_model().objects.create(username=f"s-{uuid.uuid4().hex[:6]}")
    theirs = make_recording(status=RecordingStatus.CREATED, owner=stranger)
    _start(theirs)  # no refusal


def test_a_deleted_original_is_not_matched(small_parts, make_recording):
    first = _uploaded(make_recording)
    first.deleted_at = timezone.now()
    first.status = RecordingStatus.DELETED
    first.save(update_fields=["deleted_at", "status"])
    again = make_recording(status=RecordingStatus.CREATED)
    _start(again)  # no refusal


def test_an_unfinished_upload_is_not_a_duplicate(small_parts, make_recording):
    open_one = make_recording(status=RecordingStatus.CREATED)
    _start(open_one)
    again = make_recording(status=RecordingStatus.CREATED)
    _start(again)  # resumed through lookup, never refused


def test_allow_duplicate_starts_and_records_the_request(small_parts, make_recording):
    first = _uploaded(make_recording)
    second = make_recording(status=RecordingStatus.CREATED)
    session, _, _ = _start(second, allow_duplicate=True)
    assert session.fingerprint == FP
    second.refresh_from_db()
    block = second.metadata[duplicates.METADATA_KEY]
    assert block["of"] == str(first.pk) and block["requested_by_user"] is True


def test_single_put_session_keeps_the_fingerprint_and_is_refused_too(small_parts, make_recording):
    first = _uploaded(make_recording)
    second = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(duplicates.UploadDuplicate):
        services.create_upload_session(recording=second, filename="a.mp3", fingerprint=FP)
    third = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(
        recording=third, filename="a.mp3", fingerprint=FP, allow_duplicate=True
    )
    assert session.fingerprint == FP and not session.is_multipart
    # A single-PUT session is never offered for resume, nor treated as verified.
    assert not chunked.is_verified(session)
    found = chunked.lookup(third.workspace_id, FP)
    assert found is None or found["recording"].pk != third.pk
    with pytest.raises(chunked.NotAVerifiedUpload):
        chunked.manifest(session)
    assert first.pk


def test_a_finished_single_put_with_a_fingerprint_is_matched(small_parts, make_recording):
    from stapel_recordings.storage import get_storage

    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r, filename="a.mp3", fingerprint=FP)
    get_storage().put_bytes(session.storage_key, b"ID3" + b"\x00" * 64, content_type="audio/mpeg")
    UploadSession.objects.filter(pk=session.pk).update(finalized_at=timezone.now())
    Recording.objects.filter(pk=r.pk).update(file_storage_key=session.storage_key)
    assert duplicates.find_upload_duplicate(r.owner_id, FP).pk == r.pk


# ── HTTP ─────────────────────────────────────────────────────────────────

BASE = "/recordings/api/v1/recordings"


def test_http_start_answers_409_and_allow_duplicate_passes(
    small_parts, api_client, user, make_recording
):
    first = _uploaded(make_recording)
    second = make_recording(status=RecordingStatus.CREATED)
    api_client.force_authenticate(user=user)
    body = {"file_size_bytes": len(DATA), "fingerprint": FP, "filename": "take.mp3"}
    refused = api_client.post(f"{BASE}/{second.id}/multipart", body, format="json")
    assert refused.status_code == 409
    envelope = refused.json()
    assert "error.409.recording_upload_duplicate" in str(envelope)
    assert str(first.pk) in str(envelope)
    ok = api_client.post(
        f"{BASE}/{second.id}/multipart", {**body, "allow_duplicate": True}, format="json"
    )
    assert ok.status_code == 201, ok.content


def test_http_create_with_a_known_fingerprint_creates_no_row(
    small_parts, api_client, user, make_recording
):
    first = _uploaded(make_recording)
    api_client.force_authenticate(user=user)
    before = Recording.objects.count()
    body = {
        "workspace_id": str(uuid.uuid4()), "title": "again", "filename": "take.mp3",
        "fingerprint": FP,
    }
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "MULTIPART_PART_SIZE": PART,
            "REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": False,
        }
    ):
        refused = api_client.post(BASE, body, format="json")
        assert refused.status_code == 409, refused.content
        assert str(first.pk) in str(refused.json())
        assert Recording.objects.count() == before
        ok = api_client.post(BASE, {**body, "allow_duplicate": True}, format="json")
    assert ok.status_code == 201, ok.content
    assert Recording.objects.count() == before + 1


# ── the charge guard ─────────────────────────────────────────────────────


def _completed(r, duration, size=None):
    Recording.objects.filter(pk=r.pk).update(
        status=RecordingStatus.COMPLETED, duration_seconds=duration,
        **({"file_size_bytes": size} if size is not None else {}),
    )
    r.refresh_from_db()
    return r


def test_judge_run_waives_a_verified_repeat(small_parts, make_recording):
    first = _completed(_uploaded(make_recording), 600.0)
    second = make_recording(status=RecordingStatus.CREATED, workspace_id=uuid.uuid4())
    second = _completed_upload_allowed(second)
    second.duration_seconds = 600.2
    block = duplicates.judge_run(second, run_id="run-2")
    assert block["of"] == str(first.pk)
    assert block["match"] == duplicates.MATCH_FINGERPRINT
    # requested_by_user came with allow_duplicate: that run is charged.
    assert block["charge"] == duplicates.CHARGE_CHARGED


def _completed_upload_allowed(r):
    session, _, _ = _start(r, allow_duplicate=True)
    import base64
    import hashlib

    for n, chunk in enumerate([DATA[i:i + PART] for i in range(0, len(DATA), PART)], 1):
        fakes.put_part(
            session.multipart_upload_id, n, chunk,
            sha256_b64=base64.b64encode(hashlib.sha256(chunk).digest()).decode(),
        )
    services.finalize_upload(session=session, parts=[])
    r.refresh_from_db()
    return r


def test_judge_run_waives_an_unrequested_verified_repeat(small_parts, make_recording):
    first = _completed(_uploaded(make_recording), 600.0)
    # A client that skipped the refusal: same verified file, no request marker.
    second = _completed_upload_allowed(make_recording(status=RecordingStatus.CREATED))
    meta = dict(second.metadata)
    meta.pop(duplicates.METADATA_KEY)
    Recording.objects.filter(pk=second.pk).update(metadata=meta, duration_seconds=600.0)
    second.refresh_from_db()
    block = duplicates.judge_run(second, run_id="run-1")
    assert block["of"] == str(first.pk)
    assert block["charge"] == duplicates.CHARGE_WAIVED
    assert duplicates.completed_fields(second, "run-1") == {
        "duplicate_of": str(first.pk), "charge_waived": True,
    }
    # Another run's verdict says nothing about this one.
    assert duplicates.completed_fields(second, "run-9") == {}


def test_a_trimmed_original_does_not_cover_a_full_run(small_parts, make_recording):
    _completed(_uploaded(make_recording), 600.0)  # the free ten minutes
    second = _completed_upload_allowed(make_recording(status=RecordingStatus.CREATED))
    meta = dict(second.metadata)
    meta.pop(duplicates.METADATA_KEY)
    Recording.objects.filter(pk=second.pk).update(metadata=meta, duration_seconds=2400.0)
    second.refresh_from_db()
    assert duplicates.judge_run(second, run_id="run-1") is None


def test_legacy_match_by_size_and_duration(make_recording):
    first = make_recording(status=RecordingStatus.COMPLETED, duration_seconds=1800.0, file_size_bytes=12345678)
    near = make_recording(status=RecordingStatus.QUEUED, duration_seconds=1800.3, file_size_bytes=12345678)
    block = duplicates.judge_run(near, run_id="r")
    assert block == {**block, "of": str(first.pk), "match": duplicates.MATCH_SIZE_DURATION,
                     "charge": duplicates.CHARGE_WAIVED}
    far = make_recording(status=RecordingStatus.QUEUED, duration_seconds=1801.0, file_size_bytes=12345678)
    assert duplicates.judge_run(far, run_id="r") is None
    other_size = make_recording(status=RecordingStatus.QUEUED, duration_seconds=1800.0, file_size_bytes=12345679)
    assert duplicates.judge_run(other_size, run_id="r") is None


def test_legacy_match_ignores_deleted_and_unfinished(make_recording):
    gone = make_recording(status=RecordingStatus.COMPLETED, duration_seconds=60.0, file_size_bytes=999)
    gone.deleted_at = timezone.now()
    gone.save(update_fields=["deleted_at"])
    make_recording(status=RecordingStatus.ERROR, duration_seconds=60.0, file_size_bytes=999)
    r = make_recording(status=RecordingStatus.QUEUED, duration_seconds=60.0, file_size_bytes=999)
    assert duplicates.judge_run(r, run_id="r") is None


def test_unmeasured_duration_matches_nothing(make_recording):
    make_recording(status=RecordingStatus.COMPLETED, duration_seconds=60.0, file_size_bytes=999)
    r = make_recording(status=RecordingStatus.QUEUED, duration_seconds=None, file_size_bytes=999)
    assert duplicates.judge_run(r, run_id="r") is None


def test_a_stale_verdict_is_cleared_when_nothing_matches(make_recording):
    r = make_recording(
        status=RecordingStatus.QUEUED, duration_seconds=60.0, file_size_bytes=1,
        metadata={duplicates.METADATA_KEY: {"of": str(uuid.uuid4()), "charge": "waived", "run_id": "old"}},
    )
    assert duplicates.judge_run(r, run_id="new") is None
    r.refresh_from_db()
    assert duplicates.METADATA_KEY not in r.metadata


def test_completed_event_carries_the_verdict(make_recording):
    from stapel_core.django.outbox.models import OutboxEvent

    first = make_recording(status=RecordingStatus.COMPLETED, duration_seconds=60.0, file_size_bytes=999)
    r = make_recording(status=RecordingStatus.COMPLETED, duration_seconds=60.0, file_size_bytes=999)
    duplicates.judge_run(r, run_id="run-a")
    events.emit_completed(r, run_id="run-a", attempt=1)
    payload = json.loads(
        OutboxEvent.objects.filter(topic=events.ACTION_COMPLETED).latest("pk").event_json
    )["payload"]
    assert payload["duplicate_of"] == str(first.pk)
    assert payload["charge_waived"] is True


def test_completed_event_without_a_verdict_is_unchanged(make_recording):
    from stapel_core.django.outbox.models import OutboxEvent

    r = make_recording(status=RecordingStatus.COMPLETED, duration_seconds=60.0)
    events.emit_completed(r, run_id="run-a", attempt=1)
    payload = json.loads(
        OutboxEvent.objects.filter(topic=events.ACTION_COMPLETED).latest("pk").event_json
    )["payload"]
    assert "duplicate_of" not in payload and "charge_waived" not in payload


def test_the_verdict_key_is_reserved():
    from stapel_recordings.metadata import reserved_keys

    assert duplicates.METADATA_KEY in reserved_keys()


def test_a_recording_created_with_the_request_stays_allowed(small_parts, make_recording):
    first = _uploaded(make_recording)
    second = make_recording(status=RecordingStatus.CREATED)
    duplicates.note_requested(second, first)
    assert duplicates.was_requested(second)
    session, _, _ = _start(second)  # no allow_duplicate on this call
    assert session.fingerprint == FP
    third = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(duplicates.UploadDuplicate):
        _start(third)
