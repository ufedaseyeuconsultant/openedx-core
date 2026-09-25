"""
Public API for Competency-Based Education (CBE).
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey, UsageKey

from openedx_catalog.api import get_course_run, get_course_run_ids
from openedx_catalog.models import CourseRun
from openedx_tagging.api import get_object_tags, tag_object
from openedx_tagging.models import ObjectTag, Tag, Taxonomy

from .models import CompetencyCriteriaGroup, CompetencyCriterion, CompetencyRuleProfile, LogicOperator

__all__ = [
    "associate_competency_criterion",
    "create_competency_criterion",
    "get_competency_criteria_tree",
    "get_competency_rule_profiles",
    "is_competency_taxonomy",
    "parse_course_keys",
    "resolve_competency_tag",
    "create_leaf_group",
    "resolve_supplied_leaf_group",
    "select_competency_taxonomies",
]


def get_competency_rule_profiles() -> QuerySet[CompetencyRuleProfile]:
    """
    Return every live CompetencyRuleProfile, in ascending ``id`` order.

    UNSTABLE: the rule profile family is incomplete, so the create, update, and archive entry
    points still to come may change this function's shape without a deprecation cycle.

    Archived profiles are left out: retirement is archive-only and a profile is never hard
    deleted (:ref:`openedx-learning-adr-0002` Decision 7), so an unfiltered result would grow
    without bound.

    The ordering is part of the contract rather than a cosmetic detail: an unordered queryset
    gives a paginating caller overlapping and skipped pages.
    """
    return CompetencyRuleProfile.objects.filter(archived=False).order_by("id")


def is_competency_taxonomy(taxonomy: Taxonomy) -> bool:
    """
    Return True if ``taxonomy`` is competency-enabled, i.e. has a CompetencyTaxonomy row.

    Costs one query per call unless ``taxonomy`` came from a queryset passed through
    :func:`select_competency_taxonomies`. Returns False for an unsaved ``taxonomy``.
    """
    # "competencytaxonomy" is the accessor Django generates for the multi-table-inheritance
    # link from Taxonomy to CompetencyTaxonomy.
    return hasattr(taxonomy, "competencytaxonomy")


def select_competency_taxonomies(taxonomies: QuerySet[Taxonomy]) -> QuerySet[Taxonomy]:
    """
    Return ``taxonomies`` with each CompetencyTaxonomy row joined in.

    Pair this with :func:`is_competency_taxonomy` when checking more than one taxonomy,
    so the check costs no additional query per row.
    """
    return taxonomies.select_related("competencytaxonomy")


def resolve_competency_tag(tag_id: int) -> Tag:
    """
    Return the competency Tag identified by ``tag_id``.

    Raises Http404 if no such Tag exists, or if it isn't on a CompetencyTaxonomy. Shared by
    the view and :func:`associate_competency_criterion` so both agree on what counts as valid.
    """
    tag = get_object_or_404(Tag, pk=tag_id)
    # Explicit None check narrows the type for mypy and gives a precise 404 reason.
    if tag.taxonomy is None or not is_competency_taxonomy(tag.taxonomy):
        raise Http404("Tag is not on a CompetencyTaxonomy.")
    return tag


@dataclass
class CompetencyCriteriaTree:
    """The full non-archived CompetencyCriteriaGroup tree and CompetencyCriterion leaves for one competency."""

    groups: list[CompetencyCriteriaGroup]
    criteria: list[CompetencyCriterion]
    criteria_count: int
    total_criteria_count: int


def get_competency_criteria_tree(
    tag_id: int, course_keys: list[CourseKey] | None = None,
) -> CompetencyCriteriaTree:
    """
    Return every non-archived CompetencyCriteriaGroup and CompetencyCriterion for competency
    `tag_id`.

    `course_keys=None` (the default) returns everything, unfiltered. `course_keys=[]` returns
    only the instance-wide subtree (the root). A non-empty list adds the complete subtree for
    each named course run, in the order given; an unresolvable or duplicate key is dropped or
    collapsed to its first occurrence, never an error.

    `criteria_count` is `len(criteria)`. `total_criteria_count` is the same count unfiltered
    by `course_keys`, so a caller scoped down to zero results can still tell the competency
    isn't actually empty.
    """
    groups_qs = CompetencyCriteriaGroup.objects.filter(tag_id=tag_id, archived=False)

    course_run_ids_by_key: dict[CourseKey, int] = {}
    if course_keys is not None:
        course_run_ids_by_key = get_course_run_ids(course_keys)
        course_run_ids = set(course_run_ids_by_key.values())
        groups_qs = groups_qs.filter(
            Q(parent__isnull=True) | Q(course_id__in=course_run_ids) | Q(parent__course_id__in=course_run_ids)
        )

    groups = list(groups_qs.select_related("course"))
    criteria = list(
        CompetencyCriterion.objects.filter(group__in=groups, archived=False).select_related("object_tag")
    )

    if course_keys is not None:
        groups_by_id = {group.id: group for group in groups}
        # A leaf's parent is usually already in `groups` (it matches course_id__in on its own
        # row), but not if the parent is archived -- the join above checks the leaf's archived
        # flag, not the parent's. Look such parents up directly instead of ranking their
        # children as unscoped.
        parent_ids_of_courseless_groups = (group.parent_id for group in groups if group.course_id is None)
        missing_parent_ids: set[int] = {
            parent_id for parent_id in parent_ids_of_courseless_groups
            if parent_id is not None and parent_id not in groups_by_id
        }
        parent_course_id_by_id = dict(
            CompetencyCriteriaGroup.objects.filter(id__in=missing_parent_ids).values_list("id", "course_id")
        ) if missing_parent_ids else {}

        rank_by_course_run_id = {
            run_id: index
            for index, run_id in enumerate(
                course_run_ids_by_key[key] for key in course_keys if key in course_run_ids_by_key
            )
        }

        def _group_rank(group: CompetencyCriteriaGroup) -> int:
            effective_course_id = group.course_id
            if effective_course_id is None and group.parent_id is not None:
                parent = groups_by_id.get(group.parent_id)
                if parent is not None:
                    effective_course_id = parent.course_id
                else:
                    effective_course_id = parent_course_id_by_id.get(group.parent_id)
            return rank_by_course_run_id.get(effective_course_id, -1)

        groups.sort(key=lambda g: (_group_rank(g), g.id))
        criteria.sort(key=lambda c: (_group_rank(groups_by_id[c.group_id]), c.id))

    total_criteria_count = CompetencyCriterion.objects.filter(
        group__tag_id=tag_id, group__archived=False, archived=False,
    ).count()

    return CompetencyCriteriaTree(
        groups=groups, criteria=criteria,
        criteria_count=len(criteria), total_criteria_count=total_criteria_count,
    )


MAX_COURSE_KEYS = 100


def parse_course_keys(raw: str) -> list[CourseKey]:
    """
    Parse a comma-separated course_keys query parameter into a deduplicated list of CourseKey.

    Stray separators (trailing, doubled, or all-commas) are ignored, not rejected. Raises
    ValidationError if more than MAX_COURSE_KEYS entries remain after splitting -- checked
    before dedup, so repeating a key can't dodge the cap -- or if any entry isn't a valid key.
    """
    entries = [entry.strip() for entry in raw.split(",")]
    entries = [entry for entry in entries if entry]
    if len(entries) > MAX_COURSE_KEYS:
        raise ValidationError(
            {"course_keys": _("No more than %(max)s course_keys may be requested at once.") % {"max": MAX_COURSE_KEYS}}
        )
    parsed = []
    for entry in entries:
        try:
            parsed.append(CourseKey.from_string(entry))
        except InvalidKeyError as exc:
            raise ValidationError(
                {"course_keys": _("'%(entry)s' is not a valid course key.") % {"entry": entry}}
            ) from exc
    return list(dict.fromkeys(parsed))


def create_leaf_group(
    tag: Tag,
    course_run: CourseRun,
    logic_operator: str | None = None,
) -> CompetencyCriteriaGroup:
    """
    Return a fresh leaf CompetencyCriteriaGroup under ``tag``'s root/course-level groups.

    Gets or creates the root and course-level groups (race-safe via migration 0004's partial
    UniqueConstraints), then always creates a new leaf: a tag/course pair can hold more than
    one leaf, one per criterion.
    """
    with transaction.atomic():
        root, _ = CompetencyCriteriaGroup.objects.get_or_create(
            tag=tag,
            parent=None,
            defaults={"name": f"{tag.value} (root)"},
        )
        course_level_group, _ = CompetencyCriteriaGroup.objects.get_or_create(
            tag=tag,
            course=course_run,
            parent=root,
            defaults={"name": f"{tag.value} — {course_run.title}"},
        )
        return CompetencyCriteriaGroup.objects.create(
            tag=tag,
            course=None,
            parent=course_level_group,
            logic_operator=logic_operator or LogicOperator.OR,
        )


def resolve_supplied_leaf_group(group_id: int, tag: Tag, course_run: CourseRun) -> CompetencyCriteriaGroup:
    """
    Return the CompetencyCriteriaGroup identified by ``group_id``, validated as a usable leaf.

    Raises Http404 if it doesn't exist, or ValidationError (keyed "group_id") if it isn't a
    leaf, doesn't belong to ``tag``, or its course doesn't match ``course_run``.
    """
    group = get_object_or_404(CompetencyCriteriaGroup, pk=group_id)
    if group.parent_id is None or group.course_id is not None:
        raise ValidationError({"group_id": _("group_id must reference a leaf CompetencyCriteriaGroup.")})
    if group.tag_id != tag.id:
        raise ValidationError({"group_id": _("group_id must belong to the given competency tag.")})
    if group.parent.course_id != course_run.id:
        raise ValidationError({"group_id": _("group_id must belong to the given course.")})
    return group


def create_competency_criterion(
    group: CompetencyCriteriaGroup,
    object_id: str,
    rule_profile_id: int | None = None,
    rule_type_override: str | None = None,
    rule_payload_override: dict | None = None,
) -> CompetencyCriterion:
    """
    Create and return a CompetencyCriterion under ``group``, tagging ``object_id`` along the way.

    ``tag_object()`` replaces an object's full tag list for a taxonomy rather than appending, so
    this reads the object's existing tags first and unions in the new one, rather than silently
    dropping a sibling competency's tag under the same taxonomy.

    When no rule fields are supplied, resolves to the seeded system-default CompetencyRuleProfile
    rather than persisting null: ADR-0002 Decision 4 always resolves to a concrete profile, and
    the model's own xor-override constraint forbids leaving all three fields null.
    """
    tag = group.tag
    # tag.taxonomy_id is nullable at the model level; already guaranteed set by the caller.
    assert tag.taxonomy_id is not None
    existing_values = [existing_tag.value for existing_tag in get_object_tags(object_id, taxonomy_id=tag.taxonomy_id)]
    if tag.value not in existing_values:
        existing_values.append(tag.value)
    tag_object(object_id, tag.taxonomy, existing_values)
    object_tag = ObjectTag.objects.get(object_id=object_id, taxonomy_id=tag.taxonomy_id, tag_id=tag.id)

    if rule_profile_id is None and rule_type_override is None and rule_payload_override is None:
        rule_profile_id = CompetencyRuleProfile.objects.get(
            organization__isnull=True,
            course__isnull=True,
            competency_taxonomy__isnull=True,
            archived=False,
        ).id

    # #666 inserts its parent-competency dominance/containment check here, before creation.

    return CompetencyCriterion.objects.create(
        group=group,
        object_tag=object_tag,
        rule_profile_id=rule_profile_id,
        rule_type_override=rule_type_override,
        rule_payload_override=rule_payload_override,
    )


def associate_competency_criterion(
    tag_id: int,
    object_id: str,
    *,
    group_id: int | None = None,
    logic_operator: str | None = None,
    competency_rule_profile_id: int | None = None,
    rule_type_override: str | None = None,
    rule_payload_override: dict | None = None,
) -> CompetencyCriterion:
    """
    Associate ``object_id`` with the competency ``tag_id`` names, creating a criterion for it.

    The public entry point for #665's create-criterion endpoint; the caller is expected to have
    already checked ``oel_tagging.can_tag_object``. Rejects re-targeting the same group a
    (tag_id, object_id) pair already uses, and rejects a duplicate via the derive-or-create
    path; a *different* explicit group is a deliberate second association per ADR-0002, not a
    duplicate. Branches on ``group_id`` to resolve or derive/create a leaf, then creates the
    criterion, all inside one transaction so a downstream failure rolls back any group just
    created.

    Raises ValidationError (never DRF's) on rejected input, Http404 if tag_id/group_id don't
    resolve.
    """
    tag = resolve_competency_tag(tag_id)
    # tag.taxonomy is already confirmed set; re-asserted since that doesn't cross function boundaries for mypy.
    assert tag.taxonomy_id is not None

    try:
        usage_key = UsageKey.from_string(object_id)
    except InvalidKeyError as exc:
        raise ValidationError({"object_id": _("object_id is not a valid usage key.")}) from exc

    try:
        course_run = get_course_run(usage_key.course_key)
    except CourseRun.DoesNotExist as exc:
        raise ValidationError({"object_id": _("No course run matches object_id's course.")}) from exc

    existing_object_tag = ObjectTag.objects.filter(
        object_id=object_id, taxonomy_id=tag.taxonomy_id, tag_id=tag.id,
    ).first()
    if existing_object_tag is not None:
        duplicate_criteria = CompetencyCriterion.objects.filter(object_tag=existing_object_tag)
        if group_id is None:
            # Can't know which group slot is intended, so any existing criterion is a duplicate.
            if duplicate_criteria.exists():
                raise ValidationError(
                    {"object_id": _("A CompetencyCriterion already associates this tag with this object_id.")}
                )
        elif duplicate_criteria.filter(group_id=group_id).exists():
            # A different explicit group is a deliberate second association (ADR-0002), not a duplicate.
            raise ValidationError(
                {"group_id": _("A CompetencyCriterion already associates this tag with this object_id in this group.")}
            )

    if group_id is not None and logic_operator is not None:
        raise ValidationError({"logic_operator": _("group_id and logic_operator cannot both be supplied.")})

    with transaction.atomic():
        if group_id is not None:
            group = resolve_supplied_leaf_group(group_id, tag, course_run)
        else:
            group = create_leaf_group(tag, course_run, logic_operator)
        return create_competency_criterion(
            group=group,
            object_id=object_id,
            rule_profile_id=competency_rule_profile_id,
            rule_type_override=rule_type_override,
            rule_payload_override=rule_payload_override,
        )
