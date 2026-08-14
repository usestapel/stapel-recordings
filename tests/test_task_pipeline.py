"""Long-running work is dispatched as a task; the stage waits and resumes later.

WHY THIS SUITE IS SEPARATE. The rest of the suite runs tasks synchronously
(``TASK_DISPATCH="inline"`` — the honest model for a brokerless monolith), so
the awaiting branch never triggers there: ``start()`` runs the handler,
``submit_task`` returns a result, and the stage completes in one pass. That's
exactly why it needs its own coverage — otherwise the core property of the
transition would go untested while the suite looked green.

WHAT THIS TRANSITION IS. Transcription used to be a synchronous Function
call: the caller held the worker and waited, with "how long to wait" coming
from ``FUNCTION_TIMEOUT`` (five seconds by default). A real transcription
takes on the order of seconds to tens of seconds — comfortably past that
default — so every real recording used to time out, retry three times, and
land in error a couple of hours later while the user stared at "processing".
A queue breaks the synchronous model for good: busy workers can't be waited
out within a fixed deadline.
"""
import uuid

import pytest
from django.test import override_settings

from stapel_recordings import pipeline
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.stages import StageAwaiting, TranscribeStage

pytestmark = pytest.mark.django_db


TRANSCRIPT_OK = {
    "status": "ok",
    "provider_used": "stub-asr",
    "fallback_used": False,
    "transcript": {
        "provider": "stub-asr",
        "language": "ru",
        "duration_seconds": 12.0,
        "words": [],
        "utterances": [
            {"text": "привет", "start": 0.0, "end": 2.0, "speaker": "speaker_0",
             "confidence": 0.9, "word_indexes": []},
        ],
        "speakers_detected": ["speaker_0"],
        "raw": {},
    },
}


@pytest.fixture
def deferred_tasks():
    """Tasks are NOT executed in start() — matching a real broker + queue."""
    from stapel_core.comm import tasks as _tasks

    # Handler removed: no process here executes llm.transcribe, so the task
    # stays PENDING and the stage must go into awaiting.
    saved = dict(_tasks._handlers)
    _tasks._handlers.pop("llm.transcribe", None)
    _tasks._handlers.pop("llm.summarize", None)
    with override_settings(
        STAPEL_COMM={
            "OUTBOX_ENABLED": True,
            "ACTION_TRANSPORT": "inprocess",
            "FUNCTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
            "TASK_DISPATCH": "action",
        }
    ):
        yield
    _tasks._handlers.clear()
    _tasks._handlers.update(saved)


class TestStageEntersAwaiting:
    def test_transcribe_raises_awaiting_not_error(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        assert caught.value.kind == "llm.transcribe"
        assert caught.value.task_id

    def test_task_created_and_awaits_worker(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        from stapel_core.comm import status

        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        snapshot = status(caught.value.task_id)
        assert snapshot.state == "pending"
        assert snapshot.kind == "llm.transcribe"
        # The work isn't lost: it lives in the table and survives a
        # restart — which the synchronous call never could.

    def test_correlation_id_points_to_recording(self, ready_recording, use_fakes, deferred_tasks):
        from stapel_core.django.taskstore.models import TaskRecord

        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        record = TaskRecord.objects.get(pk=caught.value.task_id)
        assert record.correlation_id == str(ready_recording.id)


class TestDriverRemembersAwaiting:
    def _drive(self, recording):
        pipeline.run_stage(str(recording.id), 0)  # convert
        pipeline.run_stage(str(recording.id), 1)  # transcribe -> awaiting

    def test_recording_neither_fails_nor_completes(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.TRANSCRIBING
        # Not error, not completed: work is in progress and the status says
        # so honestly — "transcribing", not an indicator with no promises.

    def test_awaiting_does_not_consume_attempt(self, ready_recording, use_fakes, deferred_tasks):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.retry_count == 0

    def test_stage_not_marked_completed(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert "transcribe" not in (r.workflow_state["pipeline"].get("completed") or [])
        assert r.workflow_state["pipeline"]["awaiting"]["kind"] == "llm.transcribe"


class TestResume:
    def _await_task(self, recording):
        pipeline.run_stage(str(recording.id), 0)
        pipeline.run_stage(str(recording.id), 1)
        return Recording.objects.get(pk=recording.id).workflow_state["pipeline"]["awaiting"]["task_id"]

    def test_result_completes_stage(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        task_id = self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 1
        assert r.provider_used == "stub-asr"
        assert "transcribe" in r.workflow_state["pipeline"]["completed"]
        assert "awaiting" not in r.workflow_state["pipeline"]

    def test_foreign_result_is_ignored(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        """Delivery is at-least-once, and the task could have been restarted.

        A response to a stale attempt must be dropped — otherwise the stage
        would complete with data it never asked for.
        """
        self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), str(uuid.uuid4()), TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 0
        assert "transcribe" not in (r.workflow_state["pipeline"].get("completed") or [])

    def test_duplicate_delivery_is_harmless(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        task_id = self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)
        # Second delivery: nothing is awaiting anymore, nothing to apply.
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 1


class TestTaskFailure:
    def test_final_failure_goes_to_dlq(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        pipeline.run_stage(str(ready_recording.id), 0)
        pipeline.run_stage(str(ready_recording.id), 1)
        task_id = Recording.objects.get(
            pk=ready_recording.id
        ).workflow_state["pipeline"]["awaiting"]["task_id"]

        pipeline.fail_stage(str(ready_recording.id), task_id, "provider unavailable")

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.ERROR
        # The reason is named, not hidden: this used to be a bare
        # TimeoutError with no word on what actually failed.
        assert r.workflow_state["last_error"]["reason"] == "task_failed"
        assert "provider unavailable" in str(r.workflow_state["last_error"]["detail"])
