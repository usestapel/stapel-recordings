"""Single-module Django settings for stapel-recordings's harnesses.

Single source of truth for the ``settings.configure(...)`` block shared by:

  - the pytest suite (``conftest.py``) — mounts recordings on its *bare* test
    urlconf (``stapel_recordings.tests.urls``), the historical test layout; and
  - the contract-emission harness (``_codegen.py`` / ``make contract``) —
    mounts recordings on its *canonical* public API prefix
    (``stapel_recordings.codegen_urls`` → ``recordings/`` — the module's own
    ``urls.py`` already bakes in the ``api/recordings`` segment, so the full
    canonical path is ``/recordings/api/recordings``) and enables
    drf-spectacular, so the emitted ``schema.json`` / ``flows.json`` paths
    match the module's own documented mount recipe
    (``urls.py``: ``path("recordings/", include("stapel_recordings.urls"))``)
    (contract-pipeline.md §2).

Keeping one copy here means the harness and the tests can never drift in their
``INSTALLED_APPS`` / mock config — the exact hazard contract-pipeline.md §3
calls out ("~30 lines that *reference* the already-existing config, not a
second copy of it"). Copied from stapel-auth's etalon
(``_codegen_settings.py``) via stapel-profiles' adaptation; adapted to this
module's actual conftest content (no gdpr/social_django/JWT-issuance config —
recordings only *consumes* JWTCookieAuthentication, it never issues tokens).

Unlike auth/profiles (where ``drf_spectacular`` and
``stapel_core.django.apps.CommonDjangoConfig`` already sat in the
pre-extraction conftest, so the extraction was a pure move), recordings'
original conftest carried neither. Both are added here unconditionally
(``contract`` and non-``contract`` alike) because ``CommonDjangoConfig``
supplies the ``generate_flow_docs`` / ``generate_error_keys`` management
commands the codegen harness calls — there is no way to gate them to
``contract=True`` only, since ``INSTALLED_APPS`` is one list shared by both
call sites. Verified harmless for the test suite: all pre-existing tests pass
unchanged with both apps present (``CommonDjangoConfig.ready()`` runs its
system-check registrations, admin-visibility setup, and one-time DRF
``api_settings.reload()`` — none of recordings' tests assert on any of that).
"""
from __future__ import annotations

import os
from urllib.parse import urlparse


def _postgres_database() -> dict | None:
    """Opt-in postgres test harness for the vector layer.

    ``STAPEL_RECORDINGS_TEST_DB=postgres://user:pass@host:port/dbname``
    switches the suite's database to postgres AND installs the opt-in
    ``stapel_recordings.vector`` app, unlocking the DB-bound vector tests
    (``tests/test_vector_postgres.py`` — vendor-skipped otherwise). Under
    this harness the REAL migrations run (MIGRATION_MODULES is not
    disabled — syncdb can't order cross-app FKs on postgres), which also
    exercises the vector app's 0001 for real (VectorExtension + HNSW), so
    the connecting role must be allowed to ``CREATE EXTENSION vector`` (or
    have it pre-created in template1). Default (env var unset): sqlite, no
    vector app — the canonical zero-extra suite."""
    url = os.environ.get("STAPEL_RECORDINGS_TEST_DB", "")
    if not url.startswith(("postgres://", "postgresql://")):
        return None
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (parsed.path or "/").lstrip("/") or "stapel_recordings_test",
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_recordings.tests.urls",
    contract: bool = False,
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module
    recordings instance.

    ``root_urlconf`` selects the mount: bare (``stapel_recordings.tests.urls``)
    for the test suite, canonical-prefix (``stapel_recordings.codegen_urls`` →
    ``recordings/``) for contract emission.

    ``contract=True`` swaps in the *production* ``REST_FRAMEWORK`` (the
    canonical stapel-core config, inlined as plain dotted paths — importing it
    would trip the same chicken-and-egg as spectacular). This matters for
    byte-identity: a real deployment emits with
    ``DEFAULT_SCHEMA_CLASS=PermissionAwareAutoSchema`` and the real
    permission/renderer classes, and DRF caches ``REST_FRAMEWORK`` on first
    access, so it must be right at ``configure()`` time — a post-hoc
    assignment is too late. The test suite keeps its historical config
    (``contract=False``, no ``REST_FRAMEWORK`` key at all — DRF's own
    defaults, matching the conftest before this extraction).

    ``SPECTACULAR_SETTINGS`` is deliberately *not* set. drf-spectacular builds
    its settings singleton at *import* time (``getattr(settings,
    'SPECTACULAR_SETTINGS', {})`` at module load), before a
    ``configure()``-based harness can populate it, so a Django-level
    ``SPECTACULAR_SETTINGS`` is silently ignored and the emitter runs on drf
    **defaults** (``info.title=""``, no ``x-stapel-*`` extensions) — the same
    state every other pair-backend's harness emits under. The one knob that
    still must be forced, ``SCHEMA_PATH_PREFIX``, is patched on the singleton
    directly by the harness (see ``_codegen._configure``).
    """
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under; auth/profiles inline the same
        # block). Inlined, not imported, to dodge the import-time settings
        # read; kept in lockstep by test_contract.py's identity gate.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "stapel_core.django.outbox",
            "rest_framework",
            "drf_spectacular",
            "stapel_recordings",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": _postgres_database()
            or {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        MEDIA_ROOT="/tmp/stapel-recordings-test-media",
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Realistic comm: Actions leave through the transactional outbox
        # (so producer/consumer can be tested as split synchronous halves,
        # §7.21) and schema validation is ON — the committed contracts in
        # schemas/ are enforced by the tests. Schema emission never executes
        # an action, so this only needs to be present, not exercised.
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
    if _postgres_database() is not None:
        # Postgres harness: install the opt-in vector app so its models
        # (and the vendor-gated tests) come alive, and run the REAL
        # migrations — syncdb (MIGRATION_MODULES=None) cannot create the
        # users/recordings tables on postgres because their FKs need the
        # auth tables migrated first, and running the vector app's actual
        # 0001 (VectorExtension + HNSW index) is exactly what this harness
        # exists to verify.
        kwargs["INSTALLED_APPS"].append("stapel_recordings.vector")
        kwargs["MIGRATION_MODULES"] = {}
        # VECTOR["DIM"] is baked into the column at migrate time; pin the
        # small test dimensionality here so the vector tests' 3-dim stub
        # embeddings fit the migrated column.
        kwargs["STAPEL_RECORDINGS"] = {"VECTOR": {"DIM": 3}}
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects when every
# pair-backend's schema is emitted inside an all-modules aggregate. Forced on
# the drf-spectacular settings singleton by the harness so a single-module
# instance derives the same operationIds (see _codegen._configure and the
# SCHEMA_PATH_PREFIX note above). Uniform across all pair-backends
# (contract-pipeline.md §2).
CODEGEN_SCHEMA_PATH_PREFIX = "/"
