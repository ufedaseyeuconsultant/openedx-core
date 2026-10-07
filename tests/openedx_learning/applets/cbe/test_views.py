"""
Tests for the CBE REST API views.

Fixtures live in this directory's conftest.py. Several scenarios here create rule profile rows
directly rather than through a live call, because no create or archive endpoint exists yet.
"""
from typing import Any

import pytest
import rules
from django.contrib.auth.models import User as UserType  # pylint: disable=imported-auth-user
from django.urls import reverse
from organizations.models import Organization
from rest_framework import status
from rest_framework.test import APIClient

from openedx_catalog.models import CatalogCourse, CourseRun
from openedx_learning.api import create_leaf_group
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    LogicOperator,
    RuleType,
)
from openedx_tagging.models import ObjectTag, Tag, Taxonomy

pytestmark = pytest.mark.django_db

# A marker embedded in an object_id to signal "this caller has no object-level tagging
# permission", for the one test that needs a permission failure. oel_tagging.rules hardcodes
# change_objecttag_objectid to False ("should be defined in other apps for proper permission
# checking"): real Studio-role logic lives only in openedx-platform, never in this repo. Following
# tests/openedx_tagging/test_views.py's TestObjectTagViewSet.setUp() precedent, this override
# simulates a permitted caller for every object_id except ones carrying this marker.
UNAUTHORIZED_MARKER = "unauthorized"


@pytest.fixture(autouse=True)
def _allow_object_level_tagging() -> None:
    """Let an ordinary authenticated user tag any object_id except one carrying UNAUTHORIZED_MARKER."""
    def _predicate(_user: UserType, object_id: str) -> bool:
        return UNAUTHORIZED_MARKER not in object_id

    rules.set_perm("oel_tagging.change_objecttag_objectid", _predicate)


@pytest.fixture(name="user_client")
def _user_client(api_client: APIClient, user: UserType) -> APIClient:
    """A REST client acting as `user`, an ordinary, non-staff, authenticated caller."""
    api_client.force_authenticate(user=user)
    return api_client


def criterion_create_url(tag_id: int) -> str:
    """Return the create-criterion endpoint's path for `tag_id`, resolved through the URL name."""
    return reverse("cbe:criterion-create", kwargs={"tag_id": tag_id})


def usage_key(course_run: CourseRun, block_id: str) -> str:
    """Build a gradeable-subsection-shaped usage key string under `course_run`."""
    key = course_run.course_key
    assert key is not None
    return f"block-v1:{key.org}+{key.course}+{key.run}+type@sequential+block@{block_id}"


def make_course_run(organization: Organization, course_code: str, run_code: str) -> CourseRun:
    """Create a CourseRun distinct from the `course_run` fixture, for the different-course tests."""
    catalog_course = CatalogCourse.objects.create(org=organization, course_code=course_code)
    return CourseRun.objects.create(catalog_course=catalog_course, run_code=run_code)


# What migration 0003 seeds the system default with. Asserted verbatim rather than imported, so
# that a change to the seed surfaces here as a failing contract instead of passing silently.
SEEDED_GRADE_PAYLOAD = {"op": "gte", "value": 0.8, "scale": "percent"}

# A different valid payload for rows these tests create, so no assertion about the seeded row
# can pass by accident against a row a test made itself.
FIXTURE_GRADE_PAYLOAD = {"op": "gte", "value": 0.6, "scale": "percent"}

RESPONSE_FIELDS = {"id", "scope_type", "rule_type", "rule_payload", "archived"}


def rule_profiles_url() -> str:
    """Return the collection's path, resolved through the router rather than hardcoded."""
    return reverse("cbe:rule_profile-list")


def make_profile(**scope) -> CompetencyRuleProfile:
    """Create a live rule profile at `scope`, which is at most one of the three scope columns."""
    return CompetencyRuleProfile.objects.create(
        rule_type=RuleType.GRADE, rule_payload=dict(FIXTURE_GRADE_PAYLOAD), **scope
    )


def test_collection_resolves_to_the_documented_path() -> None:
    """
    The router and the mounts compose into the path the CBE tickets name.

    Pinned in one place so the rest of these tests can use reverse() instead.
    """
    assert rule_profiles_url() == "/api/cbe/v1/rule_profiles/"


def test_read_the_rule_the_instance_requires_for_mastery(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A permitted caller reads the instance-wide default, complete enough to act on.

    The threshold arrives with the scale it is expressed on, so a caller cannot read the
    fraction as a percentage or the reverse.
    """
    response = staff_client.get(rule_profiles_url())

    assert response.status_code == status.HTTP_200_OK
    assert len(response.data["results"]) == 1
    profile = response.data["results"][0]
    assert profile["id"] == default_rule_profile.id
    assert profile["scope_type"] == "system_default"
    assert profile["rule_type"] == "Grade"
    assert profile["rule_payload"] == SEEDED_GRADE_PAYLOAD
    assert profile["archived"] is False


def test_response_omits_internal_scope_bookkeeping(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """The response describes scope in terms a caller outside this system can act on."""
    response = staff_client.get(rule_profiles_url())

    profile = response.data["results"][0]
    assert profile["id"] == default_rule_profile.id
    assert set(profile.keys()) == RESPONSE_FIELDS
    assert "scope_code" not in profile
    assert "organization" not in profile
    assert "course" not in profile
    assert "competency_taxonomy" not in profile


def test_every_profile_reports_the_scope_it_applies_to(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
    course_run: CourseRun,
    organization: Organization,
) -> None:
    """
    A caller tells the instance-wide default apart from a narrower profile by scope alone.

    Each row here holds a different scope_code, which is what shows the derivation reads the
    scope columns rather than matching that string.
    """
    taxonomy_scoped = make_profile(competency_taxonomy=competency_taxonomy)
    course_scoped = make_profile(course=course_run)
    organization_scoped = make_profile(organization=organization)

    response = staff_client.get(rule_profiles_url())

    assert response.status_code == status.HTTP_200_OK
    scope_types = {row["id"]: row["scope_type"] for row in response.data["results"]}
    assert scope_types == {
        default_rule_profile.id: "system_default",
        taxonomy_scoped.id: "taxonomy",
        course_scoped.id: "course",
        organization_scoped.id: "organization",
    }


def test_retired_profiles_are_left_out(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
) -> None:
    """A retired profile is not returned, while the instance-wide default still is."""
    archived = make_profile(competency_taxonomy=competency_taxonomy, archived=True)

    response = staff_client.get(rule_profiles_url())

    returned_ids = [row["id"] for row in response.data["results"]]
    assert archived.id not in returned_ids
    assert returned_ids == [default_rule_profile.id]


def test_instance_with_no_rule_profiles_reports_an_empty_collection(staff_client: APIClient) -> None:
    """
    An instance holding no profiles is an empty collection, not a missing resource.

    Migration 0003 seeds the system default into the test database, so the rows are cleared
    explicitly here rather than assuming an empty table.
    """
    CompetencyRuleProfile.objects.all().delete()

    response = staff_client.get(rule_profiles_url())

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 0
    assert response.data["results"] == []


def test_response_is_the_paginated_envelope(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """The collection arrives in an envelope, never as a bare array."""
    response = staff_client.get(rule_profiles_url())

    assert isinstance(response.data, dict)
    assert {"count", "next", "previous", "results"} <= set(response.data.keys())
    assert isinstance(response.data["results"], list)
    assert [row["id"] for row in response.data["results"]] == [default_rule_profile.id]


def test_a_collection_larger_than_one_response_arrives_whole(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
    course_run: CourseRun,
    organization: Organization,
) -> None:
    """
    Following the link to the remainder yields every profile once, with none skipped.

    The page size divides the four profiles unevenly on purpose, so a partial last page is
    exercised rather than a run of exactly full ones.
    """
    expected_ids = [
        default_rule_profile.id,
        make_profile(competency_taxonomy=competency_taxonomy).id,
        make_profile(course=course_run).id,
        make_profile(organization=organization).id,
    ]

    collected_ids: list[int] = []
    pages = 0
    next_url = f"{rule_profiles_url()}?page_size=3"
    while next_url:
        response = staff_client.get(next_url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == len(expected_ids)
        collected_ids.extend(row["id"] for row in response.data["results"])
        next_url = response.data["next"]
        pages += 1

    assert pages == 2, "page_size=3 should have split four profiles across two responses"
    assert collected_ids == expected_ids


def test_profiles_arrive_in_the_same_order_every_time(
    staff_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
    course_run: CourseRun,
    organization: Organization,
) -> None:
    """Two requests with nothing changing in between return the profiles in the same order."""
    make_profile(competency_taxonomy=competency_taxonomy)
    make_profile(course=course_run)
    make_profile(organization=organization)

    first = [row["id"] for row in staff_client.get(rule_profiles_url()).data["results"]]
    second = [row["id"] for row in staff_client.get(rule_profiles_url()).data["results"]]

    assert first == second
    assert first == sorted(first)
    assert default_rule_profile.id in first


@pytest.mark.parametrize(
    "user_fixture, expected_status",
    [
        (None, status.HTTP_401_UNAUTHORIZED),
        ("user", status.HTTP_403_FORBIDDEN),
        ("staff_user", status.HTTP_200_OK),
    ],
)
def test_only_a_competency_administrator_may_read_the_collection(
    request: pytest.FixtureRequest,
    api_client: APIClient,
    default_rule_profile: CompetencyRuleProfile,
    user_fixture: str | None,
    expected_status: int,
) -> None:
    """An unidentified caller and an unpermitted one are both refused, and get no profile."""
    if user_fixture is not None:
        api_client.force_authenticate(user=request.getfixturevalue(user_fixture))

    response = api_client.get(rule_profiles_url())

    assert response.status_code == expected_status
    if expected_status == status.HTTP_200_OK:
        assert [row["id"] for row in response.data["results"]] == [default_rule_profile.id]
    else:
        assert "results" not in response.data


# ==============================================================================================
# CompetencyCriterionCreateView (#665)
# ==============================================================================================


def test_no_group_no_existing_groups_creates_the_full_hierarchy_and_201s(
    user_client: APIClient, tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """The happy path: no group_id, no groups yet, no rule fields supplied."""
    object_id = usage_key(course_run, "p1")

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    criterion = CompetencyCriterion.objects.get(pk=response.data["id"])
    assert criterion.group.tag_id == tag.id
    course_level = criterion.group.parent
    assert course_level is not None
    assert course_level.course_id == course_run.id
    root = course_level.parent
    assert root is not None
    assert root.parent is None
    assert response.data["object_tag_id"] == criterion.object_tag_id
    assert response.data["group_id"] == criterion.group_id
    assert response.data["rule_profile_id"] == default_rule_profile.id


def test_logic_operator_provided_is_stored_on_the_new_leaf(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """A supplied logic_operator is stored on the newly created leaf, not defaulted."""
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "logic_operator": "AND"}, format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    criterion = CompetencyCriterion.objects.get(pk=response.data["id"])
    assert criterion.group.logic_operator == LogicOperator.AND


def test_logic_operator_omitted_defaults_to_or(user_client: APIClient, tag: Tag, course_run: CourseRun) -> None:
    """Omitting logic_operator on the derive-or-create path stores "OR" literally."""
    object_id = usage_key(course_run, "p1")

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    criterion = CompetencyCriterion.objects.get(pk=response.data["id"])
    assert criterion.group.logic_operator == LogicOperator.OR


def test_group_id_and_logic_operator_together_is_rejected(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """Supplying both group_id and logic_operator is a 400, before anything is created."""
    leaf = create_leaf_group(tag, course_run)
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id),
        {"object_id": object_id, "group_id": leaf.id, "logic_operator": "AND"},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "logic_operator" in response.data


def test_course_level_group_is_reused_within_the_same_course(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """Two criteria created for the same tag and course share one course-level group."""
    first = user_client.post(
        criterion_create_url(tag.id), {"object_id": usage_key(course_run, "p1")}, format="json",
    )
    second = user_client.post(
        criterion_create_url(tag.id), {"object_id": usage_key(course_run, "p2")}, format="json",
    )

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    first_group = CompetencyCriterion.objects.get(pk=first.data["id"]).group
    second_group = CompetencyCriterion.objects.get(pk=second.data["id"]).group
    assert first_group.id != second_group.id
    assert first_group.parent_id == second_group.parent_id


def test_new_course_level_group_for_a_different_course(
    user_client: APIClient, tag: Tag, course_run: CourseRun, organization: Organization,
) -> None:
    """A second course under the same tag gets its own course-level group, but shares the root."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")

    first = user_client.post(
        criterion_create_url(tag.id), {"object_id": usage_key(course_run, "p1")}, format="json",
    )
    second = user_client.post(
        criterion_create_url(tag.id), {"object_id": usage_key(other_course_run, "p1")}, format="json",
    )

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    first_group = CompetencyCriterion.objects.get(pk=first.data["id"]).group
    second_group = CompetencyCriterion.objects.get(pk=second.data["id"]).group
    assert first_group.parent_id != second_group.parent_id
    assert first_group.parent is not None
    assert second_group.parent is not None
    assert first_group.parent.parent_id == second_group.parent.parent_id


def test_supplied_leaf_group_happy_path(user_client: APIClient, tag: Tag, course_run: CourseRun) -> None:
    """Supplying an existing, valid leaf group_id uses it directly."""
    leaf = create_leaf_group(tag, course_run)
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": leaf.id}, format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["group_id"] == leaf.id


def test_supplied_group_non_leaf_is_rejected(user_client: APIClient, tag: Tag, course_run: CourseRun) -> None:
    """A supplied group_id that names a root (non-leaf) group is a 400."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": root.id}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "group_id" in response.data


def test_supplied_group_for_a_different_competency_is_rejected(
    user_client: APIClient, tag: Tag, course_run: CourseRun, competency_taxonomy: CompetencyTaxonomy,
) -> None:
    """A supplied group_id that belongs to a different competency tag is a 400."""
    other_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Other Competency")
    other_leaf = create_leaf_group(other_tag, course_run)
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": other_leaf.id}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "group_id" in response.data


def test_supplied_group_for_a_different_course_is_rejected(
    user_client: APIClient, tag: Tag, course_run: CourseRun, organization: Organization,
) -> None:
    """A supplied group_id whose course-level parent belongs to a different course is a 400."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    other_leaf = create_leaf_group(tag, other_course_run)
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": other_leaf.id}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "group_id" in response.data


def test_duplicate_association_supplying_the_same_group_is_rejected(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """A second criterion for the same (tag, object_id), re-supplying the group it just created, is a 400."""
    object_id = usage_key(course_run, "p1")
    first = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")
    first_group_id = first.data["group_id"]

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": first_group_id}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "group_id" in response.data


def test_duplicate_association_supplying_a_different_existing_leaf_creates_a_second_criterion(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """
    A second criterion for the same (tag, object_id), explicitly aimed at a *different* existing
    leaf, is a deliberate second association, not a duplicate -- per ADR-0002's worked example,
    the same tag/object association may participate in more than one CompetencyCriteriaGroup.
    Only re-targeting the exact same group already used is rejected (see the sibling test).
    """
    object_id = usage_key(course_run, "p1")
    first = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")
    first_group_id = first.data["group_id"]
    other_leaf = create_leaf_group(tag, course_run)
    groups_before = CompetencyCriteriaGroup.objects.count()

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": other_leaf.id}, format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["group_id"] == other_leaf.id
    # No new groups: other_leaf already existed and was supplied explicitly.
    assert CompetencyCriteriaGroup.objects.count() == groups_before
    criterion_group_ids = set(
        CompetencyCriterion.objects.filter(object_tag_id=response.data["object_tag_id"])
        .values_list("group_id", flat=True)
    )
    assert criterion_group_ids == {first_group_id, other_leaf.id}


def test_duplicate_association_via_the_derive_path_is_rejected_before_creating_a_group(
    user_client: APIClient, tag: Tag, course_run: CourseRun,
) -> None:
    """
    A second criterion for the same (tag, object_id), with no group_id, is rejected before any new group.

    Rejected regardless of which group the existing criterion is actually in: with no group_id
    supplied, there's no way to know which group slot this request intends, so any existing
    criterion for the pair counts as a duplicate.
    """
    object_id = usage_key(course_run, "p1")
    user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")
    groups_before = CompetencyCriteriaGroup.objects.count()

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "object_id" in response.data
    assert CompetencyCriteriaGroup.objects.count() == groups_before


def test_all_null_rule_fields_resolve_to_the_system_default_profile_id(
    user_client: APIClient, tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Omitting rule_profile_id, rule_type_override, and rule_payload_override together resolves
    rule_profile_id to the seeded system-default profile's id, not null.

    CompetencyCriterion's own oel_cbe_criterion_profile_xor_override_check constraint never
    allows all three fields null.
    """
    object_id = usage_key(course_run, "p1")

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["rule_profile_id"] == default_rule_profile.id
    assert response.data["rule_type_override"] is None
    assert response.data["rule_payload_override"] is None


def test_reusing_a_subsection_already_tagged_with_a_different_competency_preserves_both_tags(
    user_client: APIClient, tag: Tag, course_run: CourseRun, competency_taxonomy: CompetencyTaxonomy,
) -> None:
    """
    Creating a criterion for a second competency on an already-tagged object merges, not overwrites.

    Both tags here come from the SAME CompetencyTaxonomy, per the plan's explicit note: only the
    same-taxonomy case exercises tag_object()'s replace-the-whole-list behavior, since two
    different taxonomies' ObjectTag rows would never collide even against a naive
    overwrite-instead-of-merge implementation.
    """
    other_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Other Competency")
    object_id = usage_key(course_run, "p1")

    first = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")
    second = user_client.post(criterion_create_url(other_tag.id), {"object_id": object_id}, format="json")

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    applied_tag_ids = set(
        ObjectTag.objects.filter(object_id=object_id, taxonomy=competency_taxonomy).values_list("tag_id", flat=True)
    )
    assert applied_tag_ids == {tag.id, other_tag.id}


def test_competency_not_found_404s(user_client: APIClient, course_run: CourseRun) -> None:
    """An unresolvable tag_id (URL path parameter) 404s."""
    object_id = usage_key(course_run, "p1")

    response = user_client.post(criterion_create_url(999999), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_group_not_found_404s(user_client: APIClient, tag: Tag, course_run: CourseRun) -> None:
    """An unresolvable group_id 404s."""
    object_id = usage_key(course_run, "p1")

    response = user_client.post(
        criterion_create_url(tag.id), {"object_id": object_id, "group_id": 999999}, format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_missing_object_id_is_400(user_client: APIClient, tag: Tag) -> None:
    """A request with no object_id at all is a 400."""
    response = user_client.post(criterion_create_url(tag.id), {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "object_id" in response.data


def test_malformed_object_id_is_400(user_client: APIClient, tag: Tag) -> None:
    """An object_id that isn't a parseable usage key is a 400, keyed by object_id."""
    response = user_client.post(criterion_create_url(tag.id), {"object_id": "not-a-usage-key"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "object_id" in response.data


def test_unresolvable_course_is_400(user_client: APIClient, tag: Tag) -> None:
    """A well-formed usage key whose course has no matching CourseRun is a 400."""
    object_id = "block-v1:NoOrg+NoCourse+NoRun+type@sequential+block@p1"

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "object_id" in response.data


def test_no_permission_is_403(user_client: APIClient, tag: Tag, course_run: CourseRun) -> None:
    """A caller without object-level tagging permission on this object_id is refused with a 403."""
    object_id = usage_key(course_run, UNAUTHORIZED_MARKER)

    response = user_client.post(criterion_create_url(tag.id), {"object_id": object_id}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN


# ==============================================================================================
# CompetencyCriteriaTreeView (#681)
# ==============================================================================================


def criteria_tree_url(tag_id: int) -> str:
    """Return the criteria-tree endpoint's path for `tag_id`, resolved through the URL name."""
    return reverse("cbe:criteria-tree", kwargs={"tag_id": tag_id})


def test_full_tree_response_shape(
    user_client: APIClient,
    tag: Tag,
    course_run: CourseRun,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A permitted caller reads the full tree: every group and the leaf's criterion, complete enough to act on."""
    leaf = create_leaf_group(tag, course_run)
    object_id = usage_key(course_run, "p1")
    object_tag = ObjectTag.objects.create(object_id=object_id, taxonomy=tag.taxonomy, tag=tag)
    criterion = CompetencyCriterion.objects.create(
        group=leaf, object_tag=object_tag, rule_profile=default_rule_profile,
    )

    course_level = leaf.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None

    response = user_client.get(criteria_tree_url(tag.id))

    assert response.status_code == status.HTTP_200_OK
    groups_by_id = {group["id"]: group for group in response.data["groups"]}
    assert set(groups_by_id) == {leaf.id, course_level.id, root.id}
    assert groups_by_id[root.id]["parent_id"] is None
    assert groups_by_id[root.id]["course_key"] is None
    assert groups_by_id[course_level.id]["parent_id"] == root.id
    assert groups_by_id[leaf.id]["parent_id"] == course_level.id
    assert len(response.data["criteria"]) == 1
    criterion_data = response.data["criteria"][0]
    assert criterion_data["id"] == criterion.id
    assert criterion_data["group_id"] == leaf.id
    assert criterion_data["object_tag_id"] == object_tag.id
    assert criterion_data["object_id"] == object_id
    assert criterion_data["rule_profile_id"] == default_rule_profile.id
    assert criterion_data["rule_type_override"] is None
    assert criterion_data["rule_payload_override"] is None


def test_full_tree_combines_multiple_courses_in_one_unscoped_response(
    user_client: APIClient,
    tag: Tag,
    course_run: CourseRun,
    organization: Organization,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A GET request for one competency returns every course's groups and criteria together,
    with no course_id or date-window scoping parameter to narrow the request.
    """
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf1 = create_leaf_group(tag, course_run)
    leaf2 = create_leaf_group(tag, other_course_run)
    object_tag1 = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    object_tag2 = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    criterion1 = CompetencyCriterion.objects.create(
        group=leaf1, object_tag=object_tag1, rule_profile=default_rule_profile,
    )
    criterion2 = CompetencyCriterion.objects.create(
        group=leaf2, object_tag=object_tag2, rule_profile=default_rule_profile,
    )
    course_level1 = leaf1.parent
    course_level2 = leaf2.parent
    assert course_level1 is not None
    assert course_level2 is not None
    root = course_level1.parent
    assert root is not None
    assert course_level2.parent_id == root.id

    response = user_client.get(criteria_tree_url(tag.id))

    assert response.status_code == status.HTTP_200_OK
    returned_group_ids = {group["id"] for group in response.data["groups"]}
    assert returned_group_ids == {root.id, course_level1.id, leaf1.id, course_level2.id, leaf2.id}
    assert {c["id"] for c in response.data["criteria"]} == {criterion1.id, criterion2.id}


def test_empty_tree_is_200_with_two_empty_arrays(user_client: APIClient, tag: Tag) -> None:
    """A tag with no CompetencyCriteriaGroup rows yet is a valid 200 with two empty arrays, not a 404."""
    response = user_client.get(criteria_tree_url(tag.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.data["groups"] == []
    assert response.data["criteria"] == []


def test_archived_group_and_criterion_are_excluded(
    user_client: APIClient,
    tag: Tag,
    course_run: CourseRun,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """An archived leaf group and an archived criterion under it are left out of the response."""
    live_leaf = create_leaf_group(tag, course_run)
    live_object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, "live"), taxonomy=tag.taxonomy, tag=tag,
    )
    live_criterion = CompetencyCriterion.objects.create(
        group=live_leaf, object_tag=live_object_tag, rule_profile=default_rule_profile,
    )
    archived_leaf = create_leaf_group(tag, course_run)
    archived_leaf.archived = True
    archived_leaf.save()
    archived_object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, "archived"), taxonomy=tag.taxonomy, tag=tag,
    )
    CompetencyCriterion.objects.create(
        group=archived_leaf, object_tag=archived_object_tag, rule_profile=default_rule_profile, archived=True,
    )

    response = user_client.get(criteria_tree_url(tag.id))

    assert response.status_code == status.HTTP_200_OK
    returned_group_ids = {group["id"] for group in response.data["groups"]}
    assert live_leaf.id in returned_group_ids
    assert archived_leaf.id not in returned_group_ids
    assert [c["id"] for c in response.data["criteria"]] == [live_criterion.id]


def test_unknown_tag_id_404s(user_client: APIClient) -> None:
    """An unresolvable tag_id 404s."""
    response = user_client.get(criteria_tree_url(999999))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_tag_not_on_a_competency_taxonomy_404s(user_client: APIClient) -> None:
    """A tag_id that resolves to a real Tag, but not one on a CompetencyTaxonomy, 404s."""
    plain_taxonomy = Taxonomy.objects.create(name="Plain Tags", export_id="plain-v1")
    plain_tag = Tag.objects.create(taxonomy=plain_taxonomy, value="Not A Competency")

    response = user_client.get(criteria_tree_url(plain_tag.id))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_no_view_access_to_the_taxonomy_is_403(api_client: APIClient, user: UserType, tag: Tag) -> None:
    """A non-staff caller may not read the tree of a competency on a disabled taxonomy."""
    taxonomy = tag.taxonomy
    assert taxonomy is not None
    taxonomy.enabled = False
    taxonomy.save()
    api_client.force_authenticate(user=user)

    response = api_client.get(criteria_tree_url(tag.id))

    assert response.status_code == status.HTTP_403_FORBIDDEN


# ==============================================================================================
# CompetencyCriteriaGroupDetailView (#760)
# ==============================================================================================

GROUP_FIELDS = {"id", "parent_id", "tag_id", "course_key", "name", "ordering", "logic_operator", "archived"}


def group_detail_url(tag_id: int, group_id: int) -> str:
    """Return the group detail endpoint's path, resolved through the URL name."""
    return reverse("cbe:criteria-group-detail", kwargs={"tag_id": tag_id, "group_id": group_id})


@pytest.fixture(name="leaf")
def _leaf(tag: Tag, course_run: CourseRun) -> CompetencyCriteriaGroup:
    """A leaf group under `tag` and `course_run`. Only a leaf group holds criteria."""
    return create_leaf_group(tag, course_run)


@pytest.fixture(name="course_level")
def _course_level(leaf: CompetencyCriteriaGroup) -> CompetencyCriteriaGroup:
    """The course-level group `leaf` sits under, combining its children with AND."""
    course_level = leaf.parent
    assert course_level is not None
    course_level.logic_operator = LogicOperator.AND
    course_level.save()
    return course_level


def stored_group(group: CompetencyCriteriaGroup) -> tuple:
    """Return `group`'s columns as stored, to prove a refused request changed nothing."""
    group.refresh_from_db()
    return (
        group.logic_operator, group.name, group.ordering, group.tag_id, group.course_id, group.parent_id,
        group.archived,
    )


def test_group_detail_resolves_to_the_documented_path() -> None:
    """The group's own route sits under the competency's criteria-groups collection."""
    assert group_detail_url(7, 9) == "/api/cbe/v1/competencies/7/criteria-groups/9/"


@pytest.mark.parametrize(
    "before, after", [(LogicOperator.AND, LogicOperator.OR), (LogicOperator.OR, LogicOperator.AND)],
)
def test_update_group_switches_its_operator_and_echoes_the_rest(  # pylint: disable=too-many-positional-arguments
    before: str,
    after: str,
    user_client: APIClient,
    tag: Tag,
    course_run: CourseRun,
    course_level: CompetencyCriteriaGroup,
) -> None:
    """The response is the group as stored: the new operator, everything else as before."""
    course_level.logic_operator = before
    course_level.save()

    response = user_client.patch(group_detail_url(tag.id, course_level.id), {"logic_operator": after}, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert set(response.data.keys()) == GROUP_FIELDS
    assert response.data["logic_operator"] == after
    assert response.data["id"] == course_level.id
    assert response.data["name"] == course_level.name
    assert response.data["ordering"] == course_level.ordering
    assert response.data["tag_id"] == tag.id
    assert response.data["course_key"] == str(course_run.course_key)
    assert response.data["parent_id"] == course_level.parent_id
    assert response.data["archived"] is False


def test_update_group_accepts_back_the_representation_the_tree_endpoint_gave(
    user_client: APIClient, tag: Tag, course_level: CompetencyCriteriaGroup,
) -> None:
    """A client can read the group, change only its operator, and send the whole object back."""
    tree = user_client.get(criteria_tree_url(tag.id))
    read = next(group for group in tree.data["groups"] if group["id"] == course_level.id)

    response = user_client.patch(
        group_detail_url(tag.id, course_level.id), {**read, "logic_operator": LogicOperator.OR}, format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data == {**read, "logic_operator": LogicOperator.OR}


@pytest.mark.parametrize("sent, expected_status", [
    ("  Padded name  ", status.HTTP_200_OK),
    ("Padded name", status.HTTP_400_BAD_REQUEST),
    ("  padded name  ", status.HTTP_400_BAD_REQUEST),
])
def test_update_group_compares_a_restated_name_exactly(
    sent: str,
    expected_status: int,
    user_client: APIClient,
    tag: Tag,
    course_level: CompetencyCriteriaGroup,
) -> None:
    """A restated name matches only if it is identical, so neither trimming nor a change of case gets through."""
    course_level.name = "  Padded name  "
    course_level.save()

    response = user_client.patch(
        group_detail_url(tag.id, course_level.id), {"logic_operator": LogicOperator.OR, "name": sent}, format="json",
    )

    assert response.status_code == expected_status


def test_update_group_that_changes_nothing_is_200(
    user_client: APIClient, tag: Tag, course_level: CompetencyCriteriaGroup,
) -> None:
    """Sending the operator the group already has succeeds and reports it."""
    response = user_client.patch(
        group_detail_url(tag.id, course_level.id), {"logic_operator": LogicOperator.AND}, format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["logic_operator"] == LogicOperator.AND


@pytest.mark.parametrize(
    "case, error_key",
    [
        ("missing operator", "logic_operator"),
        ("null operator", "logic_operator"),
        ("unsupported operator", "logic_operator"),
        ("unrecognized key", "children"),
        ("rename", "name"),
        ("reorder", "ordering"),
        ("re-parent", "parent_id"),
        ("change course", "course_key"),
        ("change competency", "tag_id"),
        ("archive", "archived"),
        ("change id", "id"),
    ],
)
def test_update_group_rejects_a_malformed_body_with_400(  # pylint: disable=too-many-positional-arguments
    case: str,
    error_key: str,
    user_client: APIClient,
    tag: Tag,
    leaf: CompetencyCriteriaGroup,
    course_level: CompetencyCriteriaGroup,
) -> None:
    """Each malformed body is a 400 naming the offending field, and the group is unchanged."""
    change = {"logic_operator": LogicOperator.OR}
    bodies: dict[str, dict[str, Any]] = {
        "missing operator": {},
        "null operator": {"logic_operator": None},
        "unsupported operator": {"logic_operator": "XOR"},
        # A group's membership is not this endpoint's to change.
        "unrecognized key": {**change, "children": [leaf.id]},
        "rename": {**change, "name": "Renamed"},
        "reorder": {**change, "ordering": course_level.ordering + 1},
        "re-parent": {**change, "parent_id": None},
        "change course": {**change, "course_key": "course-v1:Org1+Other+Run"},
        "change competency": {**change, "tag_id": tag.id + 1},
        "archive": {**change, "archived": True},
        "change id": {**change, "id": leaf.id},
    }
    before = stored_group(course_level)

    response = user_client.patch(group_detail_url(tag.id, course_level.id), bodies[case], format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert error_key in response.data
    assert stored_group(course_level) == before


def test_update_root_group_is_400(user_client: APIClient, tag: Tag, course_level: CompetencyCriteriaGroup) -> None:
    """A root group is refused with a 400 saying roots cannot be targeted, and it is unchanged."""
    root = course_level.parent
    assert root is not None
    before = stored_group(root)

    response = user_client.patch(
        group_detail_url(tag.id, root.id), {"logic_operator": LogicOperator.AND}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "root" in str(response.data["group_id"])
    assert stored_group(root) == before


def test_update_course_less_child_of_the_root_is_400_but_not_as_a_root(
    user_client: APIClient, tag: Tag, course_level: CompetencyCriteriaGroup,
) -> None:
    """A group with a parent but no course anywhere above it is refused for having no course, not as a root."""
    root = course_level.parent
    assert root is not None
    course_less = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, logic_operator=LogicOperator.AND)
    before = stored_group(course_less)

    response = user_client.patch(
        group_detail_url(tag.id, course_less.id), {"logic_operator": LogicOperator.OR}, format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "no course" in str(response.data["group_id"])
    assert "root" not in str(response.data["group_id"])
    assert stored_group(course_less) == before


def test_update_group_nested_below_a_leaf_is_200(
    user_client: APIClient, tag: Tag, leaf: CompetencyCriteriaGroup,
) -> None:
    """A group nested below a leaf is editable, its course coming from the nearest ancestor that has one."""
    nested = CompetencyCriteriaGroup.objects.create(tag=tag, parent=leaf, logic_operator=LogicOperator.OR)

    response = user_client.patch(
        group_detail_url(tag.id, nested.id), {"logic_operator": LogicOperator.AND}, format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["logic_operator"] == LogicOperator.AND


def test_update_group_under_a_tag_that_is_not_a_competency_is_404(
    user_client: APIClient, course_run: CourseRun,
) -> None:
    """A group under a non-competency taxonomy is a missing resource, and it is unchanged."""
    plain_tag = Tag.objects.create(taxonomy=Taxonomy.objects.create(name="Plain Tags", export_id="plain-v1"), value="x")
    root = CompetencyCriteriaGroup.objects.create(tag=plain_tag, parent=None)
    # Shaped like any editable group, so only its taxonomy sets it apart.
    course_level = CompetencyCriteriaGroup.objects.create(
        tag=plain_tag, course=course_run, parent=root, logic_operator=LogicOperator.AND,
    )
    before = stored_group(course_level)

    # The mismatched name would be a 400 if the body were checked before the tag, as it isn't on the other endpoints.
    response = user_client.patch(
        group_detail_url(plain_tag.id, course_level.id),
        {"logic_operator": LogicOperator.OR, "name": "Renamed"},
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert stored_group(course_level) == before


def test_update_group_addressed_under_another_competency_is_404(
    user_client: APIClient, competency_taxonomy: CompetencyTaxonomy, course_level: CompetencyCriteriaGroup,
) -> None:
    """A group addressed through a competency it doesn't belong to doesn't exist there, and is unchanged."""
    other_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Reading Poetry")
    before = stored_group(course_level)

    response = user_client.patch(
        group_detail_url(other_tag.id, course_level.id), {"logic_operator": LogicOperator.OR}, format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert stored_group(course_level) == before


def test_update_group_that_does_not_exist_is_404(user_client: APIClient, tag: Tag) -> None:
    """An unknown group id in the URL is a missing resource."""
    response = user_client.patch(group_detail_url(tag.id, 999999), {"logic_operator": LogicOperator.OR}, format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_update_archived_group_is_409(user_client: APIClient, tag: Tag, leaf: CompetencyCriteriaGroup) -> None:
    """An archived group conflicts with any edit, and stays archived and otherwise unchanged."""
    leaf.archived = True
    leaf.save()
    before = stored_group(leaf)

    response = user_client.patch(
        group_detail_url(tag.id, leaf.id), {"logic_operator": LogicOperator.AND}, format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "archived" in str(response.data["detail"])
    assert stored_group(leaf) == before


@pytest.mark.parametrize("lacks", ["course write access", "taxonomy view access"])
def test_update_group_without_permission_is_403(
    lacks: str, user_client: APIClient, tag: Tag, organization: Organization,
) -> None:
    """A caller who may not tag objects in the group's course, or may not view its taxonomy, is refused."""
    course_code = UNAUTHORIZED_MARKER if lacks == "course write access" else "Python200"
    leaf = create_leaf_group(tag, make_course_run(organization, course_code, "Fall2026"))
    if lacks == "taxonomy view access":
        taxonomy = tag.taxonomy
        assert taxonomy is not None
        taxonomy.enabled = False
        taxonomy.save()
    before = stored_group(leaf)

    response = user_client.patch(
        group_detail_url(tag.id, leaf.id), {"logic_operator": LogicOperator.AND}, format="json",
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert stored_group(leaf) == before


def test_update_group_from_an_unidentified_caller_is_401(
    api_client: APIClient, tag: Tag, leaf: CompetencyCriteriaGroup,
) -> None:
    """A caller the system cannot identify is refused before anything is read."""
    response = api_client.patch(group_detail_url(tag.id, leaf.id), {"logic_operator": LogicOperator.AND}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize("method", ["get", "put", "post", "delete"])
def test_group_detail_offers_only_patch(
    method: str, user_client: APIClient, tag: Tag, leaf: CompetencyCriteriaGroup,
) -> None:
    """Every method other than PATCH, PUT included, is refused as not allowed."""
    response = getattr(user_client, method)(group_detail_url(tag.id, leaf.id), {}, format="json")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
