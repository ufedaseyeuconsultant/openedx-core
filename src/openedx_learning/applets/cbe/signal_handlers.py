"""
Receivers that keep criteria groups in step with deleted criteria and archived associations, tags, and taxonomies.

A ``CompetencyCriterion`` is deleted by cascade when its ``ObjectTag`` is deleted, which is what
happens when Studio deletes a subsection or ``tag_object()`` removes a tag. The criterion's group,
and the course-level group and root above it, are then left with nothing under them. The receivers
here walk that lineage and delete or archive each group the same way
``delete_or_archive_competency_criterion()`` does.

A tagging-layer delete removes an ``ObjectTag`` only while it is unlocked.
``lock_object_tags_for_deletion()`` locks the associations a learner status depends on, and
``_reconcile_object_tags()`` locks those that only archived criteria still use. Deleting a locked
association archives it instead, which cascades into no criteria, so none of the delete receivers fires.
The tagging layer sends ``object_tags_archived`` for it instead, and ``_associations_archived()``
archives the criteria that still use the association and each group that archiving them leaves empty.

The delete receivers react only to a delete whose origin is an ``ObjectTag`` or a ``CourseRun``. An ``ObjectTag``
always arrives as a queryset, because the tagging layer deletes through ``ObjectTagQuerySet``; a ``CourseRun``
arrives as an instance or as a queryset. Deleting a ``CourseRun`` cascades to its course-level groups,
which leaves the shared root without a course beneath it and the ``ObjectTag`` of each deleted criterion
possibly unused. A criterion deleted for any other reason, including by
``delete_or_archive_competency_criterion()`` itself, is handled by whoever deleted it.

The cleanup runs after the triggering deletion or archive commits, in a transaction of its own that takes the
competency's ``Tag`` lock first, in the same order as every authoring writer. It can never roll the deletion or the
archive back: a failure is logged and the groups stay in place. Each cleanup runs on the database alias Django or the
tagging layer sent with the signal, because the router may send reads to a replica that has not yet seen the write.
The receivers also add no knowledge of CBE to ``openedx_tagging``; the dependency points the other way, from this
applet to the tagging models.
"""
import logging

from django.db import transaction
from django.db.models import Model, QuerySet
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from openedx_catalog.models_api import CourseRun
from openedx_tagging.models import ObjectTag, Tag
from openedx_tagging.signals import object_tags_archived

from .api import (
    _cascade_remove_from_group,
    _competency_criteria_subtree,
    _parent_group,
    _reconcile_object_tags,
    _write_criterion_archived,
)
from .models import CompetencyCriteriaGroup, CompetencyCriterion

log = logging.getLogger(__name__)


def _is_delete_of(origin: object, model: type[Model]) -> bool:
    """
    Return whether a delete started from an instance of ``model`` or from a queryset of it.

    ``origin`` is the ``origin`` argument Django passes to ``post_delete`` receivers.
    """
    if isinstance(origin, QuerySet):
        return issubclass(origin.model, model)
    return isinstance(origin, model)


def _group_archived_by_id(group: CompetencyCriteriaGroup, using: str | None) -> dict[int, bool]:
    """Return ``{id: archived}`` for ``group`` and each of its ancestors, nearest first, reading on ``using``."""
    archived_by_id: dict[int, bool] = {}
    current: CompetencyCriteriaGroup | None = group
    while current is not None:
        archived_by_id[current.id] = current.archived
        current = _parent_group(current, using)
    return archived_by_id


def _clean_up_lineage(
    group_id: int, trigger: str, *, archive_object_tag_ids: set[int] | None = None, using: str | None = None
) -> None:
    """
    Walk up from ``group_id``, deleting or archiving each group left with no live children, and log the outcome.

    When ``archive_object_tag_ids`` is given, the live criteria of the group that use those associations are
    archived first, in the same transaction as the walk, so a failure leaves them live.

    Does nothing if the group is already gone. Any failure is logged and swallowed, and the groups are left as
    they were.
    """
    before: dict[int, bool] = {}
    try:
        groups = CompetencyCriteriaGroup.objects.using(using)
        group = groups.filter(pk=group_id).first()
        if group is None:
            return
        with transaction.atomic(using=using):
            # The lock serializes this walk against a concurrent criterion create in the same competency, which
            # would otherwise put a criterion into a leaf the walk is deleting, and against a concurrent delete of
            # a sibling leaf: each walk would otherwise still see the other's leaf, stop, and leave an empty group
            # behind. A unique or check constraint cannot do this: the walk's decisions depend on the absence of
            # rows.
            if Tag.objects.using(using).select_for_update().filter(pk=group.tag_id).first() is None:
                return
            # Re-read under the lock: a concurrent cleanup may have removed or changed the lineage.
            group = groups.filter(pk=group_id).first()
            if group is None:
                return
            before = _group_archived_by_id(group, using)
            if archive_object_tag_ids:
                live_criteria = CompetencyCriterion.objects.using(using).filter(
                    group_id=group.pk, object_tag_id__in=archive_object_tag_ids, archived=False
                )
                for criterion in live_criteria:
                    _write_criterion_archived(criterion, None, using)
            _cascade_remove_from_group(group, None, using)
            # Read inside the atomic block, so a failure of this read also undoes the walk.
            after = dict(
                CompetencyCriteriaGroup.objects.using(using).filter(pk__in=before).values_list("pk", "archived")
            )
    except Exception:  # pylint: disable=broad-exception-caught
        # The triggering deletion has already committed, so there is nothing to roll back. A failure before the
        # atomic block changed nothing; one inside it undid the whole walk. Either way the groups are as they were.
        log.warning(
            "Group cleanup failed after %s; CompetencyCriteriaGroups left in place: %s",
            trigger,
            sorted(before) or [group_id],
            exc_info=True,
        )
        return
    for pk, was_archived in before.items():
        if pk not in after:
            log.info("Group cleanup removed CompetencyCriteriaGroup %s after %s", pk, trigger)
        elif after[pk] and not was_archived:
            log.info("Group cleanup archived CompetencyCriteriaGroup %s after %s", pk, trigger)


def _reconcile_after_course_deletion(
    tag_id: int, object_tag_ids: set[int], trigger: str, *, using: str | None = None
) -> None:
    """Settle the ``ObjectTag`` ids a deleted course's criteria used; log and swallow any failure."""
    try:
        with transaction.atomic(using=using):
            # The lock serializes this reconcile against a concurrent criterion create that re-uses one of these
            # associations, which would otherwise gain an active criterion after the reconcile found none. A unique
            # or check constraint cannot do this: whether an association is still used depends on other rows.
            Tag.objects.using(using).select_for_update().filter(pk=tag_id).first()
            _reconcile_object_tags(object_tag_ids, using)
    except Exception:  # pylint: disable=broad-exception-caught
        log.warning(
            "Group cleanup failed after %s; ObjectTags left in place: %s",
            trigger,
            sorted(object_tag_ids),
            exc_info=True,
        )


def _clean_up_archived_associations(object_tag_ids: list[int], *, using: str | None = None) -> None:
    """
    Archive the live criteria that use the archived associations ``object_tag_ids``, then clean up each group.

    The live criteria are read before any lock is taken, restricted to associations that are archived. Each group
    is then cleaned up in its own transaction, under its competency's ``Tag`` lock, in ``(tag_id, group_id)``
    order. Any failure is logged and swallowed.
    """
    object_tag_ids_by_group: dict[tuple[int, int], set[int]] = {}
    try:
        for tag_id, group_id, object_tag_id in (
            CompetencyCriterion.objects.using(using)
            .filter(object_tag_id__in=object_tag_ids, object_tag__archived=True, archived=False)
            .values_list("group__tag_id", "group_id", "object_tag_id")
        ):
            object_tag_ids_by_group.setdefault((tag_id, group_id), set()).add(object_tag_id)
    except Exception:  # pylint: disable=broad-exception-caught
        # The archive has already committed, so there is nothing to roll back, and an exception that escaped this
        # on_commit callback would stop Django from running the remaining ones. Nothing was written, so every
        # criterion and group is as it was.
        log.warning(
            "Group cleanup failed after ObjectTags %s archived; CompetencyCriteria left in place",
            sorted(object_tag_ids),
            exc_info=True,
        )
        return
    archived_ids = {object_tag_id for ids in object_tag_ids_by_group.values() for object_tag_id in ids}
    for (_tag_id, group_id), group_object_tag_ids in sorted(object_tag_ids_by_group.items()):
        _clean_up_lineage(
            group_id,
            f"ObjectTags {sorted(group_object_tag_ids)} archived",
            archive_object_tag_ids=archived_ids,
            using=using,
        )


@receiver(object_tags_archived, sender=ObjectTag)
def _associations_archived(object_tag_ids: list[int], using: str | None = None, **kwargs) -> None:
    """
    Archive the live criteria that use newly archived associations, and each group left with no live children.

    The tagging layer sends this signal, inside the transaction that archived the associations, when it removes a
    deletion-locked ``ObjectTag``. The criteria on that association survive the removal, so their rule is archived
    by a cleanup this receiver defers until that transaction commits. The cleanup is best-effort and logged, and it
    never rolls the archive back. An archive that rolls back, including a rolled-back savepoint, never runs it,
    because Django discards the ``on_commit`` callbacks registered inside it. Re-applying the tag revives the
    association and leaves the rule archived.

    Once the archive has committed, the criteria it archived are no longer live, so the signals sent from
    ``_reconcile_object_tags()`` cost the cleanup one query. The same ids may arrive twice; the second pass finds no
    live criteria and changes nothing.

    ``using`` is the database alias the archive was written on. All of the cleanup's queries and writes run on it.
    """
    ids = list(object_tag_ids)
    transaction.on_commit(lambda: _clean_up_archived_associations(ids, using=using), using=using)


@receiver(post_delete, sender=CompetencyCriterion)
def _criterion_deleted(instance: CompetencyCriterion, origin=None, using: str | None = None, **kwargs) -> None:
    """
    Clean up the groups above a criterion that was deleted together with its ``ObjectTag``.

    Fires only for an unlocked ``ObjectTag``: deleting a locked one archives it instead, so its criteria are
    never deleted.
    """
    if not _is_delete_of(origin, ObjectTag):
        return
    group_id = instance.group_id
    trigger = f"CompetencyCriterion {instance.pk} deleted with ObjectTag {instance.object_tag_id}"
    transaction.on_commit(lambda: _clean_up_lineage(group_id, trigger, using=using), using=using)


@receiver(pre_delete, sender=CompetencyCriteriaGroup)
def _course_group_about_to_be_deleted(
    instance: CompetencyCriteriaGroup, origin=None, using: str | None = None, **kwargs
) -> None:
    """
    Remember which ``ObjectTag`` ids a course-level group's criteria use, before they are deleted.

    Django deletes the criteria before the groups, so ``post_delete`` can no longer read them.
    Django passes the same instance object to both signals, so the ids are kept on it.
    """
    if not _is_delete_of(origin, CourseRun) or instance.course_id is None:
        return
    instance._cleanup_object_tag_ids = {  # type: ignore[attr-defined]  # pylint: disable=protected-access
        criterion.object_tag_id for criterion in _competency_criteria_subtree(instance, using)[1]
    }


@receiver(post_delete, sender=CompetencyCriteriaGroup)
def _course_group_deleted(
    instance: CompetencyCriteriaGroup, origin=None, using: str | None = None, **kwargs
) -> None:
    """
    Clean up the root above a course-level group deleted with its ``CourseRun``, and the tags its criteria used.

    The tag step hands the stashed ids to ``_reconcile_object_tags()``, which keeps, archives, or deletes
    each association; its docstring states which outcome applies when.

    The two steps are independent and best-effort. Reconciling runs even when the root is already gone, which
    happens when two runs under one root are deleted together: each course-level group stashed only its own ids.
    """
    if not _is_delete_of(origin, CourseRun) or instance.course_id is None:
        return
    parent_id = instance.parent_id
    tag_id = instance.tag_id
    stashed_ids: set[int] = getattr(instance, "_cleanup_object_tag_ids", set())
    trigger = f"CompetencyCriteriaGroup {instance.pk} deleted with CourseRun {instance.course_id}"

    def clean_up() -> None:
        if parent_id is not None:
            _clean_up_lineage(parent_id, trigger, using=using)
        _reconcile_after_course_deletion(tag_id, stashed_ids, trigger, using=using)

    transaction.on_commit(clean_up, using=using)
