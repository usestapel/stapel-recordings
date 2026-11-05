from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recordings", "0008_uploadsession_fingerprint"),
    ]

    operations = [
        migrations.AddField(
            model_name="recording",
            name="asr_model",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="diar_model",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="asr_model_source",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
