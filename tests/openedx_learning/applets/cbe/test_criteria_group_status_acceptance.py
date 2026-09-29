"""
#642 acceptance-criteria tests for StudentCompetencyCriteriaGroupStatus that
test_criteria_group_status.py does not already cover.
"""
import importlib
from datetime import datetime

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, models

from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyMasteryStatus,
    MasteryStatus,
    StudentCompetencyCriteriaGroupStatus,
)

pytestmark = pytest.mark.django_db


def test_group_status_row_is_updated_in_place(user, group: CompetencyCriteriaGroup, now: datetime) -> None:
    """
    Learner status rows are updated in place, one row per learner and node under a unique constraint.
    """
    row = StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=group, status_id=MasteryStatus.PARTIALLY_ATTEMPTED, created=now, modified=now,
    )

    StudentCompetencyCriteriaGroupStatus.objects.filter(user=user, group=group).update(
        status_id=MasteryStatus.DEMONSTRATED, modified=now,
    )

    rows = StudentCompetencyCriteriaGroupStatus.objects.filter(user=user, group=group)
    assert list(rows.values_list("pk", "status_id")) == [(row.pk, MasteryStatus.DEMONSTRATED)]


def test_group_status_carries_created_and_modified() -> None:
    """
    Each table carries both `created` and `modified`. Both are caller-supplied UTC datetimes rather than
    `auto_now_add` and `auto_now`; the `learner_status` module docstring explains why.
    """
    fields = {field.name: field for field in StudentCompetencyCriteriaGroupStatus._meta.concrete_fields}

    assert isinstance(fields["created"], models.DateTimeField)
    assert isinstance(fields["modified"], models.DateTimeField)


def test_group_status_has_no_history_package_applied() -> None:
    """
    No history package is applied.
    """
    assert not hasattr(StudentCompetencyCriteriaGroupStatus, "history")
    registered = {model.__name__ for model in apps.get_app_config("openedx_learning").get_models()}
    assert "HistoricalStudentCompetencyCriteriaGroupStatus" not in registered


@pytest.mark.parametrize("status", list(MasteryStatus), ids=[status.label for status in MasteryStatus])
def test_group_status_accepts_every_status_value(
    user, group: CompetencyCriteriaGroup, now: datetime, status: MasteryStatus
) -> None:
    """
    The models accept any status value the caller writes.
    """
    row = StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=group, status_id=status, created=now, modified=now,
    )

    row.refresh_from_db()
    assert row.status_id == status


def test_group_status_accepts_a_lower_status_written_over_a_higher_one(
    user, group: CompetencyCriteriaGroup, now: datetime
) -> None:
    """
    No monotone-write logic and no staff-edit path land here. The models accept any status value the
    caller writes, so a lower status written over a higher one takes effect.
    """
    row = StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=group, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    changed = StudentCompetencyCriteriaGroupStatus.objects.filter(pk=row.pk).update(
        status_id=MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED, modified=now,
    )

    assert changed == 1
    row.refresh_from_db()
    assert row.status_id == MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED


def test_group_status_has_only_the_adr_0002_decision_6_columns() -> None:
    """
    No column exists on this model beyond those in ADR-0002 Decision 6 and the timestamps this issue lists.
    """
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(
            cursor, StudentCompetencyCriteriaGroupStatus._meta.db_table
        )

    assert {column.name for column in description} == {
        "id", "competency_criteria_group_id", "user_id", "status_id", "created", "modified",
    }


def test_group_status_foreign_keys_target_the_adr_0002_tables() -> None:
    """
    All FK relationships match the ADR definitions exactly, targets included: the learner `user_id` points
    at `settings.AUTH_USER_MODEL` rather than `auth.User`.
    """
    user_field = StudentCompetencyCriteriaGroupStatus._meta.get_field("user")
    group_field = StudentCompetencyCriteriaGroupStatus._meta.get_field("group")
    status_field = StudentCompetencyCriteriaGroupStatus._meta.get_field("status")
    assert isinstance(user_field, models.ForeignKey)
    assert isinstance(group_field, models.ForeignKey)
    assert isinstance(status_field, models.ForeignKey)

    assert user_field.swappable_setting == "AUTH_USER_MODEL"
    assert user_field.related_model is get_user_model()
    assert group_field.related_model is CompetencyCriteriaGroup
    assert group_field.column == "competency_criteria_group_id"
    assert status_field.related_model is CompetencyMasteryStatus


def test_group_status_migration_declares_the_swappable_user_dependency() -> None:
    """
    The learner `user_id` points at `settings.AUTH_USER_MODEL`, with `migrations.swappable_dependency`
    declared in the migration, so that deployments with a swapped user model still work.
    """
    dependencies = importlib.import_module(
        "openedx_learning.migrations.0012_studentcompetencycriteriagroupstatus"
    ).Migration.dependencies

    assert any(getattr(dependency, "setting", None) == settings.AUTH_USER_MODEL for dependency in dependencies)
