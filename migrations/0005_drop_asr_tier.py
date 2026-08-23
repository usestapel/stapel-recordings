"""Drop ``Recording.asr_tier`` — a column nothing ever read.

The field was stored on every recording (``fast``/``accurate``, defaulted at
INSERT) and consulted by nothing: the transcribe stage builds its payload
from the provider settings, never from this column. A write-only column is
not a seam a host can use — one host had already deleted its own surface for
it and could not drop another app's column — so it goes, model attribute and
schema together.

# stapel: contract-phase

Marker justification, since a pure drop cannot take the cutover marker (that
one is machine-checked for a data-carrying RunPython, and there is no data to
carry — the values are being discarded, deliberately): the column was never
READ by any code path in this module or its hosts' payloads, so nothing
depends on it at N-1 beyond the INSERT default itself, and this fleet deploys
stop-the-world. There is no reverse data path: rolling back re-adds the
column with its old default, not the old values.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('recordings', '0004_recording_workflow_state'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='recording',
            name='asr_tier',
        ),
    ]
