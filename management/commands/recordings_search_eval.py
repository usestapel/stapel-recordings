"""Score the search layer against a labeled question set (recall@k, MRR).

    # the current configuration
    python manage.py recordings_search_eval --dataset eval/questions.json

    # A/B one setting without editing settings.py — the override is applied
    # around the run only
    python manage.py recordings_search_eval --dataset eval/questions.json \
        --set FTS_SEARCH_TYPE=any --set SEGMENT_SCHEME=window

    # compare arms
    python manage.py recordings_search_eval --dataset eval/questions.json \
        --mode text --mode vector --mode hybrid

Run it against a COPY of real data (a restored dump on a workstation), not
against the production database: it issues one embedding call per question
per mode, and the numbers are only worth having if they come from the
corpus users actually search.

``--json <path>`` writes the machine-readable form, so a before/after pair
is a diff rather than two screenshots.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings


class Command(BaseCommand):
    help = "Evaluate search quality (recall@k, MRR) on a labeled question set"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset", required=True,
            help="Path to the labeled question set (JSON; see vector/evaluation.py)",
        )
        parser.add_argument(
            "--mode", action="append", default=None,
            help="Search mode to score (repeatable). Default: hybrid",
        )
        parser.add_argument(
            "--limit", type=int, default=10,
            help="How many hits to retrieve per question (default 10)",
        )
        parser.add_argument(
            "--set", action="append", default=None, dest="overrides",
            help=(
                "VECTOR setting override for this run only, KEY=VALUE "
                "(repeatable). Nested blocks take a dotted key, e.g. "
                "--set SEGMENT_WINDOW.TARGET_CHARS=800"
            ),
        )
        parser.add_argument(
            "--json", default=None, dest="json_path",
            help="Also write the results as JSON to this path",
        )

    def handle(self, *args, **options):
        from ...conf import vector_config
        from ...vector.evaluation import (
            format_report,
            load_questions,
            run_evaluation,
        )

        try:
            questions = load_questions(options["dataset"])
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc

        modes = options["mode"] or ["hybrid"]
        limit = max(1, int(options["limit"]))
        overrides = self._overrides(options.get("overrides") or [])

        vector_block = {**vector_config(), **overrides}
        report_label = ", ".join(
            f"{key}={value!r}" for key, value in sorted(overrides.items())
        )
        payload: dict = {
            "dataset": options["dataset"],
            "limit": limit,
            "overrides": {k: str(v) for k, v in overrides.items()},
            "modes": {},
        }

        with override_settings(STAPEL_RECORDINGS={"VECTOR": vector_block}):
            for mode in modes:
                results, summary = run_evaluation(
                    questions, mode=mode, limit=limit
                )
                label = f"mode={mode} limit={limit}"
                if report_label:
                    label += f" [{report_label}]"
                self.stdout.write(format_report(results, summary, label=label))
                self.stdout.write("")
                payload["modes"][mode] = {
                    "summary": summary,
                    "questions": [
                        {
                            "id": r.question.id,
                            "lang": r.question.lang,
                            "query": r.question.query,
                            "first_relevant_rank": r.first_relevant_rank,
                            "n_hits": r.n_hits,
                        }
                        for r in results
                    ],
                }

        if options["json_path"]:
            with open(options["json_path"], "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            self.stdout.write(f"wrote {options['json_path']}")

    def _overrides(self, raw: list) -> dict:
        """``KEY=VALUE`` / ``BLOCK.KEY=VALUE`` pairs into a VECTOR overlay.

        Values are parsed as JSON when they look like it (numbers, true,
        false), else kept as strings — so ``--set RRF_K=20`` is an int and
        ``--set FTS_SEARCH_TYPE=any`` is a string, without the caller
        having to quote anything."""
        from ...conf import vector_config

        cfg = vector_config()
        out: dict = {}
        for item in raw:
            if "=" not in item:
                raise CommandError(f"--set expects KEY=VALUE, got {item!r}")
            key, _, value = item.partition("=")
            key, value = key.strip(), value.strip()
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
            if "." in key:
                block, _, leaf = key.partition(".")
                if block not in cfg or not isinstance(cfg[block], dict):
                    raise CommandError(f"--set: {block!r} is not a VECTOR block")
                merged = {**cfg[block], **out.get(block, {}), leaf: parsed}
                out[block] = merged
            else:
                if key not in cfg:
                    raise CommandError(f"--set: unknown VECTOR key {key!r}")
                out[key] = parsed
        return out
