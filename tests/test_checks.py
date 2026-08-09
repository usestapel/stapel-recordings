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
    """No task store means the service cannot run, and this is caught at startup.

    The id is pinned: hosts silence and search checks by it, so changing it
    is a public contract change.
    """
    from stapel_recordings.checks import check_taskstore_installed

    with override_settings(INSTALLED_APPS=["stapel_recordings"]):
        errors = check_taskstore_installed(None)
    assert any(e.id == "stapel_recordings.E004" for e in errors)


def test_check_ids_are_unique():
    """Two checks sharing one id is a silent trap, not cosmetics.

    Before 2026-08-08, ``stapel_recordings.E001`` was raised by TWO different
    checks: "STORAGE not importable" and "task store app missing". Silencing
    E001 for the first would have silently disabled the second too — which
    blocks startup for a service whose transcription cannot run at all.

    This guard reads the SOURCE, not a live run: a check that returns nothing
    under the current configuration still owns its id.
    """
    import ast
    import pathlib

    from stapel_recordings import checks as checks_module

    tree = ast.parse(pathlib.Path(checks_module.__file__).read_text())
    used: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            for kw in inner.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    used.setdefault(str(kw.value.value), []).append(node.name)

    duplicates = {i: fns for i, fns in used.items() if len(set(fns)) > 1}
    assert not duplicates, f"one id shared by multiple checks: {duplicates}"
