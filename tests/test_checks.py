"""System checks: storage E-level, pipeline/normalizer W-level."""
import pytest
from django.test import override_settings

from stapel_recordings.checks import check_pipeline_stages, check_storage_backend

pytestmark = pytest.mark.django_db


def test_defaults_are_clean():
    assert check_storage_backend(None) == []
    assert check_pipeline_stages(None) == []


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
