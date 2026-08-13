"""Split server-only workflow state out of the client-writable metadata.

Adding the column is half the fix; the rows already in the database carry
their pipeline cursor, error markers and stage ``ctx`` inside ``metadata``,
where a client PATCH can still reach them and where the driver no longer
looks. So the data moves with the schema, in both directions: the reverse
migration folds it back, because a rollback to code that reads ``metadata``
must find its cursor there or every in-flight recording restarts from
stage 0.
"""
from django.db import migrations, models

#: Keys that were server state living in the client's field.
MOVED_KEYS = ("pipeline", "last_error", "recovered_error")


def move_state_out_of_metadata(apps, schema_editor):
    Recording = apps.get_model("recordings", "Recording")
    for recording in Recording.objects.exclude(metadata={}).iterator(chunk_size=500):
        metadata = dict(recording.metadata or {})
        state = dict(recording.workflow_state or {})
        moved = False
        for key in MOVED_KEYS:
            if key in metadata:
                state[key] = metadata.pop(key)
                moved = True
        if moved:
            recording.metadata = metadata
            recording.workflow_state = state
            recording.save(update_fields=["metadata", "workflow_state"])


def fold_state_back_into_metadata(apps, schema_editor):
    Recording = apps.get_model("recordings", "Recording")
    for recording in Recording.objects.exclude(workflow_state={}).iterator(chunk_size=500):
        state = dict(recording.workflow_state or {})
        metadata = dict(recording.metadata or {})
        for key in MOVED_KEYS:
            if key in state:
                metadata[key] = state[key]
        recording.metadata = metadata
        recording.save(update_fields=["metadata"])


class Migration(migrations.Migration):

    dependencies = [
        ('recordings', '0003_recordingshare'),
    ]

    operations = [
        migrations.AddField(
            model_name='recording',
            name='workflow_state',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(move_state_out_of_metadata, fold_state_back_into_metadata),
    ]
