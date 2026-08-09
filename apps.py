from django.apps import AppConfig


class RecordingsConfig(AppConfig):
    name = "stapel_recordings"
    label = "recordings"
    verbose_name = "Recording lifecycle and transcription"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: system checks, error-key registration,
        # action subscriptions (pipeline driver + GDPR consumer). Keep each
        # in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # Task bridge: the queue and state live with the recording, the work
        # goes to the agent over the bus. In a monolith the agent's own
        # handler is already in this process, so the bridge does NOT
        # register (see task_delegates).
        from .task_delegates import register_default_task_delegates
        register_default_task_delegates()

        # GDPR provider registration (monolith mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import RecordingsGDPRProvider

        if RecordingsGDPRProvider().section not in gdpr_registry.sections:
            gdpr_registry.register(RecordingsGDPRProvider())
