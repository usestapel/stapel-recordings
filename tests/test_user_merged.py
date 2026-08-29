"""``user.merged`` — a guest's recordings survive signing in.

stapel-auth absorbs an anonymous guest into an existing account and then
DELETES the guest row. Every user column this module owns is ``SET_NULL``, so
the rows are not erased by that — they are stranded: still on disk, owned by
nobody, invisible to the person who recorded them. What is pinned here:

* all three user columns move — ``Recording.owner``, ``Job.owner`` and
  ``RecordingShare.created_by`` — soft-deleted recordings included;
* the subscription is actually wired: the walk goes through ``emit`` and the
  outbox relay, not a direct call to the function;
* the handler is idempotent, and a no-op for ids it has never seen;
* a guest with rows to carry and a survivor this service has not projected
  yet RAISES rather than reporting success, so the outbox redelivers instead
  of silently discarding the transfer;
* no user-scoped unique constraint exists here, which is why the transfer is
  a plain reassignment — asserted, so the day one appears this test fails
  rather than production.
"""
import types
import uuid

import pytest

from stapel_recordings.actions import MergeTargetNotReady, handle_user_merged
from stapel_recordings.models import Job, JobType, Recording, RecordingShare

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints."""
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def survivor(db):
    from stapel_core.django.users.models import User

    return User.objects.create(username=f"s-{uuid.uuid4().hex[:8]}")


def _event(from_user_id, into_user_id, event_id="evt-merge"):
    return types.SimpleNamespace(
        payload={
            "from_user_id": str(from_user_id),
            "into_user_id": str(into_user_id),
            "reason": "anonymous_promotion",
        },
        event_id=event_id,
    )


def _merge(from_user, into_user):
    handle_user_merged(_event(getattr(from_user, "id", from_user),
                              getattr(into_user, "id", into_user)))


def _recording(owner, title="Guest standup"):
    return Recording.objects.create(
        workspace_id=uuid.uuid4(), owner=owner, title=title
    )


def _job(owner, recording):
    return Job.objects.create(
        workspace_id=recording.workspace_id,
        owner=owner,
        recording=recording,
        type=JobType.TRANSCRIBE,
    )


def _share(created_by, recording):
    return RecordingShare.objects.create(
        recording=recording,
        created_by=created_by,
        link_token_hash=uuid.uuid4().hex,
        permissions=["view"],
    )


def _seed_guest(guest):
    """Everything a guest can own in this module, in one call."""
    recording = _recording(guest)
    return recording, _job(guest, recording), _share(guest, recording)


def test_all_three_user_columns_move_to_the_survivor(guest, survivor, drain):
    """The walk, over the real subscription: emit and let the relay deliver."""
    from stapel_core.comm import emit

    recording, job, share = _seed_guest(guest)

    emit(
        "user.merged",
        {
            "from_user_id": str(guest.id),
            "into_user_id": str(survivor.id),
            "reason": "anonymous_promotion",
        },
    )
    drain()

    recording.refresh_from_db()
    job.refresh_from_db()
    share.refresh_from_db()
    assert recording.owner_id == survivor.id
    assert job.owner_id == survivor.id
    assert share.created_by_id == survivor.id
    assert not Recording.objects.filter(owner_id=guest.id).exists()


def test_soft_deleted_recording_moves_too(guest, survivor):
    """``deleted_at`` marks a recording the person removed, not one that
    stopped being theirs — it is still in their trash after the merge."""
    from django.utils import timezone

    trashed = _recording(guest, title="Deleted by the guest")
    trashed.deleted_at = timezone.now()
    trashed.save(update_fields=["deleted_at"])

    _merge(guest, survivor)

    trashed.refresh_from_db()
    assert trashed.owner_id == survivor.id


def test_second_delivery_changes_nothing(guest, survivor):
    recording, job, share = _seed_guest(guest)

    _merge(guest, survivor)
    _merge(guest, survivor)  # at-least-once delivery

    recording.refresh_from_db()
    job.refresh_from_db()
    share.refresh_from_db()
    assert (recording.owner_id, job.owner_id, share.created_by_id) == (
        survivor.id,
        survivor.id,
        survivor.id,
    )
    assert Recording.objects.count() == 1
    assert Job.objects.count() == 1
    assert RecordingShare.objects.count() == 1


def test_guest_owning_nothing_is_a_clean_no_op(guest, survivor):
    _merge(guest, survivor)
    assert Recording.objects.count() == 0


def test_merge_into_self_is_a_no_op(guest):
    recording, _job_row, _share_row = _seed_guest(guest)
    _merge(guest, guest)
    recording.refresh_from_db()
    assert recording.owner_id == guest.id


def test_missing_ids_are_reported_and_ignored(guest, survivor, caplog):
    recording = _recording(guest)

    handle_user_merged(
        types.SimpleNamespace(payload={"into_user_id": str(survivor.id)}, event_id="e1")
    )
    handle_user_merged(
        types.SimpleNamespace(payload={"from_user_id": str(guest.id)}, event_id="e2")
    )
    handle_user_merged(types.SimpleNamespace(payload={}, event_id="e3"))

    recording.refresh_from_db()
    assert recording.owner_id == guest.id


def test_unusable_user_ids_are_a_clean_no_op(survivor):
    """A key that cannot address a row here names nothing — say so quietly
    rather than starting a redelivery loop over a malformed payload."""
    handle_user_merged(_event("not-a-uuid", survivor.id))
    assert Recording.objects.count() == 0


# ── the survivor has not been projected here yet ────────────────────────


def test_unknown_survivor_raises_and_moves_nothing(guest):
    """The guest HAS rows: returning success would let the outbox mark the
    event delivered and strand them forever. Raise so it is redelivered."""
    recording, job, share = _seed_guest(guest)
    survivor_id = uuid.uuid4()

    with pytest.raises(MergeTargetNotReady) as excinfo:
        handle_user_merged(_event(guest.id, survivor_id))

    # An operator staring at a redelivery loop can name both accounts.
    assert str(guest.id) in str(excinfo.value)
    assert str(survivor_id) in str(excinfo.value)

    # Nothing half-moved: a redelivery finds the rows intact under the guest.
    for row, column in ((recording, "owner_id"), (job, "owner_id"),
                        (share, "created_by_id")):
        row.refresh_from_db()
        assert getattr(row, column) == guest.id


def test_redelivery_after_the_survivor_appears_completes_the_transfer(guest):
    """The raise is a real retry path, not just a louder failure."""
    from stapel_core.django.users.models import User

    recording, job, share = _seed_guest(guest)
    survivor_id = uuid.uuid4()

    with pytest.raises(MergeTargetNotReady):
        handle_user_merged(_event(guest.id, survivor_id))

    # The survivor's user projection lands...
    User.objects.create(id=survivor_id, username="late")

    handle_user_merged(_event(guest.id, survivor_id))  # ...and it redelivers.

    recording.refresh_from_db()
    job.refresh_from_db()
    share.refresh_from_db()
    assert (recording.owner_id, job.owner_id, share.created_by_id) == (
        survivor_id,
        survivor_id,
        survivor_id,
    )


def test_unknown_survivor_with_an_empty_guest_stays_quiet(guest):
    """No rows to carry — a genuine no-op, and the retry loop must not start."""
    handle_user_merged(_event(guest.id, uuid.uuid4()))
    assert Recording.objects.count() == 0


def test_second_delivery_after_a_completed_merge_never_raises(guest, survivor):
    """Post-merge the guest owns nothing, so redelivery takes the quiet path
    even though the guest row itself may be long gone."""
    recording, _job_row, _share_row = _seed_guest(guest)

    _merge(guest, survivor)
    _merge(guest, survivor)  # must not raise MergeTargetNotReady

    recording.refresh_from_db()
    assert recording.owner_id == survivor.id


# ── why the transfer is a plain reassignment ────────────────────────────


def test_no_user_scoped_unique_constraint_exists_in_this_module():
    """The handler updates in bulk because nothing here is unique per user.
    Add such a constraint and this fails, which is the reminder to dedup."""
    user_columns = {
        Recording: {"owner"},
        Job: {"owner"},
        RecordingShare: {"created_by"},
    }
    for model, columns in user_columns.items():
        for constraint in model._meta.constraints:
            fields = set(getattr(constraint, "fields", ()) or ())
            assert not (fields & columns), (
                f"{model.__name__}.{fields & columns} is under "
                f"{constraint.name}: handle_user_merged must dedup, not update()"
            )
        for field in model._meta.get_fields():
            if getattr(field, "name", None) in columns:
                assert not getattr(field, "unique", False)
