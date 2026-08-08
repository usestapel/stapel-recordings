"""Мост задач: очередь у записей, работа у агента.

ПОЧЕМУ МОСТ ВООБЩЕ НУЖЕН. Task-примитив хранит состояние в таблице
``TaskRecord``, и исполнить задачу может только процесс, который эту
таблицу видит. Замер на стенде ironmemo 08.08.2026: у сервисов РАЗНЫЕ базы
(``iron_recordings`` против ``iron_agent``). Значит задача, поставленная
конвейером записей, агенту не видна: его подписчик молча пропустит
незнакомый вид, и запись останется PENDING навсегда — то есть человек
снова смотрел бы на «обрабатывается», только теперь вечно.

Мост оставляет очередь, состояние и попытки там, где живёт запись, а
работу отдаёт агенту обычным Function-вызовом через шину.
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


class TestМостВстаётТолькоКогдаНекому:
    def test_регистрирует_виды_которые_никто_не_взял(self, clean_handlers):
        task_delegates.register_default_task_delegates()
        assert set(clean_handlers.registered_kinds()) == set(task_delegates.DELEGATED)

    def test_не_подменяет_настоящий_обработчик(self, clean_handlers):
        """Монолит: обработчик агента уже в этом процессе.

        Молчаливая подмена настоящего исполнителя заглушкой-мостом была бы
        худшим из возможных исходов — работа ушла бы по кругу.
        """
        real = lambda payload: {"status": "ok", "from": "agent"}  # noqa: E731
        clean_handlers.register_task("llm.transcribe", real)

        task_delegates.register_default_task_delegates()

        assert clean_handlers._handlers["llm.transcribe"] is real

    @override_settings(STAPEL_RECORDINGS={"DELEGATE_TASKS_TO_AGENT": False})
    def test_выключается_настройкой(self, clean_handlers):
        task_delegates.register_default_task_delegates()
        assert clean_handlers.registered_kinds() == []


class TestМостОтдаётРаботуСДлиннымСроком:
    def test_зовёт_функцию_того_же_имени(self, clean_handlers, monkeypatch):
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
        # Тот самый срок, которого не было: дефолт FUNCTION_TIMEOUT — пять
        # секунд, и на нём падала КАЖДАЯ настоящая расшифровка.
        assert seen["timeout"] == 1800.0

    def test_у_сводки_свой_срок(self, clean_handlers, monkeypatch):
        seen = {}

        def fake_call(name, payload, *, timeout=None):
            seen["timeout"] = timeout
            return {"status": "ok"}

        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "call", fake_call)
        task_delegates.register_default_task_delegates()
        clean_handlers._handlers["llm.summarize"]({"text": "x"})

        assert seen["timeout"] == 300.0
