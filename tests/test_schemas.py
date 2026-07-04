"""Emitted actions are validated against the committed JSON schemas."""
import pytest
from stapel_core.comm import emit
from stapel_core.comm.exceptions import SchemaValidationError

from stapel_recordings import events

pytestmark = pytest.mark.django_db


def test_valid_stage_payload_passes():
    emit(events.ACTION_STAGE, {"recording_id": "r1", "stage_index": 0})  # no raise


def test_missing_required_field_rejected():
    with pytest.raises(SchemaValidationError):
        emit(events.ACTION_STAGE, {"recording_id": "r1"})


def test_additional_property_rejected():
    with pytest.raises(SchemaValidationError):
        emit(events.ACTION_STAGE, {"recording_id": "r1", "stage_index": 0, "extra": 1})


def test_completed_schema_allows_nullable_owner():
    emit(events.ACTION_COMPLETED, {
        "recording_id": "r1",
        "workspace_id": "w1",
        "owner_id": None,
        "duration_seconds": None,
        "segments_count": 0,
        "speakers_count": 0,
        "word_count": 0,
        "provider_used": None,
    })
