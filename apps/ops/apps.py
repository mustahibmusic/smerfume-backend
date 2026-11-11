from django.apps import AppConfig


class OpsConfig(AppConfig):
    """Smerfume Operations backend (/ops/): staff UI, presentation only.

    No models and no business rules. Reads use domain models/selectors and
    writes (none yet) must go through domain services.
    """

    name = "apps.ops"
    label = "ops"
    verbose_name = "Smerfume Operations"
