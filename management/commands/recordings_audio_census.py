"""Read-only census of stored uploads that are still containers.

This module stores audio. Recordings ingested before ``AUDIO_ONLY_INGEST``
(0.22.0), or while it was off, may still have the container someone uploaded
sitting in the bucket under ``file_storage_key`` — a video file this service
never wanted and has no way to hand back. This command counts them, weighs
them, and says what the same recordings would occupy as mono audio at the
configured profile.

It **reads and reports; it changes nothing** — no delete, no re-encode, no
field written. Deciding to reclaim that space is the owner's call, and a
backfill that acts on this census is a separate command in a separate
release.

    python manage.py recordings_audio_census
    python manage.py recordings_audio_census --measure     # HEAD each object
    python manage.py recordings_audio_census --json

Sizes come from ``Recording.file_size_bytes`` (what finalize measured).
``--measure`` asks the object store instead, one HEAD per recording, which
is slower but also answers "is the object still there at all" — a row whose
key points at nothing is counted separately and is not space anyone is
paying for.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

#: Extensions this module accepts that carry a video stream. Not a guess at
#: the bytes: it is the suffix the upload key was built from
#: (``services.validated_upload_ext``), so it is what the CLIENT called the
#: file. A ``.webm`` or ``.mp4`` may in fact be audio-only — hence
#: "container", not "video", in the counts.
VIDEO_CONTAINER_EXTS = frozenset({
    "mp4", "mov", "mkv", "webm", "3gp", "avi", "m4v", "mpg", "mpeg", "ts", "wmv", "flv",
})

BATCH = 500


def _ext(key: str) -> str:
    _, dot, ext = (key or "").rpartition(".")
    return ext.lower() if dot else ""


class Command(BaseCommand):
    help = "Report stored upload containers and what they would cost as mono audio (read-only)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--measure", action="store_true",
            help="HEAD every object instead of trusting file_size_bytes (slower, exact)",
        )
        parser.add_argument("--json", action="store_true", help="Machine-readable output")
        parser.add_argument(
            "--workspace", default=None, help="Restrict the census to one workspace id",
        )

    def handle(self, *args, **options):
        from ...models import Recording
        from ...normalize import audio_profile
        from ...storage import get_storage

        profile = audio_profile()
        per_second = profile.bytes_per_hour / 3600.0

        qs = Recording.objects.exclude(file_storage_key__isnull=True).exclude(
            file_storage_key=""
        )
        if options.get("workspace"):
            qs = qs.filter(workspace_id=options["workspace"])
        qs = qs.order_by("created_at").only(
            "id", "file_storage_key", "file_size_bytes", "duration_seconds",
            "stored_size_bytes", "normalized_storage_key",
        )

        storage = get_storage() if options.get("measure") else None
        report = {
            "profile": {
                "codec": profile.codec,
                "channels": profile.channels,
                "sample_rate": profile.sample_rate,
                "bytes_per_hour": profile.bytes_per_hour,
            },
            "containers": 0,
            "container_bytes": 0,
            "video_containers": 0,
            "video_container_bytes": 0,
            "missing_objects": 0,
            "already_extracted": 0,
            "duration_known_seconds": 0.0,
            "duration_unknown": 0,
            "projected_audio_bytes": 0,
        }

        for row in qs.iterator(chunk_size=BATCH):
            size = row.file_size_bytes or 0
            if storage is not None:
                exists, measured = storage.head_object(row.file_storage_key)
                if not exists:
                    report["missing_objects"] += 1
                    continue
                size = int(measured or 0)
            report["containers"] += 1
            report["container_bytes"] += size
            if _ext(row.file_storage_key) in VIDEO_CONTAINER_EXTS:
                report["video_containers"] += 1
                report["video_container_bytes"] += size
            if row.normalized_storage_key:
                # Both objects exist: the audio was extracted and the
                # container was never purged. The whole container is
                # reclaimable — nothing has to be re-encoded first.
                report["already_extracted"] += 1
            if row.duration_seconds:
                report["duration_known_seconds"] += float(row.duration_seconds)
                report["projected_audio_bytes"] += int(row.duration_seconds * per_second)
            else:
                report["duration_unknown"] += 1

        report["reclaimable_bytes"] = max(
            0, report["container_bytes"] - report["projected_audio_bytes"]
        )

        if options.get("json"):
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return

        w = self.stdout.write
        gib = 1024 ** 3
        w("recordings_audio_census (read-only — nothing was changed)")
        w(
            f"  stored profile: {profile.codec} {profile.channels}ch "
            f"{profile.sample_rate} Hz — {profile.bytes_per_hour / 1024 / 1024:.1f} MB/hour"
        )
        w(f"  recordings still holding an uploaded container: {report['containers']}")
        w(f"    of them named as a video container:           {report['video_containers']}")
        w(f"    of them already extracted (container is pure waste): {report['already_extracted']}")
        if storage is not None:
            w(f"    rows whose object is already gone:           {report['missing_objects']}")
        w(f"  container bytes:        {report['container_bytes'] / gib:10.2f} GiB")
        w(f"    video containers:     {report['video_container_bytes'] / gib:10.2f} GiB")
        w(
            f"  same audio at profile: {report['projected_audio_bytes'] / gib:10.2f} GiB "
            f"({report['duration_known_seconds'] / 3600:.1f} h of known duration"
            + (f", {report['duration_unknown']} unknown)" if report["duration_unknown"] else ")")
        )
        w(f"  reclaimable:           {report['reclaimable_bytes'] / gib:10.2f} GiB")
        if report["duration_unknown"]:
            w(
                "  note: recordings with no duration are counted in the container "
                "total but not in the projection, so 'reclaimable' is a floor."
            )
