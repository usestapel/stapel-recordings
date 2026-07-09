"""stapel-recordings capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_recordings._codegen import _configure

    _configure()
    from stapel_recordings.conf import DEFAULTS
    from stapel_recordings.urls import GATE_REGISTRY

    # SUMMARIZE_ENABLED is the one CTO-facing axis (bool behavior gate: does
    # the pipeline produce an automatic summary). PIPELINE/STAGES/
    # PIPELINE_RESOLVER/STORAGE/NORMALIZER are extension seams (curated in
    # docs/capabilities.meta.json); TTLs, size limits, retries, thresholds
    # and SUMMARIZE_MODEL are tuning — neither axes nor extension points.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/recordings",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k == "SUMMARIZE_ENABLED",
        axis_group=axis_group_rules(exact={"SUMMARIZE_ENABLED": "recordings.summarize"}),
        prog="stapel-recordings-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
