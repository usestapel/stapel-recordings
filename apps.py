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

        # The erasure protocol (stapel-gdpr 0.5.0+), implemented once in
        # stapel-core: gdpr.erasure.requested -> erase -> gdpr.section.erased
        # with a deterministic receipt inside the erase's transaction, the
        # gdpr.owner.probe answer from the same module, and the deprecated
        # user.deleted. What stays ours is erase_subject (erasure.py).
        #
        # Registering by name is also what stands core's provider bridge
        # down for this section exactly: until 0.27.0 this module carried
        # its own copy of the protocol, and the bridge could only tell they
        # were the same APP, not the same section (gdpr.W012).
        from stapel_core.gdpr import register_gdpr_owner

        from .erasure import OWNER, SUBJECT_TYPES, erase_subject

        register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)
