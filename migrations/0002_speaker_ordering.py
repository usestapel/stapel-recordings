"""Give Speaker a deterministic order.

Not cosmetic. The canonical transcript numbers speakers positionally
(``spk_0``, ``spk_1``, ...) from this model's queryset, and those ids are what
segments reference, what gets rendered to an LLM, and what the transcript
version key hashes. With no ORDER BY the database may return the rows in any
order, so one unedited recording could canonicalize two different ways between
reads — and every artifact derived from it would read as stale for no reason.

Metadata only: ``AlterModelOptions`` emits no SQL and needs no downtime.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("recordings", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="speaker",
            options={"ordering": ["label", "id"]},
        ),
    ]
