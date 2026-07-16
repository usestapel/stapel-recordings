"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-recordings emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty here, this module has no ``@flow_step`` annotations) and
``docs/errors.json`` (generate_error_keys registry) — from a single-module
``{recordings + core}`` Django instance mounted at the canonical
``/recordings/api/v1/`` prefix. The frontend codegen consumes these committed
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
  - paths carry the canonical ``/recordings/api/v1/`` prefix.

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

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    _PY312_MSG = (
        "stapel-recordings contract tests require Python 3.12 (the "
        f"CI/monolith pin) — running {_GOT}. drf-spectacular renders "
        "component descriptions (Optional[X] vs X | None) differently "
        "across Python minor versions, so drift/identity checks "
        "emitted+compared under any other minor produce false diffs."
    )
    pytest.skip(
        _PY312_MSG + " Skipping on any non-3.12 interpreter (CI or local) — "
        "the contract canon is only defined on Python 3.12.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
CANONICAL_PREFIX = "/recordings/api/v1/"
# The fourth artifact (capability-config.md §2): config axes over
# STAPEL_RECORDINGS, emitted from conf.py DEFAULTS + the urls.py gate
# registry + schema.json + the curated docs/capabilities.meta.json.
# Same emit/drift discipline.
ARTIFACTS = TRIAD + ("capabilities.json",)


def _emit(out_dir: Path) -> None:
    for module in ("stapel_recordings._codegen", "stapel_recordings._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file(), (
        "missing docs/capabilities.meta.json — the curated layer is "
        "hand-written and committed, not generated"
    )


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed artifacts must match byte-for-byte."""
    _emit(tmp_path)
    for name in ARTIFACTS:
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
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """The mount-prefix fix: schema paths + flow endpoints are /recordings/api/v1/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith(CANONICAL_PREFIX) for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /recordings/api/v1/ prefix"
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


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_axes_inventory():
    """One bool axis: the automatic-summaries behavior gate."""
    doc = _capabilities()
    assert {a["key"] for a in doc["axes"]} == {"SUMMARIZE_ENABLED"}
    axis = doc["axes"][0]
    assert axis["kind"] == "bool"
    assert axis["default"] is True
    assert axis["group"] == "recordings.summarize"


def test_capabilities_summarize_axis_is_behavioral():
    """SUMMARIZE_ENABLED gates pipeline behavior, not endpoints."""
    axis = next(a for a in _capabilities()["axes"] if a["key"] == "SUMMARIZE_ENABLED")
    assert axis["gates"]["operations"] == []
    assert axis["gates"]["co_gates"] == []
    assert axis["gates"]["behavior"]
    assert axis["curated"]["summary"]
    assert axis["curated"]["business_label"]


def test_capabilities_extension_points_cover_the_pipeline_seams():
    """The flagship pipeline seams (MODULE.md) surface as extension points."""
    names = {e["name"] for e in _capabilities()["extension_points"]}
    assert {"PIPELINE", "STAGES", "PIPELINE_RESOLVER", "STORAGE", "NORMALIZER"} <= names


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(
        1 for item in schema["paths"].values() for m in item if m in methods
    )
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    doc = _capabilities()
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]


def test_capabilities_meta_out_of_sync_fails_loudly():
    """A curated-layer gap must be an emission ERROR, never a silent skip."""
    from stapel_tools.capabilities import axis_group_rules, build_capabilities

    from stapel_recordings.conf import DEFAULTS
    from stapel_recordings.urls import GATE_REGISTRY

    schema = json.loads((DOCS / "schema.json").read_text())
    meta = json.loads((DOCS / "capabilities.meta.json").read_text())

    def _build(broken_meta):
        return build_capabilities(
            module="stapel-recordings",
            version="0.0.0",
            defaults=DEFAULTS,
            registry=GATE_REGISTRY,
            schema=schema,
            meta=broken_meta,
            is_axis=lambda k: k == "SUMMARIZE_ENABLED",
            axis_group=axis_group_rules(
                exact={"SUMMARIZE_ENABLED": "recordings.summarize"}
            ),
            canonical_prefix="/recordings",
        )

    # Baseline: intact meta builds.
    assert _build(json.loads(json.dumps(meta)))["axes"]

    # Missing axis entry → loud failure.
    broken = json.loads(json.dumps(meta))
    del broken["axes"]["SUMMARIZE_ENABLED"]
    with pytest.raises(SystemExit, match="SUMMARIZE_ENABLED"):
        _build(broken)

    # Stale (unknown) axis entry → loud failure.
    broken = json.loads(json.dumps(meta))
    broken["axes"]["RECORDINGS_NO_SUCH_AXIS"] = {"summary": "x", "business_label": "x"}
    with pytest.raises(SystemExit, match="RECORDINGS_NO_SUCH_AXIS"):
        _build(broken)

    # Empty business_label → loud failure.
    broken = json.loads(json.dumps(meta))
    broken["axes"]["SUMMARIZE_ENABLED"]["business_label"] = ""
    with pytest.raises(SystemExit, match="business_label"):
        _build(broken)
