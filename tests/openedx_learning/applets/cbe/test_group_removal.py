"""Tests for delete_competency_criteria_group(), which reuses _cascade_from_group() unchanged."""
from datetime import datetime

import pytest
import rules
from django.contrib.auth.models import User as UserType  # pylint: disable=imported-auth-user
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from openedx_catalog.models import CourseRun
from openedx_learning.api import delete_competency_criteria_group
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaStatus,
)
from openedx_tagging.models import ObjectTag, Tag

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _allow_object_level_tagging() -> None:
    """Grant oel_tagging.can_tag_object to any caller, for every test but the permission-denied ones."""
    rules.set_perm("oel_tagging.change_objecttag_objectid", rules.always_true)


def _make_object_tag(competency_taxonomy: CompetencyTaxonomy, tag: Tag, object_id: str) -> ObjectTag:
    """Create an ObjectTag for `object_id`, distinct from the conftest `object_tag` fixture's own row."""
    return ObjectTag.objects.create(object_id=object_id, taxonomy=competency_taxonomy, tag=tag)


def _make_criterion(
    group: CompetencyCriteriaGroup, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile
) -> CompetencyCriterion:
    """Create a CompetencyCriterion under `group`, on the system-default rule profile."""
    return CompetencyCriterion.objects.create(group=group, object_tag=object_tag, rule_profile=default_rule_profile)


# =================================================================================================
# Hard-delete vs. archive, and the reported counts
# =================================================================================================


def test_leaf_group_with_no_descendants_or_criteria_hard_deletes_with_zero_counts(
    user: UserType, tag: Tag, course_run: CourseRun,
) -> None:
    """Deleting an empty, childless target group reports both cascade counts as zero."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)

    result = delete_competency_criteria_group(target.id, user)

    assert result.id == target.id
    assert result.archived is False
    assert result.cascaded_group_count == 0
    assert result.cascaded_criteria_count == 0
    assert not CompetencyCriteriaGroup.objects.filter(id=target.id).exists()


def test_multilevel_cascade_hard_delete_counts_the_descendant_group_and_both_criteria(
    user: UserType, competency_taxonomy: CompetencyTaxonomy, tag: Tag, course_run: CourseRun,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A target group with one child group holding two criteria reports exactly those counts."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    child = CompetencyCriteriaGroup.objects.create(tag=tag, parent=target)
    object_tag_a = _make_object_tag(competency_taxonomy, tag, "obj-a")
    object_tag_b = _make_object_tag(competency_taxonomy, tag, "obj-b")
    criterion_a = _make_criterion(child, object_tag_a, default_rule_profile)
    criterion_b = _make_criterion(child, object_tag_b, default_rule_profile)

    result = delete_competency_criteria_group(target.id, user)

    assert result.archived is False
    assert result.cascaded_group_count == 1
    assert result.cascaded_criteria_count == 2
    assert not CompetencyCriteriaGroup.objects.filter(id=target.id).exists()
    assert not CompetencyCriteriaGroup.objects.filter(id=child.id).exists()
    assert not CompetencyCriterion.objects.filter(id__in=[criterion_a.id, criterion_b.id]).exists()


def test_archive_when_status_exists_at_a_buried_grandchild_criterion(
    user: UserType, *, tag: Tag, course_run: CourseRun, object_tag: ObjectTag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """
    A learner status on a criterion several levels below the target archives the whole subtree
    -- every group and criterion in it -- with the cascade counts still reported accurately.
    """
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    mid = CompetencyCriteriaGroup.objects.create(tag=tag, parent=target)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=mid)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    result = delete_competency_criteria_group(target.id, user)

    assert result.archived is True
    assert result.cascaded_group_count == 2
    assert result.cascaded_criteria_count == 1
    target.refresh_from_db()
    mid.refresh_from_db()
    leaf.refresh_from_db()
    criterion.refresh_from_db()
    assert target.archived is True
    assert mid.archived is True
    assert leaf.archived is True
    assert criterion.archived is True


def test_repeat_call_on_an_already_archived_group_is_idempotent(
    user: UserType, *, tag: Tag, course_run: CourseRun, object_tag: ObjectTag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """A second call against an already-archived subtree re-enters the archive branch without error."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    criterion = _make_criterion(target, object_tag, default_rule_profile)
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    delete_competency_criteria_group(target.id, user)

    result = delete_competency_criteria_group(target.id, user)

    assert result.id == target.id
    assert result.archived is True
    target.refresh_from_db()
    assert target.archived is True


# =================================================================================================
# ObjectTag cleanup
# =================================================================================================


def test_orphaned_object_tag_is_removed_on_hard_delete(
    user: UserType, competency_taxonomy: CompetencyTaxonomy, tag: Tag, course_run: CourseRun,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """An object_tag referenced only by a criterion inside the deleted subtree is untagged too."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    object_tag = _make_object_tag(competency_taxonomy, tag, "obj-only-in-subtree")
    _make_criterion(target, object_tag, default_rule_profile)

    delete_competency_criteria_group(target.id, user)

    assert not ObjectTag.objects.filter(id=object_tag.id).exists()


def test_shared_object_tag_survives_when_a_criterion_outside_the_subtree_still_references_it(
    user: UserType, tag: Tag, course_run: CourseRun, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Two criteria, in two different groups, referencing the same object_tag (a deliberate second
    association per ADR-0002): hard-deleting one group's subtree leaves the other group, its
    criterion, and the shared object_tag, all intact.
    """
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    sibling = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(target, object_tag, default_rule_profile)
    sibling_criterion = _make_criterion(sibling, object_tag, default_rule_profile)

    delete_competency_criteria_group(target.id, user)

    assert ObjectTag.objects.filter(id=object_tag.id).exists()
    assert CompetencyCriterion.objects.filter(id=sibling_criterion.id).exists()


def test_object_tag_is_untouched_on_archive(
    user: UserType, *, tag: Tag, course_run: CourseRun, object_tag: ObjectTag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """Archiving a subtree never calls tag_object(): the object_tag row is left exactly as it was."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    criterion = _make_criterion(target, object_tag, default_rule_profile)
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    result = delete_competency_criteria_group(target.id, user)

    assert result.archived is True
    assert ObjectTag.objects.filter(id=object_tag.id).exists()


# =================================================================================================
# The ancestor cascade, above the target
# =================================================================================================


def test_ancestor_cascade_collapses_the_chain_on_hard_delete(
    user: UserType, tag: Tag, course_run: CourseRun, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Hard-deleting a subtree that was its course-level ancestor's only child collapses that
    ancestor and the root above it too, via _cascade_from_group().
    """
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(target, object_tag, default_rule_profile)

    delete_competency_criteria_group(target.id, user)

    assert not CompetencyCriteriaGroup.objects.filter(id=target.id).exists()
    assert not CompetencyCriteriaGroup.objects.filter(id=course_level.id).exists()
    assert not CompetencyCriteriaGroup.objects.filter(id=root.id).exists()


def test_ancestor_cascade_archives_the_chain_when_the_target_carries_status(
    user: UserType, *, tag: Tag, course_run: CourseRun, object_tag: ObjectTag,
    default_rule_profile: CompetencyRuleProfile, now: datetime,
) -> None:
    """Archiving a subtree that was its ancestor's only live child archives that ancestor too, up to the root."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(target, object_tag, default_rule_profile)
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    delete_competency_criteria_group(target.id, user)

    course_level.refresh_from_db()
    root.refresh_from_db()
    assert course_level.archived is True
    assert root.archived is True
    assert CompetencyCriteriaGroup.objects.filter(id=course_level.id).exists()
    assert CompetencyCriteriaGroup.objects.filter(id=root.id).exists()


def test_cascade_stops_at_an_ancestor_with_a_surviving_sibling_group(
    user: UserType, *, competency_taxonomy: CompetencyTaxonomy, tag: Tag, course_run: CourseRun,
    object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A sibling group under the same course-level ancestor, with its own live criterion, keeps that
    ancestor from being touched, even though the target's own subtree is fully removed.
    """
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(target, object_tag, default_rule_profile)
    sibling = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    sibling_object_tag = _make_object_tag(competency_taxonomy, tag, "obj-sibling")
    sibling_criterion = _make_criterion(sibling, sibling_object_tag, default_rule_profile)

    delete_competency_criteria_group(target.id, user)

    assert not CompetencyCriteriaGroup.objects.filter(id=target.id).exists()
    course_level.refresh_from_db()
    assert course_level.archived is False
    assert CompetencyCriteriaGroup.objects.filter(id=course_level.id).exists()
    assert CompetencyCriteriaGroup.objects.filter(id=sibling.id).exists()
    assert CompetencyCriterion.objects.filter(id=sibling_criterion.id).exists()


# =================================================================================================
# Root-group rejection, permission, and not-found
# =================================================================================================


def test_root_group_rejection_raises_before_the_permission_check_and_before_any_write(
    user: UserType, tag: Tag, course_run: CourseRun, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A root target_group is rejected with ValidationError before the permission check even runs --
    a denying predicate would otherwise raise PermissionDenied instead, which proves the ordering
    -- and before any write: the root, its child, and its criterion are all untouched afterward.
    """
    rules.set_perm("oel_tagging.change_objecttag_objectid", rules.always_false)
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    child = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    criterion = _make_criterion(child, object_tag, default_rule_profile)

    with pytest.raises(ValidationError):
        delete_competency_criteria_group(root.id, user)

    root.refresh_from_db()
    child.refresh_from_db()
    criterion.refresh_from_db()
    assert root.archived is False
    assert child.archived is False
    assert criterion.archived is False
    assert CompetencyCriteriaGroup.objects.filter(id=root.id).exists()
    assert CompetencyCriteriaGroup.objects.filter(id=child.id).exists()
    assert CompetencyCriterion.objects.filter(id=criterion.id).exists()


def test_permission_denied_without_can_tag_object(
    user: UserType, tag: Tag, course_run: CourseRun, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """A caller without oel_tagging.can_tag_object for a legitimate (non-root) target's subtree course is refused."""
    rules.set_perm("oel_tagging.change_objecttag_objectid", rules.always_false)
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    target = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    _make_criterion(target, object_tag, default_rule_profile)

    with pytest.raises(PermissionDenied):
        delete_competency_criteria_group(target.id, user)


def test_permission_check_resolves_course_from_the_targets_own_course_id(
    user: UserType, tag: Tag, course_run: CourseRun,
) -> None:
    """A course-level target group (its own course_id set) checks permission against that course."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    seen_object_ids: list[str] = []

    def _predicate(_user: UserType, object_id: str) -> bool:
        seen_object_ids.append(object_id)
        return True

    rules.set_perm("oel_tagging.change_objecttag_objectid", _predicate)

    delete_competency_criteria_group(course_level.id, user)

    assert seen_object_ids == [str(course_run.course_key)]


def test_permission_check_resolves_course_from_the_parents_course_id_for_a_non_course_level_target(
    user: UserType, tag: Tag, course_run: CourseRun,
) -> None:
    """
    A target group with no course_id of its own (one level under a course-level group) checks
    permission against its parent's course.
    """
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run, parent=root)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    seen_object_ids: list[str] = []

    def _predicate(_user: UserType, object_id: str) -> bool:
        seen_object_ids.append(object_id)
        return True

    rules.set_perm("oel_tagging.change_objecttag_objectid", _predicate)

    delete_competency_criteria_group(leaf.id, user)

    assert seen_object_ids == [str(course_run.course_key)]


def test_http_404_for_an_unknown_group_id(user: UserType) -> None:
    """A group_id with no matching row raises Http404."""
    with pytest.raises(Http404):
        delete_competency_criteria_group(999999999, user)
