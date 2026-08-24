"""The owner's own transcript, and when to ask again.

Two gaps closed together, because they are the same complaint from a client:
"I uploaded a recording and I cannot see what came out of it."

  - **No owner-facing transcript read.** Speaker-attributed segments used to
    leave this module through exactly one door — the projection inside a
    public share link — so reading your own transcript meant publishing it
    first. ``transcript_storage_key`` was not a second door: it is a raw
    object key nothing signs.
  - **No progress signal.** This module serves no socket, so a client learns
    that a recording moved by reading it again; nothing in the response said
    whether that was worth doing, or how soon.

What is pinned here is the *contract*, not the implementation: the owner
door has the same authority as every other per-recording read, hands back
the same segment shape as the share door, and the polling hint appears
exactly on the statuses where asking again can change the answer.
"""
import uuid

import pytest
from django.test import override_settings

from stapel_recordings.models import RecordingStatus

pytestmark = pytest.mark.django_db

TRANSCRIPT_URL = "/recordings/api/v1/recordings/{}/transcript"
DETAIL_URL = "/recordings/api/v1/recordings/{}"


class VisibleButUnreadablePolicy:
    """Host-style double: the row is in scope, the read verb still refuses.

    Exists to prove the transcript endpoint asks ``can_read`` rather than
    inferring authority from the queryset alone.
    """

    def visible_queryset(self, user, qs=None):
        from stapel_recordings.models import Recording

        return qs if qs is not None else Recording.objects.all()

    def can_read(self, user, recording) -> bool:
        return False


@pytest.fixture
def transcribed(make_recording, user):
    """A completed recording with five segments across two named speakers."""
    from stapel_recordings.models import Segment, Speaker

    recording = make_recording(status=RecordingStatus.COMPLETED)
    named = Speaker.objects.create(
        recording=recording, label="speaker_0", display_name="Ada"
    )
    unnamed = Speaker.objects.create(recording=recording, label="speaker_1")
    for i in range(5):
        Segment.objects.create(
            recording=recording,
            speaker=named if i % 2 == 0 else unnamed,
            sequence_num=i,
            start_time=float(i),
            end_time=float(i) + 1.0,
            text=f"segment {i}",
        )
    return recording


# ── the gap itself: an owner can read their own transcript ───────────────


def test_owner_reads_own_transcript_without_publishing_it(
    api_client, transcribed, user
):
    """The finding: before this endpoint the ONLY speaker-attributed
    transcript on the wire was behind a public share link."""
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    assert resp.status_code == 200, resp.content
    assert [s["text"] for s in resp.data["items"]] == [f"segment {i}" for i in range(5)]


def test_segments_arrive_in_reading_order(api_client, transcribed, user):
    """Ascending ``sequence_num`` — a transcript is read forward, unlike
    every other listing in the fleet, which is newest-first."""
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    nums = [s["sequence_num"] for s in resp.data["items"]]
    assert nums == sorted(nums)


def test_speaker_is_the_name_a_reader_can_print(api_client, transcribed, user):
    """``display_name`` when a voice has been named, the provider's own
    label otherwise — a client should never have to fall back itself."""
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    speakers = [s["speaker"] for s in resp.data["items"]]
    assert speakers == ["Ada", "speaker_1", "Ada", "speaker_1", "Ada"]


def test_owner_and_share_doors_hand_back_the_same_shape(
    api_client, transcribed, user
):
    """One DTO for both readers: a transcript renderer is written once.

    If these two ever diverge, the frontend grows a second segment type and
    the two paths drift — which is the whole reason the projection and this
    endpoint share :func:`~stapel_recordings.dto.segment_to_dto`."""
    from stapel_recordings import shares
    from stapel_recordings.dto import shared_recording_to_dto

    api_client.force_authenticate(user=user)
    owner_items = api_client.get(TRANSCRIPT_URL.format(transcribed.id)).data["items"]

    _share, link = shares.create_share(
        recording=transcribed, permissions=[shares.PERM_TRANSCRIPT]
    )
    shared = shared_recording_to_dto(shares.access_share(link))

    assert [s["text"] for s in owner_items] == [s.text for s in shared.segments]
    assert [s["speaker"] for s in owner_items] == [s.speaker for s in shared.segments]
    assert set(owner_items[0]) == {
        "sequence_num", "start_time", "end_time", "speaker", "text",
    }


# ── same authority as every other per-recording read ─────────────────────


def test_anonymous_caller_gets_no_transcript(api_client, transcribed):
    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    assert resp.status_code in (401, 403), resp.content
    assert b"segment 0" not in resp.content


def test_a_stranger_gets_no_transcript(api_client, transcribed, django_user_model):
    """Authenticated is not authorized: the object policy is asked about
    THIS recording, and the default policy is owner-only."""
    stranger = django_user_model.objects.create(username=f"s-{uuid.uuid4().hex[:8]}")
    api_client.force_authenticate(user=stranger)

    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    assert resp.status_code == 404, resp.content
    assert b"segment 0" not in resp.content


def test_unknown_recording_is_404(api_client, user):
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(uuid.uuid4()))

    assert resp.status_code == 404, resp.content


def test_a_policy_that_refuses_can_read_refuses_the_transcript(
    api_client, transcribed, user
):
    """``can_read`` is asked, not merely the visible queryset.

    ``VisibleButUnreadablePolicy`` is the shape that tells the two apart: the
    row IS in scope, so a view that stopped at ``_owned_qs`` would serve the
    transcript. The transcript must not be a side channel around the verb the
    media and detail endpoints ask."""
    api_client.force_authenticate(user=user)

    with override_settings(
        STAPEL_RECORDINGS={
            "RECORDING_POLICY": "stapel_recordings.tests.test_transcript_read."
            "VisibleButUnreadablePolicy"
        }
    ):
        resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    assert resp.status_code == 404, resp.content
    assert b"segment 0" not in resp.content


# ── pagination: the window is real and it is anchored ────────────────────


def test_a_page_is_bounded_and_carries_its_own_next_anchor(
    api_client, transcribed, user
):
    api_client.force_authenticate(user=user)

    first = api_client.get(TRANSCRIPT_URL.format(transcribed.id), {"limit": 2})

    assert [s["sequence_num"] for s in first.data["items"]] == [0, 1]
    assert first.data["has_next"] is True
    assert first.data["next_anchor"] == 1

    second = api_client.get(
        TRANSCRIPT_URL.format(transcribed.id),
        {"limit": 2, "anchor": first.data["next_anchor"]},
    )

    assert [s["sequence_num"] for s in second.data["items"]] == [2, 3]


def test_the_page_size_ceiling_is_a_setting_and_it_binds(
    api_client, transcribed, user
):
    """A client asking for more than the deployment allows gets the cap, not
    the whole transcript."""
    api_client.force_authenticate(user=user)

    with override_settings(STAPEL_RECORDINGS={"TRANSCRIPT_MAX_PAGE_SIZE": 3}):
        resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id), {"limit": 500})

    assert len(resp.data["items"]) == 3
    assert resp.data["has_next"] is True


def test_a_recording_with_no_segments_answers_an_empty_page(
    api_client, make_recording, user
):
    """Not an error: "no transcript yet" is a stage of the normal lifecycle,
    and a 404 here would be indistinguishable from "not your recording"."""
    recording = make_recording(status=RecordingStatus.TRANSCRIBING)
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(recording.id))

    assert resp.status_code == 200, resp.content
    assert resp.data["items"] == []
    assert resp.data["has_next"] is False


# ── the polling contract ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        RecordingStatus.QUEUED,
        RecordingStatus.ANALYZING,
        RecordingStatus.NORMALIZING,
        RecordingStatus.TRANSCRIBING,
        RecordingStatus.DIARIZING,
        RecordingStatus.MERGING,
    ],
)
def test_a_recording_the_pipeline_owns_says_when_to_ask_again(
    api_client, make_recording, user, status
):
    recording = make_recording(status=status)
    api_client.force_authenticate(user=user)

    with override_settings(STAPEL_RECORDINGS={"POLL_INTERVAL_SECONDS": 7}):
        resp = api_client.get(DETAIL_URL.format(recording.id))

    assert resp.data["is_processing"] is True
    assert resp.data["poll_after_seconds"] == 7
    assert resp["Retry-After"] == "7"


@pytest.mark.parametrize(
    "status",
    [
        RecordingStatus.COMPLETED,
        RecordingStatus.ERROR,
        RecordingStatus.DELETED,
        RecordingStatus.CREATED,
        RecordingStatus.UPLOADING,
    ],
)
def test_a_recording_nothing_is_working_on_says_stop(
    api_client, make_recording, user, status
):
    """The absent header and the null field are the same answer: terminal
    statuses will not change, and ``created``/``uploading`` wait on the
    client's own upload — polling either is asking yourself a question."""
    recording = make_recording(status=status)
    api_client.force_authenticate(user=user)

    resp = api_client.get(DETAIL_URL.format(recording.id))

    assert resp.data["is_processing"] is False
    assert resp.data["poll_after_seconds"] is None
    assert "Retry-After" not in resp


def test_the_header_and_the_body_cannot_disagree(api_client, make_recording, user):
    """One computation behind both, so a client that reads HTTP and a client
    that reads the payload are told the same thing."""
    recording = make_recording(status=RecordingStatus.TRANSCRIBING)
    api_client.force_authenticate(user=user)

    resp = api_client.get(DETAIL_URL.format(recording.id))

    assert resp["Retry-After"] == str(resp.data["poll_after_seconds"])


def test_the_transcript_read_carries_the_same_hint(
    api_client, make_recording, user
):
    """A client watching the transcript fill in polls the transcript, not the
    recording — it must be told when to come back on the door it is using."""
    recording = make_recording(status=RecordingStatus.TRANSCRIBING)
    api_client.force_authenticate(user=user)

    with override_settings(STAPEL_RECORDINGS={"POLL_INTERVAL_SECONDS": 4}):
        resp = api_client.get(TRANSCRIPT_URL.format(recording.id))

    assert resp["Retry-After"] == "4"


def test_a_finished_transcript_read_does_not_ask_for_another(
    api_client, transcribed, user
):
    api_client.force_authenticate(user=user)

    resp = api_client.get(TRANSCRIPT_URL.format(transcribed.id))

    assert "Retry-After" not in resp


def test_an_accepted_job_says_when_to_look_for_its_result(
    api_client, transcribed, user, stub_summarize
):
    """202 is "accepted, not finished". Without a hint the client either
    hammers the read or waits an arbitrary time before showing the summary.

    The recording here has NO ``language`` of its own — the
    ``language_mode="auto"`` shape — which is also the regression guard
    below."""
    transcribed.transcript_storage_key = "transcripts/x.json"
    transcribed.save(update_fields=["transcript_storage_key"])
    api_client.force_authenticate(user=user)

    with override_settings(STAPEL_RECORDINGS={"JOB_POLL_INTERVAL_SECONDS": 11}):
        resp = api_client.post(
            f"/recordings/api/v1/recordings/{transcribed.id}/resummarize"
        )

    assert resp.status_code == 202, resp.content
    assert resp["Retry-After"] == "11"


def test_a_recording_with_no_language_submits_a_json_payload(
    transcribed, stub_summarize
):
    """Regression: the fallback for a missing ``recording.language`` reached
    for ``UnifiedTranscript.language``, which is a LanguageMeta struct, not a
    tag — and a task payload has to be JSON. A recording that never had a
    language could not be re-summarized at all."""
    import json

    from stapel_recordings import stages, transcript_schema

    assert not transcribed.language
    payload = stages._summarize_payload(
        transcribed, transcript_schema.from_db_segments(transcribed)
    )

    json.dumps(payload)  # the submission does this; it must not raise
    assert isinstance(payload.get("language", ""), str)
