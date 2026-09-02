"""Scheme-tag the segment embeddings so two chunkings can coexist.

Expand-only, and deliberately so: the embedded unit is changing from one
STT utterance to a multi-utterance window, and a change to what is IN the
index cannot be a cutover — the new rows have to be built by a background
command while the old ones keep serving. So every row gains the scheme
that produced it, existing rows are stamped ``"segment"`` (which is what
they are), and the uniqueness key widens from (segment, model) to
(segment, model, scheme).

Nothing here changes what search returns: ``VECTOR["SEGMENT_SCHEME"]``
still defaults to ``"segment"``, so a host that migrates and does nothing
else sees byte-identical behaviour. The switch is a separate, reversible
decision.

``text`` and ``span`` exist because a window is not reconstructible from
its anchor segment: the anchor says where it starts, not where it ends,
and the embedded text carries speaker/timestamp prefixes the segment rows
do not have. Both are empty/1 for the segment scheme.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recordings_vector", "0001_initial"),
    ]

    operations = [
        # Drop first: the widened constraint replaces this one, and both
        # cannot name the same (segment, model) pair.
        migrations.RemoveConstraint(
            model_name="segmentembedding",
            name="rec_segemb_seg_model_uniq",
        ),
        migrations.AddField(
            model_name="segmentembedding",
            name="scheme",
            field=models.CharField(default="segment", max_length=16),
        ),
        migrations.AddField(
            model_name="segmentembedding",
            name="text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="segmentembedding",
            name="span",
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name="segmentembedding",
            name="chunk_index",
            field=models.IntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="segmentembedding",
            constraint=models.UniqueConstraint(
                fields=("segment", "model", "scheme", "chunk_index"),
                name="rec_segemb_seg_mdl_sch_uq",
            ),
        ),
    ]
