"""
Tests for the CompetencyMasteryStatus lookup table and its seed data.
"""
import pytest
from django.db import IntegrityError, transaction

from openedx_learning.models import CompetencyMasteryStatus, MasteryStatus

pytestmark = pytest.mark.django_db


def test_seed_produces_three_rows_in_rank_order() -> None:
    """
    The seed_competency_mastery_statuses data migration creates exactly the three
    CompetencyMasteryStatus rows, with the pinned ids from MasteryStatus, and ids ascend in
    rank (lowest to highest mastery).
    """
    rows = list(CompetencyMasteryStatus.objects.order_by("id"))
    assert [row.id for row in rows] == [
        MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED,
        MasteryStatus.PARTIALLY_ATTEMPTED,
        MasteryStatus.DEMONSTRATED,
    ]
    assert [row.status for row in rows] == [
        MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED.label,
        MasteryStatus.PARTIALLY_ATTEMPTED.label,
        MasteryStatus.DEMONSTRATED.label,
    ]


def test_status_is_unique_on_lookup_table() -> None:
    """
    CompetencyMasteryStatus.status is unique: a second row with a status string
    that already exists raises IntegrityError.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        CompetencyMasteryStatus.objects.create(status=MasteryStatus.DEMONSTRATED.label)
