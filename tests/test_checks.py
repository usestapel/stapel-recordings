"""System checks: storage E-level, pipeline/normalizer/threshold W-level."""
import pytest
from django.test import override_settings

from stapel_recordings.checks import (
    check_pipeline_stages,
    check_reconcile_threshold,
    check_storage_backend,
)

pytestmark = pytest.mark.django_db


def test_defaults_are_clean():
    assert check_storage_backend(None) == []
    assert check_pipeline_stages(None) == []
    assert check_reconcile_threshold(None) == []


def test_stuck_threshold_at_or_below_stage_timeout_is_warning():
    """Reconcile must not consider a still-running stage 'stuck' — the
    threshold has to exceed the longest stage duration."""
    with override_settings(STAPEL_RECORDINGS={"STUCK_THRESHOLD_SECONDS": 600}):
        warnings = check_reconcile_threshold(None)
    assert any(w.id == "stapel_recordings.W005" for w in warnings)


def test_bad_storage_is_error():
    with override_settings(STAPEL_RECORDINGS={"STORAGE": "stapel_recordings.models.Recording"}):
        errors = check_storage_backend(None)
    assert any(e.id == "stapel_recordings.E002" for e in errors)


def test_unimportable_storage_is_error():
    with override_settings(STAPEL_RECORDINGS={"STORAGE": "nope.NoSuch"}):
        errors = check_storage_backend(None)
    assert any(e.id == "stapel_recordings.E001" for e in errors)


def test_unknown_pipeline_stage_is_warning():
    with override_settings(STAPEL_RECORDINGS={"PIPELINE": ["convert", "ghost"]}):
        warnings = check_pipeline_stages(None)
    assert any(w.id == "stapel_recordings.W002" for w in warnings)


def test_missing_taskstore_is_error():
    """Нет склада задач — сервис не может работать, и это видно на старте.

    Пин на id: им чек глушат (``SILENCED_SYSTEM_CHECKS``) и по нему ищут, так
    что смена id — это смена публичного контракта модуля.
    """
    from stapel_recordings.checks import check_taskstore_installed

    with override_settings(INSTALLED_APPS=["stapel_recordings"]):
        errors = check_taskstore_installed(None)
    assert any(e.id == "stapel_recordings.E004" for e in errors)


def test_ids_проверок_уникальны():
    """Два чека под одним id — тихая ловушка, а не косметика.

    До 08.08.2026 ``stapel_recordings.E001`` выдавали ДВЕ разные проверки:
    «STORAGE не импортируется» и «нет приложения склада задач». Хост, заглушив
    E001 ради первой, молча выключал бы и вторую — а она блокирует старт
    сервиса, у которого расшифровка не сможет отработать вовсе.

    Сторож смотрит на ИСХОДНИК, а не на прогон: чек, который в текущей
    конфигурации ничего не вернул, всё равно занимает свой id.
    """
    import ast
    import pathlib

    from stapel_recordings import checks as checks_module

    tree = ast.parse(pathlib.Path(checks_module.__file__).read_text())
    занято: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            for kw in inner.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    занято.setdefault(str(kw.value.value), []).append(node.name)

    дубли = {i: fns for i, fns in занято.items() if len(set(fns)) > 1}
    assert not дубли, f"один id на несколько проверок: {дубли}"
