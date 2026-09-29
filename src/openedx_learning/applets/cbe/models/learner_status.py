"""
Models tracking a learner's mastery status for a competency.
"""
from django.conf import settings
from django.db import models

from openedx_django_lib.fields import manual_date_time_field
from openedx_tagging.models import Tag

__all__ = [
    "MasteryStatus",
    "CompetencyMasteryStatus",
    "StudentCompetencyStatus",
]


class MasteryStatus(models.IntegerChoices):
    """
    Ranks of competency mastery, lowest to highest.
    """

    ATTEMPTED_NOT_DEMONSTRATED = 1, "AttemptedNotDemonstrated"
    PARTIALLY_ATTEMPTED = 2, "PartiallyAttempted"
    DEMONSTRATED = 3, "Demonstrated"


class CompetencyMasteryStatus(models.Model):
    """
    Lookup table of the mastery statuses a competency can be assigned.

    System-owned lookup data, seeded by the ``seed_competency_mastery_statuses`` data
    migration and treated as immutable configuration, not user-authored rows (ADR-0002
    Decision 6.1). See :class:`MasteryStatus` for the pinned ids and names of its rows.

    .. no_pii:
    """

    # ADR-0002 Decision 5 index 10.
    status = models.CharField(max_length=64, unique=True)

    def __str__(self) -> str:
        """User-facing string representation of a CompetencyMasteryStatus."""
        return self.status


class StudentCompetencyStatus(models.Model):
    """
    A learner's current mastery status for one competency (``Tag``).

    One row per learner per tag, updated in place (ADR-0003 Decision 5).

    .. no_pii:
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
    )
    tag = models.ForeignKey(
        Tag,
        db_column="oel_tagging_tag_id",
        on_delete=models.PROTECT,
        related_name="+",
    )
    status = models.ForeignKey(
        CompetencyMasteryStatus,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created = manual_date_time_field()
    modified = manual_date_time_field()

    class Meta:
        constraints = [
            # ADR-0002 Decision 5 index 8. This is what makes "one row per learner and
            # competency" true, which is the precondition for updating a status in place
            # with a conditional UPDATE: it is load-bearing, not a lookup optimization.
            models.UniqueConstraint(
                fields=("user", "tag"),
                name="oex_learning_studentcompetencystatus_user_tag_uniq",
            ),
            # Allow list, not a negation of the excluded value: a future fourth
            # status should be rejected here by default rather than silently
            # permitted.
            models.CheckConstraint(
                condition=models.Q(status__in=(MasteryStatus.PARTIALLY_ATTEMPTED, MasteryStatus.DEMONSTRATED)),
                name="oex_learning_studentcompetencystatus_status_allowed",
            ),
        ]
