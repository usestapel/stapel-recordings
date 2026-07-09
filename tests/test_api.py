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


# ── G4: workspace-scoped list + membership + opaque resource_key ──────────


def test_list_carries_opaque_resource_key(use_fakes, api_client, user, make_recording):
    from stapel_recordings.resources import resolve_resource_key

    r = make_recording(owner=user, title="mine")
    api_client.force_authenticate(user=user)
    row = api_client.get("/recordings/api/recordings").json()[0]
    rk = row["resource_key"]
    # Opaque: not the raw id, and not trivially derivable from it.
    assert rk and rk != str(r.id) and str(r.id) not in rk
    # Server-resolvable back to the recording; a forged token does not resolve.
    assert resolve_resource_key(rk) == str(r.id)
    assert resolve_resource_key(rk + "x") is None


def test_workspace_list_returns_all_members_recordings_for_a_member(
    use_fakes, api_client, db, make_recording, stub_membership
):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    ws = uuid.uuid4()
    owner_a = User.objects.create(username=f"a-{uuid.uuid4().hex[:8]}")
    owner_b = User.objects.create(username=f"b-{uuid.uuid4().hex[:8]}")
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    make_recording(owner=owner_a, workspace_id=ws, title="a-rec")
    make_recording(owner=owner_b, workspace_id=ws, title="b-rec")
    make_recording(owner=owner_a, workspace_id=uuid.uuid4(), title="other-ws")

    stub_membership.grant(ws, viewer.pk)
    api_client.force_authenticate(user=viewer)
    resp = api_client.get(f"/recordings/api/recordings?workspace_id={ws}")
    assert resp.status_code == 200
    titles = {r["title"] for r in resp.json()}
    # Sees both members' recordings in the workspace, not the other workspace.
    assert titles == {"a-rec", "b-rec"}


def test_workspace_list_forbidden_for_non_member(
    use_fakes, api_client, user, make_recording, stub_membership
):
    ws = uuid.uuid4()
    make_recording(owner=user, workspace_id=ws, title="secret")
    # No grant → not a member.
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/recordings/api/recordings?workspace_id={ws}")
    assert resp.status_code == 403


def test_workspace_list_fails_closed_when_workspaces_unavailable(
    use_fakes, api_client, user, make_recording
):
    """No ``workspaces.check_membership`` provider registered (workspaces not
    deployed) → deny, never leak."""
    ws = uuid.uuid4()
    make_recording(owner=user, workspace_id=ws, title="secret")
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/recordings/api/recordings?workspace_id={ws}")
    assert resp.status_code == 403
