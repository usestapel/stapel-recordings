"""Task bridge: the queue lives with the recording, the work goes to the agent.

WHY THE BRIDGE EXISTS. The Task primitive keeps its state in the
``TaskRecord`` table, which only the process that can see that table can
execute. In a microservices layout the recordings and agent databases are
different, so a task submitted by the recordings pipeline is invisible to
the agent: its subscriber would silently skip the unknown kind, and the
recording would stay PENDING forever.

The bridge keeps the queue, state and retries where the recording lives, and
hands the work to the agent as an ordinary Function call over the bus.
"""
import pytest
from django.test import override_settings

from stapel_recordings import task_delegates

pytestmark = pytest.mark.django_db


@pytest.fixture
def clean_handlers():
    from stapel_core.comm import tasks as tp

    saved = dict(tp._handlers)
    tp._handlers.clear()
    yield tp
    tp._handlers.clear()
    tp._handlers.update(saved)


class TestBridgeRegistersOnlyWhenNoHandler:
    def test_registers_kinds_nobody_claimed(self, clean_handlers):
        task_delegates.register_default_task_delegates()
        assert set(clean_handlers.registered_kinds()) == set(task_delegates.DELEGATED)

    def test_does_not_override_real_handler(self, clean_handlers):
        """Monolith: the agent's handler already runs in this process.

        Silently swapping the real executor for the bridge stub would be the
        worst possible outcome — the work would go in a circle.
        """
        real = lambda payload: {"status": "ok", "from": "agent"}  # noqa: E731
        clean_handlers.register_task("llm.transcribe", real)

        task_delegates.register_default_task_delegates()

        assert clean_handlers._handlers["llm.transcribe"] is real

    @override_settings(STAPEL_RECORDINGS={"DELEGATE_TASKS_TO_AGENT": False})
    def test_disabled_by_setting(self, clean_handlers):
        task_delegates.register_default_task_delegates()
        assert clean_handlers.registered_kinds() == []


class TestBridgeDelegatesWithLongTimeout:
    def test_calls_function_of_same_name(self, clean_handlers, monkeypatch):
        seen = {}

        def fake_call(name, payload, *, timeout=None):
            seen.update(name=name, payload=payload, timeout=timeout)
            return {"status": "ok"}

        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "call", fake_call)
        task_delegates.register_default_task_delegates()

        result = clean_handlers._handlers["llm.transcribe"]({"audio_url": "u"})

        assert result == {"status": "ok"}
        assert seen["name"] == "llm.transcribe"
        assert seen["payload"] == {"audio_url": "u"}
        # The timeout that used to be missing: the FUNCTION_TIMEOUT default
        # is five seconds, which every real transcription used to exceed.
        assert seen["timeout"] == 1800.0

    def test_summary_has_its_own_timeout(self, clean_handlers, monkeypatch):
        seen = {}

        def fake_call(name, payload, *, timeout=None):
            seen["timeout"] = timeout
            return {"status": "ok"}

        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "call", fake_call)
        task_delegates.register_default_task_delegates()
        clean_handlers._handlers["llm.summarize"]({"text": "x"})

        assert seen["timeout"] == 300.0
