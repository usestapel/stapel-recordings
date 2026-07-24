"""Initial migration for the opt-in vector app.

Runs only for hosts that added ``stapel_recordings.vector`` to
INSTALLED_APPS. First operation is pgvector's ``VectorExtension`` — the
standard ``CREATE EXTENSION IF NOT EXISTS vector``, subclassing Django's
``CreateExtension`` which is **vendor-guarded** (a no-op on any non-postgres
database), so the migration never breaks a stray sqlite/mysql run.

``VectorField`` dimensions and the HNSW parameters are read from
``STAPEL_RECORDINGS["VECTOR"]`` at migrate time — the same source the
models read, so model state and schema cannot drift from each other.
Configure DIM / HNSW before the first migrate; changing DIM later is a
host-side migration + re-embed.
"""
import uuid

import django.db.models.deletion
import pgvector.django
from django.db import migrations, models

from stapel_recordings.conf import vector_config

_CFG = vector_config()
_DIM = int(_CFG["DIM"])
_HNSW = _CFG["HNSW"]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("recordings", "0001_initial"),
    ]

    operations = [
        # CREATE EXTENSION IF NOT EXISTS vector — vendor-guarded (postgres
        # only; silently skipped elsewhere). Needs a role allowed to create
        # extensions, or pre-create the extension in the database.
        pgvector.django.VectorExtension(),
        migrations.CreateModel(
            name="SegmentEmbedding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("vector", pgvector.django.VectorField(dimensions=_DIM)),
                ("model", models.CharField(blank=True, default="", max_length=128)),
                ("content_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("segment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="embeddings", to="recordings.segment")),
            ],
            options={
                "db_table": "recordings_segment_embedding",
            },
        ),
        migrations.CreateModel(
            name="RecordingEmbedding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("chunk_index", models.IntegerField(default=0)),
                ("text_hash", models.CharField(max_length=64)),
                ("vector", pgvector.django.VectorField(dimensions=_DIM)),
                ("model", models.CharField(blank=True, default="", max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("recording", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="embeddings", to="recordings.recording")),
            ],
            options={
                "db_table": "recordings_recording_embedding",
            },
        ),
        migrations.AddConstraint(
            model_name="segmentembedding",
            constraint=models.UniqueConstraint(fields=("segment", "model"), name="rec_segemb_seg_model_uniq"),
        ),
        migrations.AddIndex(
            model_name="segmentembedding",
            index=pgvector.django.HnswIndex(
                fields=["vector"],
                name="rec_segemb_hnsw_idx",
                m=int(_HNSW["M"]),
                ef_construction=int(_HNSW["EF_CONSTRUCTION"]),
                opclasses=["vector_cosine_ops"],
            ),
        ),
        migrations.AddConstraint(
            model_name="recordingembedding",
            constraint=models.UniqueConstraint(fields=("recording", "model", "chunk_index"), name="rec_recemb_chunk_uniq"),
        ),
        migrations.AddIndex(
            model_name="recordingembedding",
            index=models.Index(fields=["recording"], name="rec_recemb_rec_idx"),
        ),
    ]
