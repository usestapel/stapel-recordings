"""Исполнитель задач по умолчанию: держим очередь у себя, работу отдаём агенту.

ЗАЧЕМ ЭТОТ МОСТ. Task-примитив хранит состояние в таблице ``TaskRecord``, и
исполнить задачу может только процесс, который эту таблицу ВИДИТ. В
монолите это одна база, и обработчик ``llm.transcribe`` из stapel-agent
живёт в том же процессе — мост не нужен, ничего не регистрируем.

В микросервисной раскладке базы РАЗНЫЕ (замер на стенде ironmemo
08.08.2026: ``iron_recordings`` против ``iron_agent``). Задача, поставленная
конвейером записей, лежит в базе записей, и агент про неё не знает —
``handle_task_requested`` в его процессе молча пропустит незнакомый вид, а
запись останется PENDING навсегда.

ПОЭТОМУ: очередь, состояние, попытки и наблюдаемость остаются у записей —
там, где живёт и сама запись, и человек, который ждёт результата. А работу
исполнитель отдаёт агенту обычным Function-вызовом через шину, как и всё
остальное межсервисное.

Это НЕ возврат к синхронному вызову, от которого мы ушли:

* ждёт фоновый исполнитель задач, а не HTTP-воркер — занятость воркера
  больше не оплачивается временем работы модели;
* срок ожидания задан явно и по-настоящему длинный, а не пятисекундный
  дефолт ``FUNCTION_TIMEOUT``, на котором падала каждая настоящая запись;
* пока идёт работа, у человека есть ``status(task_id)``: pending / running,
  число попыток — то, чего у синхронного вызова не было в принципе.

Отключается ``STAPEL_RECORDINGS["DELEGATE_TASKS_TO_AGENT"] = False`` —
развёртыванию, которое исполняет ``llm.*`` как-то иначе.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Виды задач, которые мост берёт на себя, и срок ожидания каждой.
#: Ключ — имя настройки со сроком; None — берём общий бюджет расшифровки.
DELEGATED = {
    "llm.transcribe": "TRANSCRIBE_TIMEOUT_SECONDS",
    "llm.summarize": "SUMMARIZE_TIMEOUT_SECONDS",
}


def _make_delegate(kind: str, timeout_setting: str):
    def delegate(payload: dict):
        from stapel_core.comm import call

        from .conf import recordings_settings

        timeout = float(getattr(recordings_settings, timeout_setting))
        # noqa: R009 — это и есть САНКЦИОНИРОВАННЫЙ мост, а не тот дефект,
        # против которого правило писалось. Здесь: (1) ждёт фоновый
        # исполнитель задачи, а не HTTP-воркер; (2) срок задан явно и
        # длинный; (3) состояние ожидания видно снаружи через status().
        # Правило запрещает ждать долгую операцию ТАМ, где ждать нельзя, —
        # а не запрещает межсервисный вызов как таковой.
        return call(kind, payload, timeout=timeout)  # noqa: R009

    delegate.__name__ = f"delegate_{kind.replace('.', '_')}"
    delegate.__doc__ = f"Отдать {kind} агенту через шину и вернуть его ответ."
    return delegate


def register_default_task_delegates() -> None:
    """Зарегистрировать мост для видов, которые НИКТО ЕЩЁ не взял.

    Порядок важен и держится на одном свойстве: если stapel-agent уже
    зарегистрировал настоящий обработчик в этом же процессе (монолит), мы
    не трогаем его — ``register_task`` на занятое имя поднял бы ValueError,
    и молчаливая подмена настоящего исполнителя заглушкой-мостом была бы
    худшим из возможных исходов.
    """
    from stapel_core.comm import tasks as task_primitive

    from .conf import recordings_settings

    if not recordings_settings.DELEGATE_TASKS_TO_AGENT:
        return

    taken = set(task_primitive.registered_kinds())
    for kind, timeout_setting in DELEGATED.items():
        if kind in taken:
            continue  # настоящий обработчик рядом — мост не нужен
        task_primitive.register_task(kind, _make_delegate(kind, timeout_setting))
        logger.debug("recordings: мост задачи %s → Function того же имени", kind)
