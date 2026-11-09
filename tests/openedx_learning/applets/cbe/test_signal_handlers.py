"""Tests for the receivers that clean up criteria groups when a criterion is deleted with its ObjectTag."""
import logging
from collections.abc import Callable, Iterator

import pytest
import rules
from django.db import OperationalError
from rules.permissions import permissions as rule_permissions

from openedx_catalog.models import CourseRun
from openedx_learning.api import delete_or_archive_competency_criterion
from openedx_learning.applets.cbe import signal_handlers
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaGroupStatus,
)
from openedx_tagging.api import delete_object_tags, tag_object
from openedx_tagging.models import ObjectTag, Tag

pytestmark = pytest.mark.django_db

CHANGE_OBJECTTAG_OBJECTID = "oel_tagging.change_objecttag_objectid"


@pytest.fixture(autouse=True)
def _allow_object_level_tagging() -> Iterator[None]:
    """
    Let any user tag any object; restore the real rule afterwards.

    openedx_tagging denies change_objecttag_objectid to everyone and leaves the real Studio-role
    check to openedx-platform, so these tests substitute a permissive one.
    """
    original = rule_permissions[CHANGE_OBJECTTAG_OBJECTID]
    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, lambda _user, _object_id: True)
    yield
    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, original)


def _make_object_tag(competency_taxonomy: CompetencyTaxonomy, tag: Tag, object_id: str) -> ObjectTag:
    """Create an ObjectTag for `object_id`, distinct from the conftest `object_tag` fixture's own row."""
    return ObjectTag.objects.create(object_id=object_id, taxonomy=competency_taxonomy, tag=tag)


def _make_criterion(
    group: CompetencyCriteriaGroup, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile
) -> CompetencyCriterion:
    """Create a CompetencyCriterion under `group`, on the system-default rule profile."""
    return CompetencyCriterion.objects.create(group=group, object_tag=object_tag, rule_profile=default_rule_profile)


def _tree(
    tag: Tag, course_run: CourseRun
) -> tuple[CompetencyCriteriaGroup, CompetencyCriteriaGroup, CompetencyCriteriaGroup]:
    """Return (root, course_level, leaf) for `tag` scoped to `course_run`."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=course_run)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    return root, course_level, leaf


def _cleanup_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the messages the cleanup receivers logged."""
    return [r.getMessage() for r in caplog.records if r.name == signal_handlers.__name__]


def _ids_named_in(lines: list[str]) -> list[int]:
    """Return the group id each log line names, in order."""
    return [int(line.split("CompetencyCriteriaGroup ")[1].split(" ")[0]) for line in lines]


def test_instance_origin_removes_the_whole_empty_lineage_and_logs_each_group(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """An ObjectTag removed through tag_object() takes its criterion and every group it leaves empty."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        tag_object(object_tag.object_id, competency_taxonomy, [])  # the ObjectTag is deleted as an instance

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]


def test_a_sibling_leaf_with_a_criterion_stops_the_walk_and_logs_only_the_removed_leaf(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """A sibling leaf that still holds a criterion keeps the course-level group and the root in place."""
    root, course_level, leaf_a = _tree(tag, course_run)
    leaf_b = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(leaf_a, object_tag, default_rule_profile)
    other_object_tag = _make_object_tag(competency_taxonomy, tag, "block-v1:Org1+Python100+Fall2026+problem+p2")
    _make_criterion(leaf_b, other_object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk=leaf_a.pk).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf_b.pk]).count() == 3
    assert _cleanup_lines(caplog) == [f"Group cleanup removed CompetencyCriteriaGroup {leaf_a.pk} after {trigger}"]


def test_queryset_origin_removes_both_leaves_the_course_group_and_the_root(
    *, tag, course_run, object_tag, default_rule_profile, caplog,
) -> None:
    """One ObjectTag with criteria in two leaves empties both leaves, the course-level group and the root."""
    root, course_level, leaf_1 = _tree(tag, course_run)
    leaf_2 = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(leaf_1, object_tag, default_rule_profile)
    _make_criterion(leaf_2, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)  # a QuerySet[ObjectTag] origin

    group_ids = [root.pk, course_level.pk, leaf_1.pk, leaf_2.pk]
    assert not CompetencyCriteriaGroup.objects.filter(pk__in=group_ids).exists()
    lines = _cleanup_lines(caplog)
    # The collector's order of post_delete calls differs between database vendors.
    assert len(lines) == 4
    assert all(line.startswith("Group cleanup removed CompetencyCriteriaGroup ") for line in lines)
    assert set(_ids_named_in(lines)) == set(group_ids)


def test_two_criteria_in_one_leaf_remove_and_log_the_leaf_once(
    *, tag, course_run, object_tag, default_rule_profile, caplog,
) -> None:
    """The second criterion's receiver finds the lineage already gone and logs nothing."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()
    named = _ids_named_in(_cleanup_lines(caplog))
    assert sorted(named) == sorted([root.pk, course_level.pk, leaf.pk])
    assert all(named.count(pk) == 1 for pk in (root.pk, course_level.pk, leaf.pk))


def test_two_competencies_on_one_subsection_clean_up_independently(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """Deleting a subsection's tags removes the lineage that is now empty and keeps the one still in use."""
    tag_b = Tag.objects.create(taxonomy=competency_taxonomy, value="Revising Poetry")
    root_a, course_a, leaf_a = _tree(tag, course_run)
    root_b, course_b, leaf_b = _tree(tag_b, course_run)
    object_tag_b = _make_object_tag(competency_taxonomy, tag_b, object_tag.object_id)
    other_object_tag = _make_object_tag(competency_taxonomy, tag_b, "block-v1:Org1+Python100+Fall2026+problem+p2")
    _make_criterion(leaf_a, object_tag, default_rule_profile)
    _make_criterion(leaf_b, object_tag_b, default_rule_profile)
    _make_criterion(leaf_b, other_object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root_a.pk, course_a.pk, leaf_a.pk]).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root_b.pk, course_b.pk, leaf_b.pk]).count() == 3
    assert sorted(_ids_named_in(_cleanup_lines(caplog))) == sorted([root_a.pk, course_a.pk, leaf_a.pk])


def test_a_leaf_directly_under_the_root_removes_both_and_logs_two_lines(
    *, tag, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """A leaf with no course-level group above it removes itself and the root."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        tag_object(object_tag.object_id, competency_taxonomy, [])

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, leaf.pk]).exists()
    assert len(_cleanup_lines(caplog)) == 2
    assert sorted(_ids_named_in(_cleanup_lines(caplog))) == sorted([root.pk, leaf.pk])


def _delete_criterion_through_the_api(criterion, user):
    """Delete the criterion the way the authoring API does, which walks the groups itself."""
    delete_or_archive_competency_criterion(criterion.id, user)


def _delete_criterion_instance(criterion, _user):
    """Delete the criterion directly."""
    criterion.delete()


def _delete_group_instance(criterion, _user):
    """Delete the criterion's group, which cascades to the criterion."""
    criterion.group.delete()


def _delete_tag(criterion, _user):
    """Delete the competency Tag, which cascades to its groups and criteria."""
    criterion.group.tag.delete()


def _delete_taxonomy(criterion, _user):
    """Delete the taxonomy, which cascades to its tags, object tags and criteria."""
    criterion.object_tag.taxonomy.delete()


@pytest.mark.parametrize(
    "make_delete",
    [
        _delete_criterion_through_the_api,
        _delete_criterion_instance,
        _delete_group_instance,
        _delete_tag,
        _delete_taxonomy,
    ],
)
def test_other_origins_never_run_the_walk_or_log(
    make_delete: Callable, *, user, tag, course_run, object_tag, default_rule_profile, caplog, monkeypatch,
) -> None:
    """A criterion that goes away for any reason but its ObjectTag's deletion never makes the receiver walk or log."""
    _, _, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    def _walk_must_not_run(*_args, **_kwargs):
        raise AssertionError("the group walk ran for an ignored origin")

    monkeypatch.setattr(signal_handlers, "_cascade_remove_from_group", _walk_must_not_run)
    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        make_delete(criterion, user)

    # The receiver would turn the AssertionError into a warning line, so no line means the walk never ran.
    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert _cleanup_lines(caplog) == []


OTHER_OBJECT_ID = "block-v1:Org1+Python100+Fall2026+problem+p2"


def _archived(group: CompetencyCriteriaGroup) -> bool:
    """Return the group's archived flag as stored."""
    return CompetencyCriteriaGroup.objects.get(pk=group.pk).archived


def test_a_leaf_that_also_holds_an_archived_criterion_is_archived_not_removed(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """A leaf still holding an archived criterion is archived; a live sibling leaf keeps the ancestors unchanged."""
    root, course_level, leaf = _tree(tag, course_run)
    other_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    archived_tag = _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID)
    archived_criterion = _make_criterion(leaf, archived_tag, default_rule_profile)
    archived_criterion.archived = True
    archived_criterion.save(update_fields=["archived"])
    live_tag = _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID + "2")
    _make_criterion(other_leaf, live_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert _archived(leaf) is True
    assert [_archived(g) for g in (course_level, root, other_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}"]


def test_a_leaf_with_its_own_group_status_is_archived(
    *, user, now, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """A leaf with a learner group status row is archived instead of removed; a live sibling leaf protects the rest."""
    root, course_level, leaf = _tree(tag, course_run)
    other_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    _make_criterion(other_leaf, _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID), default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert _archived(leaf) is True
    assert [_archived(g) for g in (course_level, root, other_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}"]


def test_a_course_group_left_with_only_archived_leaves_is_archived_too(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
) -> None:
    """A course-level group whose remaining leaves are all archived is archived; another course keeps the root."""
    root, course_level, leaf_y = _tree(tag, course_run)
    leaf_x = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level, archived=True)
    criterion = _make_criterion(leaf_y, object_tag, default_rule_profile)
    archived_criterion = _make_criterion(
        leaf_x, _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID), default_rule_profile,
    )
    archived_criterion.archived = True
    archived_criterion.save(update_fields=["archived"])
    spring_run = CourseRun.objects.create(catalog_course=course_run.catalog_course, run_code="Spring2027")
    spring_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=spring_run)
    spring_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=spring_level)
    _make_criterion(
        spring_leaf, _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID + "2"), default_rule_profile,
    )
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk=leaf_y.pk).exists()
    assert _archived(course_level) is True
    assert [_archived(g) for g in (root, spring_level, spring_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf_y.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
    ]


def test_an_archived_leaf_losing_its_last_criterion_is_removed_and_logged_as_removed(
    *, tag, course_run, object_tag, default_rule_profile, caplog,
) -> None:
    """An already-archived leaf with no status row is deleted, and so is every group above it left empty."""
    root, course_level, leaf = _tree(tag, course_run)
    leaf.archived = True
    leaf.save(update_fields=["archived"])
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]


def test_archiving_the_last_live_leaf_archives_every_ancestor_up_to_the_root(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog,
) -> None:
    """With no live group left anywhere beneath them, the course-level group and the root are archived too."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]


def test_a_helper_failure_leaves_the_groups_warns_and_lets_the_delete_finish(
    *, tag, course_run, object_tag, default_rule_profile, monkeypatch, caplog,
) -> None:
    """A failure inside the walk keeps every group, logs one warning with the traceback, and the delete completes."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(signal_handlers, "_cascade_remove_from_group", _boom)

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__):
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).count() == 3
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Group cleanup failed after CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}; "
        f"CompetencyCriteriaGroups left in place: {sorted([leaf.pk, course_level.pk, root.pk])}"
    )
    assert record.exc_info is not None


def test_a_lock_error_is_not_swallowed(
    *, tag, course_run, object_tag, default_rule_profile, monkeypatch,
) -> None:
    """A database OperationalError, such as a MySQL deadlock, propagates out of the delete."""
    _, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)

    def _deadlock(*_args, **_kwargs):
        raise OperationalError("Deadlock found")

    monkeypatch.setattr(signal_handlers, "_cascade_remove_from_group", _deadlock)

    with pytest.raises(OperationalError):
        delete_object_tags(object_tag.object_id)
