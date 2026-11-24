"""
Tests for the deployer-replaceable learner resolver.
"""
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.http import Http404
from django.test import override_settings
from opaque_keys.edx.locator import CourseLocator
from rest_framework.exceptions import PermissionDenied

from openedx_learning.applets.cbe.rest_api.learner_resolver import default_learner_resolver, resolve_learner

pytestmark = pytest.mark.django_db

COURSE_KEY = CourseLocator("Org", "Course", "Run")
SETTING_PATH = "tests.openedx_learning.applets.cbe.test_learner_resolver"


def _user(username):
    return get_user_model().objects.create_user(username=username)


def resolver_returning_fixed_user(request, course_key, learner_id):
    """Test resolver: records its arguments on the request and returns a user named "resolved"."""
    request.calls.append((course_key, learner_id))
    return get_user_model().objects.get(username="resolved")


def resolver_denying(request, course_key, learner_id):
    """Test resolver: always refuses."""
    del request, course_key, learner_id
    raise PermissionDenied()


@pytest.mark.parametrize("own_id", [False, True], ids=["no learner_id", "own id"])
def test_default_returns_requester_for_no_id_or_own_id(own_id):
    user = _user("requester")
    request = SimpleNamespace(user=user)
    assert default_learner_resolver(request, COURSE_KEY, user.pk if own_id else None) == user


def test_default_404_is_bare_for_other_and_unknown_ids(django_assert_num_queries):
    user = _user("requester")
    other = _user("other")
    request = SimpleNamespace(user=user)
    unknown_id = other.pk + 1000
    with django_assert_num_queries(0):
        with pytest.raises(Http404) as other_exc:
            default_learner_resolver(request, COURSE_KEY, other.pk)
        with pytest.raises(Http404) as unknown_exc:
            default_learner_resolver(request, COURSE_KEY, unknown_id)
    assert other_exc.value.args == ()
    assert unknown_exc.value.args == ()


def test_resolve_learner_uses_configured_resolver():
    _user("resolved")
    request = SimpleNamespace(user=_user("requester"), calls=[])
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{SETTING_PATH}.resolver_returning_fixed_user"):
        assert resolve_learner(request, COURSE_KEY, 5).username == "resolved"
    assert request.calls == [(COURSE_KEY, 5)]


def test_resolve_learner_rereads_setting_per_call():
    _user("resolved")
    request = SimpleNamespace(user=_user("requester"), calls=[])
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{SETTING_PATH}.resolver_denying"):
        with pytest.raises(PermissionDenied):
            resolve_learner(request, COURSE_KEY, None)
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{SETTING_PATH}.resolver_returning_fixed_user"):
        assert resolve_learner(request, COURSE_KEY, None).username == "resolved"


def _resolver_errors():
    """Return the ids of the system check errors Django reports for the current settings."""
    return [message.id for message in run_checks() if message.id.startswith("openedx_learning.")]


@pytest.mark.parametrize(
    "path",
    [f"{SETTING_PATH}.no_such_function", "tests.no_such_module.resolver", "not_a_dotted_path"],
    ids=["missing attribute", "missing module", "no dot"],
)
def test_system_check_reports_a_resolver_path_that_cannot_be_imported(path):
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=path):
        assert _resolver_errors() == ["openedx_learning.E001"]


@pytest.mark.parametrize("path", [None, "", f"{SETTING_PATH}.resolver_denying"], ids=["unset", "empty", "valid"])
def test_system_check_accepts_an_absent_or_importable_resolver_path(path):
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=path):
        assert _resolver_errors() == []
