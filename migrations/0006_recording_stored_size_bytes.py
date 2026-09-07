"""Add ``Recording.stored_size_bytes`` — the bytes actually kept.

``file_size_bytes`` measures what was RECEIVED. With audio-only ingest the
object it describes is deleted minutes later, once the ``convert`` stage has
extracted its audio track, so a host that sized a bucket or billed storage
from that column was reading the size of something that no longer exists.
This column is the other number: the extracted mono audio object, written by
``convert`` alongside ``normalized_storage_key``.

Nullable and defaulted to NULL: additive, expand-safe, readable by N-1 code
that simply never looks at it. NULL means "not converted yet (or converted
before this column existed)", which is exactly what
``recordings_audio_census`` reports on.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recordings', '0005_drop_asr_tier'),
    ]

    operations = [
        migrations.AddField(
            model_name='recording',
            name='stored_size_bytes',
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
