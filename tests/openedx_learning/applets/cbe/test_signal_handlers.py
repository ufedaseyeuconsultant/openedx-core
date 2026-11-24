"""
Tests for the receivers that keep criteria groups in step with the deletion of their criteria, and with the
archiving of associations, tags, and taxonomies.
"""
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from types import SimpleNamespace

import pytest
import rules
from django.apps import apps
from django.db import OperationalError, transaction
from django.db.models import ProtectedError
from django.test import override_settings
from rules.permissions import permissions as rule_permissions

from openedx_catalog.models import CourseRun
from openedx_learning.api import delete_or_archive_competency_criteria_group, delete_or_archive_competency_criterion
from openedx_learning.applets.cbe import signal_handlers
from openedx_learning.applets.cbe.signal_handlers import _clean_up_archived_associations
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaGroupStatus,
    StudentCompetencyCriterionStatus,
)
from openedx_tagging.api import delete_object_tags, lock_object_tags_for_deletion, tag_object
from openedx_tagging.models import ObjectTag, Tag
from openedx_tagging.signals import object_tags_archived

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


@pytest.fixture(name="after_commit")
def _after_commit(django_capture_on_commit_callbacks) -> Callable[[], AbstractContextManager]:
    """
    Return a context manager that runs the on_commit callbacks registered inside it when it exits.

    The cleanup runs after the triggering delete commits. A test's own transaction never commits, so each
    triggering delete is wrapped in this to run the cleanup the way a commit would.
    """
    return lambda: django_capture_on_commit_callbacks(execute=True)


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


@pytest.mark.parametrize("has_course_level", [True, False], ids=["root_course_level_leaf", "root_leaf"])
def test_tag_object_removal_removes_the_whole_empty_lineage_and_logs_each_group(
    has_course_level: bool, *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog,
    after_commit,
) -> None:
    """An ObjectTag removed through tag_object() takes its criterion and every group it leaves empty."""
    root = CompetencyCriteriaGroup.objects.create(tag=tag)
    lineage = [root]
    if has_course_level:
        lineage.append(CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=course_run))
    lineage.append(CompetencyCriteriaGroup.objects.create(tag=tag, parent=lineage[-1]))
    criterion = _make_criterion(lineage[-1], object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        # tag_object() deletes through a queryset, so the origin is a QuerySet[ObjectTag].
        tag_object(object_tag.object_id, competency_taxonomy, [])

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[group.pk for group in lineage]).exists()
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {group.pk} after {trigger}" for group in reversed(lineage)
    ]


def test_a_sibling_leaf_with_a_criterion_stops_the_walk_and_logs_only_the_removed_leaf(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A sibling leaf that still holds a criterion keeps the course-level group and the root in place."""
    root, course_level, leaf_a = _tree(tag, course_run)
    leaf_b = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(leaf_a, object_tag, default_rule_profile)
    other_object_tag = _make_object_tag(competency_taxonomy, tag, "block-v1:Org1+Python100+Fall2026+problem+p2")
    _make_criterion(leaf_b, other_object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk=leaf_a.pk).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf_b.pk]).count() == 3
    assert _cleanup_lines(caplog) == [f"Group cleanup removed CompetencyCriteriaGroup {leaf_a.pk} after {trigger}"]


def test_queryset_origin_removes_both_leaves_the_course_group_and_the_root(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """One ObjectTag with criteria in two leaves empties both leaves, the course-level group and the root."""
    root, course_level, leaf_1 = _tree(tag, course_run)
    leaf_2 = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(leaf_1, object_tag, default_rule_profile)
    _make_criterion(leaf_2, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)  # a QuerySet[ObjectTag] origin

    group_ids = [root.pk, course_level.pk, leaf_1.pk, leaf_2.pk]
    assert not CompetencyCriteriaGroup.objects.filter(pk__in=group_ids).exists()
    lines = _cleanup_lines(caplog)
    # The order of post_delete calls within a batch is an implementation detail of Django's collector, so
    # the test compares sets.
    assert len(lines) == 4
    assert all(line.startswith("Group cleanup removed CompetencyCriteriaGroup ") for line in lines)
    assert set(_ids_named_in(lines)) == set(group_ids)


def test_two_criteria_in_one_leaf_remove_and_log_the_leaf_once(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """The second criterion's cleanup finds the lineage already gone and logs nothing."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()
    named = _ids_named_in(_cleanup_lines(caplog))
    assert sorted(named) == sorted([root.pk, course_level.pk, leaf.pk])
    assert all(named.count(pk) == 1 for pk in (root.pk, course_level.pk, leaf.pk))


def test_two_competencies_on_one_subsection_clean_up_independently(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
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

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root_a.pk, course_a.pk, leaf_a.pk]).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root_b.pk, course_b.pk, leaf_b.pk]).count() == 3
    assert sorted(_ids_named_in(_cleanup_lines(caplog))) == sorted([root_a.pk, course_a.pk, leaf_a.pk])


def test_deleting_a_locked_object_tag_archives_it_and_its_criterion_and_groups(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A locked ObjectTag is archived by the delete, which archives its criterion and groups instead of deleting."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)
    trigger = f"ObjectTags [{object_tag.pk}] archived"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    stored = ObjectTag.objects.get(pk=object_tag.pk)
    assert stored.archived is True
    assert CompetencyCriterion.objects.filter(pk=criterion.pk, object_tag=stored, archived=True).exists()
    assert [_archived(g) for g in (root, course_level, leaf)] == [True, True, True]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]


def _delete_criterion_through_the_api(criterion, user):
    """Delete the criterion the way the authoring API does, which walks the groups itself."""
    delete_or_archive_competency_criterion(criterion.id, user)


def _delete_criterion_instance(criterion, _user):
    """Delete the criterion directly."""
    criterion.delete()


def _delete_group_instance(criterion, _user):
    """Delete the criterion's group, which cascades to the criterion."""
    criterion.group.delete()


def _delete_course_level_group_instance(criterion, _user):
    """Delete the course-level group directly, which cascades to its leaf groups and their criteria."""
    criterion.group.parent.delete()


def _delete_tag(criterion, _user):
    """Delete the competency Tag, which cascades to its groups and criteria."""
    criterion.group.tag.delete()


def _delete_taxonomy(criterion, _user):
    """Delete the taxonomy, which cascades to its tags and criteria groups and nulls the object tags' taxonomy."""
    criterion.object_tag.taxonomy.delete()


@pytest.mark.parametrize(
    ("make_delete", "object_tag_survives"),
    [
        (_delete_criterion_through_the_api, None),
        (_delete_criterion_instance, None),
        (_delete_group_instance, None),
        (_delete_course_level_group_instance, True),
        (_delete_tag, True),
        (_delete_taxonomy, True),
    ],
    ids=[
        "criterion_through_api", "criterion_instance", "group_instance", "course_level_group_instance", "tag",
        "taxonomy",
    ],
)
def test_other_origins_never_clean_up_or_log(
    make_delete: Callable, object_tag_survives: bool | None, *,
    user, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """
    A criterion that goes away for any reason but its ObjectTag's deletion never makes a receiver clean up or log.

    A Tag or Taxonomy delete in particular leaves the ObjectTag in place, which only the CourseRun origin guard on
    the group receivers guarantees.
    """
    _, _, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        make_delete(criterion, user)

    assert _cleanup_lines(caplog) == []
    if object_tag_survives:
        assert ObjectTag.objects.filter(pk=object_tag.pk).exists()


OTHER_OBJECT_ID = "block-v1:Org1+Python100+Fall2026+problem+p2"


def _archived(group: CompetencyCriteriaGroup) -> bool:
    """Return the group's archived flag as stored."""
    return CompetencyCriteriaGroup.objects.get(pk=group.pk).archived


def _status_rows() -> list[dict]:
    """Return every learner group status row as stored, to compare before and after a cleanup."""
    return [dict(row) for row in StudentCompetencyCriteriaGroupStatus.objects.order_by("pk").values()]


def test_a_leaf_that_also_holds_an_archived_criterion_is_archived_not_removed(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
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

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert _archived(leaf) is True
    assert [_archived(g) for g in (course_level, root, other_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}"]


def test_a_leaf_with_its_own_group_status_is_archived(
    *, user, now, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A leaf with a learner group status row is archived instead of removed; a live sibling leaf protects the rest."""
    root, course_level, leaf = _tree(tag, course_run)
    other_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    _make_criterion(other_leaf, _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID), default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    status_rows = _status_rows()
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert _archived(leaf) is True
    assert [_archived(g) for g in (course_level, root, other_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}"]
    assert _status_rows() == status_rows


def test_a_course_group_left_with_only_archived_leaves_is_archived_too(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
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

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk=leaf_y.pk).exists()
    assert _archived(course_level) is True
    assert [_archived(g) for g in (root, spring_level, spring_leaf)] == [False, False, False]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf_y.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
    ]


def test_an_archived_leaf_losing_its_last_criterion_is_removed_and_logged_as_removed(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """An already-archived leaf with no status row is deleted, and so is every group above it left empty."""
    root, course_level, leaf = _tree(tag, course_run)
    leaf.archived = True
    leaf.save(update_fields=["archived"])
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]


def test_archiving_the_last_live_leaf_archives_every_ancestor_up_to_the_root(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """With no live group left anywhere beneath them, the course-level group and the root are archived too."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    status_rows = _status_rows()
    trigger = f"CompetencyCriterion {criterion.pk} deleted with ObjectTag {object_tag.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]
    assert _status_rows() == status_rows


def test_a_leaf_four_levels_deep_is_cleaned_up_to_the_root_and_its_object_tag_is_reconciled(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Group trees deeper than root, course-level group and leaf are walked to the root on both origins."""
    root, course_level, middle = _tree(tag, course_run)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=middle)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, middle.pk, leaf.pk]).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()


def test_a_deep_lineage_is_logged_group_by_group_when_an_object_tag_is_removed(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Every group from the leaf to the root gets a log line, however deep the tree is."""
    root, course_level, middle = _tree(tag, course_run)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=middle)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert _ids_named_in(_cleanup_lines(caplog)) == [leaf.pk, middle.pk, course_level.pk, root.pk]


def _fail_with(error: Exception, *, removing_the_leaf_first: bool = False) -> Callable:
    """Return a stand-in for the walk, with its real signature, that optionally deletes the leaf and then raises."""

    def _walk(group, _user, _using=None):
        if removing_the_leaf_first:
            CompetencyCriteriaGroup.objects.filter(pk=group.pk).delete()
        raise error

    return _walk


@pytest.mark.parametrize("removing_the_leaf_first", [False, True], ids=["fails_at_once", "fails_after_the_leaf_went"])
def test_a_failure_in_the_walk_leaves_the_groups_warns_and_keeps_the_delete(
    removing_the_leaf_first: bool, *, tag, course_run, object_tag, default_rule_profile, monkeypatch, caplog,
    after_commit,
) -> None:
    """
    A failure inside the walk keeps every group, logs one warning with the traceback, and the delete stands.

    When the walk had already deleted the leaf, the cleanup's own transaction puts the leaf back.
    """
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    monkeypatch.setattr(
        signal_handlers,
        "_cascade_remove_from_group",
        _fail_with(RuntimeError("boom"), removing_the_leaf_first=removing_the_leaf_first),
    )

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__), after_commit():
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


def test_a_database_error_in_the_lineage_cleanup_is_swallowed_and_logged(
    *, tag, course_run, object_tag, default_rule_profile, monkeypatch, caplog, after_commit,
) -> None:
    """A database OperationalError, such as a MySQL deadlock, cannot undo the delete that already committed."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    monkeypatch.setattr(signal_handlers, "_cascade_remove_from_group", _fail_with(OperationalError("Deadlock found")))

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).count() == 3
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Group cleanup failed after ")


def _second_run(course_run: CourseRun) -> CourseRun:
    """Create another run of the same catalog course."""
    return CourseRun.objects.create(catalog_course=course_run.catalog_course, run_code="Spring2027")


@pytest.mark.parametrize(
    "delete",
    [
        lambda run: run.delete(),
        lambda run: CourseRun.objects.filter(pk=run.pk).delete(),
    ],
    ids=["instance", "queryset"],
)
def test_deleting_the_only_course_under_a_root_removes_the_root_and_its_object_tags(
    delete: Callable, *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """The root left with no course below it is removed, and so is the ObjectTag no criterion uses any more."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    trigger = f"CompetencyCriteriaGroup {course_level.pk} deleted with CourseRun {course_run.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete(course_run)

    assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()
    assert _cleanup_lines(caplog) == [f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after {trigger}"]


def test_the_root_survives_unlogged_when_another_course_still_has_groups(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A second course's groups under the same root keep the root in place and unarchived."""
    root, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    spring_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=_second_run(course_run))
    CompetencyCriteriaGroup.objects.create(tag=tag, parent=spring_level)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert _archived(root) is False
    assert _cleanup_lines(caplog) == []


def test_the_root_is_archived_when_it_has_its_own_status_row(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A root with a learner group status row is archived, not removed, once no course is left under it."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=root, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    trigger = f"CompetencyCriteriaGroup {course_level.pk} deleted with CourseRun {course_run.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert _archived(root) is True
    assert _cleanup_lines(caplog) == [f"Group cleanup archived CompetencyCriteriaGroup {root.pk} after {trigger}"]


def test_an_object_tag_another_course_still_uses_is_kept(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """An ObjectTag that a criterion in another course still references survives the deletion of this course."""
    root, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    spring_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=_second_run(course_run))
    spring_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=spring_level)
    spring_criterion = _make_criterion(spring_leaf, object_tag, default_rule_profile)

    with after_commit():
        course_run.delete()

    assert ObjectTag.objects.filter(pk=object_tag.pk).exists()
    assert CompetencyCriterion.objects.filter(pk=spring_criterion.pk).exists()


def _archived_criterion_elsewhere_uses(object_tag: ObjectTag, tag: Tag, course_run: CourseRun, rule_profile):
    """Create a second, archived course whose only criterion is archived and uses ``object_tag``."""
    root = CompetencyCriteriaGroup.objects.get(tag=tag, parent__isnull=True)
    spring_level = CompetencyCriteriaGroup.objects.create(
        tag=tag, parent=root, course=_second_run(course_run), archived=True,
    )
    spring_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=spring_level, archived=True)
    spring_criterion = _make_criterion(spring_leaf, object_tag, rule_profile)
    spring_criterion.archived = True
    spring_criterion.save(update_fields=["archived"])
    return root, spring_level, spring_leaf, spring_criterion


def test_an_object_tag_only_an_archived_criterion_elsewhere_uses_is_locked_and_archived(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """Deleting this course archives the ObjectTag that only another course's archived criterion still uses."""
    _, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    root, spring_level, spring_leaf, spring_criterion = _archived_criterion_elsewhere_uses(
        object_tag, tag, course_run, default_rule_profile
    )

    with after_commit():
        course_run.delete()

    stored = ObjectTag.objects.get(pk=object_tag.pk)
    assert stored.archived is True
    assert stored.deletion_locked is True
    assert CompetencyCriterion.objects.filter(pk=spring_criterion.pk, object_tag=stored, archived=True).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, spring_level.pk, spring_leaf.pk]).count() == 3
    assert _archived(root) is True


def test_an_already_locked_object_tag_nothing_uses_is_archived_by_a_course_deletion(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """A locked ObjectTag that no criterion uses after the course is gone is archived, not deleted."""
    _, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)

    with after_commit():
        course_run.delete()

    stored = ObjectTag.objects.get(pk=object_tag.pk)
    assert stored.archived is True
    assert stored.deletion_locked is True


def test_one_delete_archives_a_locked_object_tag_and_removes_an_unlocked_one_with_its_groups(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A single delete archives the locked ObjectTag's rule, and removes the unlocked one's criterion and groups."""
    tag_b = Tag.objects.create(taxonomy=competency_taxonomy, value="Revising Poetry")
    root_a, course_a, leaf_a = _tree(tag, course_run)
    root_b, course_b, leaf_b = _tree(tag_b, course_run)
    object_tag_b = _make_object_tag(competency_taxonomy, tag_b, object_tag.object_id)
    criterion_a = _make_criterion(leaf_a, object_tag, default_rule_profile)
    criterion_b = _make_criterion(leaf_b, object_tag_b, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)
    trigger = f"CompetencyCriterion {criterion_b.pk} deleted with ObjectTag {object_tag_b.pk}"
    archive_trigger = f"ObjectTags [{object_tag.pk}] archived"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(object_tag.object_id)

    stored = ObjectTag.objects.get(pk=object_tag.pk)
    assert stored.archived is True
    assert CompetencyCriterion.objects.filter(pk=criterion_a.pk, object_tag=stored, archived=True).exists()
    assert [_archived(g) for g in (root_a, course_a, leaf_a)] == [True, True, True]
    assert not ObjectTag.objects.filter(pk=object_tag_b.pk).exists()
    assert not CompetencyCriterion.objects.filter(pk=criterion_b.pk).exists()
    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root_b.pk, course_b.pk, leaf_b.pk]).exists()
    assert _cleanup_lines(caplog) == [
        f"Group cleanup removed CompetencyCriteriaGroup {leaf_b.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {course_b.pk} after {trigger}",
        f"Group cleanup removed CompetencyCriteriaGroup {root_b.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {leaf_a.pk} after {archive_trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_a.pk} after {archive_trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {root_a.pk} after {archive_trigger}",
    ]


def test_a_course_with_learner_status_cannot_be_deleted_and_logs_nothing(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Learner status on a criterion blocks the course deletion, and the receivers never run."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        with pytest.raises(ProtectedError):
            course_run.delete()

    assert CourseRun.objects.filter(pk=course_run.pk).exists()
    assert CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).count() == 3
    assert CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert ObjectTag.objects.filter(pk=object_tag.pk).exists()
    assert _cleanup_lines(caplog) == []


def test_two_runs_sharing_a_root_deleted_in_one_queryset_remove_the_root_once(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """The second course-level group finds the root already gone, logs nothing, and still reconciles its tags."""
    spring_run = _second_run(course_run)
    root, _, leaf = _tree(tag, course_run)
    spring_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=root, course=spring_run)
    spring_leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=spring_level)
    spring_object_tag = _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID)
    _make_criterion(leaf, object_tag, default_rule_profile)
    _make_criterion(spring_leaf, spring_object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        CourseRun.objects.filter(pk__in=[course_run.pk, spring_run.pk]).delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert not ObjectTag.objects.filter(pk__in=[object_tag.pk, spring_object_tag.pk]).exists()
    lines = _cleanup_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith(f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after ")


def test_a_course_group_with_no_criteria_still_cleans_up_the_root(
    *, tag, course_run, caplog, after_commit,
) -> None:
    """A course-level group with no criteria anywhere beneath it still lets the root be removed."""
    root, course_level, _ = _tree(tag, course_run)
    trigger = f"CompetencyCriteriaGroup {course_level.pk} deleted with CourseRun {course_run.pk}"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert _cleanup_lines(caplog) == [f"Group cleanup removed CompetencyCriteriaGroup {root.pk} after {trigger}"]


@pytest.mark.parametrize("object_tag_goes_first", [True, False], ids=["object_tag_first", "course_first"])
def test_both_orders_of_a_course_deletion_end_with_no_group_and_no_object_tag(
    object_tag_goes_first: bool, *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Whichever of the ObjectTag and the course is deleted first, nothing is left and nothing warns."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__):
        with after_commit():
            if object_tag_goes_first:
                delete_object_tags(object_tag.object_id)
        with after_commit():
            course_run.delete()

    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == []
    # Deleting the ObjectTag first removes the whole lineage; deleting the course first leaves only the root.
    expected = [leaf.pk, course_level.pk, root.pk] if object_tag_goes_first else [root.pk]
    assert _ids_named_in(_cleanup_lines(caplog)) == expected
    assert not CompetencyCriteriaGroup.objects.filter(tag=tag).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()


@pytest.mark.parametrize("failing_step", ["_reconcile_object_tags", "_cascade_remove_from_group"])
@pytest.mark.parametrize("error", [RuntimeError("boom"), OperationalError("Deadlock found")], ids=["error", "db_error"])
def test_a_failed_step_of_a_course_cleanup_warns_and_leaves_the_other_step_and_the_delete_alone(
    failing_step: str, error: Exception, *,
    tag, course_run, object_tag, default_rule_profile, monkeypatch, caplog, after_commit,
) -> None:
    """Either step failing never undoes the course deletion, and the other step still runs."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    course_run_pk = course_run.pk

    def _fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(signal_handlers, failing_step, _fail)

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert not CourseRun.objects.filter(pk=course_run_pk).exists()
    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[course_level.pk, leaf.pk]).exists()
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    trigger = f"CompetencyCriteriaGroup {course_level.pk} deleted with CourseRun {course_run_pk}"
    if failing_step == "_reconcile_object_tags":
        assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
        assert ObjectTag.objects.filter(pk=object_tag.pk).exists()
        assert record.getMessage() == (
            f"Group cleanup failed after {trigger}; ObjectTags left in place: [{object_tag.pk}]"
        )
    else:
        assert CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
        assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()
        assert record.getMessage() == (
            f"Group cleanup failed after {trigger}; CompetencyCriteriaGroups left in place: [{root.pk}]"
        )


def test_a_course_group_with_no_parent_still_reconciles_its_object_tags(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """With no root above it there is nothing to walk, but the ObjectTags its criteria used are still removed."""
    course_level = CompetencyCriteriaGroup.objects.create(tag=tag, course=course_run)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=course_level)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        course_run.delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[course_level.pk, leaf.pk]).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()
    assert _cleanup_lines(caplog) == []


def _archive_locked_association(object_tag: ObjectTag, taxonomy: CompetencyTaxonomy) -> None:
    """Lock ``object_tag`` and remove it with tag_object(), which archives it instead of deleting it."""
    lock_object_tags_for_deletion(object_tag.pk)
    tag_object(object_tag.object_id, taxonomy, [])


def _criterion_archived(criterion: CompetencyCriterion) -> bool:
    """Return the criterion's archived flag as stored."""
    return CompetencyCriterion.objects.get(pk=criterion.pk).archived


class _Rollback(Exception):
    """Raised inside an ``atomic()`` block to roll it back."""


def _record_learner_progress(user, now, criterion: CompetencyCriterion, groups) -> None:
    """Give ``user`` a status row on ``criterion`` and on each of ``groups``."""
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    for group in groups:
        StudentCompetencyCriteriaGroupStatus.objects.create(
            user=user, group=group, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
        )


def _learner_progress() -> tuple[list[dict], list[dict]]:
    """Return every learner criterion status row and group status row as stored."""
    return (
        [dict(row) for row in StudentCompetencyCriterionStatus.objects.order_by("pk").values()],
        _status_rows(),
    )


def _history_count(model_name: str, ids: list[int]) -> int:
    """
    Return how many history rows the given ``CompetencyCriterion`` or ``CompetencyCriteriaGroup`` ids have.

    The historical model comes from the app registry because simple_history's ``.history`` has no type stubs.
    """
    return apps.get_model("openedx_learning", f"Historical{model_name}").objects.filter(id__in=ids).count()


def test_archiving_a_locked_association_archives_its_criterion_and_empty_lineage(
    *, user, now, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """
    Removing a locked association archives its criterion and each group above it left with no live child.

    The learner's status rows on the criterion and its groups are not touched.
    """
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    _record_learner_progress(user, now, criterion, (leaf, course_level, root))
    progress = _learner_progress()
    trigger = f"ObjectTags [{object_tag.pk}] archived"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)

    assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]
    assert _cleanup_lines(caplog) == [
        f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {course_level.pk} after {trigger}",
        f"Group cleanup archived CompetencyCriteriaGroup {root.pk} after {trigger}",
    ]
    assert _learner_progress() == progress


def test_association_archive_cleanup_waits_for_the_commit(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, django_capture_on_commit_callbacks,
) -> None:
    """The receiver only registers the cleanup: nothing is archived while the transaction is open, only at commit."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        _archive_locked_association(object_tag, competency_taxonomy)
        assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
        assert _criterion_archived(criterion) is False
        assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]

    assert _criterion_archived(criterion) is False
    assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]
    for callback in callbacks:
        callback()
    assert _criterion_archived(criterion) is True
    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]


def test_association_archive_in_a_rolled_back_savepoint_runs_no_cleanup(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, after_commit,
) -> None:
    """Django discards the cleanup registered inside a savepoint that rolls back, together with the archive."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)

    with after_commit() as callbacks:
        with pytest.raises(_Rollback), transaction.atomic():
            tag_object(object_tag.object_id, competency_taxonomy, [])
            assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
            raise _Rollback

    assert callbacks == []
    assert ObjectTag.objects.get(pk=object_tag.pk).archived is False
    assert _criterion_archived(criterion) is False
    assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("migration_seed_rows")
def test_association_archive_in_a_rolled_back_transaction_runs_no_cleanup_and_a_commit_runs_it_once(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, monkeypatch,
) -> None:
    """Over real commits, a rolled-back outer transaction never runs the cleanup, and a committed one runs it once."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)
    calls: list[list[int]] = []

    def _spy(object_tag_ids, **kwargs):
        calls.append(object_tag_ids)
        _clean_up_archived_associations(object_tag_ids, **kwargs)

    monkeypatch.setattr(signal_handlers, "_clean_up_archived_associations", _spy)

    with pytest.raises(_Rollback), transaction.atomic():
        tag_object(object_tag.object_id, competency_taxonomy, [])
        raise _Rollback

    assert not calls
    assert _criterion_archived(criterion) is False

    tag_object(object_tag.object_id, competency_taxonomy, [])

    assert calls == [[object_tag.pk]]
    assert _criterion_archived(criterion) is True
    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]


def test_two_criteria_in_one_group_log_the_group_once(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Both criteria are archived, and the group they share is logged once, not once per criterion."""
    _, _, leaf = _tree(tag, course_run)
    first = _make_criterion(leaf, object_tag, default_rule_profile)
    other_tag = _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID)
    second = _make_criterion(leaf, other_tag, default_rule_profile)
    lock_object_tags_for_deletion([object_tag.pk, other_tag.pk])

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        ObjectTag.objects.filter(pk__in=[object_tag.pk, other_tag.pk]).delete()

    assert CompetencyCriterion.objects.filter(pk__in=[first.pk, second.pk], archived=True).count() == 2
    assert _ids_named_in(_cleanup_lines(caplog)).count(leaf.pk) == 1


def test_reviving_an_association_leaves_the_rule_archived(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Re-applying the tag revives the association, while the criterion and its groups stay archived, silently."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    with after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        tag_object(object_tag.object_id, competency_taxonomy, [tag.value])

    assert ObjectTag.objects.get(pk=object_tag.pk).archived is False
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]
    assert _cleanup_lines(caplog) == []


@pytest.mark.parametrize("sibling_group", ["same_leaf", "sibling_leaf"])
def test_live_sibling_criterion_stops_the_archive_walk(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, sibling_group, after_commit,
) -> None:
    """
    A live criterion next to the archived one keeps its own group and every group above it live.

    A sibling in the same leaf keeps that leaf live, and a sibling in another leaf keeps the course-level group
    and the root live. Only the groups the archive emptied are logged.
    """
    root, course_level, leaf = _tree(tag, course_run)
    sibling_leaf = leaf if sibling_group == "same_leaf" else CompetencyCriteriaGroup.objects.create(
        tag=tag, parent=course_level
    )
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    sibling = _make_criterion(
        sibling_leaf, _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID), default_rule_profile
    )
    trigger = f"ObjectTags [{object_tag.pk}] archived"

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)

    leaf_is_archived = sibling_group == "sibling_leaf"
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert CompetencyCriterion.objects.get(pk=sibling.pk).archived is False
    assert [_archived(g) for g in (leaf, course_level, root, sibling_leaf)] == [
        leaf_is_archived, False, False, False
    ]
    assert _cleanup_lines(caplog) == (
        [f"Group cleanup archived CompetencyCriteriaGroup {leaf.pk} after {trigger}"] if leaf_is_archived else []
    )


def test_archiving_the_association_of_an_already_archived_rule_is_a_silent_no_op(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog, django_assert_num_queries, after_commit,
) -> None:
    """
    Archiving a criterion through the authoring API archives its association, and that signal changes nothing more.

    The receiver is reached, finds no live criterion, and writes no further history row. Sent again, it runs one
    query, so it takes no group lock.
    """
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    group_ids = [leaf.pk, course_level.pk, root.pk]
    criterion_history = _history_count("CompetencyCriterion", [criterion.pk])
    group_history = _history_count("CompetencyCriteriaGroup", group_ids)
    sent: list[list[int]] = []

    def _record(object_tag_ids, **_kwargs):
        sent.append(object_tag_ids)

    object_tags_archived.connect(_record)
    try:
        with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
            delete_or_archive_competency_criterion(criterion.id, user)
    finally:
        object_tags_archived.disconnect(_record)

    assert sent == [[object_tag.pk]]
    assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]
    assert _history_count("CompetencyCriterion", [criterion.pk]) == criterion_history + 1
    assert _history_count("CompetencyCriteriaGroup", group_ids) == group_history + len(group_ids)
    assert _cleanup_lines(caplog) == []
    with django_assert_num_queries(1), after_commit():
        object_tags_archived.send(sender=ObjectTag, object_tag_ids=sent[0], using="default")


def test_group_archive_reconcile_is_a_silent_no_op(
    *, user, now, tag, course_run, object_tag, default_rule_profile, caplog, django_assert_num_queries, after_commit,
) -> None:
    """The signal sent from inside a group archive's own reconcile finds no live criteria and runs one query."""
    _, _, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    sent: list[list[int]] = []

    def _record(object_tag_ids, **_kwargs):
        sent.append(object_tag_ids)

    object_tags_archived.connect(_record)
    try:
        with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
            delete_or_archive_competency_criteria_group(leaf.pk, user)
    finally:
        object_tags_archived.disconnect(_record)

    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert sent == [[object_tag.pk]]
    assert _cleanup_lines(caplog) == []
    with django_assert_num_queries(1), after_commit():
        object_tags_archived.send(sender=ObjectTag, object_tag_ids=sent[0], using="default")


def test_object_tags_archived_twice_changes_and_logs_nothing_the_second_time(
    *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """Sending the same ids again, as a concurrent archive can, writes no history row and logs nothing."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    with after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)
    group_ids = [leaf.pk, course_level.pk, root.pk]
    criterion_history = _history_count("CompetencyCriterion", [criterion.pk])
    group_history = _history_count("CompetencyCriteriaGroup", group_ids)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        object_tags_archived.send(sender=ObjectTag, object_tag_ids=[object_tag.pk])

    assert _history_count("CompetencyCriterion", [criterion.pk]) == criterion_history
    assert _history_count("CompetencyCriteriaGroup", group_ids) == group_history
    assert _cleanup_lines(caplog) == []


def test_object_tags_archived_for_a_live_association_changes_and_logs_nothing(
    *, tag, course_run, object_tag, default_rule_profile, caplog, after_commit,
) -> None:
    """A signal that names an association that is not archived leaves its live criterion and groups alone."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    group_ids = [leaf.pk, course_level.pk, root.pk]
    group_history = _history_count("CompetencyCriteriaGroup", group_ids)

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        object_tags_archived.send(sender=ObjectTag, object_tag_ids=[object_tag.pk], using="default")

    assert _criterion_archived(criterion) is False
    assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]
    assert _history_count("CompetencyCriteriaGroup", group_ids) == group_history
    assert _cleanup_lines(caplog) == []


def test_association_archive_cleans_up_groups_in_tag_then_group_order(
    *, competency_taxonomy, tag, course_run, default_rule_profile, caplog, after_commit,
) -> None:
    """
    Groups are processed by competency tag, then by group, so every receiver takes the Tag locks in one order.

    The second competency's groups and association are created first, so they have the lower primary keys, while
    its tag has the higher one. Only the receiver's ordering puts the first competency's groups first.
    """
    tag_b = Tag.objects.create(taxonomy=competency_taxonomy, value="Other Competency")
    groups_b = _tree(tag_b, course_run)
    object_tag_b = _make_object_tag(competency_taxonomy, tag_b, OTHER_OBJECT_ID)
    groups_a = _tree(tag, course_run)
    object_tag_a = _make_object_tag(competency_taxonomy, tag, OTHER_OBJECT_ID)
    _make_criterion(groups_b[2], object_tag_b, default_rule_profile)
    _make_criterion(groups_a[2], object_tag_a, default_rule_profile)
    lock_object_tags_for_deletion([object_tag_a.pk, object_tag_b.pk])

    with caplog.at_level(logging.INFO, logger=signal_handlers.__name__), after_commit():
        delete_object_tags(OTHER_OBJECT_ID)

    logged_ids = _ids_named_in(_cleanup_lines(caplog))
    assert len(logged_ids) == 6
    assert set(logged_ids[:3]) == {g.pk for g in groups_a}
    assert set(logged_ids[3:]) == {g.pk for g in groups_b}


@pytest.mark.parametrize("error", [RuntimeError("boom"), OperationalError("Deadlock found")], ids=["error", "db_error"])
def test_association_archive_cleanup_failure_is_logged_and_criteria_stay_unarchived(
    error: Exception, *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, monkeypatch, caplog,
    after_commit,
) -> None:
    """
    A failing cascade is logged and undoes that group's transaction, including the criterion archive.

    The association archive has already committed, so it stands, and a database error is swallowed like any other.
    """
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    monkeypatch.setattr(signal_handlers, "_cascade_remove_from_group", _fail_with(error))

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__), after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)

    assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is False
    assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Group cleanup failed after ObjectTags [{object_tag.pk}] archived; "
        f"CompetencyCriteriaGroups left in place: {sorted([leaf.pk, course_level.pk, root.pk])}"
    )
    assert record.exc_info is not None


@pytest.mark.parametrize("error", [RuntimeError("boom"), OperationalError("Deadlock found")], ids=["error", "db_error"])
def test_a_failure_finding_the_criteria_of_an_archived_association_is_logged(
    error: Exception, *, tag, course_run, competency_taxonomy, object_tag, default_rule_profile, monkeypatch, caplog,
    after_commit,
) -> None:
    """The query that groups the live criteria fails after the archive committed; the failure is logged, not raised."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    def _fail(_alias):
        raise error

    failing_criterion = SimpleNamespace(objects=SimpleNamespace(using=_fail))
    monkeypatch.setattr(signal_handlers, "CompetencyCriterion", failing_criterion)

    with caplog.at_level(logging.WARNING, logger=signal_handlers.__name__), after_commit():
        _archive_locked_association(object_tag, competency_taxonomy)

    assert ObjectTag.objects.get(pk=object_tag.pk).archived is True
    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is False
    assert [_archived(g) for g in (leaf, course_level, root)] == [False, False, False]
    [record] = caplog.records
    assert record.getMessage() == (
        f"Group cleanup failed after ObjectTags [{object_tag.pk}] archived; CompetencyCriteria left in place"
    )
    assert record.exc_info is not None


class _ReadsElsewhereRouter:
    """Sends reads to a database alias that is not configured, and writes to the default one."""

    def db_for_read(self, _model, **_hints):
        return "replica"

    def db_for_write(self, _model, **_hints):
        return "default"


class _WritesElsewhereRouter:
    """Sends the writes of every model the cleanup touches to a database alias that is not configured."""

    def db_for_write(self, model, **_hints):
        if model in (CompetencyCriteriaGroup, CompetencyCriterion, Tag, ObjectTag):
            return "replica"
        return None


def test_the_criterion_receiver_runs_every_query_on_the_alias_it_was_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """With reads routed to an unconfigured alias, the cleanup still completes, so no query left the sent alias."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        ObjectTag.objects.filter(pk=object_tag.pk).delete()

    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, leaf.pk]).exists()


def test_the_course_run_receivers_run_every_query_on_the_alias_they_were_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """The receivers take the alias from the delete signals, not from the router, which would send reads elsewhere."""
    root, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        CourseRun.objects.filter(pk=course_run.pk).delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()


def test_a_four_level_lineage_is_walked_on_the_alias_the_criterion_receiver_was_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """Loading each ancestor, however far up, reads on the sent alias and not through the router."""
    root, course_level, middle = _tree(tag, course_run)
    leaf = CompetencyCriteriaGroup.objects.create(tag=tag, parent=middle)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        ObjectTag.objects.filter(pk=object_tag.pk).delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk__in=[root.pk, course_level.pk, middle.pk, leaf.pk]).exists()


def test_archiving_a_leaf_with_a_status_row_runs_on_the_alias_the_criterion_receiver_was_sent(
    *, user, now, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """Saving an archived group checks its tree position, which must read on the group's own database."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        ObjectTag.objects.filter(pk=object_tag.pk).delete()

    assert [_archived(g) for g in (leaf, course_level, root)] == [True, True, True]


def test_archiving_a_root_with_a_status_row_runs_on_the_alias_the_course_run_receivers_were_sent(
    *, user, now, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """The root's archive writes through the alias of the CourseRun delete, whatever the router says."""
    root, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=root, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        CourseRun.objects.filter(pk=course_run.pk).delete()

    assert _archived(root) is True


def test_locking_an_object_tag_for_a_course_deletion_runs_on_the_alias_the_receivers_were_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """The reconcile locks an ObjectTag only archived criteria use on the alias of the CourseRun delete."""
    _, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    _archived_criterion_elsewhere_uses(object_tag, tag, course_run, default_rule_profile)

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        CourseRun.objects.filter(pk=course_run.pk).delete()

    assert ObjectTag.objects.get(pk=object_tag.pk).archived is True


@pytest.mark.parametrize("has_group_status", [False, True], ids=["removes_the_groups", "archives_the_groups"])
def test_the_criterion_receiver_writes_on_the_alias_it_was_sent(
    has_group_status: bool, *, user, now, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """With the cleanup's models routed elsewhere for writes, the cleanup still completes on the sent alias."""
    root, course_level, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)
    if has_group_status:
        StudentCompetencyCriteriaGroupStatus.objects.create(
            user=user, group=leaf, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
        )

    with override_settings(DATABASE_ROUTERS=[_WritesElsewhereRouter()]), after_commit():
        ObjectTag.objects.using("default").filter(pk=object_tag.pk).delete()

    groups = [leaf, course_level, root]
    if has_group_status:
        assert [_archived(g) for g in groups] == [True, True, True]
    else:
        assert not CompetencyCriteriaGroup.objects.filter(pk__in=[g.pk for g in groups]).exists()


def test_the_course_run_receivers_write_on_the_alias_they_were_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """With the cleanup's models routed elsewhere for writes, the course cleanup still completes on the sent alias."""
    root, _, leaf = _tree(tag, course_run)
    _make_criterion(leaf, object_tag, default_rule_profile)

    with override_settings(DATABASE_ROUTERS=[_WritesElsewhereRouter()]), after_commit():
        CourseRun.objects.using("default").filter(pk=course_run.pk).delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk=root.pk).exists()
    assert not ObjectTag.objects.filter(pk=object_tag.pk).exists()


def test_the_association_archive_receiver_runs_every_query_on_the_alias_it_was_sent(
    *, tag, course_run, object_tag, default_rule_profile, after_commit,
) -> None:
    """With reads routed to an unconfigured alias, the cleanup still completes, so no query left the sent alias."""
    root, course_level, leaf = _tree(tag, course_run)
    criterion = _make_criterion(leaf, object_tag, default_rule_profile)
    lock_object_tags_for_deletion(object_tag.pk)

    with override_settings(DATABASE_ROUTERS=[_ReadsElsewhereRouter()]), after_commit():
        ObjectTag.objects.filter(pk=object_tag.pk).delete()

    assert CompetencyCriterion.objects.get(pk=criterion.pk).archived is True
    assert [_archived(g) for g in (root, course_level, leaf)] == [True, True, True]
