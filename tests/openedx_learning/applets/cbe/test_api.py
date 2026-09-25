"""
Tests for the CBE public API surface (openedx_learning.api).
"""
import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.http import Http404
from opaque_keys.edx.keys import CourseKey
from organizations.models import Organization

from openedx_catalog.models import CatalogCourse, CourseRun
from openedx_learning.api import (
    associate_competency_criterion,
    create_leaf_group,
    get_competency_criteria_tree,
    get_competency_rule_profiles,
    is_competency_taxonomy,
    resolve_supplied_leaf_group,
    select_competency_taxonomies,
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
    assert tree.criteria_count == 1
    assert tree.total_criteria_count == 1


def test_get_competency_criteria_tree_excludes_archived_rows_when_scoped_to_their_own_course(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    An archived leaf group and an archived criterion under it stay excluded even when the
    request is scoped to exactly the course run they belong to, not just when unscoped.
    """
    assert course_run.course_key is not None
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

    tree = get_competency_criteria_tree(tag.id, [course_run.course_key])

    returned_group_ids = {group.id for group in tree.groups}
    returned_criterion_ids = {criterion.id for criterion in tree.criteria}
    assert live_leaf.id in returned_group_ids
    assert archived_leaf.id not in returned_group_ids
    assert live_criterion.id in returned_criterion_ids
    assert archived_criterion.id not in returned_criterion_ids
    assert tree.criteria_count == 1
    assert tree.total_criteria_count == 1


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
    Reading several groups and criteria costs a fixed three queries, not one per row.

    Pins the select_related("course")/select_related("object_tag") calls in
    get_competency_criteria_tree(): dropping either would still pass every other test in this
    module (the rows returned would be identical), since select_related only changes how many
    queries fetch them, not which rows come back. The third query is the always-run
    total_criteria_count count, fixed regardless of how many groups/criteria exist.
    """
    leaf = create_leaf_group(tag, course_run)
    for i in range(3):
        object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, f"p{i}"), taxonomy=tag.taxonomy, tag=tag)
        CompetencyCriterion.objects.create(group=leaf, object_tag=object_tag, rule_profile=default_rule_profile)

    with django_assert_num_queries(3):
        tree = get_competency_criteria_tree(tag.id)
        for group in tree.groups:
            _ = group.course  # accessing the select_related'd relation must not add a query
        for criterion in tree.criteria:
            _ = criterion.object_tag


# ==============================================================================================
# get_competency_criteria_tree: course_keys scoping
# ==============================================================================================


def test_get_competency_criteria_tree_course_keys_none_matches_unfiltered_behavior(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """course_keys=None is unfiltered, identical to omitting it, and criteria_count equals total_criteria_count."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf1 = create_leaf_group(tag, course_run)
    leaf2 = create_leaf_group(tag, other_course_run)
    object_tag1 = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    object_tag2 = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    CompetencyCriterion.objects.create(group=leaf1, object_tag=object_tag1, rule_profile=default_rule_profile)
    CompetencyCriterion.objects.create(group=leaf2, object_tag=object_tag2, rule_profile=default_rule_profile)

    unfiltered = get_competency_criteria_tree(tag.id)
    explicit_none = get_competency_criteria_tree(tag.id, None)

    assert {g.id for g in explicit_none.groups} == {g.id for g in unfiltered.groups}
    assert {c.id for c in explicit_none.criteria} == {c.id for c in unfiltered.criteria}
    assert explicit_none.criteria_count == len(explicit_none.criteria) == 2
    assert explicit_none.criteria_count == explicit_none.total_criteria_count


def test_get_competency_criteria_tree_course_keys_empty_list_returns_only_the_root(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """course_keys=[] scopes to the instance-wide subtree only: the root, no course-level or leaf groups."""
    leaf = create_leaf_group(tag, course_run)
    course_level = leaf.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None
    object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    CompetencyCriterion.objects.create(group=leaf, object_tag=object_tag, rule_profile=default_rule_profile)

    tree = get_competency_criteria_tree(tag.id, [])

    assert {g.id for g in tree.groups} == {root.id}
    assert not tree.criteria
    assert tree.criteria_count == 0
    assert tree.total_criteria_count == 1


def test_get_competency_criteria_tree_course_keys_scoped_to_one_course(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Scoping to course A's key returns only A's subtree and criteria, nothing from course B."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf_a = create_leaf_group(tag, course_run)
    leaf_b = create_leaf_group(tag, other_course_run)
    object_tag_a = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    object_tag_b = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    criterion_a = CompetencyCriterion.objects.create(
        group=leaf_a, object_tag=object_tag_a, rule_profile=default_rule_profile,
    )
    CompetencyCriterion.objects.create(group=leaf_b, object_tag=object_tag_b, rule_profile=default_rule_profile)
    course_level_a = leaf_a.parent
    assert course_level_a is not None
    root = course_level_a.parent
    assert root is not None
    assert course_run.course_key is not None

    tree = get_competency_criteria_tree(tag.id, [course_run.course_key])

    assert {g.id for g in tree.groups} == {root.id, course_level_a.id, leaf_a.id}
    assert [c.id for c in tree.criteria] == [criterion_a.id]
    assert tree.criteria_count == 1
    assert tree.total_criteria_count == 2


def test_get_competency_criteria_tree_course_keys_preserve_request_order(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Requesting [B, A] sorts B's groups/criteria before A's; requesting [A, B] reverses the order."""
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf_a = create_leaf_group(tag, course_run)
    leaf_b = create_leaf_group(tag, other_course_run)
    object_tag_a = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    object_tag_b = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    criterion_a = CompetencyCriterion.objects.create(
        group=leaf_a, object_tag=object_tag_a, rule_profile=default_rule_profile,
    )
    criterion_b = CompetencyCriterion.objects.create(
        group=leaf_b, object_tag=object_tag_b, rule_profile=default_rule_profile,
    )
    assert course_run.course_key is not None
    assert other_course_run.course_key is not None
    a_key = course_run.course_key
    b_key = other_course_run.course_key
    a_group_ids = {leaf_a.id, leaf_a.parent_id}
    b_group_ids = {leaf_b.id, leaf_b.parent_id}

    tree_b_first = get_competency_criteria_tree(tag.id, [b_key, a_key])
    b_positions = [i for i, g in enumerate(tree_b_first.groups) if g.id in b_group_ids]
    a_positions = [i for i, g in enumerate(tree_b_first.groups) if g.id in a_group_ids]
    assert max(b_positions) < min(a_positions)
    assert [c.id for c in tree_b_first.criteria] == [criterion_b.id, criterion_a.id]

    tree_a_first = get_competency_criteria_tree(tag.id, [a_key, b_key])
    a_positions2 = [i for i, g in enumerate(tree_a_first.groups) if g.id in a_group_ids]
    b_positions2 = [i for i, g in enumerate(tree_a_first.groups) if g.id in b_group_ids]
    assert max(a_positions2) < min(b_positions2)
    assert [c.id for c in tree_a_first.criteria] == [criterion_a.id, criterion_b.id]


def test_get_competency_criteria_tree_course_keys_duplicate_key_is_not_duplicated(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A duplicate key in the request list doesn't duplicate that course's subtree, and its criteria count once."""
    leaf = create_leaf_group(tag, course_run)
    object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    criterion = CompetencyCriterion.objects.create(
        group=leaf, object_tag=object_tag, rule_profile=default_rule_profile,
    )
    assert course_run.course_key is not None
    key = course_run.course_key

    tree = get_competency_criteria_tree(tag.id, [key, key])

    assert len(tree.groups) == len({g.id for g in tree.groups})
    assert [c.id for c in tree.criteria] == [criterion.id]
    assert tree.criteria_count == 1


def test_get_competency_criteria_tree_course_keys_unresolvable_key_is_silently_ignored(
    tag: Tag, course_run: CourseRun,
) -> None:
    """A well-formed course key with no matching CourseRun is dropped: no error, no contribution to the result."""
    leaf = create_leaf_group(tag, course_run)
    course_level = leaf.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None
    unresolvable_key = CourseKey.from_string("course-v1:NoOrg+NoCourse+NoRun")

    tree = get_competency_criteria_tree(tag.id, [unresolvable_key])

    assert {g.id for g in tree.groups} == {root.id}
    assert not tree.criteria
    assert tree.criteria_count == 0


def test_get_competency_criteria_tree_course_keys_scoped_to_a_course_with_nothing(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Criteria exist under courses A and B; scoping to a real course C with nothing under it
    returns zero criteria but still reports the true instance-wide total.
    """
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    empty_course_run = make_course_run(organization, "Python300", "Fall2026")
    leaf_a = create_leaf_group(tag, course_run)
    leaf_b = create_leaf_group(tag, other_course_run)
    object_tag_a = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    object_tag_b = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    CompetencyCriterion.objects.create(group=leaf_a, object_tag=object_tag_a, rule_profile=default_rule_profile)
    CompetencyCriterion.objects.create(group=leaf_b, object_tag=object_tag_b, rule_profile=default_rule_profile)
    assert empty_course_run.course_key is not None

    tree = get_competency_criteria_tree(tag.id, [empty_course_run.course_key])

    assert not tree.criteria
    assert tree.criteria_count == 0
    assert tree.total_criteria_count == 2


def test_get_competency_criteria_tree_ranks_a_leaf_by_its_own_course_even_when_the_parent_is_archived(
    tag: Tag, course_run: CourseRun, organization: Organization, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A non-archived leaf under an archived course-level parent still ranks under its real
    course. The scope filter matches such a leaf via its parent's course_id, but the parent
    itself is excluded from `groups` (archived=False), so ranking must look its course_id up
    directly rather than falling back to "unscoped".
    """
    other_course_run = make_course_run(organization, "Python200", "Fall2026")
    leaf_archived_parent = create_leaf_group(tag, course_run)
    course_level_group = leaf_archived_parent.parent
    assert course_level_group is not None
    course_level_group.archived = True
    course_level_group.save()
    leaf_other = create_leaf_group(tag, other_course_run)
    object_tag_archived_parent = ObjectTag.objects.create(
        object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    object_tag_other = ObjectTag.objects.create(
        object_id=usage_key(other_course_run, "p1"), taxonomy=tag.taxonomy, tag=tag,
    )
    criterion_archived_parent = CompetencyCriterion.objects.create(
        group=leaf_archived_parent, object_tag=object_tag_archived_parent, rule_profile=default_rule_profile,
    )
    criterion_other = CompetencyCriterion.objects.create(
        group=leaf_other, object_tag=object_tag_other, rule_profile=default_rule_profile,
    )
    assert course_run.course_key is not None
    assert other_course_run.course_key is not None

    # other_course_run requested first, so a correct rank sorts criterion_other first. The
    # bug this pins would rank the archived-parent leaf as unscoped (-1), always sorting it
    # first regardless of request order -- wrong here, since its course was requested second.
    tree = get_competency_criteria_tree(tag.id, [other_course_run.course_key, course_run.course_key])

    assert [c.id for c in tree.criteria] == [criterion_other.id, criterion_archived_parent.id]


def test_get_competency_criteria_tree_zero_criteria_anywhere(tag: Tag) -> None:
    """A competency with no criteria at all reports both counts as zero, with or without a course_keys scope."""
    tree_unfiltered = get_competency_criteria_tree(tag.id)
    tree_scoped = get_competency_criteria_tree(tag.id, [])

    assert tree_unfiltered.criteria_count == 0
    assert tree_unfiltered.total_criteria_count == 0
    assert tree_scoped.criteria_count == 0
    assert tree_scoped.total_criteria_count == 0


def test_get_competency_criteria_tree_a_different_tag_never_leaks_into_this_ones_counts(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A second, independent competency tag with its own criteria never contributes to this tag's
    returned rows or counts.

    This test file's fixtures have no modeled "sub-competency" relationship distinct from an
    ordinary Tag; two unrelated tags are the closest analogue available, and isolation here is
    already guaranteed structurally by filtering the group query on tag_id.
    """
    assert tag.taxonomy is not None
    other_tag = Tag.objects.create(taxonomy=tag.taxonomy, value="Other Competency")
    leaf = create_leaf_group(tag, course_run)
    other_leaf = create_leaf_group(other_tag, course_run)
    object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, "p1"), taxonomy=tag.taxonomy, tag=tag)
    other_object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, "p2"), taxonomy=tag.taxonomy, tag=other_tag,
    )
    criterion = CompetencyCriterion.objects.create(
        group=leaf, object_tag=object_tag, rule_profile=default_rule_profile,
    )
    CompetencyCriterion.objects.create(group=other_leaf, object_tag=other_object_tag, rule_profile=default_rule_profile)

    tree = get_competency_criteria_tree(tag.id)

    assert [c.id for c in tree.criteria] == [criterion.id]
    assert tree.criteria_count == 1
    assert tree.total_criteria_count == 1
