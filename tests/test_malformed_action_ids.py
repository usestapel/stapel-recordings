"""A malformed subject key in an action payload must not become a poison pill.

``recordings_for`` promises in its docstring to return an empty queryset —
never to raise — for a key that cannot address a recording. It caught only
``(ValueError, TypeError)``, but Django answers a key it cannot coerce to a
UUID column with ``django.core.exceptions.ValidationError``, which is NOT a
subclass of ``ValueError``. The escape reached ``consume_actions``, which
re-raises to the bus, and ``user.deleted`` with a typo'd id was redelivered
forever.

The ``user.merged`` survivor probe had the same hole one step further in: the
*from* id was read under a guard, the *into* id was not.

Pinned here: both handlers ACK the malformed payload and touch no rows.
"""
import types
import uuid

import pytest

from stapel_recordings.actions import handle_user_deleted, handle_user_merged
from stapel_recordings.erasure import SUBJECT_ACCOUNT, recordings_for
from stapel_recordings.models import Job, Recording, RecordingShare

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "  ", "['x']"]


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


@pytest.fixture
def guest(db):
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def rows(db, guest):
    return Recording.objects.create(
        workspace_id=uuid.uuid4(), owner=guest, title="Guest standup"
    )


def _snapshot():
    return (
        sorted(Recording.objects.values_list("id", "owner_id", "title")),
        sorted(Job.objects.values_list("id", "owner_id")),
        sorted(RecordingShare.objects.values_list("id", "created_by_id")),
    )


def test_recordings_for_a_malformed_account_key_is_empty_not_an_error(rows):
    for bad in BAD_IDS:
        assert not recordings_for(SUBJECT_ACCOUNT, bad).exists()


def test_user_deleted_with_a_malformed_id_acks_and_erases_nothing(rows):
    before = _snapshot()
    for bad in BAD_IDS:
        handle_user_deleted(_event(user_id=bad))
    assert _snapshot() == before


def test_user_merged_with_a_malformed_id_acks_and_moves_nothing(rows, guest):
    """Both directions: a bad *from* id, and — the second door — a bad *into*
    id while the guest genuinely owns rows here."""
    before = _snapshot()
    for bad in BAD_IDS:
        handle_user_merged(_event(from_user_id=bad, into_user_id=str(guest.id)))
        handle_user_merged(_event(from_user_id=str(guest.id), into_user_id=bad))
    assert _snapshot() == before


def test_a_wellformed_stranger_is_still_a_quiet_no_op(rows):
    before = _snapshot()
    stranger = str(uuid.uuid4())
    handle_user_deleted(_event(user_id=stranger))
    handle_user_merged(_event(from_user_id=stranger, into_user_id=stranger))
    assert _snapshot() == before


def test_the_retry_signal_survives_the_widened_guard(rows, guest):
    """A survivor id that parses but has no row here still RAISES when the
    guest owns rows, so the outbox redelivers instead of losing the transfer."""
    from stapel_recordings.actions import MergeTargetNotReady

    with pytest.raises(MergeTargetNotReady):
        handle_user_merged(
            _event(from_user_id=str(guest.id), into_user_id=str(uuid.uuid4()))
        )
    assert Recording.objects.get(pk=rows.pk).owner_id == guest.id
