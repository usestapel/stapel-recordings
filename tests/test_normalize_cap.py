"""Обрезка по длительности на входе конвейера — основа бесплатных тарифов.

«Первые N минут любой записи бесплатно» реализуется ровно здесь и больше
нигде. Причина в деньгах: обрезав аудио на нормализации, мы отдаём провайдеру
расшифровки только оплаченные минуты. Любая обрезка ПОЗЖЕ означала бы, что за
сорок седьмую минуту мы уже заплатили, а показывать её не будем, — худший из
возможных исходов.

Тесты держат три вещи, каждая из которых ломается молча:
  • без потолка команда ffmpeg обязана остаться БАЙТ В БАЙТ прежней — иначе
    обрезка меняет поведение всех, кто её не просил;
  • ``-t`` стоит ПОСЛЕ ``-i``: перед ``-i`` он ограничивает ввод по времени
    декодирования, и для потоковых контейнеров это другая длительность;
  • возвращается длительность ТОГО, ЧТО ЗАПИСАНО. Вернуть исходную значит
    положить в поле длительности число, которого в файле нет, и весь интерфейс
    начнёт обещать минуты, которых не существует.
"""
import pytest

from stapel_recordings import normalize


class _Готово:
    returncode = 0
    stdout = b""
    stderr = b""


@pytest.fixture
def прогон(monkeypatch):
    """Перехватывает subprocess: проверяем НАСТОЯЩУЮ команду, а не пересказ.

    Подменяется именно ``subprocess.run``, а не ``_run_ffmpeg``: подмена
    сборщика команды означала бы, что тест проверяет тест. Здесь же в
    ``записано["cmd"]`` попадает то, что ушло бы в ffmpeg.
    """
    записано = {}

    def fake_run(cmd, **kwargs):
        записано["cmd"] = list(cmd)
        return _Готово()

    def fake_probe(path):
        return True, записано.get("длительность_исходника", 2820.0)  # 47 минут

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    monkeypatch.setattr(normalize, "_probe_audio", fake_probe)
    return записано


def test_без_потолка_команда_прежняя(прогон):
    normalize.ffmpeg_normalize("in.mp4", "out.wav")
    assert "-t" not in прогон["cmd"], (
        "ограничение просочилось в вызов, которого о нём не просили"
    )


def test_потолок_уходит_в_ffmpeg_после_ввода(прогон):
    normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600)
    cmd = прогон["cmd"]
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "600.000"
    assert cmd.index("-t") > cmd.index("in.mp4"), (
        "-t стоит перед -i: это ограничение ВВОДА, а не вывода"
    )


def test_возвращается_длительность_записанного(прогон):
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 600.0


def test_короткая_запись_не_растягивается(прогон):
    прогон["длительность_исходника"] = 120.0
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 120.0


def test_нулевой_и_отрицательный_потолок_игнорируются(прогон):
    for кривой in (0, -1, -600):
        normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=кривой)
        assert "-t" not in прогон["cmd"], (
            f"потолок {кривой} принят всерьёз — это дало бы файл нулевой длины"
        )


def test_неизвестная_длительность_с_потолком(monkeypatch, прогон):
    """ffprobe не отдал длительность, но обрезка применена.

    Потолок — лучшее, что мы знаем о файле на диске; None означал бы «не знаем
    ничего», хотя мы сами и ограничили запись.
    """
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, None))
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 600.0


def test_probe_duration_не_перекодирует(monkeypatch):
    """Публичная проба нужна ради честной надписи «первые 10 минут из 47»."""
    вызовы = []

    def fake_run(cmd, **kwargs):
        вызовы.append(list(cmd))
        return _Готово()

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, 2820.0))
    assert normalize.probe_duration("in.mp4") == 2820.0
    assert вызовы == [], "проба запустила перекодирование"
