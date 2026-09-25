"""
Serializers for the CBE REST API, v1.
"""
from __future__ import annotations

from rest_framework import serializers

from ...models import CompetencyCriteriaGroup, CompetencyCriterion, CompetencyRuleProfile, LogicOperator


class CompetencyRuleProfileSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a CompetencyRuleProfile.

    UNSTABLE: the rule profile family is incomplete, so the create, update, and archive
    endpoints still to come may change this shape without a deprecation cycle.

    ``rule_payload`` is emitted verbatim as stored: :ref:`openedx-learning-adr-0002` Decision 3
    owns the payload contract, and normalizing it here would make this a second, competing
    definition of it. That is also why the payload's own ``scale`` key matters, since it is what
    stops a caller reading the threshold fraction as a percentage or the reverse.

    ``scope_code`` and the raw ``organization``, ``course``, and ``competency_taxonomy`` columns
    are internal bookkeeping that ADR-0002 Decision 3 keeps out of anything exported; the
    ``scope_type`` below is what a client reads instead.
    """

    scope_type = serializers.SerializerMethodField()

    class Meta:
        model = CompetencyRuleProfile
        fields = ["id", "scope_type", "rule_type", "rule_payload", "archived"]
        # scope_type is absent here because DRF refuses a field that is both declared above and
        # named in read_only_fields; a SerializerMethodField is read-only in any case.
        read_only_fields = ["id", "rule_type", "rule_payload", "archived"]

    def get_scope_type(self, profile: CompetencyRuleProfile) -> str:
        """
        Return which kind of scope ``profile`` applies to.

        All four kinds are recognized from the outset, even though only the system default can
        exist today, so enabling a narrower scope needs no edit here. The scope columns are read
        by their ``_id`` attributes so that no row costs a query, and the system default is
        recognized by those columns being null rather than by matching the internal
        ``scope_code`` string.
        """
        if profile.competency_taxonomy_id is not None:
            return "taxonomy"
        if profile.course_id is not None:
            return "course"
        if profile.organization_id is not None:
            return "organization"
        return "system_default"


class CompetencyCriterionSerializer(serializers.ModelSerializer):
    """
    Doubles as the request-body parser and the response representation for a criterion.

    ``object_id`` and ``logic_operator`` are not CompetencyCriterion fields at all (``object_id``
    isn't stored anywhere on this model; ``logic_operator`` belongs to CompetencyCriteriaGroup),
    so they're declared as plain write_only fields the view reads out of ``validated_data``, not
    model-bound fields. Every other field's JSON name matches its model attribute exactly
    (``group_id``, ``rule_profile_id``, ``object_tag_id``), so none of them need a ``source=``.
    """

    object_id = serializers.CharField(write_only=True)
    group_id = serializers.IntegerField(required=False, allow_null=True)
    # Only ever applies on the derive-or-create path (group_id omitted): it sets the AND/OR
    # operator on the brand-new leaf group this request creates. Rejected alongside an explicit
    # group_id because changing an existing leaf's operator is a future group-update endpoint's
    # job, not this one's -- a caller can't use this field to silently change an existing
    # group's behavior.
    logic_operator = serializers.ChoiceField(
        choices=LogicOperator.choices, write_only=True, required=False, allow_null=True,
    )
    rule_profile_id = serializers.IntegerField(required=False, allow_null=True)
    object_tag_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CompetencyCriterion
        fields = [
            "id", "object_id", "group_id", "logic_operator",
            "rule_profile_id", "rule_type_override", "rule_payload_override", "object_tag_id",
        ]
        read_only_fields = ["id", "object_tag_id"]


class CompetencyCriteriaGroupSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a CompetencyCriteriaGroup, for the criteria-tree read endpoint.

    course_key is a CharField with a dotted source, not a SlugRelatedField: CourseRun.course_key
    is an opaque-keys CourseKeyField, and SlugRelatedField.to_representation would return the raw
    CourseLocator object, which the JSON renderer can't serialize. CharField.to_representation
    calls str() on it instead, and DRF's dotted-source traversal already returns None cleanly
    when `course` itself is null, so no extra null-handling is needed.
    """

    course_key = serializers.CharField(source="course.course_key", read_only=True, allow_null=True)

    class Meta:
        model = CompetencyCriteriaGroup
        fields = ["id", "parent_id", "tag_id", "course_key", "name", "ordering", "logic_operator", "archived"]


class CompetencyCriterionReadSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a CompetencyCriterion, for the criteria-tree read endpoint.

    A separate serializer from CompetencyCriterionSerializer (#665's create-request serializer),
    not an added field on it: that serializer's own `object_id` is write_only, used as the
    create endpoint's *input* (the subsection to tag). Giving it a dotted source to also serve
    as this endpoint's *output* would silently break the create endpoint, because a dotted
    source on a writable field changes where to_internal_value() places the value in
    validated_data (nested under validated_data["object_tag"]["object_id"] instead of
    validated_data["object_id"]), which CompetencyCriterionCreateView.create() depends on.
    """

    object_id = serializers.CharField(source="object_tag.object_id", read_only=True)

    class Meta:
        model = CompetencyCriterion
        fields = [
            "id", "group_id", "object_tag_id", "object_id",
            "rule_profile_id", "rule_type_override", "rule_payload_override",
        ]
