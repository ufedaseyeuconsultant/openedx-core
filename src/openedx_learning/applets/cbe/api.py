"""
Public API for Competency-Based Education (CBE).
"""
from __future__ import annotations

from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import UsageKey

from openedx_catalog.api import get_course_run
from openedx_catalog.models import CourseRun
from openedx_tagging.api import get_object_tags, tag_object
from openedx_tagging.models import ObjectTag, Tag, Taxonomy
from openedx_tagging.rules import ObjectTagPermissionItem, UserType

from .models import CompetencyCriteriaGroup, CompetencyCriterion, CompetencyRuleProfile, LogicOperator

__all__ = [
    "CompetencyCriterionArchivedError",
    "associate_competency_criterion",
    "bulk_update_competency_criteria",
    "create_competency_criterion",
    "get_competency_rule_profiles",
    "is_competency_taxonomy",
    "resolve_competency_tag",
    "create_leaf_group",
    "resolve_supplied_leaf_group",
    "select_competency_taxonomies",
]


class CompetencyCriterionArchivedError(Exception):
    """
    Raised when a request would edit a CompetencyCriterion that has been archived.
    """


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


def _get_system_default_rule_profile() -> CompetencyRuleProfile | None:
    """
    Return the system-default profile, or None once it has been archived.
    """
    return CompetencyRuleProfile.objects.filter(
        organization__isnull=True,
        course__isnull=True,
        competency_taxonomy__isnull=True,
        archived=False,
    ).first()


def _resolve_applicable_rule_profile(
    criterion: CompetencyCriterion,  # pylint: disable=unused-argument
    system_default: CompetencyRuleProfile | None,
) -> CompetencyRuleProfile | None:
    """
    Return the profile ADR-0002 Decision 4's assignment table gives ``criterion``, or None.

    A stand-in for the shared resolution helper #679 owns. Only the system default exists in this
    phase, so every criterion resolves to ``system_default``, which the caller looks up once per
    batch so a large batch costs no extra queries. Callers still ask per criterion: once scoped
    profiles exist, two criteria in the same group need not resolve to the same one.
    """
    return system_default


def _validate_rule_source(
    competency_rule_profile_id: int | None,
    rule_type_override: str | None,
    rule_payload_override: dict | None,
) -> CompetencyRuleProfile | None:
    """
    Validate the one rule source a bulk update applies, returning the named profile if there is one.

    Exactly one of a live profile's id, or both override values, must be supplied; ADR-0002
    Decision 4 allows a criterion no other state. Override values are validated once for the whole
    batch through the model's own ``full_clean()``, so the payload contract has no second copy here.
    Raises ValidationError keyed by the REST field names.
    """
    has_override = rule_type_override is not None or rule_payload_override is not None
    if competency_rule_profile_id is not None and has_override:
        raise ValidationError(
            {"rule_profile_id": _("rule_profile_id and the rule override fields are mutually exclusive.")}
        )
    if competency_rule_profile_id is not None:
        profile = CompetencyRuleProfile.objects.filter(pk=competency_rule_profile_id).first()
        if profile is None:
            raise ValidationError({"rule_profile_id": _("No CompetencyRuleProfile has this id.")})
        # ADR-0002 Decision 3 keeps archived profiles out of new associations.
        if profile.archived:
            raise ValidationError({"rule_profile_id": _("An archived CompetencyRuleProfile cannot be assigned.")})
        return profile
    if not has_override:
        raise ValidationError(
            {NON_FIELD_ERRORS: _("Supply rule_profile_id, or both rule_type_override and rule_payload_override.")}
        )
    if rule_type_override is None:
        raise ValidationError(
            {"rule_type_override": _("The rule is incompletely specified: rule_type_override is missing.")}
        )
    if rule_payload_override is None:
        raise ValidationError(
            {"rule_payload_override": _("The rule is incompletely specified: rule_payload_override is missing.")}
        )
    CompetencyCriterion(
        rule_type_override=rule_type_override,
        rule_payload_override=rule_payload_override,
    ).full_clean(exclude=["group", "object_tag", "rule_profile"], validate_unique=False, validate_constraints=False)
    return None


def _apply_rule_source(
    criterion: CompetencyCriterion,
    profile: CompetencyRuleProfile | None,
    rule_type_override: str | None,
    rule_payload_override: dict | None,
    user: UserType,
    *,
    system_default: CompetencyRuleProfile | None,
) -> None:
    """
    Point ``criterion`` at ``profile``, or give it the override values, saving only if that changes it.

    Override values that exactly match the criterion's applicable profile reassign it to that
    profile instead. All three rule columns are written in one save, so the criterion's new history
    row never records both a profile and overrides, or neither.
    """
    if profile is None:
        applicable = _resolve_applicable_rule_profile(criterion, system_default)
        # Compares the parsed rule, never stored JSON text, so key order and formatting don't matter.
        if (
            applicable is not None
            and applicable.rule_type == rule_type_override
            and applicable.rule_payload == rule_payload_override
        ):
            profile = applicable

    target: tuple[int | None, str | None, dict | None]
    if profile is not None:
        target, change_reason = (profile.id, None, None), "Reassigned to rule profile"
    else:
        target, change_reason = (None, rule_type_override, rule_payload_override), "Rule values set"
    if (criterion.rule_profile_id, criterion.rule_type_override, criterion.rule_payload_override) == target:
        return

    criterion.rule_profile_id, criterion.rule_type_override, criterion.rule_payload_override = target
    # Set on the instance so attribution doesn't depend on simple_history's request middleware.
    criterion._history_user = user  # type: ignore[attr-defined]  # pylint: disable=protected-access
    criterion._change_reason = change_reason  # type: ignore[attr-defined]  # pylint: disable=protected-access
    # Never QuerySet.update(): it skips post_save, so simple_history would record nothing.
    criterion.save(update_fields=["rule_profile", "rule_type_override", "rule_payload_override"])


def bulk_update_competency_criteria(
    criterion_ids: list[int],
    competency_criteria_group_id: int,
    *,
    competency_rule_profile_id: int | None = None,
    rule_type_override: str | None = None,
    rule_payload_override: dict | None = None,
    user: UserType,
) -> list[CompetencyCriterion]:
    """
    Apply one rule source to every criterion ``criterion_ids`` names in one leaf group, atomically.

    Supply either ``competency_rule_profile_id`` or both override values; each criterion's three
    rule columns are then rewritten together, so a profile clears any overrides and overrides clear
    any profile. Learner status is never touched.

    Returns the named criteria in request order. Raises PermissionDenied if ``user`` fails
    ``oel_tagging.can_tag_object`` for the group's taxonomy and course, Http404 if the group or a
    named criterion doesn't resolve, and ValidationError, keyed by the REST field names, for a
    non-leaf group, an empty or repeated id list, or an invalid rule source. Raises
    CompetencyCriterionArchivedError if any named criterion is archived.
    """
    with transaction.atomic():
        group = get_object_or_404(
            CompetencyCriteriaGroup.objects.select_related("tag__taxonomy", "parent__course"),
            pk=competency_criteria_group_id,
        )
        # Criteria hang only off leaf groups, and a leaf's course is on its course-level parent.
        if group.parent is None or group.course_id is not None or group.parent.course is None:
            raise ValidationError({"group_id": _("group_id must reference a leaf CompetencyCriteriaGroup.")})
        # A group's tag is always a competency tag, so its taxonomy is always set.
        assert group.tag.taxonomy is not None
        perm_obj = ObjectTagPermissionItem(taxonomy=group.tag.taxonomy, object_id=str(group.parent.course.course_key))
        if not user.has_perm("oel_tagging.can_tag_object", perm_obj):
            raise PermissionDenied

        if not criterion_ids:
            raise ValidationError({"criterion_ids": _("criterion_ids must name at least one criterion.")})
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValidationError({"criterion_ids": _("criterion_ids must not name the same criterion twice.")})
        profile = _validate_rule_source(competency_rule_profile_id, rule_type_override, rule_payload_override)

        criteria_by_id = CompetencyCriterion.objects.filter(group=group).in_bulk(criterion_ids)
        if len(criteria_by_id) != len(criterion_ids):
            raise Http404("Not every criterion_id names a criterion in this group.")

        # One archived criterion refuses the whole batch: ADR 0002 Decision 4 closes archived criteria to authoring.
        archived_ids = [criterion_id for criterion_id in criterion_ids if criteria_by_id[criterion_id].archived]
        if archived_ids:
            raise CompetencyCriterionArchivedError(
                _("Archived criteria cannot be edited: {criterion_ids}.").format(
                    criterion_ids=", ".join(str(criterion_id) for criterion_id in archived_ids)
                )
            )

        # Looked up once for the whole batch, and only when override values need comparing against it.
        system_default = _get_system_default_rule_profile() if profile is None else None
        criteria = [criteria_by_id[criterion_id] for criterion_id in criterion_ids]
        for criterion in criteria:
            _apply_rule_source(
                criterion, profile, rule_type_override, rule_payload_override, user, system_default=system_default,
            )
        return criteria
