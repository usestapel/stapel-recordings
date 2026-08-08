"""Отметка об ошибке обязана сниматься, когда конвейер снова поехал.

До 08.08.2026 ``metadata["last_error"]`` писалась при падении стадии и не
снималась НИКОГДА: ни при удачной повторной попытке, ни при requeue, ни при
доведении записи до ``completed``. Запись с полной расшифровкой, сводкой и
эмбеддингами продолжала носить причину давно пережитого падения — и по этому
полю принимались решения. На стенде айронмемо восемь встреч выглядели
вставшими на эмбеддингах, будучи полностью обработанными.

Причина не выбрасывается, а переезжает в ``recovered_error``: диагноз для
операций сохраняется, но перестаёт выдавать себя за текущее состояние.
"""
import pytest
from django.test import override_settings

from stapel_recordings import events, stages
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.stages import Stage, StageRetryable

pytestmark = pytest.mark.django_db

_FAKE = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
}


class ФлакающаяСтадия(Stage):
    """Падает на первом прогоне, проходит на втором — как сеть или квота."""

    name = "flaky"
    status = RecordingStatus.MERGING

    падений = 0

    def run(self, recording, ctx):
        type(self).падений += 1
        if type(self).падений == 1:
            raise StageRetryable(reason="эмбеддер отбил пачку", detail="413")
        return ctx


@pytest.fixture
def флака():
    ФлакающаяСтадия.падений = 0
    stages.register_stage("flaky", ФлакающаяСтадия())
    yield ФлакающаяСтадия


def _прогнать(recording_id, drain, индекс=0):
    """Один проход конвейера ``convert -> flaky``.

    *индекс* — то же, что кладёт в событие reconcile, пере-driving
    припаркованную запись: он бьёт в ТЕКУЩУЮ стадию, а не в нулевую
    (повторная выдача уже завершённой отбрасывается сторожем дублей).
    """
    with override_settings(STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["convert", "flaky"]}):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(recording_id, индекс)
        drain()


def test_отметка_ставится_при_падении(ready_recording, флака, drain):
    _прогнать(ready_recording.id, drain)

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.metadata["last_error"]["reason"] == "эмбеддер отбил пачку"
    assert "recovered_error" not in r.metadata


def test_отметка_снимается_когда_стадия_прошла(ready_recording, флака, drain):
    _прогнать(ready_recording.id, drain)  # падение, запись припаркована
    _прогнать(ready_recording.id, drain, 1)  # повтор — стадия проходит

    r = Recording.objects.get(pk=ready_recording.id)
    assert "last_error" not in r.metadata, (
        "запись доехала, а поле продолжает утверждать, что она сломана"
    )
    assert r.status == RecordingStatus.COMPLETED


def test_причина_не_теряется_а_переезжает(ready_recording, флака, drain):
    _прогнать(ready_recording.id, drain)
    _прогнать(ready_recording.id, drain, 1)

    r = Recording.objects.get(pk=ready_recording.id)
    восстановлено = r.metadata["recovered_error"]
    assert восстановлено["reason"] == "эмбеддер отбил пачку"
    assert восстановлено["stage"] == "flaky"
    assert восстановлено["recovered_at"], "нужна отметка, КОГДА отпустило"


def test_на_чистой_записи_ничего_не_появляется(ready_recording, флака, drain):
    """Успех без предшествующего падения не должен плодить пустых полей."""
    ФлакающаяСтадия.падений = 1  # первый же прогон успешный
    _прогнать(ready_recording.id, drain)

    r = Recording.objects.get(pk=ready_recording.id)
    assert "last_error" not in r.metadata
    assert "recovered_error" not in r.metadata


def test_пока_запись_сломана_поле_остаётся(ready_recording, флака, drain):
    """Обратная сторона: у по-настоящему упавшей записи причина обязана быть."""
    with override_settings(
        STAPEL_RECORDINGS={**_FAKE, "PIPELINE": ["convert", "flaky"], "MAX_STAGE_RETRIES": 0}
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        events.emit_stage(ready_recording.id, 0)
        drain()

    r = Recording.objects.get(pk=ready_recording.id)
    assert r.status == RecordingStatus.ERROR
    assert r.metadata["last_error"]["stage"] == "flaky"
    assert "recovered_error" not in r.metadata
