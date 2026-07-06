"""Models for stapel-recordings.

Owns the recording lifecycle: capture/upload -> storage -> transcribe ->
summarize. The unified transcript (Recording + Speaker + Segment), the
upload sessions (presigned single-PUT and multipart) and the Job ledger.

Speech-to-text and summarization are NOT implemented here — they are
delegated to stapel-agent through the ``llm.transcribe`` / ``llm.summarize``
comm Functions (called by name, no import). Object storage is delegated to
the STORAGE seam (see ``stapel_recordings.storage``).

House rules (docs/library-standard.md §3.8):
- cross-service references are UUID fields, not FKs (``workspace_id``);
- the user is only ``settings.AUTH_USER_MODEL``;
- index names must be <= 30 characters (models.E034).
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from stapel_core.access import access


# =====================================================================
# Enums
# =====================================================================


class SourceType(models.TextChoices):
    MEET = "meet", "Meeting"
    DICTAPHONE = "dictaphone", "Dictaphone"
    UPLOAD = "upload", "Upload"
    OTHER = "other", "Other"


class ASRTier(models.TextChoices):
    FAST = "fast", "Fast"
    ACCURATE = "accurate", "Accurate"


class LanguageMode(models.TextChoices):
    AUTO = "auto", "Auto"
    MANUAL = "manual", "Manual"
    MULTILINGUAL = "multilingual", "Multilingual"


class RecordingStatus(models.TextChoices):
    CREATED = "created", "Created"
    UPLOADING = "uploading", "Uploading"
    QUEUED = "queued", "Queued"
    ANALYZING = "analyzing", "Analyzing"
    NORMALIZING = "normalizing", "Normalizing"
    TRANSCRIBING = "transcribing", "Transcribing"
    DIARIZING = "diarizing", "Diarizing"
    MERGING = "merging", "Merging"
    COMPLETED = "completed", "Completed"
    ERROR = "error", "Error"
    DELETED = "deleted", "Deleted"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class JobType(models.TextChoices):
    FULL_PIPELINE = "full_pipeline", "Full Pipeline"
    NORMALIZE_AUDIO = "normalize_audio", "Normalize Audio"
    TRANSCRIBE = "transcribe", "Transcribe"
    DIARIZE = "diarize", "Diarize"
    MERGE = "merge", "Merge"
    SUMMARIZE = "summarize", "Summarize"


# =====================================================================
# Recording + children
# =====================================================================


class Recording(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace_id = models.UUIDField(db_index=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recordings",
    )
    title = models.CharField(max_length=500)
    source_type = models.CharField(
        max_length=32, choices=SourceType.choices, default=SourceType.UPLOAD
    )
    asr_tier = models.CharField(
        max_length=16, choices=ASRTier.choices, default=ASRTier.FAST
    )
    language_mode = models.CharField(
        max_length=16, choices=LanguageMode.choices, default=LanguageMode.AUTO
    )
    language = models.CharField(max_length=10, null=True, blank=True)
    diarization_enabled = models.BooleanField(default=True)
    status = models.CharField(
        max_length=32, choices=RecordingStatus.choices, default=RecordingStatus.CREATED
    )

    duration_seconds = models.FloatField(null=True, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    file_storage_key = models.CharField(max_length=512, null=True, blank=True)
    normalized_storage_key = models.CharField(max_length=512, null=True, blank=True)
    transcript_storage_key = models.CharField(max_length=512, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    segments_count = models.IntegerField(default=0)
    speakers_count = models.IntegerField(default=0)
    word_count = models.IntegerField(default=0)
    confidence_avg = models.FloatField(null=True, blank=True)

    provider_used = models.CharField(max_length=64, null=True, blank=True)
    provider_override = models.CharField(max_length=64, null=True, blank=True)
    fallback_used = models.BooleanField(default=False)
    processing_latency_ms = models.IntegerField(null=True, blank=True)
    retry_count = models.IntegerField(default=0)

    summary = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "recordings_recording"
        indexes = [
            models.Index(fields=["workspace_id", "status"], name="rec_ws_status_idx"),
            models.Index(fields=["owner"], name="rec_owner_idx"),
            models.Index(fields=["status", "updated_at"], name="rec_status_upd_idx"),
            models.Index(fields=["deleted_at"], name="rec_deleted_idx"),
        ]

    def __str__(self):
        return f"{self.title} ({self.status})"


class Speaker(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, related_name="speakers"
    )
    label = models.CharField(max_length=64, help_text="Provider label, e.g. speaker_0")
    display_name = models.CharField(max_length=255, null=True, blank=True)
    total_duration_seconds = models.FloatField(default=0)
    segment_count = models.IntegerField(default=0)
    color = models.CharField(max_length=7, default="#4A90D9")

    SPEAKER_PALETTE = [
        "#4A90D9", "#E84393", "#27AE60", "#F39C12",
        "#8E44AD", "#16A085", "#E74C3C", "#2980B9",
        "#D35400", "#1ABC9C", "#C0392B", "#7F8C8D",
    ]

    @classmethod
    def color_for_index(cls, idx: int) -> str:
        return cls.SPEAKER_PALETTE[idx % len(cls.SPEAKER_PALETTE)]

    class Meta:
        db_table = "recordings_speaker"
        indexes = [models.Index(fields=["recording"], name="rec_speaker_rec_idx")]


class Segment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, related_name="segments"
    )
    speaker = models.ForeignKey(
        Speaker, on_delete=models.SET_NULL, null=True, related_name="segments"
    )
    sequence_num = models.IntegerField()
    start_time = models.FloatField()
    end_time = models.FloatField()
    text = models.TextField()
    original_text = models.TextField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    is_edited = models.BooleanField(default=False)
    word_count = models.IntegerField(default=0)
    language = models.CharField(max_length=10, null=True, blank=True)
    words_json = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "recordings_segment"
        ordering = ["recording", "sequence_num"]
        indexes = [
            models.Index(fields=["recording", "sequence_num"], name="rec_seg_seq_idx"),
            models.Index(fields=["speaker"], name="rec_seg_speaker_idx"),
        ]


# =====================================================================
# Upload sessions + jobs
# =====================================================================


@access.ops  # TTL-bounded upload-in-progress tracker, never staff-authored
# (admin-suite AS-5): rows only ever come from ``services.create_upload_session``
# / ``start_multipart_upload``, mutated by ``finalize_upload``, and deleted by
# ``abort_multipart_upload_session`` or expiry cleanup — no add/change/delete
# workflow through the admin exists to protect.
class UploadSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, related_name="upload_sessions"
    )
    presigned_url = models.TextField(blank=True)
    storage_key = models.CharField(max_length=512)
    max_size_bytes = models.BigIntegerField()
    expires_at = models.DateTimeField()
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Multipart fields (null for single-PUT sessions). ``multipart_upload_id``
    # is whatever the STORAGE backend returns from create_multipart_upload;
    # it is storage-implementation opaque (S3 UploadId, or a synthetic id).
    is_multipart = models.BooleanField(default=False)
    multipart_upload_id = models.CharField(max_length=512, null=True, blank=True)

    class Meta:
        db_table = "recordings_upload_session"
        indexes = [models.Index(fields=["recording"], name="rec_upload_rec_idx")]


@access.ops  # processing-job ledger (admin-suite AS-5): status/type tracker in
# the doc's own TaskRecord shape. No code path in this repo writes a Job row
# today (the pipeline driver tracks progress on Recording.status/metadata
# instead — see pipeline.py) or exposes a staff-facing create/retry form; the
# model exists as the ledger a host/consumer may populate. Treated as
# machinery nobody is expected to hand-author through the admin.
class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace_id = models.UUIDField(db_index=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recording_jobs",
    )
    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs"
    )
    type = models.CharField(max_length=32, choices=JobType.choices)
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.QUEUED
    )
    progress_percent = models.IntegerField(default=0)
    current_step = models.CharField(max_length=64, null=True, blank=True)
    options = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    provider_used = models.CharField(max_length=64, null=True, blank=True)
    processing_latency_ms = models.IntegerField(null=True, blank=True)
    retry_count = models.IntegerField(default=0)

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "recordings_job"
        indexes = [
            models.Index(fields=["workspace_id", "status"], name="rec_job_ws_status_idx"),
            models.Index(fields=["recording"], name="rec_job_rec_idx"),
        ]
