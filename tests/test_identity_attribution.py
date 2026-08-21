"""Who a delegated AI call is for, and how it gets there.

This package makes no AI call of its own — it delegates every one of them to
stapel-agent over comm, and the agent writes one billable ledger row per
provider call. Those rows carried no subject, for a reason that only shows up
when you look at both sides at once: a pipeline stage runs on a queue long
after the request that created the recording, so there is no "current user" to
read, and the comm schemas would have rejected an id anyway.

Both halves are fixed now (stapel-agent 0.12.0 opened the fields), and what
these tests protect is the half that lives here: the id actually being ON the
payload. A silent omission is the exact failure this closes, and it is
invisible from inside this package — the call succeeds, the work happens, and
the spend belongs to nobody.
"""
import uuid

import pytest

from stapel_recordings import events
from stapel_recordings.stages import identity_fields, identity_payload

pytestmark = pytest.mark.django_db


class TestTheBlockItself:
    def test_both_ids_are_stringified(self):
        """Hosts number their subjects differently — int pk here, UUID
        workspace — and the ledger columns are text."""
        ws = uuid.uuid4()
        assert identity_fields(7, ws) == {"user_id": "7", "workspace_id": str(ws)}

    def test_absent_ids_are_omitted_not_nulled(self):
        """The llm.* schemas type both as strings AND forbid unknown
        properties, so a null would be rejected where a missing key is fine."""
        assert identity_fields(None, None) == {}
        assert identity_fields(1, None) == {"user_id": "1"}
        assert identity_fields(None, "w") == {"workspace_id": "w"}

    def test_a_recording_answers_for_itself(self, make_recording, user):
        r = make_recording()
        assert identity_payload(r) == {
            "user_id": str(user.id),
            "workspace_id": str(r.workspace_id),
        }

    def test_an_ownerless_recording_still_names_its_tenant(self, make_recording):
        """Partial attribution beats none: the workspace is still billable."""
        r = make_recording(owner=None)
        assert identity_payload(r) == {"workspace_id": str(r.workspace_id)}


class TestThePipelineAttributesItsCalls:
    def test_transcribe(self, ready_recording, stub_transcribe,
                        stub_summarize, drain, user):
        events.emit_stage(ready_recording.id, 0)
        drain()
        payload = stub_transcribe.calls[0]
        assert payload["user_id"] == str(user.id)
        assert payload["workspace_id"] == str(ready_recording.workspace_id)

    def test_summarize(self, ready_recording, stub_transcribe,
                       stub_summarize, drain, user):
        events.emit_stage(ready_recording.id, 0)
        drain()
        payload = stub_summarize.calls[0]
        assert payload["user_id"] == str(user.id)
        assert payload["workspace_id"] == str(ready_recording.workspace_id)

    def test_the_identity_does_not_displace_the_work(
        self, ready_recording, stub_transcribe, stub_summarize, drain
    ):
        """The payload gained keys; it must not have lost any."""
        events.emit_stage(ready_recording.id, 0)
        drain()
        payload = stub_transcribe.calls[0]
        assert payload["audio_url"].startswith("https://fake.invalid/get/")
        assert payload["diarization"] is True
        assert "timeout_seconds" in payload


class TestTheAgentVersionFloor:
    """W009 — the seam check.

    The llm.* schemas are ``additionalProperties: false``, so sending these
    fields to stapel-agent < 0.12.0 is not a lost field, it is every call
    failing validation. Only answerable where the agent is importable; in a
    split deployment the check stays quiet rather than guessing.
    """

    def _run(self, monkeypatch, installed):
        from stapel_recordings import checks as checks_mod

        def fake_version(name):
            if installed is None:
                from importlib.metadata import PackageNotFoundError

                raise PackageNotFoundError(name)
            return installed

        monkeypatch.setattr("importlib.metadata.version", fake_version)
        return checks_mod.check_agent_version_for_identity(None)

    def test_an_old_agent_is_reported(self, monkeypatch):
        issues = self._run(monkeypatch, "0.11.0")
        assert [i.id for i in issues] == ["stapel_recordings.W009"]
        assert "0.11.0" in issues[0].msg

    def test_the_floor_itself_passes(self, monkeypatch):
        assert self._run(monkeypatch, "0.12.0") == []

    def test_a_newer_agent_passes(self, monkeypatch):
        assert self._run(monkeypatch, "1.2.3") == []

    def test_a_split_deployment_is_not_second_guessed(self, monkeypatch):
        """The agent runs elsewhere; this process cannot see its version and
        must not invent a verdict."""
        assert self._run(monkeypatch, None) == []

    def test_an_unparseable_version_is_not_adjudicated(self, monkeypatch):
        assert self._run(monkeypatch, "0.12.0.dev0+local") == []
