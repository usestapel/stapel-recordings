"""HTTP surface: create + upload session, detail, finalize."""
import uuid

import pytest

from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def test_create_recording_opens_upload_session(use_fakes, api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/recordings",
        {"workspace_id": str(uuid.uuid4()), "title": "Standup", "diarization_enabled": True},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["recording"]["title"] == "Standup"
    assert body["recording"]["status"] == RecordingStatus.UPLOADING
    assert body["upload"]["presigned_url"].startswith("memory://put/")


def test_detail_and_finalize(use_fakes, api_client, user):
    from stapel_recordings.storage import get_storage

    api_client.force_authenticate(user=user)
    create = api_client.post(
        "/recordings/api/recordings",
        {"workspace_id": str(uuid.uuid4()), "title": "Interview"},
        format="json",
    ).json()
    rec_id = create["recording"]["id"]
    storage_key = create["upload"]["storage_key"]
    get_storage().put_bytes(storage_key, b"audio")

    detail = api_client.get(f"/recordings/api/recordings/{rec_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == rec_id

    fin = api_client.post(
        f"/recordings/api/recordings/{rec_id}/finalize", {"file_size_bytes": 5}, format="json"
    )
    assert fin.status_code == 200
    assert Recording.objects.get(pk=rec_id).status == RecordingStatus.QUEUED


def test_detail_404_for_unknown(api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/recordings/api/recordings/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_list_is_owner_scoped(use_fakes, api_client, user, make_recording):
    make_recording(owner=user, title="mine")
    api_client.force_authenticate(user=user)
    resp = api_client.get("/recordings/api/recordings")
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()]
    assert "mine" in titles
