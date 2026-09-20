"""Reconcile watchdog — auto-heal stuck recordings.

Scans for recordings parked in a transient pipeline state with no recent
progress and re-emits ``recording.stage`` for the stage they were on (read
from ``workflow_state['pipeline']['stage_index']``). Catches the failure modes
per-stage retry can't: a broker gap between emit and consume, a worker
crash mid-stage, or a stage parked for retry. Idempotency is the driver's
job (status guards + stale-index drop), so a duplicate re-drive is cheap.

Also marks abandoned uploads (stuck ``uploading`` with no stored object)
as ``error``.

    python manage.py recordings_reconcile --once
    python manage.py recordings_reconcile --poll-interval 300
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

BATCH = 200


class Command(BaseCommand):
    help = "Re-drive recordings stuck in transient pipeline states"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Single pass then exit")
        parser.add_argument("--poll-interval", type=float, default=None, help="Loop interval seconds")

    def handle(self, *args, **options):
        from ...conf import recordings_settings

        interval = options.get("poll_interval") or (int(recordings_settings.STUCK_THRESHOLD_SECONDS) / 2)
        once = bool(options.get("once"))
        self.stdout.write(f"recordings_reconcile: started (once={once}, interval={interval}s)")
        while True:
            # A management-command loop has no request boundary, so nothing
            # ever retires a connection the server closed underneath it
            # (restart, failover, pgbouncer/idle kill, a DB recreated on a
            # stand). Django reuses the dead handle and EVERY later pass
            # raises "server closed the connection unexpectedly" — the
            # watchdog is then permanently down while looking alive, paging
            # on each tick (a client stand, 2026-07-26). This is the same
            # line Celery/Channels put at the top of their loops.
            close_old_connections()
            try:
                n = self.reconcile_once()
                m = self.cleanup_abandoned_uploads()
                if n or m:
                    self.stdout.write(f"recordings_reconcile: re-drove {n}, abandoned {m}")
            except Exception:
                logger.exception("recordings_reconcile: pass failed")
                # Drop whatever is left of a possibly-broken connection so
                # the next pass starts clean instead of inheriting it.
                close_old_connections()
            if once:
                break
            time.sleep(interval)

    def reconcile_once(self) -> int:
        from django.db import transaction

        from ...conf import recordings_settings
        from ...events import emit_stage
        from ...models import Recording, RecordingStatus

        # Anything that is neither pre-pipeline (created/uploading) nor
        # terminal is treated as in-pipeline. Deliberately inverted (not a
        # hardcoded list of built-in stage statuses) so recordings parked in
        # a *custom* stage status are re-driven too instead of hanging
        # forever.
        #
        # needs_payment is on this list: it is parked on an empty wallet, not
        # stuck. Re-driving it would make the watchdog spend money the account
        # does not have, every pass, forever — and the driver would only drop
        # the event anyway (needs_payment is terminal for deliveries). It
        # leaves this state through pipeline.resume_after_payment alone.
        non_transient = [
            RecordingStatus.CREATED, RecordingStatus.UPLOADING,
            RecordingStatus.COMPLETED, RecordingStatus.ERROR,
            RecordingStatus.NEEDS_PAYMENT, RecordingStatus.DELETED,
        ]
        cutoff = timezone.now() - timedelta(seconds=int(recordings_settings.STUCK_THRESHOLD_SECONDS))
        qs = (
            Recording.objects
            .exclude(status__in=non_transient)
            .filter(updated_at__lt=cutoff, deleted_at__isnull=True)
            .order_by("updated_at")[:BATCH]
        )
        from ...pipeline import note_reconcile_redrive

        count = 0
        exhausted = 0
        # One atomic batch: the outbox rows commit together (and emit() is
        # correctly inside a transaction — no outside-atomic warning noise).
        with transaction.atomic():
            for r in qs:
                # COUNTED, and finite. A re-drive costs nothing it can see:
                # it does not touch retry_count, so before this counter a
                # recording the pipeline could not finish was re-driven
                # every pass forever — the iron-agent stand did exactly
                # that for a fortnight against an empty allowlist
                # (2026-08-20), and a storm of 64 errors in one week on
                # 2026-09-10 looked the same from the outside. The stage
                # ladder and the task ladder both declare a ceiling; a
                # watchdog that declares none is where the ceilings leak.
                if not note_reconcile_redrive(r):
                    exhausted += 1
                    continue
                stage_index = (r.workflow_state or {}).get("pipeline", {}).get("stage_index")
                if stage_index is None:
                    stage_index = 0  # never started a stage — restart from the top
                emit_stage(r.id, int(stage_index))
                count += 1
        if exhausted:
            logger.warning(
                "recordings_reconcile: %d recording(s) were failed instead of "
                "re-driven again — see last_error.reason=reconcile_exhausted",
                exhausted,
            )
        return count

    def cleanup_abandoned_uploads(self) -> int:
        from ...conf import recordings_settings
        from ...models import Recording, RecordingStatus

        cutoff = timezone.now() - timedelta(seconds=int(recordings_settings.ABANDONED_UPLOAD_THRESHOLD_SECONDS))
        qs = (
            Recording.objects
            .filter(status=RecordingStatus.UPLOADING, file_storage_key__isnull=True,
                    deleted_at__isnull=True, updated_at__lt=cutoff)
            .order_by("updated_at")[:BATCH]
        )
        count = 0
        for r in qs:
            # Conditional per-row UPDATE (no plain save over a stale read):
            # if finalize_upload won the race after our snapshot, the filter
            # no longer matches and we don't clobber status/workflow_state of a
            # recording whose pipeline has started.
            count += Recording.objects.filter(
                pk=r.pk,
                status=RecordingStatus.UPLOADING,
                file_storage_key__isnull=True,
            ).update(
                status=RecordingStatus.ERROR,
                workflow_state={**(r.workflow_state or {}), "last_error": {
                    "stage": "upload", "reason": "upload_abandoned", "at": timezone.now().isoformat(),
                }},
                updated_at=timezone.now(),
            )
        return count
