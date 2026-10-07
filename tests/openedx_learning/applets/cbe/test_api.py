"""
Tests for the CBE public API surface (openedx_learning.api).
"""
from collections.abc import Iterator

import pytest
import rules
from django.apps import apps
from django.contrib.auth.models import User as UserType  # pylint: disable=imported-auth-user
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.utils import IntegrityError
from django.forms.models import model_to_dict
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from organizations.models import Organization
from rules.permissions import permissions as rule_permissions

from openedx_catalog.models import CatalogCourse, CourseRun
from openedx_learning.api import (
    CompetencyCriteriaGroupArchivedError,
    associate_competency_criterion,
    create_leaf_group,
    get_competency_criteria_tree,
    get_competency_rule_profiles,
    is_competency_taxonomy,
    resolve_supplied_leaf_group,
    select_competency_taxonomies,
    update_competency_criteria_group,
)
from openedx_learning.applets.cbe import api as cbe_api
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

GRADE_PAYLOAD = {"op": "gte", "value": 0.8, "scale": "percent"}


def make_course_run(organization: Organization, course_code: str, run_code: str) -> CourseRun:
    """Create a CourseRun distinct from the `course_run` fixture, for the same-tag-different-course tests."""
    catalog_course = CatalogCourse.objects.create(org=organization, course_code=course_code)
    return CourseRun.objects.create(catalog_course=catalog_course, run_code=run_code)


def usage_key(course_run: CourseRun, block_id: str) -> str:
    """Build a gradeable-subsection-shaped usage key string under `course_run`."""
    key = course_run.course_key
    assert key is not None
    return f"block-v1:{key.org}+{key.course}+{key.run}+type@sequential+block@{block_id}"


def test_is_competency_taxonomy() -> None:
    """
    is_competency_taxonomy() is True for a competency taxonomy, False for a plain one.
    """
    competency = CompetencyTaxonomy.objects.create(name="Nursing", export_id="nursing-v1")
    plain = Taxonomy.objects.create(name="Plain Tags", export_id="plain-v1")

    assert is_competency_taxonomy(Taxonomy.objects.get(pk=competency.pk)) is True
    assert is_competency_taxonomy(plain) is False


def test_is_competency_taxonomy_on_child_instance_directly() -> None:
    """
    is_competency_taxonomy() also returns True when handed a CompetencyTaxonomy
    instance directly, not just a parent Taxonomy fetched from the DB.
    """
    competency = CompetencyTaxonomy.objects.create(name="Nursing", export_id="nursing-v1")
    assert is_competency_taxonomy(competency) is True


def test_is_competency_taxonomy_on_unsaved_instance() -> None:
    """
    is_competency_taxonomy() returns False for an unsaved Taxonomy, rather than raising.
    """
    # pk is None, so the reverse one-to-one descriptor short-circuits and raises
    # RelatedObjectDoesNotExist, which Django defines as an AttributeError subclass
    # precisely so hasattr() catches it here instead of propagating.
    unsaved = Taxonomy(name="Unsaved", export_id="unsaved-v1")
    assert is_competency_taxonomy(unsaved) is False


def test_select_competency_taxonomies_avoids_n_plus_1(django_assert_num_queries) -> None:
    """
    select_competency_taxonomies() joins the CompetencyTaxonomy row in, so checking
    is_competency_taxonomy() on every row in the queryset costs one query, not N+1.
    """
    competency1 = CompetencyTaxonomy.objects.create(name="Nursing", export_id="nursing-v1")
    competency2 = CompetencyTaxonomy.objects.create(name="Welding", export_id="welding-v1")
    plain = Taxonomy.objects.create(name="Plain Tags", export_id="plain-v1")
    # Scoped to just these three: unfiltered Taxonomy.objects.all() would also pick up
    # any taxonomies seeded outside this test, which would make the True/False counts
    # below depend on incidental fixture data.
    taxonomies = Taxonomy.objects.filter(pk__in=[competency1.pk, competency2.pk, plain.pk])

    with django_assert_num_queries(1):
        results = [is_competency_taxonomy(t) for t in select_competency_taxonomies(taxonomies)]

    assert results.count(True) == 2
    assert results.count(False) == 1


def test_get_competency_rule_profiles_returns_the_seeded_default(
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """get_competency_rule_profiles() returns the system default an instance starts with."""
    assert list(get_competency_rule_profiles()) == [default_rule_profile]


def test_get_competency_rule_profiles_excludes_archived(
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
) -> None:
    """get_competency_rule_profiles() leaves retired profiles out."""
    archived = CompetencyRuleProfile.objects.create(
        rule_type=RuleType.GRADE,
        rule_payload=GRADE_PAYLOAD,
        competency_taxonomy=competency_taxonomy,
        archived=True,
    )

    profiles = list(get_competency_rule_profiles())

    assert archived not in profiles
    assert profiles == [default_rule_profile]


def test_get_competency_rule_profiles_is_ordered_by_id(
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
    organization: Organization,
) -> None:
    """
    get_competency_rule_profiles() returns profiles in ascending id order, every time.

    Without a deterministic order, paginating the collection would repeat and skip rows.
    """
    taxonomy_scoped = CompetencyRuleProfile.objects.create(
        rule_type=RuleType.GRADE, rule_payload=GRADE_PAYLOAD, competency_taxonomy=competency_taxonomy
    )
    organization_scoped = CompetencyRuleProfile.objects.create(
        rule_type=RuleType.GRADE, rule_payload=GRADE_PAYLOAD, organization=organization
    )

    expected = [default_rule_profile, taxonomy_scoped, organization_scoped]
    assert list(get_competency_rule_profiles()) == expected
    assert list(get_competency_rule_profiles()) == expected


# ==============================================================================================
# create_leaf_group
# ==============================================================================================


def test_create_leaf_group_builds_the_full_hierarchy_on_first_use(
    tag: Tag, course_run: CourseRun
) -> None:
    """A first call with no existing groups creates a root, a course-level group, and a leaf."""
    leaf = create_leaf_group(tag, course_run)

    course_level = leaf.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None
    assert root.parent is None
    assert root.tag_id == tag.id
    assert root.course_id is None
    assert root.name == f"{tag.value} (root)"
    assert course_level.tag_id == tag.id
    assert course_level.course_id == course_run.id
    assert course_level.name == f"{tag.value} — {course_run.title}"
    assert leaf.tag_id == tag.id
    assert leaf.course_id is None
    assert leaf.logic_operator == LogicOperator.OR


def test_create_leaf_group_passes_through_an_explicit_logic_operator(
    tag: Tag, course_run: CourseRun
) -> None:
    """A supplied logic_operator is stored on the leaf as-is, not defaulted to OR."""
    leaf = create_leaf_group(tag, course_run, logic_operator=LogicOperator.AND)
    assert leaf.logic_operator == LogicOperator.AND


def test_create_leaf_group_reuses_the_root_and_course_level_group_on_a_second_call(
    tag: Tag, course_run: CourseRun
) -> None:
    """A second call for the same tag and course reuses the root and course-level group."""
    first_leaf = create_leaf_group(tag, course_run)
    second_leaf = create_leaf_group(tag, course_run)

    assert first_leaf.id != second_leaf.id
    assert first_leaf.parent_id == second_leaf.parent_id
    assert first_leaf.parent is not None
    assert second_leaf.parent is not None
    assert first_leaf.parent.parent_id == second_leaf.parent.parent_id
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, parent__isnull=True).count() == 1
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, course=course_run).count() == 1


def test_create_leaf_group_creates_a_new_course_level_group_for_a_different_course(
    tag: Tag, course_run: CourseRun, organization: Organization
) -> None:
    """A second course under the same tag gets its own course-level group, but shares the root."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")

    first_leaf = create_leaf_group(tag, course_run)
    second_leaf = create_leaf_group(tag, other_course_run)

    assert first_leaf.parent is not None
    assert second_leaf.parent is not None
    assert first_leaf.parent.parent_id == second_leaf.parent.parent_id
    assert first_leaf.parent_id != second_leaf.parent_id
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, parent__isnull=True).count() == 1
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, course__isnull=False).count() == 2


def test_create_leaf_group_reuses_a_root_a_concurrent_request_already_committed(
    tag: Tag, course_run: CourseRun
) -> None:
    """
    A root created out-of-band (standing in for a concurrent request's winning commit) is reused.

    This exercises the same fallback ``get_or_create()`` relies on for real concurrent callers:
    the ``oel_cbe_criteria_group_one_root_per_tag`` constraint means a second INSERT attempt for
    the same tag's root fails, and ``get_or_create()`` falls back to fetching the row that is
    already there instead of raising.
    """
    already_committed_root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None, name="pre-existing root")

    leaf = create_leaf_group(tag, course_run)

    assert leaf.parent is not None
    assert leaf.parent.parent_id == already_committed_root.id
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, parent__isnull=True).count() == 1


def test_create_leaf_group_reuses_a_course_level_group_a_concurrent_request_already_committed(
    tag: Tag, course_run: CourseRun
) -> None:
    """The course-level group half of the same race-safety guarantee, isolated from the root."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    already_committed_course_level = CompetencyCriteriaGroup.objects.create(
        tag=tag, course=course_run, parent=root, name="pre-existing course-level group",
    )

    leaf = create_leaf_group(tag, course_run)

    assert leaf.parent_id == already_committed_course_level.id
    assert CompetencyCriteriaGroup.objects.filter(tag=tag, course=course_run).count() == 1


def test_root_group_unique_constraint_rejects_a_second_root_for_the_same_tag(tag: Tag) -> None:
    """
    The DB constraint create_leaf_group's get_or_create() relies on actually exists.

    Proven directly (bypassing get_or_create) so the race-safety tests above aren't the only
    thing standing between this suite and a silently-dropped migration.
    """
    CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    with pytest.raises(IntegrityError):
        CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)


def test_course_level_group_unique_constraint_rejects_a_second_group_for_the_same_tag_and_course(
    tag: Tag, course_run: CourseRun
) -> None:
    """The course-level half of the same constraint-existence proof."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    with pytest.raises(IntegrityError):
        CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)


# ==============================================================================================
# resolve_supplied_leaf_group
# ==============================================================================================


def test_resolve_supplied_leaf_group_returns_the_leaf_when_it_is_valid(tag: Tag, course_run: CourseRun) -> None:
    """A group_id that names a genuine, matching leaf is returned unchanged."""
    leaf = create_leaf_group(tag, course_run)
    assert resolve_supplied_leaf_group(leaf.id, tag, course_run) == leaf


def test_resolve_supplied_leaf_group_404s_when_the_group_does_not_exist(tag: Tag, course_run: CourseRun) -> None:
    """An unknown group_id 404s rather than raising an unhandled 500."""
    with pytest.raises(Http404):
        resolve_supplied_leaf_group(999999, tag, course_run)


def test_resolve_supplied_leaf_group_rejects_a_root_group_as_not_a_leaf(tag: Tag, course_run: CourseRun) -> None:
    """A root group (no parent) is not a usable leaf."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    with pytest.raises(ValidationError, match="group_id"):
        resolve_supplied_leaf_group(root.id, tag, course_run)


def test_resolve_supplied_leaf_group_rejects_a_course_level_group_as_not_a_leaf(
    tag: Tag, course_run: CourseRun
) -> None:
    """A course-level group (has its own course) is not a usable leaf either."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag, parent=None)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    with pytest.raises(ValidationError, match="group_id"):
        resolve_supplied_leaf_group(course_level.id, tag, course_run)


def test_resolve_supplied_leaf_group_rejects_a_group_for_a_different_tag(
    tag: Tag, course_run: CourseRun, competency_taxonomy: CompetencyTaxonomy
) -> None:
    """A leaf that belongs to a different competency tag is rejected."""
    other_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Other Competency")
    leaf = create_leaf_group(other_tag, course_run)
    with pytest.raises(ValidationError, match="group_id"):
        resolve_supplied_leaf_group(leaf.id, tag, course_run)


def test_resolve_supplied_leaf_group_rejects_a_group_for_a_different_course(
    tag: Tag, course_run: CourseRun, organization: Organization
) -> None:
    """A leaf whose course-level parent belongs to a different course is rejected."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf = create_leaf_group(tag, other_course_run)
    with pytest.raises(ValidationError, match="group_id"):
        resolve_supplied_leaf_group(leaf.id, tag, course_run)


# ==============================================================================================
# associate_competency_criterion
# ==============================================================================================


def test_associate_competency_criterion_creates_the_hierarchy_and_criterion_when_nothing_exists(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile
) -> None:
    """The happy path: no group_id, no existing groups, no rule fields supplied."""
    object_id = usage_key(course_run, "p1")

    criterion = associate_competency_criterion(tag_id=tag.id, object_id=object_id)

    assert criterion.group.tag_id == tag.id
    assert criterion.group.parent is not None
    assert criterion.group.parent.course_id == course_run.id
    root = criterion.group.parent.parent
    assert root is not None
    assert root.parent is None
    assert root.tag_id == tag.id
    assert criterion.object_tag.object_id == object_id
    assert criterion.object_tag.tag_id == tag.id
    assert criterion.rule_profile_id == default_rule_profile.id
    assert criterion.rule_type_override is None
    assert criterion.rule_payload_override is None


def test_associate_competency_criterion_uses_a_supplied_group_id(tag: Tag, course_run: CourseRun) -> None:
    """A caller-supplied group_id is used as-is, rather than deriving or creating a new leaf."""
    leaf = create_leaf_group(tag, course_run)
    object_id = usage_key(course_run, "p1")

    criterion = associate_competency_criterion(tag_id=tag.id, object_id=object_id, group_id=leaf.id)

    assert criterion.group_id == leaf.id


def test_associate_competency_criterion_rejects_group_id_and_logic_operator_together(
    tag: Tag, course_run: CourseRun
) -> None:
    """Supplying both group_id and logic_operator is rejected before anything is created."""
    leaf = create_leaf_group(tag, course_run)
    object_id = usage_key(course_run, "p1")

    with pytest.raises(ValidationError, match="logic_operator"):
        associate_competency_criterion(
            tag_id=tag.id, object_id=object_id, group_id=leaf.id, logic_operator=LogicOperator.AND,
        )


def test_associate_competency_criterion_rejects_a_duplicate_tag_object_association(
    tag: Tag, course_run: CourseRun
) -> None:
    """Via the derive-or-create path (no group_id), a second criterion for the same pair is rejected."""
    object_id = usage_key(course_run, "p1")
    associate_competency_criterion(tag_id=tag.id, object_id=object_id)

    with pytest.raises(ValidationError, match="object_id"):
        associate_competency_criterion(tag_id=tag.id, object_id=object_id)


def test_associate_competency_criterion_rejects_re_targeting_the_same_group(
    tag: Tag, course_run: CourseRun
) -> None:
    """Re-supplying the exact group a (tag_id, object_id) pair is already associated with is rejected."""
    object_id = usage_key(course_run, "p1")
    first = associate_competency_criterion(tag_id=tag.id, object_id=object_id)

    with pytest.raises(ValidationError, match="group_id"):
        associate_competency_criterion(tag_id=tag.id, object_id=object_id, group_id=first.group_id)


def test_associate_competency_criterion_allows_a_different_explicit_group_for_the_same_pair(
    tag: Tag, course_run: CourseRun
) -> None:
    """
    A different, explicitly supplied existing group creates a second, deliberate association --
    not a duplicate. ADR-0002's own worked example requires the same tag/object association to
    be able to participate in more than one CompetencyCriteriaGroup.
    """
    object_id = usage_key(course_run, "p1")
    first = associate_competency_criterion(tag_id=tag.id, object_id=object_id)
    other_leaf = create_leaf_group(tag, course_run)

    second = associate_competency_criterion(tag_id=tag.id, object_id=object_id, group_id=other_leaf.id)

    assert second.object_tag_id == first.object_tag_id
    assert second.group_id == other_leaf.id
    assert CompetencyCriterion.objects.filter(object_tag_id=first.object_tag_id).count() == 2


def test_associate_competency_criterion_404s_for_an_unknown_tag_id(course_run: CourseRun) -> None:
    """An unresolvable tag_id 404s before anything else is validated."""
    object_id = usage_key(course_run, "p1")
    with pytest.raises(Http404):
        associate_competency_criterion(tag_id=999999, object_id=object_id)


def test_associate_competency_criterion_rejects_a_malformed_object_id(tag: Tag) -> None:
    """An object_id that isn't a parseable usage key is a 400, keyed by object_id."""
    with pytest.raises(ValidationError, match="object_id"):
        associate_competency_criterion(tag_id=tag.id, object_id="not-a-usage-key")


def test_associate_competency_criterion_rejects_an_unresolvable_course(tag: Tag) -> None:
    """A well-formed usage key whose course has no matching CourseRun is a 400, keyed by object_id."""
    object_id = "block-v1:NoOrg+NoCourse+NoRun+type@sequential+block@p1"
    with pytest.raises(ValidationError, match="object_id"):
        associate_competency_criterion(tag_id=tag.id, object_id=object_id)


def test_associate_competency_criterion_rolls_back_newly_created_groups_when_criterion_creation_fails(
    monkeypatch: pytest.MonkeyPatch, tag: Tag, course_run: CourseRun
) -> None:
    """
    A downstream failure during criterion creation rolls back any group this call created.

    ADR-0002 forbids persisting empty groups, so a failed attempt via the derive-or-create path
    must not leave a root, course-level, or leaf group behind. Simulated here by making the final
    CompetencyCriterion.objects.create() call itself fail -- standing in for any failure at that
    point, including the containment check #666 will add right before it.
    """
    def _reject(**_kwargs) -> None:
        raise ValidationError({"object_id": "rejected for this test"})

    monkeypatch.setattr(cbe_api.CompetencyCriterion.objects, "create", _reject)
    object_id = usage_key(course_run, "p1")

    with pytest.raises(ValidationError):
        associate_competency_criterion(tag_id=tag.id, object_id=object_id)

    assert not CompetencyCriteriaGroup.objects.filter(tag=tag).exists()


# ==============================================================================================
# get_competency_criteria_tree
# ==============================================================================================


def test_get_competency_criteria_tree_is_empty_when_no_groups_exist_yet(tag: Tag) -> None:
    """A competency with no CompetencyCriteriaGroup rows at all returns two empty lists."""
    tree = get_competency_criteria_tree(tag.id)

    assert not tree.groups
    assert not tree.criteria


def test_get_competency_criteria_tree_returns_the_full_hierarchy_and_its_leaf_criterion(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A root, its course-level child, and a leaf under it, plus the leaf's one criterion, all come back."""
    leaf = create_leaf_group(tag, course_run)
    course_level = leaf.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None
    object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    criterion = CompetencyCriterion.objects.create(
        group=leaf, object_tag=object_tag, rule_profile=default_rule_profile,
    )

    tree = get_competency_criteria_tree(tag.id)

    returned_groups_by_id = {group.id: group for group in tree.groups}
    assert set(returned_groups_by_id) == {root.id, course_level.id, leaf.id}
    assert returned_groups_by_id[root.id].parent_id is None
    assert returned_groups_by_id[root.id].course_id is None
    assert returned_groups_by_id[course_level.id].parent_id == root.id
    assert returned_groups_by_id[leaf.id].parent_id == course_level.id
    assert [c.id for c in tree.criteria] == [criterion.id]
    assert tree.criteria[0].group_id == leaf.id


def test_get_competency_criteria_tree_excludes_an_archived_group_and_its_archived_criterion(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """An archived leaf group and an archived criterion under it are both left out."""
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
    archived_criterion = CompetencyCriterion.objects.create(
        group=archived_leaf, object_tag=archived_object_tag, rule_profile=default_rule_profile, archived=True,
    )

    tree = get_competency_criteria_tree(tag.id)

    returned_group_ids = {group.id for group in tree.groups}
    returned_criterion_ids = {criterion.id for criterion in tree.criteria}
    assert live_leaf.id in returned_group_ids
    assert archived_leaf.id not in returned_group_ids
    assert live_criterion.id in returned_criterion_ids
    assert archived_criterion.id not in returned_criterion_ids


def test_get_competency_criteria_tree_combines_groups_and_criteria_from_every_course_unscoped(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Two course-level groups under one root, each with its own leaf and criterion, both come back together."""
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
    root1 = course_level1.parent
    root2 = course_level2.parent
    assert root1 is not None
    assert root2 is not None

    tree = get_competency_criteria_tree(tag.id)

    assert course_level1.id != course_level2.id, "the two leaves should sit under different course-level groups"
    assert root1.id == root2.id, "both course-level groups should share one root"
    assert {group.id for group in tree.groups} == {
        root1.id, course_level1.id, leaf1.id, course_level2.id, leaf2.id,
    }
    assert {criterion.id for criterion in tree.criteria} == {criterion1.id, criterion2.id}


def test_get_competency_criteria_tree_costs_a_bounded_number_of_queries(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile, django_assert_num_queries,
) -> None:
    """
    Reading several groups and criteria costs a fixed two queries, not one per row.

    Pins the select_related("course")/select_related("object_tag") calls in
    get_competency_criteria_tree(): dropping either would still pass every other test in this
    module (the rows returned would be identical), since select_related only changes how many
    queries fetch them, not which rows come back.
    """
    leaf = create_leaf_group(tag, course_run)
    for i in range(3):
        object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, f"p{i}"), taxonomy=tag.taxonomy, tag=tag)
        CompetencyCriterion.objects.create(group=leaf, object_tag=object_tag, rule_profile=default_rule_profile)

    with django_assert_num_queries(2):
        tree = get_competency_criteria_tree(tag.id)
        for group in tree.groups:
            _ = group.course  # accessing the select_related'd relation must not add a query
        for criterion in tree.criteria:
            _ = criterion.object_tag


# ==============================================================================================
# update_competency_criteria_group (#760)
# ==============================================================================================

CHANGE_OBJECTTAG_OBJECTID = "oel_tagging.change_objecttag_objectid"


@pytest.fixture(name="checked_object_ids", autouse=True)
def _checked_object_ids() -> Iterator[list[str]]:
    """
    Let any user tag any object, recording each object_id checked; restore the real rule afterwards.

    openedx_tagging denies change_objecttag_objectid to everyone and leaves the real Studio-role
    check to openedx-platform, so these tests substitute a permissive one.
    """
    original = rule_permissions[CHANGE_OBJECTTAG_OBJECTID]
    checked: list[str] = []

    def _predicate(_user: UserType, object_id: str) -> bool:
        checked.append(object_id)
        return True

    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, _predicate)
    yield checked
    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, original)


@pytest.fixture(name="leaf")
def _leaf(tag: Tag, course_run: CourseRun) -> CompetencyCriteriaGroup:
    """A leaf group under `tag` and `course_run`. Only a leaf group holds criteria."""
    return create_leaf_group(tag, course_run)


def course_level_of(leaf: CompetencyCriteriaGroup) -> CompetencyCriteriaGroup:
    """Return the course-level group `leaf` sits under."""
    assert leaf.parent is not None
    return leaf.parent


def root_of(leaf: CompetencyCriteriaGroup) -> CompetencyCriteriaGroup:
    """Return the root group of `leaf`'s tree."""
    root = course_level_of(leaf).parent
    assert root is not None
    return root


def make_criterion(leaf: CompetencyCriteriaGroup, block_id: str, **rule) -> CompetencyCriterion:
    """Create a criterion on `leaf` for a fresh object in its course, with `rule` as its rule columns."""
    assert leaf.parent is not None and leaf.parent.course is not None
    object_tag = ObjectTag.objects.create(
        object_id=usage_key(leaf.parent.course, block_id), taxonomy=leaf.tag.taxonomy, tag=leaf.tag,
    )
    return CompetencyCriterion.objects.create(group=leaf, object_tag=object_tag, **rule)


def with_operator(group: CompetencyCriteriaGroup, logic_operator: str | None) -> CompetencyCriteriaGroup:
    """Store `logic_operator` on `group` directly, as the starting state for a test."""
    group.logic_operator = logic_operator
    group.save()
    return group


def group_history(group: CompetencyCriteriaGroup):
    """
    Return `group`'s history rows, newest first.

    Looked up through the app registry, as the model tests do, because simple_history's `.history`
    descriptor has no type stubs.
    """
    historical_group = apps.get_model("openedx_learning", "HistoricalCompetencyCriteriaGroup")
    return historical_group.objects.filter(id=group.pk).order_by("-history_date", "-history_id")


def stored(group: CompetencyCriteriaGroup) -> tuple[dict, int]:
    """Return `group`'s columns as stored and its history length, to show what a call changed."""
    return model_to_dict(CompetencyCriteriaGroup.objects.get(pk=group.pk)), group_history(group).count()


@pytest.mark.parametrize("level", ["course-level", "leaf"])
@pytest.mark.parametrize(
    "before, after", [(LogicOperator.AND, LogicOperator.OR), (LogicOperator.OR, LogicOperator.AND)],
)
def test_update_switches_how_a_group_combines_its_children(
    level: str, before: str, after: str, leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """Either operator replaces the other, on a course-level group and on a leaf alike."""
    group = with_operator(course_level_of(leaf) if level == "course-level" else leaf, before)

    result = update_competency_criteria_group(group.id, logic_operator=after, user=user)

    assert result.logic_operator == after
    group.refresh_from_db()
    assert group.logic_operator == after


def test_update_writes_one_history_row_attributed_to_the_caller(
    leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """A real change is one save, so one history row, attributed without any request middleware."""
    rows_before = group_history(leaf).count()

    update_competency_criteria_group(leaf.id, logic_operator=LogicOperator.AND, user=user)

    assert group_history(leaf).count() == rows_before + 1
    latest = group_history(leaf).first()
    assert latest.history_user == user
    assert latest.logic_operator == LogicOperator.AND


def test_update_that_changes_nothing_saves_nothing(leaf: CompetencyCriteriaGroup, user: UserType) -> None:
    """Asking for the operator a group already has succeeds without adding a history row."""
    assert leaf.logic_operator == LogicOperator.OR
    before = stored(leaf)

    result = update_competency_criteria_group(leaf.id, logic_operator=LogicOperator.OR, user=user)

    assert result.logic_operator == LogicOperator.OR
    assert stored(leaf) == before


def test_update_sets_an_unset_operator_even_to_or(leaf: CompetencyCriteriaGroup, user: UserType) -> None:
    """
    A null operator given OR is a real change.

    Null is evaluated like OR, but it is stored differently, and the caller asked for an explicit value.
    """
    course_level = course_level_of(leaf)
    assert course_level.logic_operator is None
    rows_before = group_history(course_level).count()

    update_competency_criteria_group(course_level.id, logic_operator=LogicOperator.OR, user=user)

    course_level.refresh_from_db()
    assert course_level.logic_operator == LogicOperator.OR
    assert group_history(course_level).count() == rows_before + 1


def test_update_leaves_everything_else_about_the_group_and_its_branch_unchanged(
    leaf: CompetencyCriteriaGroup, user: UserType, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Only the operator changes: not the group's name, ordering, competency, course, or parent, nor anything below."""
    course_level = with_operator(course_level_of(leaf), LogicOperator.AND)
    # A non-default ordering, so that a reset to the default would show.
    course_level.ordering = 3
    course_level.save()
    criterion = make_criterion(leaf, "p1", rule_profile=default_rule_profile)
    group_before, _ = stored(course_level)
    leaf_before = stored(leaf)
    criterion_before = model_to_dict(criterion)

    update_competency_criteria_group(course_level.id, logic_operator=LogicOperator.OR, user=user)

    group_after, _ = stored(course_level)
    assert group_after == {**group_before, "logic_operator": LogicOperator.OR}
    assert stored(leaf) == leaf_before
    assert model_to_dict(CompetencyCriterion.objects.get(pk=criterion.pk)) == criterion_before


@pytest.mark.parametrize("children", [0, 1])
def test_update_accepts_a_group_with_fewer_than_two_children(
    children: int, leaf: CompetencyCriteriaGroup, user: UserType, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A group's child count is creation's and deletion's concern, so it never blocks this edit."""
    for n in range(children):
        make_criterion(leaf, f"p{n}", rule_profile=default_rule_profile)

    result = update_competency_criteria_group(leaf.id, logic_operator=LogicOperator.AND, user=user)

    assert result.logic_operator == LogicOperator.AND


@pytest.mark.parametrize("logic_operator", [None, "", "XOR", "and", 1])
def test_update_rejects_an_operator_the_group_cannot_store(
    logic_operator, leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """Anything but AND or OR is refused, keyed by the field, and the group keeps its operator."""
    before = stored(leaf)

    with pytest.raises(ValidationError) as exc_info:
        update_competency_criteria_group(leaf.id, logic_operator=logic_operator, user=user)

    assert "logic_operator" in exc_info.value.message_dict
    assert stored(leaf) == before


@pytest.mark.parametrize("logic_operator", [LogicOperator.OR, LogicOperator.AND])
def test_update_refuses_an_archived_group_even_when_nothing_would_change(
    logic_operator: str, leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """
    An archived group is refused outright, whatever the request.

    Asking for the operator it already has is refused too. That pins the archived check ahead of the
    no-op check, since a success would suggest the edit was accepted.
    """
    leaf.archived = True
    leaf.save()
    before = stored(leaf)

    with pytest.raises(CompetencyCriteriaGroupArchivedError):
        update_competency_criteria_group(leaf.id, logic_operator=logic_operator, user=user)

    assert stored(leaf) == before


def test_update_rejects_a_root_group_before_checking_permission(
    checked_object_ids: list[str], leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """A root spans every course beneath it, so it is refused before any permission check or write."""
    root = with_operator(root_of(leaf), LogicOperator.AND)
    before = stored(root)

    with pytest.raises(ValidationError, match="root") as exc_info:
        update_competency_criteria_group(root.id, logic_operator=LogicOperator.OR, user=user)

    assert "group_id" in exc_info.value.message_dict
    assert not checked_object_ids
    assert stored(root) == before


def test_update_does_not_mistake_a_course_less_child_of_the_root_for_a_root(
    checked_object_ids: list[str], tag: Tag, leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """
    A group with a parent is no root, even with no course of its own and none above it.

    The model and ADR-0002 allow such a group, but with no course anywhere above it there's nothing to check
    permission against. So it's refused, for that reason and not as a root, before any permission check or write.
    """
    course_less = CompetencyCriteriaGroup.objects.create(
        tag=tag, parent=root_of(leaf), logic_operator=LogicOperator.AND,
    )
    before = stored(course_less)

    with pytest.raises(ValidationError, match="no course") as exc_info:
        update_competency_criteria_group(course_less.id, logic_operator=LogicOperator.OR, user=user)

    assert "root" not in str(exc_info.value)
    assert "group_id" in exc_info.value.message_dict
    assert not checked_object_ids
    assert stored(course_less) == before


def test_update_resolves_the_course_of_a_group_nested_below_a_leaf(
    checked_object_ids: list[str], leaf: CompetencyCriteriaGroup, user: UserType, course_run: CourseRun,
) -> None:
    """ADR-0002 supports deeply nested groups, so one below a leaf takes the course of its nearest ancestor with one."""
    nested = CompetencyCriteriaGroup.objects.create(tag=leaf.tag, parent=leaf, logic_operator=LogicOperator.OR)

    result = update_competency_criteria_group(nested.id, logic_operator=LogicOperator.AND, user=user)

    assert result.logic_operator == LogicOperator.AND
    nested.refresh_from_db()
    assert nested.logic_operator == LogicOperator.AND
    assert checked_object_ids == [str(course_run.course_key)]


def test_update_404s_for_a_group_whose_tag_is_not_on_a_competency_taxonomy(
    checked_object_ids: list[str], course_run: CourseRun, user: UserType,
) -> None:
    """A group under any other kind of taxonomy isn't a competency's group, so it's missing, and nothing changes."""
    plain_tag = Tag.objects.create(taxonomy=Taxonomy.objects.create(name="Plain Tags", export_id="plain-v1"), value="x")
    root = CompetencyCriteriaGroup.objects.create(tag=plain_tag, parent=None)
    # Shaped like any editable group, so only its taxonomy sets it apart.
    course_level = CompetencyCriteriaGroup.objects.create(
        tag=plain_tag, course=course_run, parent=root, logic_operator=LogicOperator.AND,
    )
    before = stored(course_level)

    with pytest.raises(Http404):
        update_competency_criteria_group(course_level.id, logic_operator=LogicOperator.OR, user=user)

    assert not checked_object_ids
    assert stored(course_level) == before


def test_update_404s_for_a_group_that_does_not_exist(user: UserType) -> None:
    """An unknown group id is a missing resource."""
    with pytest.raises(Http404):
        update_competency_criteria_group(999999, logic_operator=LogicOperator.AND, user=user)


@pytest.mark.parametrize("level", ["course-level", "leaf"])
def test_update_checks_permission_against_the_groups_course(  # pylint: disable=too-many-positional-arguments
    level: str,
    checked_object_ids: list[str],
    leaf: CompetencyCriteriaGroup,
    user: UserType,
    course_run: CourseRun,
    organization: Organization,
) -> None:
    """A course-level group's own course is checked, and a leaf's is resolved through its course-level parent."""
    # Another course under the same root, so checking any course but the group's own would show.
    create_leaf_group(leaf.tag, make_course_run(organization, "Python200", "Fall2026"))
    group = course_level_of(leaf) if level == "course-level" else leaf

    update_competency_criteria_group(group.id, logic_operator=LogicOperator.AND, user=user)

    assert checked_object_ids == [str(course_run.course_key)]


@pytest.mark.parametrize("lacks", ["course write access", "taxonomy view access"])
def test_update_refuses_a_user_without_permission_changing_nothing(
    lacks: str, leaf: CompetencyCriteriaGroup, user: UserType, competency_taxonomy: CompetencyTaxonomy,
) -> None:
    """A user failing oel_tagging.can_tag_object's taxonomy check or object_id check is refused, and nothing changes."""
    if lacks == "course write access":
        rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, lambda _user, _object_id: False)
    else:
        # can_tag_object's taxonomy check fails on a disabled taxonomy for anyone but a superuser.
        competency_taxonomy.enabled = False
        competency_taxonomy.save()
    before = stored(leaf)

    with pytest.raises(PermissionDenied):
        update_competency_criteria_group(leaf.id, logic_operator=LogicOperator.AND, user=user)

    assert stored(leaf) == before


@pytest.mark.parametrize("path", ["changed", "no-op", "archived", "root", "unsupported operator"])
def test_update_never_reads_or_writes_learner_status(
    path: str, leaf: CompetencyCriteriaGroup, user: UserType,
) -> None:
    """
    No path issues a query against a StudentCompetency*Status table.

    Whether learners have progress under a group plays no part in this edit; warning about that is #723's job.
    """
    group, logic_operator = leaf, str(LogicOperator.AND)
    if path == "no-op":
        logic_operator = LogicOperator.OR
    elif path == "archived":
        leaf.archived = True
        leaf.save()
    elif path == "root":
        group = root_of(leaf)
    elif path == "unsupported operator":
        logic_operator = "XOR"

    with CaptureQueriesContext(connection) as queries:
        try:
            update_competency_criteria_group(group.id, logic_operator=logic_operator, user=user)
        except (ValidationError, CompetencyCriteriaGroupArchivedError):
            pass

    assert not [q for q in queries.captured_queries if "studentcompetency" in q["sql"].lower()]
