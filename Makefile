# stapel-recordings — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json + errors.json)
# per-module from a single-module {recordings + core} Django instance mounted at
# the canonical /recordings/api/ prefix (see _codegen.py / _codegen_settings.py /
# codegen_urls.py).
#
# Unlike auth/profiles, stapel-recordings is NOT mounted in
# stapel-example-monolith, so there is no monolith aggregate slice to diff this
# artifact against for byte-identity — validation is standalone (determinism +
# closure + canonical prefix + security presence; see tests/test_contract.py).
#
# PYTHON must have the module + its deps importable (the workspace venv, or a CI
# venv). The authoritative CI gate is tests/test_contract.py (run under pytest);
# these targets are the dev-loop convenience.
PYTHON ?= python3

.PHONY: contract contract-check

# Emit the contract triad + capabilities.json + llms.txt (the fifth contract
# artifact, stapel_tools.llms_txt) into docs/.
#
# The llms.txt budget is raised from the generator's default 4000 to 5000,
# same exception stapel-auth (8000) and stapel-workspaces (4500) already
# take. This is the fleet's most file-rich module (27 usage-surface entries
# across services, storage, pipeline, stages, sources, resources), and the
# QA capability (vector/qa.py) pushed it past the old 4500 ceiling. Raise
# the ceiling, do NOT shorten `intent` lines in docs/capabilities.meta.json
# to fit — a trimmed context file is indistinguishable from a complete one
# at the point of use, which is the failure mode the budget gate exists to
# prevent. contract-check below enforces the same 5000 ceiling.
contract:
	$(PYTHON) -m stapel_recordings._codegen --out docs
	$(PYTHON) -m stapel_recordings._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 5000

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.json
# (mirrors the monolith's `make codegen-check` and the frontend's `gen:*:check`).
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_recordings._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_recordings._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget 5000 || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} up to date"; fi; \
	exit $$rc


.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
