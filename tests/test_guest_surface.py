"""The guest (anonymous session) surface of stapel-recordings.

With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a
bare ``IsAuthenticated`` gate lets it through, and until now nothing in the
source said whether that was wanted (``stapel_core.adoption`` W002).

This module's answer is uniform, and it follows from what a recording is: a
durable, owned artifact with a processing pipeline behind it. An anonymous
session is not an owner. The listing and per-recording verbs were already
owner-scoped, so a guest's answers were empty or 404 all along — but
``POST /recordings`` was genuinely open, and it mints a row, opens an upload
session and enqueues transcription/diarization/summarization. Metering that
on an account means nothing when a session costs one unauthenticated POST to
mint.

These tests pin both facts: the door is shut for a guest, and it is shut for
*anonymous* rather than for *authenticated* — an ordinary user is unaffected.
"""

import uuid

import pytest

from stapel_core.django.api.permissions import IsNotAnonymousUser
from stapel_recordings import views
from stapel_recordings.models import Recording

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints:
    authenticated, ``is_anonymous=True``."""
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


def test_guest_cannot_open_a_recording_and_its_pipeline(use_fakes, guest_client):
    """The endpoint that was genuinely open, and the expensive one."""
    resp = guest_client.post(
        "/recordings/api/v1/recordings",
        {
            "workspace_id": str(uuid.uuid4()),
            "title": "Free transcription please",
            "filename": "long.mp3",
        },
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert not Recording.objects.exists()


def test_guest_cannot_list(guest_client):
    assert guest_client.get("/recordings/api/v1/recordings").status_code == 403


def test_guest_cannot_read_a_recording(guest_client, make_recording):
    recording = make_recording()
    resp = guest_client.get(f"/recordings/api/v1/recordings/{recording.id}")
    assert resp.status_code == 403, resp.content


def test_guest_cannot_finalize(guest_client, make_recording):
    recording = make_recording()
    resp = guest_client.post(
        f"/recordings/api/v1/recordings/{recording.id}/finalize", {}, format="json"
    )
    assert resp.status_code == 403, resp.content


def test_guest_cannot_reprocess(guest_client, make_recording):
    """Reprocess is the one verb that can spend the pipeline's cost twice."""
    recording = make_recording()
    resp = guest_client.post(
        f"/recordings/api/v1/recordings/{recording.id}/reprocess"
    )
    assert resp.status_code == 403, resp.content


def test_a_registered_user_is_unaffected(use_fakes, api_client, user, stub_membership):
    """The gate is about *anonymous*, not about *authenticated*."""
    ws = uuid.uuid4()
    stub_membership.grant(ws, user.pk)
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {
            "workspace_id": str(ws),
            "title": "Standup",
            "filename": "standup.mp3",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert api_client.get("/recordings/api/v1/recordings").status_code == 200


def test_every_view_carries_the_permission_class():
    for view in (
        views.RecordingListCreateView,
        views.RecordingDetailView,
        views.FinalizeUploadView,
        views.ReprocessRecordingView,
    ):
        assert IsNotAnonymousUser in view.permission_classes, view.__name__


def test_no_view_is_left_silent():
    """The question ``stapel_core.adoption`` E001/W002 asks a consumer's
    deployment, asked here — where it can be answered."""
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView

    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATIONS

    silent = [
        name
        for name, obj in vars(views).items()
        if isinstance(obj, type)
        and issubclass(obj, APIView)
        and set(getattr(obj, "permission_classes", ()) or ()) == {IsAuthenticated}
        and getattr(obj, "stapel_anonymous_access", None) not in ANONYMOUS_DECLARATIONS
    ]
    assert silent == []
