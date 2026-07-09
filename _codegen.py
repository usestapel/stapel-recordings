"""stapel-recordings contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{recordings + core}`` Django instance mounted at the canonical
``recordings/api/`` prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact — empty here, this
                      module has no ``@flow_step`` annotations
  docs/errors.json   generate_error_keys registry (the per-module etalon)

Copied from stapel-auth's reference implementation (``_codegen.py``, ETALON)
via stapel-profiles' adaptation; the *mechanism* is stapel_tools.codegen
(unchanged, shared), this file is the thin per-module *config* that wires the
module's settings + canonical mount into it.

Unlike auth/profiles, stapel-recordings is **not mounted in
stapel-example-monolith** (grep-confirmed: no route for it in
``stapel-example-monolith/svc-app/core/urls.py``), so there is no monolith
aggregate slice to diff this artifact against for byte-identity. Validation is
standalone instead — see ``tests/test_contract.py``.

Usage:
    python -m stapel_recordings._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    # `python -m` prepends cwd to sys.path; strip the repo root the same way
    # conftest.py does (defensively — recordings has no colliding subpackage
    # today, but the guard costs nothing and keeps the harness identical in
    # shape to auth/profiles).
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_recordings._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_recordings.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before this
    # harness ran configure()), so it is on drf defaults — the same state
    # every other pair-backend's harness emits under. The one knob to force is
    # SCHEMA_PATH_PREFIX: left None, drf derives the operationId prefix from
    # the common path of all endpoints — "/" across a multi-module aggregate
    # (operationIds keep the mount segment, recordings_api_*), but
    # "/recordings/api" in a single-module harness (which would strip it to
    # bare anonymous names). Pin it to the aggregate convention so the
    # operationIds match every other module's harness; SCHEMA_PATH_PREFIX_TRIM
    # stays False (default) so the path *keys* keep /recordings/api/ on both
    # sides.
    from drf_spectacular.settings import spectacular_settings

    from stapel_recordings._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # A real all-modules deployment registers drf-spectacular's JWT-cookie
    # security-scheme extension as a side effect of its own dev-only Swagger
    # URLs (DJANGO_ENV=local) — a *global* registration on drf-spectacular's
    # extension registry, not tied to any one module's urls.py. stapel-auth's
    # harness gets it for free only because its co-mounted sibling
    # (stapel_gdpr.urls) happens to trigger the same registration; recordings
    # has no co-mounted sibling (profiles found and documented this same gap —
    # contract-pipeline.md brief, "profiles finding"). Without registering it
    # explicitly here, recordings' protected endpoints (all three views are
    # ``permission_classes = [IsAuthenticated]``) would emit without their
    # `security: [{"JWTCookieAuth": []}]` entry.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular's rendering of component descriptions (``Optional[X]`` vs
    ``X | None``) depends on the Python **minor** version — contracts emitted
    on anything other than 3.12 (the CI/monolith pin) produce false diffs
    against the committed docs/*.json. Emission must never proceed on the
    wrong minor.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-recordings contract emission ABORTED: running Python "
            f"{got}, but contracts must be emitted on Python 3.12 (the "
            "CI/monolith pin). drf-spectacular renders component "
            "descriptions (Optional[X] vs X | None) differently across "
            "Python minor versions, so emitting on any other minor produces "
            "false diffs against the committed docs/*.json. Re-run under a "
            "3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-recordings-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /recordings/api/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    # Reuse the shared mechanism's byte-stable emitters (contract-pipeline.md §2:
    # "the single-module harness already exists").
    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-recordings contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
