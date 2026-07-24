from django.apps import AppConfig


class RecordingsVectorConfig(AppConfig):
    name = "stapel_recordings.vector"
    label = "recordings_vector"
    verbose_name = "Recording embeddings (vector search)"
    default_auto_field = "django.db.models.BigAutoField"
