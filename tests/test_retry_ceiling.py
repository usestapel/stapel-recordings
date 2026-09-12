"""Two retry ladders multiplied, and one recording was transcribed six times.

Production data from a client stand (2026-09-12): ONE 148-minute recording
reached ElevenLabs SIX times — TWO tasks × THREE attempts — and 59.7% of a
23,736-credit quota went to machine duplication of identical media. Nothing
had failed at the provider. The transcription succeeded every time; what
failed was the reply, downstream of the money.

Two defects, one in each ladder, and this module owns both:

* ``stapel_tasks_taskrecord.dedupe_key`` was EMPTY on every row ever
  written, so nothing coalesced the second task into the first.
* ``MAX_STAGE_RETRIES`` (3) and the task's ``max_attempts`` (3) multiply,
  and nobody had done that multiplication.
"""
import pytest

from stapel_recordings import stages
from stapel_recordings.conf import recordings_settings
from stapel_recordings.stages import (
    TranscribeStage,
    stage_dedupe_key,
    transcribe_attempt_ceiling,
)

pytestmark = pytest.mark.django_db


class TestTheCeiling:
    def test_the_two_ladders_multiply_to_at_most_three(self):
        # The number that was NINE by configuration and six in practice.
        # The ladders are independent settings and nothing else in the
        # codebase multiplies them, which is why this assertion exists at
        # all: raising either one silently raises the product.
        assert (
            recordings_settings.MAX_STAGE_RETRIES
            * recordings_settings.TRANSCRIBE_TASK_MAX_ATTEMPTS
        ) <= 3
        assert transcribe_attempt_ceiling() <= 3

    def test_the_priced_call_gets_exactly_one_task_attempt(self):
        # The transport retry is the one that cannot be made safe from
        # here: the provider charges for a job it finished even when our
        # side never read the answer. A deliberate re-run is the stage's
        # to make — and with stapel-agent >= 0.24.0 it is free.
        assert recordings_settings.TRANSCRIBE_TASK_MAX_ATTEMPTS == 1

    def test_the_transcribe_stage_submits_with_that_ceiling(
        self, ready_recording, monkeypatch
    ):
        seen = {}

        def _capture(kind, payload, *, recording, deadline_seconds=None,
                     max_attempts=3, dedupe_key=None, stage=None):
            seen.update(
                kind=kind, max_attempts=max_attempts, stage=stage,
                dedupe_key=dedupe_key, payload=payload,
            )
            raise stages.StageAwaiting("task-1", kind)

        monkeypatch.setattr(stages, "submit_task", _capture)
        ready_recording.normalized_storage_key = "recordings/ws/rec/audio.wav"

        with pytest.raises(stages.StageAwaiting):
            TranscribeStage().run(ready_recording, {})

        assert seen["kind"] == "llm.transcribe"
        assert seen["max_attempts"] == 1
        assert seen["stage"] == "transcribe"


class TestEverySubmissionIsDeduplicated:
    def test_the_key_names_the_recording_the_object_and_the_stage(
        self, ready_recording
    ):
        ready_recording.normalized_storage_key = "recordings/ws/rec/audio.wav"
        key = stage_dedupe_key(ready_recording, "transcribe")

        assert key == (
            f"{ready_recording.id}:recordings/ws/rec/audio.wav:transcribe"
        )

    def test_a_reconverted_object_is_new_work_not_a_duplicate(
        self, ready_recording
    ):
        ready_recording.normalized_storage_key = "recordings/ws/rec/v1.wav"
        first = stage_dedupe_key(ready_recording, "transcribe")
        ready_recording.normalized_storage_key = "recordings/ws/rec/v2.wav"
        second = stage_dedupe_key(ready_recording, "transcribe")

        # The OBJECT is what gets transcribed. A recording re-converted to
        # a new normalized key must not be coalesced into the task still
        # transcribing the old one.
        assert first != second

    def test_two_stages_of_one_recording_are_different_work(
        self, ready_recording
    ):
        assert stage_dedupe_key(ready_recording, "transcribe") != (
            stage_dedupe_key(ready_recording, "summarize")
        )

    def test_submit_task_never_starts_a_task_without_a_key(
        self, ready_recording, monkeypatch
    ):
        # The whole defect in one assertion: the column existed, the
        # primitive honoured it, and this module passed nothing.
        seen = {}

        def _start(kind, payload, **kwargs):
            seen.update(kwargs)
            return "task-1"

        def _status(task_id):
            class Snapshot:
                state = "pending"
            return Snapshot()

        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "start", _start)
        monkeypatch.setattr(comm, "status", _status)

        with pytest.raises(stages.StageAwaiting):
            stages.submit_task(
                "llm.transcribe", {}, recording=ready_recording, stage="transcribe"
            )

        assert seen["dedupe_key"] == stage_dedupe_key(ready_recording, "transcribe")

    def test_a_second_submission_while_the_first_is_live_coalesces(
        self, ready_recording
    ):
        # Against the REAL primitive, because the coalescing is core's and
        # the defect was the seam between us: core reuses a task whose key
        # is still PENDING/RUNNING, and this module never gave it one.
        #
        # Dispatch is pinned to "action" so the first task STAYS pending —
        # under inline dispatch it finishes inside start(), and core
        # releases the key at DONE by design (it deduplicates work in
        # flight; remembering a finished job forever is the agent
        # checkpoint's half of this fix, not this one).
        from django.test import override_settings

        from stapel_core.comm import tasks as _tasks
        from stapel_core.django.taskstore.models import TaskRecord

        # No handler in this process, so the task stays PENDING and the
        # stage goes into awaiting — the production shape with a broker.
        saved = dict(_tasks._handlers)
        _tasks._handlers.pop("llm.transcribe", None)

        submitted = []
        with override_settings(
            STAPEL_COMM={
                "OUTBOX_ENABLED": False,
                "ACTION_TRANSPORT": "inprocess",
                "FUNCTION_TRANSPORT": "inprocess",
                "VALIDATE_SCHEMAS": False,
                "TASK_DISPATCH": "action",
            }
        ):
            for _ in range(2):
                try:
                    stages.submit_task(
                        "llm.transcribe",
                        {"audio_url": "https://minio.test/a.wav"},
                        recording=ready_recording,
                        stage="transcribe",
                    )
                except stages.StageAwaiting as exc:
                    submitted.append(exc.task_id)

        _tasks._handlers.clear()
        _tasks._handlers.update(saved)

        assert len(submitted) == 2
        # ONE task for one unit of work — the second submission joined the
        # first instead of buying a second transcription.
        assert submitted[0] == submitted[1]
        assert TaskRecord.objects.filter(kind="llm.transcribe").count() == 1


class TestTheAgentIsGivenWhatItNeedsToNotChargeTwice:
    def _payload(self, recording):
        recording.normalized_storage_key = "recordings/ws/rec/audio.wav"
        return TranscribeStage().build_payload(recording)

    def test_the_payload_carries_the_content_hash_and_the_submitted_length(
        self, ready_recording
    ):
        ready_recording.workflow_state = {
            **(ready_recording.workflow_state or {}),
            "audio_content_hash": "sha256:" + "b" * 64,
        }
        ready_recording.duration_seconds = 148 * 60

        payload = self._payload(ready_recording)

        # The hash is the agent's checkpoint key: without it a retry of
        # this stage pays the provider again.
        assert payload["audio_content_hash"] == "sha256:" + "b" * 64
        # The duration is what the agent's ledger METERS. This side
        # measured the file during convert; the agent would have to
        # download it again to measure it itself, and several providers
        # report the last word's end timestamp instead — which is how two
        # paid calls landed in the ledger at zero minutes.
        assert payload["audio_duration_ms"] == 148 * 60 * 1000

    def test_a_retry_passes_the_SAME_hash_so_it_hits_the_checkpoint(
        self, ready_recording
    ):
        # The handoff-failure path: llm.transcribe answers
        # `transcript_handoff_failed`, the stage retries, and the retry is
        # free ONLY if it re-derives the identical key. Nothing in the
        # payload may be minted per attempt.
        ready_recording.workflow_state = {
            **(ready_recording.workflow_state or {}),
            "audio_content_hash": "sha256:" + "c" * 64,
        }
        ready_recording.duration_seconds = 90

        first = self._payload(ready_recording)
        second = self._payload(ready_recording)

        # Every field the agent's checkpoint key is built from is
        # identical across attempts — that is what makes attempt 2 free.
        keyed = ("audio_content_hash", "diarization", "language", "provider")
        assert {k: first.get(k) for k in keyed} == {
            k: second.get(k) for k in keyed
        }
        assert first["audio_duration_ms"] == second["audio_duration_ms"]

    def test_a_host_stored_hash_on_metadata_is_honoured(self, ready_recording):
        ready_recording.metadata = {"audio_content_hash": "sha256:" + "d" * 64}
        payload = self._payload(ready_recording)
        assert payload["audio_content_hash"] == "sha256:" + "d" * 64

    def test_the_server_written_hash_wins_over_a_client_writable_one(
        self, ready_recording
    ):
        # A client that could choose the checkpoint key could name another
        # recording's hash and be handed that recording's paid transcript.
        # The key is reserved in metadata AND the server's own value wins.
        ready_recording.metadata = {"audio_content_hash": "sha256:" + "e" * 64}
        ready_recording.workflow_state = {
            "audio_content_hash": "sha256:" + "f" * 64
        }
        payload = self._payload(ready_recording)
        assert payload["audio_content_hash"] == "sha256:" + "f" * 64

    def test_a_client_cannot_write_the_hash_through_metadata(
        self, ready_recording
    ):
        from stapel_recordings.metadata import (
            ReservedMetadataKey,
            sanitize_user_metadata,
        )

        with pytest.raises(ReservedMetadataKey):
            sanitize_user_metadata({"audio_content_hash": "sha256:" + "0" * 64})

    def test_nothing_is_sent_when_nothing_measured_it(self, ready_recording):
        ready_recording.duration_seconds = None
        payload = self._payload(ready_recording)

        # Absent, not guessed: the agent falls back to measuring or to the
        # provider's number, and says so in its log. A fabricated duration
        # would be a fabricated invoice line.
        assert "audio_content_hash" not in payload
        assert "audio_duration_ms" not in payload


class TestConvertWritesTheHash:
    def test_the_normalized_object_is_hashed_while_it_is_still_local(
        self, ready_recording
    ):
        # The passthrough normalizer the suite configures copies the
        # source, so the stored object's bytes are the fixture's — and the
        # hash is over the object that will actually be TRANSCRIBED, not
        # over the upload it came from.
        import hashlib

        from stapel_recordings import stages as stages_module

        stages_module.ConvertStage().run(ready_recording, {})
        ready_recording.refresh_from_db()

        expected = "sha256:" + hashlib.sha256(b"raw-audio-bytes").hexdigest()
        assert ready_recording.workflow_state["audio_content_hash"] == expected
        # ...and it is NOT in the client's half of the row (REC-01).
        assert "audio_content_hash" not in (ready_recording.metadata or {})
