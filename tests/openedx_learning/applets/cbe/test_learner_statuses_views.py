"""
Tests for the learner competency statuses endpoint, ``GET cbe/v1/courses/<course_key>/competency-statuses/`` (#738).

Fixtures live in this directory's conftest.py. The resolver that chooses whose statuses are read is covered in
test_learner_resolver.py; here it is exercised through the view.
"""
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import override_settings
from django.urls import reverse
from opaque_keys.edx.locator import CourseLocator
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.test import APIClient

from openedx_catalog.models import CourseRun
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaGroupStatus,
    StudentCompetencyCriterionStatus,
    StudentCompetencyStatus,
)
from openedx_tagging.models import Tag

from .utils import make_course_run, make_leaf_criterion, make_tag

pytestmark = pytest.mark.django_db

RESOLVER_PATH = "tests.openedx_learning.applets.cbe.test_learner_statuses_views"
UNKNOWN_USER_ID = 987654


def statuses_url(course_key: str, **query) -> str:
    """Return the endpoint's path for `course_key`, resolved through the learner URL name."""
    url = reverse("cbe_learner:course-competency-statuses", kwargs={"course_key": course_key})
    if query:
        url += "?" + urlencode(query)
    return url


def resolver_returning_resolved_user(request, course_key, learner_id):
    """Test resolver: returns the user named "resolved", whoever asks."""
    del request, course_key, learner_id
    return get_user_model().objects.get(username="resolved")


def resolver_denying(request, course_key, learner_id):
    """Test resolver: refuses every request."""
    del request, course_key, learner_id
    raise PermissionDenied()


def resolver_raising_http404_with_message(request, course_key, learner_id):
    """Test resolver: refuses with a 404 that carries a message."""
    del request, course_key, learner_id
    raise Http404("secret")


def resolver_raising_not_found_with_message(request, course_key, learner_id):
    """Test resolver: refuses with a DRF NotFound that carries a message."""
    del request, course_key, learner_id
    raise NotFound("secret")


def _locator(course_run: CourseRun) -> CourseLocator:
    """Return the course key of `course_run`, typed as the locator it is."""
    key = course_run.course_key
    assert isinstance(key, CourseLocator)
    return key


@pytest.fixture(name="plain_key")
def _plain_key(course_run: CourseRun) -> str:
    """The key of the `course_run` fixture, as the string a client puts in the URL."""
    return str(course_run.course_key)


@pytest.fixture(name="user_client")
def _user_client(api_client: APIClient, user) -> APIClient:
    """A REST client acting as `user`, an ordinary, non-staff, authenticated caller."""
    api_client.force_authenticate(user=user)
    return api_client


def _store(user, criterion, tag: Tag, now: datetime) -> None:
    """Store a Demonstrated status of `user` for `criterion` and for the competency `tag`."""
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    StudentCompetencyStatus.objects.create(
        user=user, tag=tag, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )


def _earn_in_other_course_run(
    user, organization, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
    now: datetime,
) -> None:
    """Store Demonstrated statuses of `user` for a criterion and a competency that only another course run assesses."""
    other_run = make_course_run(organization, "Python200", "Fall2026")
    other_tag = make_tag(competency_taxonomy, "Assessed elsewhere")
    _store(user, make_leaf_criterion(other_tag, other_run, default_rule_profile, "o1"), other_tag, now)


# =================================================================================================
# Authentication and the order of checks
# =================================================================================================


def test_signed_out_caller_gets_401_and_no_statuses(
    api_client: APIClient, *, user, plain_key: str, course_run: CourseRun, tag: Tag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """A caller who is not signed in is refused with 401 and the body carries no statuses."""
    criterion = make_leaf_criterion(tag, course_run, default_rule_profile, "s1")
    _store(user, criterion, tag, now)

    response = api_client.get(statuses_url(plain_key))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("course_key", ["not-a-key", "lib:Org1:Lib1", "course-v1:Org1+Python100"])
def test_malformed_course_key_gets_400_with_the_course_criteria_message(
    user_client: APIClient, course_key: str,
) -> None:
    """A value that is not a course run key gets 400 and the same message as the course criteria endpoint."""
    response = user_client.get(statuses_url(course_key))

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"course_key": [f"'{course_key}' is not a valid course run key."]}


def test_malformed_course_key_is_checked_before_learner_id(user_client: APIClient) -> None:
    """The course key is validated first, so a bad key with a bad learner_id still gets 400."""
    response = user_client.get(statuses_url("not-a-key", learner_id="abc"))

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{RESOLVER_PATH}.resolver_denying")
@pytest.mark.parametrize(
    ("course_key", "query", "expected_status"),
    [
        ("not-a-key", {}, status.HTTP_400_BAD_REQUEST),
        (None, {"learner_id": "abc"}, status.HTTP_404_NOT_FOUND),
    ],
    ids=["malformed course key", "malformed learner_id"],
)
def test_resolver_is_asked_only_after_the_course_key_and_learner_id_are_valid(
    user_client: APIClient, plain_key: str, course_key: str | None, query: dict, expected_status: int,
) -> None:
    """A resolver that refuses everything is not reached while the key or the learner_id is invalid."""
    response = user_client.get(statuses_url(course_key or plain_key, **query))

    assert response.status_code == expected_status


# =================================================================================================
# learner_id handling with the default resolver
# =================================================================================================


def test_unusable_and_unreachable_learner_ids_get_identical_404s(
    user_client: APIClient, *, user, plain_key: str,
) -> None:
    """
    A value that is not a canonical positive integer, including the requester's own id in another spelling, an
    integer matching no user and another real user's id all get the same bare 404, so the response does not
    reveal whether the learner exists.
    """
    other = get_user_model().objects.create(username="other")
    arabic_indic_own_id = "".join(chr(0x0660 + int(digit)) for digit in str(user.id))
    own_id_spellings = (f" {user.id} ", f"+{user.id}", f"00{user.id}", f"{user.id}.0", arabic_indic_own_id)
    values = (
        "abc", "", "1.5", " 7 ", "1_0", "0", "-1", "9" * 5000, *own_id_spellings, UNKNOWN_USER_ID, other.id,
    )
    responses = [user_client.get(statuses_url(plain_key, learner_id=value)) for value in values]

    assert [response.status_code for response in responses] == [status.HTTP_404_NOT_FOUND] * len(values)
    assert len({response.content for response in responses}) == 1


@pytest.mark.parametrize("pass_learner_id", [False, True], ids=["absent", "own id"])
def test_requester_gets_their_own_statuses(
    user_client: APIClient, *, user, plain_key: str, course_run: CourseRun, tag: Tag,
    default_rule_profile: CompetencyRuleProfile, now: datetime, pass_learner_id: bool,
) -> None:
    """With no learner_id, or with the requester's own id, the requester's statuses are returned."""
    criterion = make_leaf_criterion(tag, course_run, default_rule_profile, "s1")
    _store(user, criterion, tag, now)
    other = get_user_model().objects.create(username="other")
    StudentCompetencyStatus.objects.create(
        user=other, tag=tag, status_id=MasteryStatus.PARTIALLY_ATTEMPTED, created=now, modified=now,
    )

    response = user_client.get(statuses_url(plain_key, **({"learner_id": user.id} if pass_learner_id else {})))

    assert response.status_code == status.HTTP_200_OK
    assert [row["status"] for row in response.json()["competency_statuses"]] == ["Demonstrated"]


# =================================================================================================
# Empty results and payload shape
# =================================================================================================


@pytest.mark.parametrize("unknown_run", [False, True], ids=["course run without criteria", "key without a course run"])
def test_course_run_without_criteria_and_unknown_key_get_two_empty_arrays(
    user_client: APIClient, *, user, organization, competency_taxonomy: CompetencyTaxonomy, course_run: CourseRun,
    plain_key: str, default_rule_profile: CompetencyRuleProfile, now: datetime, unknown_run: bool,
) -> None:
    """
    A course run with no criteria and a well-formed key with no course run are each a 200 with two empty arrays,
    even when the learner has statuses from another course run.
    """
    _earn_in_other_course_run(user, organization, competency_taxonomy, default_rule_profile, now)

    course_key = str(_locator(course_run).replace(run="NoSuchRun")) if unknown_run else plain_key

    response = user_client.get(statuses_url(course_key))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"competency_statuses": [], "criterion_statuses": []}


def test_learner_without_rows_in_a_course_with_criteria_gets_two_empty_arrays(
    user_client: APIClient, *, user, plain_key: str, organization, competency_taxonomy: CompetencyTaxonomy,
    course_run: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """
    A course run with criteria but no stored statuses for the learner in it gives two empty arrays, even when the
    learner has statuses from another course run.
    """
    make_leaf_criterion(tag, course_run, default_rule_profile, "s1")
    _earn_in_other_course_run(user, organization, competency_taxonomy, default_rule_profile, now)

    response = user_client.get(statuses_url(plain_key))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"competency_statuses": [], "criterion_statuses": []}


def test_payload_shape_ordering_and_no_learner_identity(
    user_client: APIClient, *, user, plain_key: str, organization, course_run: CourseRun, competency_taxonomy,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """
    The body has exactly the two documented arrays, each ordered by the referenced id, with the other course
    run named on a Demonstrated competency, and names neither the learner's username nor the user id.
    """
    del organization
    first_tag = make_tag(competency_taxonomy, "First")
    second_tag = make_tag(competency_taxonomy, "Second")
    first = make_leaf_criterion(first_tag, course_run, default_rule_profile, "s1")
    second = make_leaf_criterion(second_tag, course_run, default_rule_profile, "s2")
    # Stored in the reverse order, so that ordering comes from the ids and not from insertion.
    _store(user, second, second_tag, now)
    _store(user, first, first_tag, now)

    response = user_client.get(statuses_url(plain_key))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "competency_statuses": [
            {"tag_id": first_tag.id, "status": "Demonstrated", "demonstrated_in_course": None},
            {"tag_id": second_tag.id, "status": "Demonstrated", "demonstrated_in_course": None},
        ],
        "criterion_statuses": [
            {"criterion_id": first.id, "status": "Demonstrated"},
            {"criterion_id": second.id, "status": "Demonstrated"},
        ],
    }
    # The exact comparison above leaves no room for a learner field; the username is checked as raw text too.
    assert user.username not in response.content.decode()


def test_demonstrated_in_course_is_rendered_as_key_and_title(
    user_client: APIClient, *, user, plain_key: str, organization, course_run: CourseRun, tag: Tag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """A competency demonstrated in another course run names that run with its key and title."""
    other_run = make_course_run(organization, "Python200", "Fall2026")
    make_leaf_criterion(tag, course_run, default_rule_profile, "s1")
    in_other = make_leaf_criterion(tag, other_run, default_rule_profile, "s2")
    StudentCompetencyStatus.objects.create(
        user=user, tag=tag, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    course_level = in_other.group.parent
    assert isinstance(course_level, CompetencyCriteriaGroup)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=course_level, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    response = user_client.get(statuses_url(plain_key))

    assert response.json()["competency_statuses"] == [{
        "tag_id": tag.id,
        "status": "Demonstrated",
        "demonstrated_in_course": {"course_key": str(other_run.course_key), "title": other_run.title},
    }]


# =================================================================================================
# A configured resolver
# =================================================================================================


@override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{RESOLVER_PATH}.resolver_returning_resolved_user")
def test_configured_resolver_chooses_whose_statuses_are_returned(
    user_client: APIClient, *, user, plain_key: str, course_run: CourseRun, tag: Tag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """The statuses returned are those of the user the configured resolver returns, not the requester's."""
    resolved = get_user_model().objects.create(username="resolved")
    criterion = make_leaf_criterion(tag, course_run, default_rule_profile, "s1")
    StudentCompetencyCriterionStatus.objects.create(
        user=resolved, criterion=criterion, status_id=MasteryStatus.PARTIALLY_ATTEMPTED, created=now, modified=now,
    )
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    response = user_client.get(statuses_url(plain_key))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["criterion_statuses"] == [
        {"criterion_id": criterion.id, "status": "PartiallyAttempted"},
    ]


@override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{RESOLVER_PATH}.resolver_returning_resolved_user")
@pytest.mark.parametrize("pass_learner_id", [False, True], ids=["absent", "present"])
def test_resolver_receives_the_plain_course_key_and_an_integer_learner_id(
    user_client: APIClient, *, user, course_run: CourseRun, pass_learner_id: bool,
) -> None:
    """
    The resolver is handed the course key with its branch stripped, and ``learner_id`` as an int, or None when the
    client named no learner.
    """
    get_user_model().objects.create(username="resolved")
    query = {"learner_id": user.id} if pass_learner_id else {}
    drafted_key = str(_locator(course_run).for_branch("draft-branch"))

    with patch(f"{RESOLVER_PATH}.resolver_returning_resolved_user", wraps=resolver_returning_resolved_user) as resolver:
        response = user_client.get(statuses_url(drafted_key, **query))

    assert response.status_code == status.HTTP_200_OK
    (_, course_key, learner_id), _ = resolver.call_args
    assert drafted_key.endswith("+branch@draft-branch")
    assert course_key == course_run.course_key
    assert course_key.branch is None
    assert learner_id == (user.id if pass_learner_id else None)
    assert learner_id is None or isinstance(learner_id, int)


@override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{RESOLVER_PATH}.resolver_denying")
@pytest.mark.parametrize("with_criteria", [True, False], ids=["course with criteria", "course without criteria"])
def test_resolver_denial_gets_403_whether_or_not_the_course_has_criteria(
    user_client: APIClient, *, plain_key: str, course_run: CourseRun, tag: Tag,
    default_rule_profile: CompetencyRuleProfile, with_criteria: bool,
) -> None:
    """A resolver that raises PermissionDenied gives 403 for any course, so the refusal does not depend on data."""
    if with_criteria:
        make_leaf_criterion(tag, course_run, default_rule_profile, "s1")

    response = user_client.get(statuses_url(plain_key))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize(
    "resolver", ["resolver_raising_http404_with_message", "resolver_raising_not_found_with_message"],
)
def test_resolver_404_with_a_message_gets_the_standard_bare_404(
    user_client: APIClient, plain_key: str, resolver: str,
) -> None:
    """A message on a resolver's 404 does not reach the client, so a refusal cannot be told from an unknown id."""
    unknown_id = user_client.get(statuses_url(plain_key, learner_id=UNKNOWN_USER_ID))
    with override_settings(OPENEDX_LEARNING_LEARNER_RESOLVER=f"{RESOLVER_PATH}.{resolver}"):
        response = user_client.get(statuses_url(plain_key, learner_id=UNKNOWN_USER_ID))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.content == unknown_id.content
    assert b"secret" not in response.content
