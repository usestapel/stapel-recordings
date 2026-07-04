"""Reconcile watchdog — auto-heal stuck recordings.

Scans for recordings parked in a transient pipeline state with no recent
progress and re-emits ``recording.stage`` for the stage they were on (read
from ``metadata['pipeline']['stage_index']``). Catches the failure modes
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
            try:
                n = self.reconcile_once()
                m = self.cleanup_abandoned_uploads()
                if n or m:
                    self.stdout.write(f"recordings_reconcile: re-drove {n}, abandoned {m}")
            except Exception:
                logger.exception("recordings_reconcile: pass failed")
            if once:
                break
            time.sleep(interval)

    def reconcile_once(self) -> int:
        from ...conf import recordings_settings
        from ...events import emit_stage
        from ...models import Recording, RecordingStatus

        transient = [
            RecordingStatus.QUEUED, RecordingStatus.ANALYZING, RecordingStatus.NORMALIZING,
            RecordingStatus.TRANSCRIBING, RecordingStatus.DIARIZING, RecordingStatus.MERGING,
        ]
        cutoff = timezone.now() - timedelta(seconds=int(recordings_settings.STUCK_THRESHOLD_SECONDS))
        qs = (
            Recording.objects
            .filter(status__in=transient, updated_at__lt=cutoff, deleted_at__isnull=True)
            .order_by("updated_at")[:BATCH]
        )
        count = 0
        for r in qs:
            stage_index = (r.metadata or {}).get("pipeline", {}).get("stage_index")
            if stage_index is None:
                stage_index = 0  # never started a stage — restart from the top
            emit_stage(r.id, int(stage_index))
            count += 1
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
            r.status = RecordingStatus.ERROR
            r.metadata = {**(r.metadata or {}), "last_error": {
                "stage": "upload", "reason": "upload_abandoned", "at": timezone.now().isoformat(),
            }}
            r.save(update_fields=["status", "metadata", "updated_at"])
            count += 1
        return count
