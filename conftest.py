def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py
        # so the test harness and the contract-emission harness (make
        # contract) can never drift (contract-pipeline.md §3). Tests keep the
        # bare mount + no production REST_FRAMEWORK, exactly as before the
        # extraction. The one addition vs. the pre-extraction conftest:
        # INSTALLED_APPS now also carries drf_spectacular + stapel_core's
        # CommonDjangoConfig (needed for the contract harness's management
        # commands) — verified harmless for the test suite (all pre-existing
        # tests pass unchanged; see _codegen_settings.py docstring).
        from stapel_recordings._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture(autouse=True)
def _reset_recordings_state():
    """Isolate runtime stage registrations, the storage-backend cache, and
    any stub comm Functions between tests."""
    from stapel_core.comm.registry import function_registry

    from stapel_recordings import stages, storage

    yield
    stages.reset_runtime_stages()
    storage.reset_storage_cache()
    for name in ("llm.transcribe", "llm.summarize", "workspaces.check_membership"):
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)
