"""Object policy seam (audit REC-03).

The rule that decides who may mutate a recording has a name and a seam, so a
host can widen READING without widening the destructive verbs — which is how
"every workspace member may retry/delete/reprocess" gets built by accident.
"""
import pytest
from django.test import override_settings

from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.policy import OwnerOnlyPolicy, RecordingPolicy, get_policy

pytestmark = pytest.mark.django_db


class ReadOnlyForEveryonePolicy(OwnerOnlyPolicy):
    """Host-style policy double: anyone authenticated reads the workspace,
    only the owner mutates."""

    def visible_queryset(self, user, qs=None):
        return (qs if qs is not None else Recording.objects.all()).filter(
            deleted_at__isnull=True
        )

    def can_read(self, user, recording) -> bool:
        return True


_POLICY_SETTINGS = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
    "RECORDING_POLICY": "stapel_recordings.tests.test_policy.ReadOnlyForEveryonePolicy",
}


def test_default_policy_is_owner_only(make_recording, user):
    recording = make_recording(owner=user)
    policy = get_policy()
    assert isinstance(policy, OwnerOnlyPolicy)
    assert policy.can_read(user, recording)
    assert policy.can_reprocess(user, recording)


def test_base_policy_denies_every_verb(make_recording, user):
    recording = make_recording(owner=user)
    policy = RecordingPolicy()
    assert not any(
        [
            policy.can_read(user, recording),
            policy.can_edit(user, recording),
            policy.can_delete(user, recording),
            policy.can_upload(user, recording),
            policy.can_reprocess(user, recording),
        ]
    )
    assert policy.visible_queryset(user).count() == 0


def test_anonymous_scope_is_empty(db, make_recording):
    """An unauthenticated caller sees nothing.

    The inline scope this replaced returned every non-deleted recording when
    the request had no authenticated user — only the view's permission class
    stood between that queryset and a response."""

    class Anonymous:
        is_authenticated = False
        pk = None

    make_recording()
    assert OwnerOnlyPolicy().visible_queryset(Anonymous()).count() == 0
    assert OwnerOnlyPolicy().visible_queryset(None).count() == 0


def test_widened_read_does_not_widen_reprocess(use_fakes, api_client, make_recording, user, django_user_model):
    other = django_user_model.objects.create(username="not-the-owner")
    recording = make_recording(owner=other, status=RecordingStatus.COMPLETED)
    api_client.force_authenticate(user=user)
    with override_settings(STAPEL_RECORDINGS=_POLICY_SETTINGS):
        detail = api_client.get(f"/recordings/api/v1/recordings/{recording.id}")
        reprocess = api_client.post(f"/recordings/api/v1/recordings/{recording.id}/reprocess")
    assert detail.status_code == 200, detail.content
    assert reprocess.status_code == 404, reprocess.content
    assert Recording.objects.get(pk=recording.id).status == RecordingStatus.COMPLETED
