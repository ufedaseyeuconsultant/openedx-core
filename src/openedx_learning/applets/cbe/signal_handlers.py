"""
Receivers that keep competency criteria groups in step with the deletion of their criteria.

A ``CompetencyCriterion`` is deleted by cascade when its ``ObjectTag`` is deleted, which is what
happens when Studio deletes a subsection or ``tag_object()`` removes a tag. The criterion's group,
and the course-level group and root above it, are then left with nothing under them. The receivers
here walk that lineage and delete or archive each group the same way
``delete_or_archive_competency_criterion()`` does.

The receivers react only to a delete whose origin is an ``ObjectTag`` or a ``CourseRun``, as an instance or
as a queryset. Deleting a ``CourseRun`` cascades to its course-level groups, which leaves the shared root
without a course beneath it and the ``ObjectTag`` of each deleted criterion possibly unused. A criterion
deleted for any other reason, including by ``delete_or_archive_competency_criterion()`` itself, is handled
by whoever deleted it.

The receivers never roll back the deletion that triggered them: a failure while cleaning up is logged
and the groups stay in place. They also add no knowledge of CBE to ``openedx_tagging``; the dependency
points the other way, from this applet to the tagging models.
"""
import logging

from django.db import OperationalError, transaction
from django.db.models import Model, Q, QuerySet
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from openedx_catalog.models_api import CourseRun
from openedx_tagging.models import ObjectTag, Tag

from .api import _cascade_remove_from_group, _reconcile_object_tags
from .models import CompetencyCriteriaGroup, CompetencyCriterion

log = logging.getLogger(__name__)


def _is_delete_of(origin: object, model: type[Model]) -> bool:
    """
    Return whether a delete started from an instance of ``model`` or from a queryset of it.

    ``origin`` is the ``origin`` argument Django passes to ``post_delete`` receivers. The check
    queries nothing.
    """
    if isinstance(origin, QuerySet):
        return origin.model is model
    return isinstance(origin, model)


def _lineage_state(group: CompetencyCriteriaGroup) -> dict[int, bool]:
    """Return ``{id: archived}`` for ``group``, its parent and its grandparent, in that order, skipping missing ones."""
    state: dict[int, bool] = {}
    current: CompetencyCriteriaGroup | None = group
    for _ in range(3):
        if current is None:
            break
        state[current.id] = current.archived
        current = current.parent
    return state


def _clean_up_from(group_id: int, trigger: str) -> None:
    """
    Walk up from ``group_id``, deleting or archiving each group left with no live children, and log the outcome.

    Does nothing if the group is already gone.
    Any failure other than a database ``OperationalError`` is logged and swallowed.
    """
    group = CompetencyCriteriaGroup.objects.select_related("parent__parent").filter(pk=group_id).first()
    if group is None:
        return
    before: dict[int, bool] = {}
    try:
        with transaction.atomic():
            # The same lock, for the same three races, as delete_or_archive_competency_criterion(); see its comment.
            if Tag.objects.select_for_update().filter(pk=group.tag_id).first() is None:
                return
            # Re-read under the lock: a concurrent cleanup may have removed or changed the lineage.
            group = CompetencyCriteriaGroup.objects.select_related("parent__parent").filter(pk=group_id).first()
            if group is None:
                return
            before = _lineage_state(group)
            _cascade_remove_from_group(group, None)
    except OperationalError:
        # A MySQL deadlock has already rolled back the whole transaction, so swallowing it would only move
        # the error to the next statement.
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        # A failure here must not roll back the deletion that triggered the signal. The savepoint undoes
        # any partial work, so the groups are left as they were.
        log.warning(
            "Group cleanup failed after %s; CompetencyCriteriaGroups left in place: %s",
            trigger,
            sorted(before) or [group_id],
            exc_info=True,
        )
        return
    after = dict(CompetencyCriteriaGroup.objects.filter(pk__in=before).values_list("pk", "archived"))
    for pk, was_archived in before.items():
        if pk not in after:
            log.info("Group cleanup removed CompetencyCriteriaGroup %s after %s", pk, trigger)
        elif after[pk] and not was_archived:
            log.info("Group cleanup archived CompetencyCriteriaGroup %s after %s", pk, trigger)


@receiver(post_delete, sender=CompetencyCriterion)
def _criterion_deleted(instance: CompetencyCriterion, origin=None, **kwargs) -> None:
    """Clean up the groups above a criterion that was deleted together with its ``ObjectTag``."""
    if not _is_delete_of(origin, ObjectTag):
        return
    _clean_up_from(
        instance.group_id,
        f"CompetencyCriterion {instance.pk} deleted with ObjectTag {instance.object_tag_id}",
    )


@receiver(pre_delete, sender=CompetencyCriteriaGroup)
def _course_group_about_to_be_deleted(instance: CompetencyCriteriaGroup, origin=None, **kwargs) -> None:
    """
    Remember which ``ObjectTag`` ids a course-level group's criteria use, before they are deleted.

    Django deletes the criteria before the groups, so ``post_delete`` can no longer read them.
    Django passes the same instance object to both signals, so the ids are kept on it.
    """
    if not _is_delete_of(origin, CourseRun) or instance.course_id is None:
        return
    instance._cleanup_object_tag_ids = set(  # type: ignore[attr-defined]  # pylint: disable=protected-access
        CompetencyCriterion.objects.filter(Q(group_id=instance.pk) | Q(group__parent_id=instance.pk)).values_list(
            "object_tag_id", flat=True
        )
    )


@receiver(post_delete, sender=CompetencyCriteriaGroup)
def _course_group_deleted(instance: CompetencyCriteriaGroup, origin=None, **kwargs) -> None:
    """
    Clean up the root above a course-level group deleted with its ``CourseRun``, and the tags its criteria used.

    The two steps are independent and best-effort. Reconciling runs even when the root is already gone, which
    happens when two runs under one root are deleted together: each course-level group stashed only its own ids.
    """
    if not _is_delete_of(origin, CourseRun) or instance.course_id is None:
        return
    trigger = f"CompetencyCriteriaGroup {instance.pk} deleted with CourseRun {instance.course_id}"
    if instance.parent_id is not None:
        _clean_up_from(instance.parent_id, trigger)
    stashed_ids: set[int] = getattr(instance, "_cleanup_object_tag_ids", set())
    try:
        with transaction.atomic():
            # The same lock, for the same three races, as delete_or_archive_competency_criterion(); see its comment.
            # The walk above may have returned before taking it, so it is taken here too. Taking it again when the
            # walk already holds it is harmless.
            Tag.objects.select_for_update().filter(pk=instance.tag_id).first()
            _reconcile_object_tags(stashed_ids)
    except OperationalError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        # As in _clean_up_from(), a failure here must not roll back the course deletion that triggered the signal.
        log.warning(
            "Group cleanup failed after %s; ObjectTags left in place: %s",
            trigger,
            sorted(stashed_ids),
            exc_info=True,
        )
