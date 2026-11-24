"""
Django system checks for the CBE applet's settings.
"""
from django.conf import settings
from django.core import checks
from django.utils.module_loading import import_string


def check_learner_resolver_setting(app_configs, **kwargs):
    """
    Report an error when ``OPENEDX_LEARNING_LEARNER_RESOLVER`` is set to a path that cannot be imported.

    The resolver is imported on every request, so a typo in the setting would otherwise show up as an error on every
    valid request instead of at startup.
    """
    del app_configs, kwargs
    path = getattr(settings, "OPENEDX_LEARNING_LEARNER_RESOLVER", None)
    if not path:
        return []
    try:
        import_string(path)
    except ImportError as exc:
        return [
            checks.Error(
                f"OPENEDX_LEARNING_LEARNER_RESOLVER is {path!r}, which cannot be imported: {exc}",
                hint="Set it to the dotted path of a callable, or remove it to use the built-in resolver.",
                id="openedx_learning.E001",
            )
        ]
    return []
