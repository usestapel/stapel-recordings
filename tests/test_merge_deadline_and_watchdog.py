"""A deadline that killed the retries it declared, and a watchdog with no end.

TWO MEASURED DEFECTS, both from a client stand's 2026-09-19 audit.

1. MERGE / "deadline exceeded" (one recording, 2026-09-13). The merge
   stage submitted ``llm.summarize`` with ``deadline_seconds =
   SUMMARIZE_TIMEOUT_SECONDS`` — the very number the executor uses as the
   CALL timeout, and with ``max_attempts`` left at three. So attempt one
   consumed the whole deadline, the 60-second sweep failed the row with
   "deadline exceeded" before attempt two could exist, and the pipeline
   DLQ'd the recording at ``merge/task_failed``. A retry ladder nobody
   could climb. The same flat number also had to cover a four-hour
   meeting's map-reduce, which it cannot.

2. RECONCILE. The watchdog re-emits ``recording.stage`` for anything
   non-terminal that has not moved, and a re-drive that lands on
   ``StageAwaiting`` does not touch ``retry_count`` — so a recording the
   pipeline cannot finish was re-driven every ``STUCK_THRESHOLD_SECONDS``
   for as long as it existed. The iron-agent stand did exactly that for a
   fortnight against an empty download allowlist (2026-08-20). Every
   other ladder here declares a ceiling; this one declared none.

No provider is called anywhere in this file.
"""
import pytest

from stapel_recordings import pipeline, stages
from stapel_recordings.conf import recordings_settings
from stapel_recordings.models import RecordingStatus
from stapel_recordings.stages import (
    TASK_TIMEOUT_KEY,
    MergeStage,
    summarize_attempt_ceiling,
    summarize_budget_seconds,
    task_deadline_seconds,
)

pytestmark = pytest.mark.django_db


class TestTheBudgetFitsTheMeeting:
    def test_a_short_recording_stays_near_the_shipped_budget(self, make_recording):
        r = make_recording(status="queued")
        r.duration_seconds = 600.0  # ten minutes

        base = int(recordings_settings.SUMMARIZE_TIMEOUT_SECONDS)
        # The shipped number was written for a meeting this size, so it is
        # the floor and the growth over it is a tenth of an hour's worth.
        assert base <= summarize_budget_seconds(r) < base * 2

    def test_a_four_hour_meeting_gets_more_than_a_ten_minute_one(
        self, make_recording
    ):
        short = make_recording(status="queued")
        short.duration_seconds = 600.0
        long = make_recording(status="queued")
        long.duration_seconds = 4 * 3600.0

        assert summarize_budget_seconds(long) > summarize_budget_seconds(short)

    def test_the_budget_is_capped_so_a_bad_duration_cannot_hang_a_worker(
        self, make_recording
    ):
        r = make_recording(status="queued")
        r.duration_seconds = 500 * 3600.0  # a corrupt header's idea of length

        assert summarize_budget_seconds(r) == int(
            recordings_settings.SUMMARIZE_TIMEOUT_MAX_SECONDS
        )

    def test_an_unknown_duration_is_the_floor_not_a_crash(self, make_recording):
        r = make_recording(status="queued")
        r.duration_seconds = None

        assert summarize_budget_seconds(r) == int(
            recordings_settings.SUMMARIZE_TIMEOUT_SECONDS
        )


class TestTheDeadlineCanHoldTheAttempts:
    def test_the_deadline_exceeds_every_attempt_it_declares(self):
        budget = 300
        for attempts in (1, 2, 3):
            deadline = task_deadline_seconds(budget, attempts)
            # THE defect: deadline == budget meant attempt 2 was already
            # past the deadline the moment attempt 1 timed out.
            assert deadline > budget * attempts

    def test_the_headroom_covers_the_sweep_that_declares_the_deadline_missed(
        self,
    ):
        # The sweep runs every 60s; anything less than that as slack is a
        # race between "the last attempt finished" and "the row is late".
        assert int(recordings_settings.TASK_DEADLINE_HEADROOM_SECONDS) >= 60

    def test_the_merge_stage_submits_a_deadline_it_can_live_with(
        self, ready_recording, monkeypatch
    ):
        seen = {}

        def _capture(kind, payload, *, recording, deadline_seconds=None,
                     max_attempts=3, dedupe_key=None, stage=None):
            seen.update(
                kind=kind, deadline=deadline_seconds,
                max_attempts=max_attempts, payload=payload,
            )
            raise stages.StageAwaiting("task-1", kind)

        monkeypatch.setattr(stages, "submit_task", _capture)
        _one_segment(ready_recording)

        with pytest.raises(stages.StageAwaiting):
            MergeStage().run(ready_recording, {})

        assert seen["kind"] == "llm.summarize"
        budget = seen["payload"][TASK_TIMEOUT_KEY]
        assert seen["deadline"] > budget * seen["max_attempts"]
        assert seen["max_attempts"] == int(
            recordings_settings.SUMMARIZE_TASK_MAX_ATTEMPTS
        )


class TestTheSummaryIsNotBoughtTwice:
    def test_the_payload_carries_the_transcript_hash_as_the_checkpoint_key(
        self, ready_recording, monkeypatch
    ):
        seen = {}

        def _capture(kind, payload, **kwargs):
            seen.update(payload=payload)
            raise stages.StageAwaiting("task-1", kind)

        monkeypatch.setattr(stages, "submit_task", _capture)
        _one_segment(ready_recording)

        with pytest.raises(stages.StageAwaiting):
            MergeStage().run(ready_recording, {})

        key = seen["payload"]["idempotency_key"]
        assert key.startswith("summary:")
        # The CONTENT is the identity: two attempts at one transcript are
        # one purchase, an edited transcript is a new one.
        assert len(key) > len("summary:")

    def test_the_worst_case_is_a_declared_number(self):
        ceiling = summarize_attempt_ceiling()
        assert ceiling == int(recordings_settings.MAX_STAGE_RETRIES) * int(
            recordings_settings.SUMMARIZE_TASK_MAX_ATTEMPTS
        )
        # Six calls at most, of which one is paid — the rest carry the
        # same idempotency_key and are served from the agent's checkpoint.
        assert ceiling <= 6

    def test_the_executors_budget_never_reaches_the_agents_contract(self):
        """``llm.summarize`` refuses keys it does not declare."""
        from stapel_recordings import task_delegates

        sent = {}

        def _call(kind, payload, timeout=None):
            sent.update(kind=kind, payload=payload, timeout=timeout)
            return {"status": "ok", "summary": "s"}

        import stapel_core.comm as comm

        delegate = task_delegates._make_delegate(
            "llm.summarize", "SUMMARIZE_TIMEOUT_SECONDS"
        )
        original = comm.call
        comm.call = _call
        try:
            delegate({"text": "hello", TASK_TIMEOUT_KEY: 900})
        finally:
            comm.call = original

        assert TASK_TIMEOUT_KEY not in sent["payload"]
        assert sent["timeout"] == 900.0

    def test_without_a_stated_budget_the_delegate_uses_the_setting(self):
        from stapel_recordings import task_delegates

        sent = {}

        def _call(kind, payload, timeout=None):
            sent.update(timeout=timeout)
            return {"status": "ok"}

        import stapel_core.comm as comm

        delegate = task_delegates._make_delegate(
            "llm.summarize", "SUMMARIZE_TIMEOUT_SECONDS"
        )
        original = comm.call
        comm.call = _call
        try:
            delegate({"text": "hello"})
        finally:
            comm.call = original

        assert sent["timeout"] == float(
            recordings_settings.SUMMARIZE_TIMEOUT_SECONDS
        )


class TestTheWatchdogRunsOut:
    def test_a_recording_that_never_moves_is_re_driven_a_declared_number_of_times(
        self, ready_recording
    ):
        cap = int(recordings_settings.RECONCILE_MAX_REDRIVES)

        allowed = sum(
            1 for _ in range(cap + 5)
            if pipeline.note_reconcile_redrive(ready_recording)
        )

        assert allowed == cap

    def test_the_last_refusal_fails_it_where_a_person_can_see_it(
        self, ready_recording
    ):
        cap = int(recordings_settings.RECONCILE_MAX_REDRIVES)
        for _ in range(cap):
            pipeline.note_reconcile_redrive(ready_recording)

        assert pipeline.note_reconcile_redrive(ready_recording) is False

        ready_recording.refresh_from_db()
        assert ready_recording.status == RecordingStatus.ERROR
        assert (
            ready_recording.workflow_state["last_error"]["reason"]
            == "reconcile_exhausted"
        )

    def test_an_exhausted_recording_is_no_longer_picked_up_at_all(
        self, ready_recording
    ):
        """ERROR is terminal for the watchdog's own query."""
        cap = int(recordings_settings.RECONCILE_MAX_REDRIVES)
        for _ in range(cap + 1):
            pipeline.note_reconcile_redrive(ready_recording)

        ready_recording.refresh_from_db()
        assert ready_recording.status in {
            RecordingStatus.ERROR, RecordingStatus.DELETED
        }

    def test_a_pipeline_that_moves_forgets_the_count(self, ready_recording):
        pipeline.note_reconcile_redrive(ready_recording)
        pipeline.note_reconcile_redrive(ready_recording)

        # What the cap bounds is "re-driven and got nowhere", never "took
        # a long time" — a slow pipeline must not be failed for being slow.
        pipeline._reset_reconcile_redrives(ready_recording)

        assert (
            ready_recording.workflow_state["pipeline"]["reconcile_redrives"] == 0
        )

    def test_zero_restores_the_unbounded_behaviour_explicitly(
        self, ready_recording, settings
    ):
        settings.STAPEL_RECORDINGS = {
            **getattr(settings, "STAPEL_RECORDINGS", {}),
            "RECONCILE_MAX_REDRIVES": 0,
        }

        for _ in range(20):
            assert pipeline.note_reconcile_redrive(ready_recording) is True


def _one_segment(recording):
    """The merge stage needs something to merge."""
    from stapel_recordings.models import Segment

    Segment.objects.create(
        recording=recording,
        start_time=0.0,
        end_time=1.0,
        text="hello",
        sequence_num=0,
    )
