"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

The pytest urlconf (``stapel_recordings.tests.urls``) mounts recordings at
``recordings/`` too — unlike auth/profiles, this module's own ``urls.py``
already bakes the ``api/recordings`` segment into its own path entries
(``path("api/recordings", ...)`` etc.), so the module's *documented* canonical
mount (its own ``urls.py`` module docstring: ``path("recordings/",
include("stapel_recordings.urls"))``) already yields the canonical
``/recordings/api/recordings`` public prefix without a harness-side rename.

This file exists anyway (rather than pointing the harness straight at
``stapel_recordings.tests.urls``) so the contract-emission mount is declared
independently of the test urlconf and can never silently drift from the
module's documented public mount recipe (contract-pipeline.md §2, §9) — the
same one-small-file-per-concern shape as every other pair-backend's
``codegen_urls.py``.

stapel-recordings is **not mounted in stapel-example-monolith** (grep-confirmed
2026-07-09: no ``include("stapel_recordings.urls")`` in
``svc-app/core/urls.py``) — there is no monolith aggregate slice to reproduce
byte-for-byte here. The validation this module's ``tests/test_contract.py``
performs is therefore standalone (determinism + closure + canonical prefix +
security presence), not a byte-identity diff.
"""
from django.urls import include, path

urlpatterns = [
    path("recordings/", include("stapel_recordings.urls")),
]
