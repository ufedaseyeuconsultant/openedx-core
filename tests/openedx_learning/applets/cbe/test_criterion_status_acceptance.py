"""
#642 acceptance-criteria tests for StudentCompetencyCriteriaStatus that test_criterion_status.py
does not already cover.
"""
import importlib
from datetime import datetime

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, models, transaction
from django.db.models import ProtectedError

from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyMasteryStatus,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaStatus,
)
from openedx_tagging.models import ObjectTag, Tag, Taxonomy

pytestmark = pytest.mark.django_db


@pytest.fixture(name="criterion")
def _criterion(
    group: CompetencyCriteriaGroup, object_tag: ObjectTag, default_rule_profile: CompetencyRuleProfile
) -> CompetencyCriterion:
    """A leaf CompetencyCriterion directly under the root `group`."""
    return CompetencyCriterion.objects.create(group=group, object_tag=object_tag, rule_profile=default_rule_profile)


def test_criterion_status_row_is_updated_in_place(user, criterion: CompetencyCriterion, now: datetime) -> None:
    """
    Learner status rows are updated in place, one row per learner and node under a unique constraint.
    """
    row = StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.PARTIALLY_ATTEMPTED, created=now, modified=now,
    )

    StudentCompetencyCriteriaStatus.objects.filter(user=user, criterion=criterion).update(
        status_id=MasteryStatus.DEMONSTRATED, modified=now,
    )

    rows = StudentCompetencyCriteriaStatus.objects.filter(user=user, criterion=criterion)
    assert list(rows.values_list("pk", "status_id")) == [(row.pk, MasteryStatus.DEMONSTRATED)]


def test_criterion_status_carries_created_and_modified() -> None:
    """
    Each table carries both `created` and `modified`. Both are caller-supplied UTC datetimes rather than
    `auto_now_add` and `auto_now`; the `learner_status` module docstring explains why.
    """
    fields = {field.name: field for field in StudentCompetencyCriteriaStatus._meta.concrete_fields}

    assert isinstance(fields["created"], models.DateTimeField)
    assert isinstance(fields["modified"], models.DateTimeField)


def test_criterion_status_has_no_history_package_applied() -> None:
    """
    No history package is applied.
    """
    assert not hasattr(StudentCompetencyCriteriaStatus, "history")
    registered = {model.__name__ for model in apps.get_app_config("openedx_learning").get_models()}
    assert "HistoricalStudentCompetencyCriteriaStatus" not in registered


@pytest.mark.parametrize("status", list(MasteryStatus), ids=[status.label for status in MasteryStatus])
def test_criterion_status_accepts_every_status_value(
    user, criterion: CompetencyCriterion, now: datetime, status: MasteryStatus
) -> None:
    """
    The models accept any status value the caller writes.
    """
    row = StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=status, created=now, modified=now,
    )

    row.refresh_from_db()
    assert row.status_id == status


def test_criterion_status_accepts_a_lower_status_written_over_a_higher_one(
    user, criterion: CompetencyCriterion, now: datetime
) -> None:
    """
    No monotone-write logic and no staff-edit path land here. The models accept any status value the
    caller writes, so a lower status written over a higher one takes effect.
    """
    row = StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    changed = StudentCompetencyCriteriaStatus.objects.filter(pk=row.pk).update(
        status_id=MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED, modified=now,
    )

    assert changed == 1
    row.refresh_from_db()
    assert row.status_id == MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED


def test_criterion_status_has_only_the_adr_0002_decision_6_columns() -> None:
    """
    No column exists on this model beyond those in ADR-0002 Decision 6 and the timestamps this issue lists.
    """
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(
            cursor, StudentCompetencyCriteriaStatus._meta.db_table
        )

    assert {column.name for column in description} == {
        "id", "competency_criteria_id", "user_id", "status_id", "created", "modified",
    }


def test_criterion_status_foreign_keys_target_the_adr_0002_tables() -> None:
    """
    All FK relationships match the ADR definitions exactly, targets included: the learner `user_id` points
    at `settings.AUTH_USER_MODEL` rather than `auth.User`.
    """
    user_field = StudentCompetencyCriteriaStatus._meta.get_field("user")
    criterion_field = StudentCompetencyCriteriaStatus._meta.get_field("criterion")
    status_field = StudentCompetencyCriteriaStatus._meta.get_field("status")
    assert isinstance(user_field, models.ForeignKey)
    assert isinstance(criterion_field, models.ForeignKey)
    assert isinstance(status_field, models.ForeignKey)

    assert user_field.swappable_setting == "AUTH_USER_MODEL"
    assert user_field.related_model is get_user_model()
    assert criterion_field.related_model is CompetencyCriterion
    assert criterion_field.column == "competency_criteria_id"
    assert status_field.related_model is CompetencyMasteryStatus


def test_criterion_status_migration_declares_the_swappable_user_dependency() -> None:
    """
    The learner `user_id` points at `settings.AUTH_USER_MODEL`, with `migrations.swappable_dependency`
    declared in the migration, so that deployments with a swapped user model still work.
    """
    dependencies = importlib.import_module(
        "openedx_learning.migrations.0011_studentcompetencycriteriastatus"
    ).Migration.dependencies

    assert any(getattr(dependency, "setting", None) == settings.AUTH_USER_MODEL for dependency in dependencies)


def test_tag_delete_with_no_status_beneath_it_cascades_the_whole_criteria_tree_away(
    user, competency_taxonomy: CompetencyTaxonomy, tag: Tag, criterion: CompetencyCriterion, now: datetime
) -> None:
    """
    Deleting an `oel_tagging_tag` with no status rows beneath it succeeds and cascades the whole criteria
    tree away, while another tag's tree holds status.
    """
    other_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Decimals")
    other_criterion = CompetencyCriterion.objects.create(
        group=CompetencyCriteriaGroup.objects.create(tag=other_tag),
        object_tag=ObjectTag.objects.create(
            object_id="block-v1:Org1+Python100+Fall2026+problem+p3", taxonomy=competency_taxonomy, tag=other_tag,
        ),
        rule_profile=criterion.rule_profile,
    )
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=other_criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    tag.delete()

    assert not CompetencyCriteriaGroup.objects.filter(pk=criterion.group_id).exists()
    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert CompetencyCriterion.objects.filter(pk=other_criterion.pk).exists()


def test_group_at_depth_delete_is_protected_by_a_criterion_status_beneath_it(
    user, group: CompetencyCriteriaGroup, criterion: CompetencyCriterion, now: datetime
) -> None:
    """
    Deleting a `CompetencyCriteriaGroup` at depth with a learner status row anywhere beneath it raises
    `ProtectedError`.
    """
    depth_1_group = CompetencyCriteriaGroup.objects.create(tag=group.tag, parent=group)
    depth_2_group = CompetencyCriteriaGroup.objects.create(tag=group.tag, parent=depth_1_group)
    criterion.group = depth_2_group
    criterion.save()
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    with pytest.raises(ProtectedError), transaction.atomic():
        depth_1_group.delete()

    assert CompetencyCriteriaGroup.objects.filter(pk__in=[depth_1_group.pk, depth_2_group.pk]).count() == 2
    assert CompetencyCriterion.objects.filter(pk=criterion.pk).exists()


def test_object_tag_delete_with_no_status_beneath_it_cascades_its_criterion_away(
    user, competency_taxonomy: CompetencyTaxonomy, tag: Tag, criterion: CompetencyCriterion, now: datetime
) -> None:
    """
    Deleting an `oel_tagging_objecttag` with no status rows beneath it succeeds and cascades its criterion
    away, while a criterion on another object tag holds status.
    """
    criterion_with_status = CompetencyCriterion.objects.create(
        group=criterion.group,
        object_tag=ObjectTag.objects.create(
            object_id="block-v1:Org1+Python100+Fall2026+problem+p4", taxonomy=competency_taxonomy, tag=tag,
        ),
        rule_profile=criterion.rule_profile,
    )
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion_with_status, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    criterion.object_tag.delete()

    assert not CompetencyCriterion.objects.filter(pk=criterion.pk).exists()
    assert CompetencyCriterion.objects.filter(pk=criterion_with_status.pk).exists()


def test_oel_tagging_taxonomy_delete_is_protected_by_a_criterion_status_beneath_its_tags(
    user, competency_taxonomy: CompetencyTaxonomy, criterion: CompetencyCriterion, now: datetime
) -> None:
    """
    Deleting an `oel_tagging_taxonomy` raises `ProtectedError` when a status row exists beneath any of its
    tags.
    """
    StudentCompetencyCriteriaStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    with pytest.raises(ProtectedError), transaction.atomic():
        Taxonomy.objects.get(pk=competency_taxonomy.pk).delete()

    assert Taxonomy.objects.filter(pk=competency_taxonomy.pk).exists()
