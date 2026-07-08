"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-recordings emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty here, this module has no ``@flow_step`` annotations) and
``docs/errors.json`` (generate_error_keys registry) — from a single-module
``{recordings + core}`` Django instance mounted at the canonical
``/recordings/api/`` prefix. The frontend codegen consumes these committed
artifacts.

Unlike auth/profiles, **stapel-recordings is not mounted in
stapel-example-monolith** (grep-confirmed: no route for it in
``svc-app/core/urls.py`` as of this writing — recordings is a standalone
pair-backend pending its own frontend pair). There is therefore no monolith
aggregate slice to assert byte-identity against. Validation here is
**standalone** instead:

  - determinism (two independent emissions are byte-identical — the drift
    gate below is only meaningful if this holds);
  - the schema's ``$ref`` closure is self-contained (every path/component
    reference resolves within this one file — no dangling refs, no reference
    to a component this harness never defined);
  - the protected endpoints carry the ``JWTCookieAuth`` security requirement
    (the profiles-finding gap: without an explicit
    ``_register_jwt_auth_extension()`` call, a module with no co-mounted
    sibling silently drops ``security`` from every operation);
  - paths carry the canonical ``/recordings/api/`` prefix.

Regenerate after any change to a serializer / view / url / error key:

    make contract        # or: python -m stapel_recordings._codegen --out docs

then commit ``docs/{schema,flows,errors}.json``. Without regenerating, the drift
gate below fails — the same byte-stable regenerate-and-diff discipline as
every other pair-backend's contract.

The harness runs in a **subprocess**: this test process already configured Django
(via conftest, on the bare test urlconf), and the harness needs its own
canonical-prefix urlconf + drf-spectacular singleton — a clean interpreter is the
honest way to exercise exactly what ``make contract`` runs.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
CANONICAL_PREFIX = "/recordings/api/"


def _emit(out_dir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "stapel_recordings._codegen", "--out", str(out_dir)],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def test_contract_triad_committed():
    for name in TRIAD:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed triad must match byte-for-byte."""
    _emit(tmp_path)
    for name in TRIAD:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in TRIAD:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """The mount-prefix fix: schema paths + flow endpoints are /recordings/api/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith(CANONICAL_PREFIX) for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /recordings/api/ prefix"
    )
    flows = json.loads((DOCS / "flows.json").read_text())
    for flow in flows:
        for step in flow.get("steps", []):
            for ep in step.get("endpoints", []):
                assert ep["path"].startswith(CANONICAL_PREFIX), (
                    f"flow endpoint {ep['path']} is not canonically prefixed"
                )


def test_flows_is_empty_flowless_module():
    """recordings has no @flow_step annotations — [] is the valid, expected artifact."""
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == [], (
        "flows.json is non-empty — recordings gained @flow_step annotations; "
        "update this test's assumption (it is no longer a flowless module)"
    )


def _refs(obj) -> set[str]:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_ref_closure_is_self_contained():
    """Every $ref reachable from a path resolves inside this one schema.json.

    Standalone analog of the byte-identity-vs-monolith check auth/profiles run
    (contract-pipeline.md §9 Q2): with no monolith slice to diff against here,
    the guarantee that matters is that the ``{module + core}`` harness emitted
    a *closed* component table — no path or component references a schema
    this module never defined (e.g. a sibling-only type that would require
    installing that sibling in the harness).
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema.get("components", {}).get("schemas", {})

    seeds: set[str] = set()
    for path_obj in schema["paths"].values():
        seeds |= _refs(path_obj)
    assert seeds, "no component is referenced from any path — unexpected for a DRF API"

    seen: set[str] = set()
    stack = list(seeds)
    dangling: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in comps:
            dangling.add(name)
            continue
        stack.extend(_refs(comps[name]))

    assert not dangling, f"dangling $ref(s), not defined in this module's own schema: {dangling}"


def test_protected_endpoints_carry_jwt_security():
    """The profiles-finding gap: a module with no co-mounted sibling loses
    `security: [{"JWTCookieAuth": []}]` unless _codegen.py explicitly calls
    stapel_core's `_register_jwt_auth_extension()` before emission. All three
    recordings views are `permission_classes = [IsAuthenticated]`, so every
    operation here is expected to carry the JWT cookie security requirement.
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "JWTCookieAuth" in security_schemes, (
        "JWTCookieAuth security scheme missing — _register_jwt_auth_extension() "
        "regression (see _codegen.py._configure)"
    )
    for path, path_obj in schema["paths"].items():
        for method, op in path_obj.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security")
            assert security and any("JWTCookieAuth" in s for s in security), (
                f"{method.upper()} {path} is missing the JWTCookieAuth security "
                "requirement — protected endpoint emitted without security"
            )
