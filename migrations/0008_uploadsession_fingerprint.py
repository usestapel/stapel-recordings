"""Verified multipart uploads: fingerprint, frozen part size and count.

Three nullable columns on ``recordings_upload_session`` (one indexed) —
expand-only: N-1 code never reads them and every existing row stays a
legacy session with all three null.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recordings', '0007_recording_needs_payment_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='uploadsession',
            name='fingerprint',
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='uploadsession',
            name='part_size_bytes',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='uploadsession',
            name='total_parts',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
