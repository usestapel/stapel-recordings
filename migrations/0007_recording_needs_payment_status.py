"""Add the ``needs_payment`` choice to ``Recording.status``.

A wallet that cannot buy the work left on a recording used to end it in
``error`` with the reason ``insufficient_credits`` — a failure the UI
rendered as a breakage, that a retry could never fix, and that a refund
consumer of ``recording.failed`` had to special-case by reading the reason
string. Nothing broke: the recording is parked, waiting for money.

Choices only — no column change, no data to move. ``AlterField`` on a
``CharField`` whose ``max_length`` is unchanged is a no-op in PostgreSQL
(Django emits no ALTER TABLE for a choices-only edit), so it is expand-safe:
N-1 code reads a row whose status it does not know only after a host has
deployed the code that writes it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recordings', '0006_recording_stored_size_bytes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='recording',
            name='status',
            field=models.CharField(
                choices=[
                    ('created', 'Created'),
                    ('uploading', 'Uploading'),
                    ('queued', 'Queued'),
                    ('analyzing', 'Analyzing'),
                    ('normalizing', 'Normalizing'),
                    ('transcribing', 'Transcribing'),
                    ('diarizing', 'Diarizing'),
                    ('merging', 'Merging'),
                    ('completed', 'Completed'),
                    ('error', 'Error'),
                    ('needs_payment', 'Needs payment'),
                    ('deleted', 'Deleted'),
                ],
                default='created',
                max_length=32,
            ),
        ),
    ]
