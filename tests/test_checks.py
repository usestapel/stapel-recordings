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
