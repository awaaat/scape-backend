from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "projects"
    verbose_name = "Projects"

    def ready(self):
        # Connects broadcast_new_project_message (the post_save receiver
        # that pushes new messages to the websocket group) — same
        # ready()-time import pattern as contracts.apps.ContractsConfig,
        # for the same reason: guaranteed to run after every app's models
        # are loaded, so this can safely import this app's own models
        # without any import-order risk.
        from . import signals  # noqa: F401
