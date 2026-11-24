"""
App Config for our umbrella openedx_learning app.
"""
from django.apps import AppConfig
from django.core import checks

from .applets.cbe.checks import check_learner_resolver_setting


class LearningConfig(AppConfig):
    """
    Initialization for all applets must happen in here.
    """

    name = "openedx_learning"
    verbose_name = "Open edX Core > Learning"
    default_auto_field = "django.db.models.BigAutoField"
    label = "openedx_learning"

    def ready(self):
        """
        Register the system checks of the applets.
        """
        checks.register(check_learner_resolver_setting)
