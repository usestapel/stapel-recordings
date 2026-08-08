"""Action subscriptions of stapel-recordings.

Handlers are idempotent (delivery is at-least-once — outbox retries, broker
redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.
"""
import logging

from stapel_core.comm import on_action

from . import events

logger = logging.getLogger(__name__)


@on_action(events.ACTION_UPLOADED)
def handle_uploaded(event):
    """A file landed — start the pipeline driver."""
    from .pipeline import start_pipeline

    recording_id = event.payload.get("recording_id")
    if not recording_id:
        logger.error("recording.uploaded without recording_id: %s", event.event_id)
        return
    start_pipeline(recording_id)


@on_action(events.ACTION_STAGE)
def handle_stage(event):
    """Run one stage of the resolved pipeline (the generic driver step)."""
    from .pipeline import run_stage

    recording_id = event.payload.get("recording_id")
    stage_index = event.payload.get("stage_index")
    if recording_id is None or stage_index is None:
        logger.error("recording.stage missing fields: %s", event.event_id)
        return
    run_stage(recording_id, int(stage_index))


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase a user's recordings + their storage objects (GDPR Art. 17)."""
    from .gdpr import RecordingsGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    RecordingsGDPRProvider().delete(user_id)
    logger.info("recordings erased for deleted user %s", user_id)


# ─── Возобновление стадии, ждавшей задачу ──────────────────────────────
#
# Долгая работа (расшифровка, сводка) идёт Task-примитивом: стадия ставит
# задачу и отпускает воркер, а досчитывается здесь, когда результат придёт.
# Раньше на её месте был синхронный вызов, и вся система стояла ровно
# столько, сколько работала модель, — с потолком в пять секунд, за которым
# начинались отказы (инцидент 08.08.2026).
#
# Фильтруем по ``kind``: подписка на ``task.completed`` общая для всего
# процесса, и мимо неё едут задачи чужих модулей. Дальше решает сама запись
# — сверка ``task_id`` с ожидаемым живёт в ``resume_stage``.


def _recording_of(event):
    """id записи из correlation_id задачи (его ставит ``submit_task``)."""
    return event.payload.get("correlation_id") or ""


@on_action("task.completed")
def handle_task_completed(event):
    from stapel_core.comm import status

    from .pipeline import resume_stage

    recording_id = _recording_of(event)
    task_id = event.payload.get("task_id")
    if not recording_id or not task_id:
        return
    try:
        snapshot = status(task_id)
    except Exception:
        logger.exception("task.completed: не удалось прочитать задачу %s", task_id)
        return
    resume_stage(recording_id, task_id, snapshot.result)


@on_action("task.failed")
def handle_task_failed(event):
    from .pipeline import fail_stage

    recording_id = _recording_of(event)
    task_id = event.payload.get("task_id")
    if not recording_id or not task_id:
        return
    fail_stage(recording_id, task_id, event.payload.get("error") or "")
