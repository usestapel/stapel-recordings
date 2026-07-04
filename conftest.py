def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.admin",
                "django.contrib.messages",
                "stapel_core.django.users",
                "stapel_core.django.outbox",
                "rest_framework",
                "stapel_recordings",
            ],
            AUTH_USER_MODEL="users.User",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            ROOT_URLCONF="stapel_recordings.tests.urls",
            MEDIA_ROOT="/tmp/stapel-recordings-test-media",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # Realistic comm: Actions leave through the transactional outbox
            # (so producer/consumer can be tested as split synchronous halves,
            # §7.21) and schema validation is ON — the committed contracts in
            # schemas/ are enforced by the tests.
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            STAPEL_COMM={
                "OUTBOX_ENABLED": True,
                "ACTION_TRANSPORT": "inprocess",
                "FUNCTION_TRANSPORT": "inprocess",
                "VALIDATE_SCHEMAS": True,
            },
            MIGRATION_MODULES={
                "users": None,
                "recordings": None,
            },
        )
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
    for name in ("llm.transcribe", "llm.summarize"):
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)
