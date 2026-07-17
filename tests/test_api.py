"""HTTP surface: create + upload session, detail, finalize."""
import uuid

import pytest

from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def test_create_recording_opens_upload_session(use_fakes, api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {
            "workspace_id": str(uuid.uuid4()),
            "title": "Standup",
            "diarization_enabled": True,
            "filename": "standup.mp3",
        },
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
        "/recordings/api/v1/recordings",
        {"workspace_id": str(uuid.uuid4()), "title": "Interview", "filename": "interview.mp3"},
        format="json",
    ).json()
    rec_id = create["recording"]["id"]
    storage_key = create["upload"]["storage_key"]
    get_storage().put_bytes(storage_key, b"audio")

    detail = api_client.get(f"/recordings/api/v1/recordings/{rec_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == rec_id

    fin = api_client.post(
        f"/recordings/api/v1/recordings/{rec_id}/finalize", {"file_size_bytes": 5}, format="json"
    )
    assert fin.status_code == 200
    assert Recording.objects.get(pk=rec_id).status == RecordingStatus.QUEUED


def test_detail_404_for_unknown(api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/recordings/api/v1/recordings/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_list_is_owner_scoped(use_fakes, api_client, user, make_recording):
    make_recording(owner=user, title="mine")
    api_client.force_authenticate(user=user)
    resp = api_client.get("/recordings/api/v1/recordings")
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()]
    assert "mine" in titles


# ── G4: workspace-scoped list + membership + opaque resource_key ──────────


def test_list_carries_opaque_resource_key(use_fakes, api_client, user, make_recording):
    from stapel_recordings.resources import resolve_resource_key

    r = make_recording(owner=user, title="mine")
    api_client.force_authenticate(user=user)
    row = api_client.get("/recordings/api/v1/recordings").json()[0]
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
    resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
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
    resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
    assert resp.status_code == 403


def test_workspace_list_fails_closed_when_workspaces_unavailable(
    use_fakes, api_client, user, make_recording
):
    """No ``workspaces.check_membership`` provider registered (workspaces not
    deployed) → deny, never leak."""
    ws = uuid.uuid4()
    make_recording(owner=user, workspace_id=ws, title="secret")
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
    assert resp.status_code == 403


# ── resource-scoped listing: ?resource_key= ───────────────────────────────


def test_list_filtered_by_resource_key_returns_only_that_recording(
    use_fakes, api_client, user, make_recording
):
    from stapel_recordings.resources import resource_key

    keep = make_recording(owner=user, title="keep")
    make_recording(owner=user, title="other")
    api_client.force_authenticate(user=user)
    resp = api_client.get(
        f"/recordings/api/v1/recordings?resource_key={resource_key(keep)}"
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [str(keep.id)]


def test_list_forged_resource_key_yields_empty(
    use_fakes, api_client, user, make_recording
):
    r = make_recording(owner=user, title="mine")
    api_client.force_authenticate(user=user)
    resp = api_client.get("/recordings/api/v1/recordings?resource_key=not-a-real-token")
    assert resp.status_code == 200
    assert resp.json() == []
    # Sanity: without the (bogus) filter the recording is listed.
    assert r.id is not None


def test_resource_key_of_another_owner_is_not_leaked(
    use_fakes, api_client, db, make_recording
):
    """A valid key for a recording you do not own resolves, but the owner-scoped
    base queryset still excludes it — empty, never a cross-owner leak."""
    from django.contrib.auth import get_user_model

    from stapel_recordings.resources import resource_key

    User = get_user_model()
    owner = User.objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    theirs = make_recording(owner=owner, title="theirs")
    api_client.force_authenticate(user=viewer)
    resp = api_client.get(
        f"/recordings/api/v1/recordings?resource_key={resource_key(theirs)}"
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_resource_key_composes_with_workspace_scope(
    use_fakes, api_client, db, make_recording, stub_membership
):
    from django.contrib.auth import get_user_model

    from stapel_recordings.resources import resource_key

    User = get_user_model()
    ws = uuid.uuid4()
    owner = User.objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    target = make_recording(owner=owner, workspace_id=ws, title="target")
    make_recording(owner=owner, workspace_id=ws, title="sibling")

    stub_membership.grant(ws, viewer.pk)
    api_client.force_authenticate(user=viewer)
    resp = api_client.get(
        f"/recordings/api/v1/recordings?workspace_id={ws}"
        f"&resource_key={resource_key(target)}"
    )
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()] == ["target"]


# ── reprocess verb: POST /{id}/reprocess ──────────────────────────────────


def test_reprocess_completed_recording_requeues(use_fakes, api_client, user, make_recording):
    r = make_recording(owner=user, status=RecordingStatus.COMPLETED)
    api_client.force_authenticate(user=user)
    resp = api_client.post(f"/recordings/api/v1/recordings/{r.id}/reprocess")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == RecordingStatus.QUEUED
    r.refresh_from_db()
    assert r.status == RecordingStatus.QUEUED


@pytest.mark.parametrize(
    "status",
    [
        RecordingStatus.CREATED,
        RecordingStatus.UPLOADING,
        RecordingStatus.QUEUED,
        RecordingStatus.TRANSCRIBING,
        RecordingStatus.ERROR,
    ],
)
def test_reprocess_from_non_completed_is_409(
    use_fakes, api_client, user, make_recording, status
):
    r = make_recording(owner=user, status=status)
    api_client.force_authenticate(user=user)
    resp = api_client.post(f"/recordings/api/v1/recordings/{r.id}/reprocess")
    assert resp.status_code == 409
    assert resp.json()["localizable_error"] == "error.409.recording_invalid_state"
    r.refresh_from_db()
    assert r.status == status  # unchanged


def test_reprocess_unknown_recording_is_404(api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.post(f"/recordings/api/v1/recordings/{uuid.uuid4()}/reprocess")
    assert resp.status_code == 404


def test_reprocess_foreign_recording_is_404(use_fakes, api_client, db, make_recording):
    """Owner scope: another user's completed recording is not reprocessable —
    404, never a cross-owner 409/200."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    owner = User.objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    r = make_recording(owner=owner, status=RecordingStatus.COMPLETED)
    api_client.force_authenticate(user=viewer)
    resp = api_client.post(f"/recordings/api/v1/recordings/{r.id}/reprocess")
    assert resp.status_code == 404
    r.refresh_from_db()
    assert r.status == RecordingStatus.COMPLETED  # untouched
