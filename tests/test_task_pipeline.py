"""Долгая работа ставится задачей, а стадия ждёт и досчитывается потом.

ПОЧЕМУ ЭТОТ НАБОР ОТДЕЛЬНЫЙ. Остальной набор гоняет задачи синхронно
(``TASK_DISPATCH="inline"`` — честная модель монолита без брокера), и в нём
ветка ожидания просто не встречается: ``start()`` успевает выполнить
обработчик, ``submit_task`` возвращает результат, стадия завершается в один
проход. Ровно поэтому её надо проверять отдельно — иначе главное свойство
перехода останется непокрытым, а набор будет выглядеть зелёным.

ЧТО ЭТО ЗА ПЕРЕХОД. Раньше расшифровка звалась синхронным Function:
вызывающий держал воркер и ждал ответа, а «сколько ждать» брал из
``FUNCTION_TIMEOUT`` — пять секунд по умолчанию. Замер на стенде айронмемо
08.08.2026: настоящая расшифровка укладывается в 14 секунд, сводка — в 36.
То есть КАЖДАЯ настоящая запись падала по таймауту, ретраилась трижды и
уходила в error спустя два с половиной часа, а человек всё это время
смотрел на «обрабатывается». Очередь ломает синхронную модель до конца:
занятых исполнителей нельзя дождаться за фиксированный срок в принципе.
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
    """Задачи НЕ исполняются в start() — ровно как с брокером и очередью."""
    from stapel_core.comm import tasks as _tasks

    # Обработчик снят: никакой процесс здесь llm.transcribe не исполняет,
    # значит задача останется PENDING, а стадия обязана уйти в ожидание.
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


class ОжиданиеЭтоНеОтказ:
    """Пометка-заголовок: StageAwaiting не должен считаться попыткой."""


class TestСтадияУходитВОжидание:
    def test_transcribe_поднимает_awaiting_а_не_ошибку(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        assert caught.value.kind == "llm.transcribe"
        assert caught.value.task_id

    def test_задача_создана_и_ждёт_исполнителя(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        from stapel_core.comm import status

        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        snapshot = status(caught.value.task_id)
        assert snapshot.state == "pending"
        assert snapshot.kind == "llm.transcribe"
        # Работа НЕ потеряна: она лежит в таблице и переживёт рестарт —
        # именно этого не умел синхронный вызов.

    def test_корреляция_ведёт_к_записи(self, ready_recording, use_fakes, deferred_tasks):
        from stapel_core.django.taskstore.models import TaskRecord

        with pytest.raises(StageAwaiting) as caught:
            TranscribeStage().run(ready_recording, {})
        record = TaskRecord.objects.get(pk=caught.value.task_id)
        assert record.correlation_id == str(ready_recording.id)


class TestДрайверЗапоминаетОжидание:
    def _drive(self, recording):
        pipeline.run_stage(str(recording.id), 0)  # convert
        pipeline.run_stage(str(recording.id), 1)  # transcribe -> ожидание

    def test_запись_не_падает_и_не_завершается(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.TRANSCRIBING
        # Не error и не completed: работа идёт, и статус об этом ЧЕСТНО
        # говорит. Человеку есть что показать — «расшифровываем», а не
        # индикатор без обещаний.

    def test_ожидание_не_тратит_попытку(self, ready_recording, use_fakes, deferred_tasks):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert r.retry_count == 0

    def test_стадия_не_отмечена_завершённой(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        self._drive(ready_recording)
        r = Recording.objects.get(pk=ready_recording.id)
        assert "transcribe" not in (r.metadata["pipeline"].get("completed") or [])
        assert r.metadata["pipeline"]["awaiting"]["kind"] == "llm.transcribe"


class TestВозобновление:
    def _await_task(self, recording):
        pipeline.run_stage(str(recording.id), 0)
        pipeline.run_stage(str(recording.id), 1)
        return Recording.objects.get(pk=recording.id).metadata["pipeline"]["awaiting"]["task_id"]

    def test_результат_досчитывает_стадию(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        task_id = self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 1
        assert r.provider_used == "stub-asr"
        assert "transcribe" in r.metadata["pipeline"]["completed"]
        assert "awaiting" not in r.metadata["pipeline"]

    def test_чужой_результат_игнорируется(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        """Доставка at-least-once, задача могла быть перезапущена.

        Ответ на устаревшую попытку обязан быть отброшен — иначе стадия
        досчитается по данным, которых у неё уже не просили.
        """
        self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), str(uuid.uuid4()), TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 0
        assert "transcribe" not in (r.metadata["pipeline"].get("completed") or [])

    def test_повторная_доставка_того_же_результата_безвредна(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        task_id = self._await_task(ready_recording)
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)
        # Второй раз: ожидания уже нет, применять нечего.
        pipeline.resume_stage(str(ready_recording.id), task_id, TRANSCRIPT_OK)

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.segments_count == 1


class TestПровалЗадачи:
    def test_окончательный_отказ_уводит_в_dlq(
        self, ready_recording, use_fakes, deferred_tasks
    ):
        pipeline.run_stage(str(ready_recording.id), 0)
        pipeline.run_stage(str(ready_recording.id), 1)
        task_id = Recording.objects.get(
            pk=ready_recording.id
        ).metadata["pipeline"]["awaiting"]["task_id"]

        pipeline.fail_stage(str(ready_recording.id), task_id, "провайдер недоступен")

        r = Recording.objects.get(pk=ready_recording.id)
        assert r.status == RecordingStatus.ERROR
        # Причина названа, а не спрятана: раньше на этом месте был
        # TimeoutError без единого слова о том, что именно не получилось.
        assert r.metadata["last_error"]["reason"] == "task_failed"
        assert "провайдер недоступен" in str(r.metadata["last_error"]["detail"])
