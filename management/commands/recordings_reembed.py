"""Re-embed recordings under the current embedding model (reindex path).

Embedding vectors of two different models are two incomparable spaces of
the same width, so the vector search arm only matches rows stamped with
the model that embedded the query (``VECTOR["SEARCH_MODEL_FILTER"]``).
Changing the embedder therefore does not corrupt ranking — it makes the
old rows invisible. This command is the way back: it re-runs the embed
stage's body over recordings in scope, writing rows under whatever model
``llm.embed`` reports now, and can prune the rows left behind by the
previous model.

    # what is stored, per model (no provider calls)
    python manage.py recordings_reembed --dry-run

    # re-embed one workspace, then drop everything not on the new model
    python manage.py recordings_reembed --workspace <uuid>
    python manage.py recordings_reembed --workspace <uuid> \
        --prune-other-models --keep-model Qwen/Qwen3-Embedding-8B

Re-embedding is idempotent (rows are keyed by (segment, model) and by the
sha256 of the embedded text), so a re-run only pays for what is missing;
an interrupted pass can simply be repeated. Pruning is a plain delete —
it is a separate opt-in flag, and never runs implicitly after a re-embed.
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-embed recordings under the current embedding model"

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default=None, help="Limit to one workspace id")
        parser.add_argument(
            "--recording", action="append", default=None,
            help="Limit to a recording id (repeatable)",
        )
        parser.add_argument("--limit", type=int, default=None, help="Process at most N recordings")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report scope and stored rows per model; call no provider",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-embed texts already stored (needed after an embedder swap)",
        )
        parser.add_argument(
            "--prune-other-models", action="store_true",
            help="Delete embedding rows whose model differs from --keep-model",
        )
        parser.add_argument(
            "--keep-model", default=None,
            help="Model to keep when pruning (default: VECTOR['MODEL'] when pinned)",
        )

    def handle(self, *args, **options):
        from ...conf import vector_config
        from ...vector import vector_app_installed

        if not vector_app_installed():
            raise CommandError(
                "the opt-in vector app is not installed: pip install "
                "stapel-recordings[vector] and add 'stapel_recordings.vector' "
                "to INSTALLED_APPS (then migrate)."
            )

        cfg = vector_config()
        recordings = self._scope(options)
        self.stdout.write(f"recordings_reembed: {recordings.count()} recording(s) in scope")
        self._report_models(options)

        if options["dry_run"]:
            self.stdout.write("recordings_reembed: dry run — nothing embedded, nothing pruned")
            return

        from ...vector.embedding import embed_recording

        force = bool(options["force"])
        segments = chunks = 0
        for recording in recordings.iterator():
            counters = embed_recording(recording, force=force)
            segments += counters["segments_embedded"]
            chunks += counters["summary_chunks_embedded"]
        self.stdout.write(
            f"recordings_reembed: embedded {segments} segment(s), {chunks} summary chunk(s)"
        )
        if not force and not segments and not chunks:
            # The trap this command exists for: with VECTOR["MODEL"]
            # unpinned the hash check cannot see that stored rows belong
            # to the PREVIOUS model, so a plain pass is a silent no-op.
            self.stdout.write(
                "recordings_reembed: nothing embedded — if you swapped the "
                "embedder, stored rows are on the old model and the hash "
                "check skips them; re-run with --force (or pin "
                "STAPEL_RECORDINGS['VECTOR']['MODEL'])."
            )

        if options["prune_other_models"]:
            keep = options["keep_model"] or str(cfg.get("MODEL") or "")
            if not keep:
                raise CommandError(
                    "--prune-other-models needs the model to keep: pass "
                    "--keep-model <name> (VECTOR['MODEL'] is not pinned, so "
                    "the stored model name comes from the provider response — "
                    "run --dry-run to see which names are stored)."
                )
            self._prune(options, keep)

    # ── helpers ────────────────────────────────────────────────────────

    def _scope(self, options):
        from ...models import Recording

        qs = Recording.objects.filter(deleted_at__isnull=True).order_by("created_at")
        if options["workspace"]:
            qs = qs.filter(workspace_id=options["workspace"])
        if options["recording"]:
            qs = qs.filter(id__in=options["recording"])
        if options["limit"]:
            qs = qs[: int(options["limit"])]
        return qs

    def _row_filters(self, options) -> tuple[dict, dict]:
        """Scope filters for (SegmentEmbedding, RecordingEmbedding)."""
        seg: dict = {}
        rec: dict = {}
        if options["workspace"]:
            seg["segment__recording__workspace_id"] = options["workspace"]
            rec["recording__workspace_id"] = options["workspace"]
        if options["recording"]:
            seg["segment__recording_id__in"] = options["recording"]
            rec["recording_id__in"] = options["recording"]
        return seg, rec

    def _report_models(self, options) -> None:
        from django.db.models import Count

        from ...vector.models import RecordingEmbedding, SegmentEmbedding

        seg_filter, rec_filter = self._row_filters(options)
        for label, model_cls, filters in (
            ("segment", SegmentEmbedding, seg_filter),
            ("summary", RecordingEmbedding, rec_filter),
        ):
            rows = (
                model_cls.objects.filter(**filters)
                .values("model")
                .annotate(n=Count("id"))
                .order_by("-n")
            )
            for row in rows:
                self.stdout.write(
                    f"  {label}: {row['n']} row(s) on model {row['model'] or '(unset)'!r}"
                )

    def _prune(self, options, keep: str) -> None:
        from ...vector.models import RecordingEmbedding, SegmentEmbedding

        seg_filter, rec_filter = self._row_filters(options)
        deleted_segments, _ = (
            SegmentEmbedding.objects.filter(**seg_filter).exclude(model=keep).delete()
        )
        deleted_summaries, _ = (
            RecordingEmbedding.objects.filter(**rec_filter).exclude(model=keep).delete()
        )
        self.stdout.write(
            f"recordings_reembed: pruned {deleted_segments} segment row(s) and "
            f"{deleted_summaries} summary row(s) not on model {keep!r}"
        )
