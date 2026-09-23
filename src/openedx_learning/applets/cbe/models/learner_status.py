"""
The lookup table of competency mastery ranks.

:class:`CompetencyMasteryStatus` is system-owned lookup data, seeded by migration, that the
learner-status models added in the next PR in this stack point at.
"""
from django.db import models

__all__ = [
    "MasteryStatus",
    "CompetencyMasteryStatus",
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
