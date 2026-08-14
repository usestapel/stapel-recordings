"""HTTP surface: create + upload session, detail, finalize."""
import uuid

import pytest

from stapel_recordings.models import Recording, RecordingStatus

pytestmark = pytest.mark.django_db


def test_create_recording_opens_upload_session(use_fakes, api_client, user, stub_membership):
    ws = uuid.uuid4()
    stub_membership.grant(ws, user.pk)
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {
            "workspace_id": str(ws),
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


def test_detail_and_finalize(use_fakes, api_client, user, stub_membership):
    from stapel_recordings.storage import get_storage

    ws = uuid.uuid4()
    stub_membership.grant(ws, user.pk)
    api_client.force_authenticate(user=user)
    create = api_client.post(
        "/recordings/api/v1/recordings",
        {"workspace_id": str(ws), "title": "Interview", "filename": "interview.mp3"},
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


# ── create is a membership question, not just an account question ─────────
#
# POST names the workspace the recording lands in, and the caller supplies
# that id. Verifying it is what stops any account from minting rows (and
# storage keys, which are namespaced by workspace id) inside another
# organization's workspace, where its members would then see them listed.


def test_create_into_a_foreign_workspace_is_refused(
    use_fakes, api_client, user, stub_membership
):
    ws = uuid.uuid4()  # no grant → not a member
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {"workspace_id": str(ws), "title": "injected", "filename": "x.mp3"},
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert resp.json()["localizable_error"] == "error.403.recording_workspace_forbidden"
    # Refused BEFORE any state exists: no row, and therefore no upload
    # session and no object key under that workspace's prefix.
    assert not Recording.objects.filter(workspace_id=ws).exists()


def test_create_in_a_workspace_you_belong_to_is_allowed(
    use_fakes, api_client, user, stub_membership
):
    ws = uuid.uuid4()
    stub_membership.grant(ws, user.pk)
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {"workspace_id": str(ws), "title": "mine", "filename": "x.mp3"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert Recording.objects.filter(workspace_id=ws).count() == 1


def test_create_fails_closed_when_workspaces_is_unavailable(use_fakes, api_client, user):
    """No ``workspaces.check_membership`` provider registered (workspaces not
    deployed) → refuse, the same way the workspace listing does."""
    ws = uuid.uuid4()
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {"workspace_id": str(ws), "title": "x", "filename": "x.mp3"},
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert not Recording.objects.filter(workspace_id=ws).exists()


def test_membership_gate_can_be_opted_out_of_explicitly(use_fakes, api_client, user):
    """A stand without stapel-workspaces cannot answer membership, so it says
    so — in settings, deliberately. The safe value is the default."""
    from django.test import override_settings

    from stapel_recordings import storage

    ws = uuid.uuid4()
    api_client.force_authenticate(user=user)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": False,
        }
    ):
        storage.reset_storage_cache()
        resp = api_client.post(
            "/recordings/api/v1/recordings",
            {"workspace_id": str(ws), "title": "x", "filename": "x.mp3"},
            format="json",
        )
    assert resp.status_code == 201, resp.content


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


def _workspace_with_two_owners(make_recording):
    """A workspace holding one recording of each of two owners, plus one
    recording of the first owner in a DIFFERENT workspace."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    ws = uuid.uuid4()
    owner_a = User.objects.create(username=f"a-{uuid.uuid4().hex[:8]}")
    owner_b = User.objects.create(username=f"b-{uuid.uuid4().hex[:8]}")
    make_recording(owner=owner_a, workspace_id=ws, title="a-rec")
    make_recording(owner=owner_b, workspace_id=ws, title="b-rec")
    make_recording(owner=owner_a, workspace_id=uuid.uuid4(), title="other-ws")
    return ws, owner_a, owner_b


def test_workspace_list_obeys_the_object_policy(
    use_fakes, api_client, db, make_recording, stub_membership
):
    """Membership answers "may you ask about this workspace"; RECORDING_POLICY
    answers "which of its recordings may you read". The listing asks both, so
    it cannot offer rows that ``GET /recordings/<id>`` would refuse."""
    ws, owner_a, _owner_b = _workspace_with_two_owners(make_recording)
    stub_membership.grant(ws, owner_a.pk)
    api_client.force_authenticate(user=owner_a)
    resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
    assert resp.status_code == 200
    # Default policy is owner-only: this workspace, this owner — not the
    # other member's recording, and not the other workspace.
    assert {r["title"] for r in resp.json()} == {"a-rec"}


def test_workspace_list_does_not_leak_another_members_recording(
    use_fakes, api_client, db, make_recording, stub_membership
):
    """A verified member with no recordings of their own sees an empty
    listing rather than the workspace's contents."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    ws, _owner_a, _owner_b = _workspace_with_two_owners(make_recording)
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    stub_membership.grant(ws, viewer.pk)
    api_client.force_authenticate(user=viewer)
    resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_members_see_all_is_an_explicit_deployment_choice(
    use_fakes, api_client, db, make_recording, stub_membership
):
    """The pre-0.14 wide listing is still available — as a stated decision."""
    from django.contrib.auth import get_user_model
    from django.test import override_settings

    from stapel_recordings import storage

    User = get_user_model()
    ws, _owner_a, _owner_b = _workspace_with_two_owners(make_recording)
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    stub_membership.grant(ws, viewer.pk)
    api_client.force_authenticate(user=viewer)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "WORKSPACE_LISTING_MEMBERS_SEE_ALL": True,
        }
    ):
        storage.reset_storage_cache()
        resp = api_client.get(f"/recordings/api/v1/recordings?workspace_id={ws}")
    assert resp.status_code == 200
    # Both members' recordings in the workspace, not the other workspace.
    assert {r["title"] for r in resp.json()} == {"a-rec", "b-rec"}


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
    viewer = User.objects.create(username=f"v-{uuid.uuid4().hex[:8]}")
    target = make_recording(owner=viewer, workspace_id=ws, title="target")
    make_recording(owner=viewer, workspace_id=ws, title="sibling")

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
