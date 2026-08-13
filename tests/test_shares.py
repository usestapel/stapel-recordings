"""Public share links and passcode unlock (audit SHARE-01).

The finding this pins: a passcode-protected share that accepts any nonempty
token. Every test below is a property the primitive must have for that to be
impossible — the token is verified, bound to its share, bound to its
generation, time-limited, guessing is bounded, and the recording's own
lifecycle outranks the link.
"""
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_recordings import shares
from stapel_recordings.models import RecordingShare, RecordingStatus

pytestmark = pytest.mark.django_db


def _protected(make_recording, passcode="1234", **kwargs):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    return shares.create_share(recording=recording, passcode=passcode, **kwargs)


# ── the finding itself ───────────────────────────────────────────────────


def test_arbitrary_unlock_token_is_refused(make_recording):
    share, link = _protected(make_recording)
    for forged in ("x", "any-nonempty-token", uuid.uuid4().hex, link):
        with pytest.raises(shares.SharePasscodeRequired):
            shares.access_share(link, unlock_token=forged)


def test_correct_passcode_yields_a_working_token(make_recording):
    share, link = _protected(make_recording)
    token = shares.unlock_share(share, "1234")
    access = shares.access_share(link, unlock_token=token)
    assert access.recording.id == share.recording_id


def test_unlock_token_of_another_share_is_refused(make_recording):
    share_a, link_a = _protected(make_recording)
    share_b, _link_b = _protected(make_recording)
    token_b = shares.unlock_share(share_b, "1234")
    with pytest.raises(shares.SharePasscodeRequired):
        shares.access_share(link_a, unlock_token=token_b)


def test_wrong_passcode_is_refused(make_recording):
    share, _link = _protected(make_recording)
    with pytest.raises(shares.SharePasscodeRequired):
        shares.unlock_share(share, "9999")


# ── the link token itself ────────────────────────────────────────────────


def test_link_token_is_never_stored_in_the_clear(make_recording):
    share, link = _protected(make_recording, passcode=None)
    row = RecordingShare.objects.get(pk=share.pk)
    assert link not in row.link_token_hash
    assert len(row.link_token_hash) == 64  # sha-256 hex
    assert len(link) >= 32  # high entropy, not a guessable id
    assert shares.resolve_share(link).pk == share.pk
    assert shares.resolve_share(link + "x") is None
    assert shares.resolve_share("") is None


def test_passcode_is_hashed_not_stored(make_recording):
    share, _link = _protected(make_recording, passcode="hunter2")
    assert "hunter2" not in RecordingShare.objects.get(pk=share.pk).passcode_hash


# ── rotation and revocation ──────────────────────────────────────────────


def test_passcode_change_invalidates_issued_tokens(make_recording):
    share, link = _protected(make_recording)
    token = shares.unlock_share(share, "1234")
    shares.set_share_passcode(share, "5678")
    with pytest.raises(shares.SharePasscodeRequired):
        shares.access_share(link, unlock_token=token)
    fresh = shares.unlock_share(share, "5678")
    assert shares.access_share(link, unlock_token=fresh).share.pk == share.pk


def test_revoke_kills_link_and_tokens(make_recording):
    share, link = _protected(make_recording)
    token = shares.unlock_share(share, "1234")
    shares.revoke_share(share)
    with pytest.raises(shares.ShareNotFound):
        shares.access_share(link, unlock_token=token)


def test_expired_share_is_refused(make_recording):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    share, link = shares.create_share(
        recording=recording, expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    with pytest.raises(shares.ShareNotFound):
        shares.access_share(link)


def test_unlock_token_expires(make_recording):
    share, link = _protected(make_recording)
    token = shares.unlock_share(share, "1234")
    with override_settings(STAPEL_RECORDINGS={"SHARE_UNLOCK_TOKEN_TTL_SECONDS": -1}):
        with pytest.raises(shares.SharePasscodeRequired):
            shares.access_share(link, unlock_token=token)


# ── brute force is bounded ───────────────────────────────────────────────


def test_unlock_locks_out_after_repeated_failures(make_recording):
    share, _link = _protected(make_recording)
    with override_settings(
        STAPEL_RECORDINGS={"SHARE_UNLOCK_MAX_ATTEMPTS": 3, "SHARE_UNLOCK_LOCKOUT_SECONDS": 600}
    ):
        for _ in range(3):
            with pytest.raises(shares.SharePasscodeRequired):
                shares.unlock_share(share, "0000")
        # Even the CORRECT passcode is refused while locked out — otherwise
        # the lockout is not a bound, just a delay for the wrong guesser.
        with pytest.raises(shares.ShareThrottled):
            shares.unlock_share(share, "1234")


def test_successful_unlock_clears_the_counter(make_recording):
    share, _link = _protected(make_recording)
    with override_settings(STAPEL_RECORDINGS={"SHARE_UNLOCK_MAX_ATTEMPTS": 3}):
        with pytest.raises(shares.SharePasscodeRequired):
            shares.unlock_share(share, "0000")
        shares.unlock_share(share, "1234")
    assert RecordingShare.objects.get(pk=share.pk).failed_unlock_count == 0


# ── the recording's lifecycle outranks the link ──────────────────────────


def test_soft_deleted_recording_is_not_readable_through_a_share(make_recording):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    share, link = shares.create_share(recording=recording)
    recording.deleted_at = timezone.now()
    recording.save(update_fields=["deleted_at"])
    with pytest.raises(shares.ShareNotFound):
        shares.access_share(link)


def test_deleted_status_recording_is_not_readable(make_recording):
    recording = make_recording(status=RecordingStatus.DELETED)
    share, link = shares.create_share(recording=recording)
    with pytest.raises(shares.ShareNotFound):
        shares.access_share(link)


# ── permissions are a grant, not a request ───────────────────────────────


def test_default_grant_is_minimal(make_recording):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    share, link = shares.create_share(recording=recording)
    access = shares.access_share(link)
    assert access.permissions == (shares.PERM_VIEW,)
    with pytest.raises(shares.SharePermissionDenied):
        shares.require_permission(access, shares.PERM_TRANSCRIPT)


def test_unknown_permission_is_rejected_at_creation(make_recording):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    with pytest.raises(ValueError):
        shares.create_share(recording=recording, permissions=["everything"])


def test_projection_hides_what_the_share_does_not_grant(make_recording):
    from stapel_recordings.dto import shared_recording_to_dto
    from stapel_recordings.models import Segment

    recording = make_recording(status=RecordingStatus.COMPLETED)
    recording.summary = "secret summary"
    recording.save(update_fields=["summary"])
    Segment.objects.create(
        recording=recording, sequence_num=0, start_time=0.0, end_time=1.0, text="secret words"
    )

    _view_only, view_link = shares.create_share(recording=recording)
    payload = shared_recording_to_dto(shares.access_share(view_link))
    assert payload.summary is None
    assert payload.segments == []

    _full, full_link = shares.create_share(
        recording=recording, permissions=[shares.PERM_TRANSCRIPT, shares.PERM_SUMMARY]
    )
    full = shared_recording_to_dto(shares.access_share(full_link))
    assert full.summary == "secret summary"
    assert [s.text for s in full.segments] == ["secret words"]


# ── counters ─────────────────────────────────────────────────────────────


def test_access_count_increments_atomically(make_recording):
    recording = make_recording(status=RecordingStatus.COMPLETED)
    share, link = shares.create_share(recording=recording)
    # Two accesses through STALE in-memory copies: a read-modify-write would
    # lose one of them, an F() update cannot.
    stale_a = RecordingShare.objects.get(pk=share.pk)
    stale_b = RecordingShare.objects.get(pk=share.pk)
    assert stale_a.access_count == stale_b.access_count == 0
    shares.access_share(link)
    shares.access_share(link)
    row = RecordingShare.objects.get(pk=share.pk)
    assert row.access_count == 2
    assert row.last_accessed_at is not None


def test_unknown_link_is_not_found(make_recording):
    with pytest.raises(shares.ShareNotFound):
        shares.access_share("no-such-token")
