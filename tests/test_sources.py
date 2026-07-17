"""G9: the source-type registry is a settings-overlay, not a code enum."""
import uuid

import pytest
from django.test import override_settings

from stapel_recordings import sources

pytestmark = pytest.mark.django_db


def test_default_registry_mirrors_the_model_enum():
    from stapel_recordings.models import SourceType

    resolved = sources.resolve_source_types()
    assert set(resolved) == {c.value for c in SourceType}
    assert sources.is_valid_source_type("meet")
    assert not sources.is_valid_source_type("zoom")  # not a built-in


def test_overlay_merges_over_builtins():
    with override_settings(
        STAPEL_RECORDINGS={"SOURCE_TYPES": {"zoom": "Zoom", "teams": "Microsoft Teams"}}
    ):
        resolved = sources.resolve_source_types()
        # Built-ins survive; overlay adds new kinds.
        assert resolved["upload"] == "Upload"
        assert resolved["zoom"] == "Zoom"
        assert sources.is_valid_source_type("zoom")
        assert sources.is_valid_source_type("teams")
        assert "zoom" in sources.registered_source_types()


def test_overlay_can_relabel_a_builtin():
    with override_settings(STAPEL_RECORDINGS={"SOURCE_TYPES": {"meet": "Video call"}}):
        assert sources.resolve_source_types()["meet"] == "Video call"


def test_empty_overlay_is_the_default(settings):
    with override_settings(STAPEL_RECORDINGS={"SOURCE_TYPES": {}}):
        assert sources.resolve_source_types() == sources.DEFAULT_SOURCE_TYPES


# ── enforced at the API boundary ──────────────────────────────────────────


def test_create_rejects_unregistered_source_type(use_fakes, api_client, user):
    api_client.force_authenticate(user=user)
    resp = api_client.post(
        "/recordings/api/v1/recordings",
        {
            "workspace_id": str(uuid.uuid4()),
            "title": "x",
            "source_type": "zoom",
            "filename": "take.mp3",
        },
        format="json",
    )
    assert resp.status_code == 400


def test_create_accepts_overlaid_source_type(use_fakes, api_client, user):
    api_client.force_authenticate(user=user)
    with override_settings(
        STAPEL_RECORDINGS={
            "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
            "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
            "SOURCE_TYPES": {"zoom": "Zoom"},
        }
    ):
        from stapel_recordings import storage

        storage.reset_storage_cache()
        resp = api_client.post(
            "/recordings/api/v1/recordings",
            {
                "workspace_id": str(uuid.uuid4()),
                "title": "x",
                "source_type": "zoom",
                "filename": "take.mp3",
            },
            format="json",
        )
    assert resp.status_code == 201, resp.content
    assert resp.json()["recording"]["source_type"] == "zoom"
