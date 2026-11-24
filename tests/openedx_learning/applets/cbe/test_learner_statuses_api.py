"""
Tests for get_learner_competency_statuses(), the read of one learner's statuses for a course run (#738).

Fixtures live in this directory's conftest.py.
"""
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NamedTuple

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from opaque_keys.edx.keys import CourseKey
from organizations.models import Organization

from openedx_catalog.models import CourseRun
from openedx_learning.applets.cbe.api import (
    LearnerCompetencyStatus,
    LearnerCompetencyStatuses,
    LearnerCourseRun,
    LearnerCriterionStatus,
    get_course_competency_criteria,
    get_learner_competency_statuses,
)
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriteriaGroupStatus,
    StudentCompetencyCriterionStatus,
    StudentCompetencyStatus,
)
from openedx_tagging.models import ObjectTag, Tag, Taxonomy
from openedx_tagging.models.base import TAXONOMY_MAX_DEPTH

from .utils import make_course_run, make_leaf_criterion, make_tag, usage_key

pytestmark = pytest.mark.django_db

# The ancestor tags, the competency statuses and the criterion statuses.
QUERIES_FOR_STATUSES = 3
# The course-level group statuses read when a competency status is Demonstrated.
QUERIES_FOR_DEMONSTRATED_IN_COURSE = 1


@pytest.fixture(name="taxonomy_b")
def _taxonomy_b() -> CompetencyTaxonomy:
    """A second competency taxonomy, created after (so with a higher id than) `competency_taxonomy`."""
    return CompetencyTaxonomy.objects.create(name="Accounting", export_id="accounting-v1")


class Skills(NamedTuple):
    """Competency tags in two taxonomies: a three-level chain with a sibling in the first, one tag in the second."""

    problem_solving: Tag
    problem_definition: Tag
    root_cause: Tag
    fishbone: Tag
    communication: Tag


@pytest.fixture(name="skills")
def _skills(competency_taxonomy: CompetencyTaxonomy, taxonomy_b: CompetencyTaxonomy) -> Skills:
    """
    Tags "Problem Solving" > "Problem Definition" > {"Root Cause Analysis", "Fishbone Diagrams"} in A.

    "Communication" is a top-level tag in B.
    """
    problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    problem_definition = make_tag(competency_taxonomy, "Problem Definition", parent=problem_solving)
    root_cause = make_tag(competency_taxonomy, "Root Cause Analysis", parent=problem_definition)
    fishbone = make_tag(competency_taxonomy, "Fishbone Diagrams", parent=problem_definition)
    communication = make_tag(taxonomy_b, "Communication")
    return Skills(problem_solving, problem_definition, root_cause, fishbone, communication)


def _key(course_run: CourseRun) -> CourseKey:
    """Return `course_run`'s course key."""
    key = course_run.course_key
    assert key is not None
    return key


def _set_criterion_status(user, criterion: CompetencyCriterion, status: MasteryStatus, now: datetime) -> None:
    """Store `status` as `user`'s status for `criterion`."""
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=status, created=now, modified=now,
    )


def _set_competency_status(user, tag: Tag, status: MasteryStatus, now: datetime) -> None:
    """Store `status` as `user`'s status for the competency `tag`."""
    StudentCompetencyStatus.objects.create(user=user, tag=tag, status_id=status, created=now, modified=now)


def _set_group_status(user, group: CompetencyCriteriaGroup, status: MasteryStatus, modified: datetime) -> None:
    """Store `status` as `user`'s status for `group`, last changed at `modified`."""
    StudentCompetencyCriteriaGroupStatus.objects.create(
        user=user, group=group, status_id=status, created=modified, modified=modified,
    )


def _add_criterion(
    group: CompetencyCriteriaGroup, tag: Tag, course_run: CourseRun, block_id: str, rule_profile: CompetencyRuleProfile,
) -> CompetencyCriterion:
    """Create a live criterion for the subsection `block_id` of `course_run` under `group`."""
    object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, block_id), taxonomy=tag.taxonomy, tag=tag,
    )
    return CompetencyCriterion.objects.create(group=group, object_tag=object_tag, rule_profile=rule_profile)


def _course_level_group(criterion: CompetencyCriterion) -> CompetencyCriteriaGroup:
    """Return the course-level group above the bottom-tier group that `criterion` was made under."""
    course_level = criterion.group.parent
    assert course_level is not None
    return course_level


def _criteria_queries(course_key: CourseKey) -> int:
    """
    Return how many queries get_course_competency_criteria() runs for `course_key` as the data stands now.

    That count is the part of get_learner_competency_statuses()'s cost that belongs to the criteria read, so
    the tests below pin only what the statuses read adds to it.
    """
    with CaptureQueriesContext(connection) as queries:
        get_course_competency_criteria(course_key)
    return len(queries)


def _tag_ids(result) -> list[int]:
    """Return the tag ids of a result's competency statuses, in order."""
    return [row.tag_id for row in result.competency_statuses]


def _criterion_ids(result) -> list[int]:
    """Return the criterion ids of a result's criterion statuses, in order."""
    return [row.criterion_id for row in result.criterion_statuses]


# =================================================================================================
# Result contents
# =================================================================================================


def test_statuses_are_returned_by_id_as_mastery_statuses(
    user, now: datetime, course_run: CourseRun, skills: Skills, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Each stored status is returned under its criterion or tag id as a MasteryStatus, in id order whatever the
    order the rows were stored in. A criterion with no row is absent.
    """
    first = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "b1")
    second = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "b2")
    make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "b3")
    make_leaf_criterion(skills.communication, course_run, default_rule_profile, "b4")
    _set_criterion_status(user, second, MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED, now)
    _set_criterion_status(user, first, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.communication, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.root_cause, MasteryStatus.PARTIALLY_ATTEMPTED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert result == LearnerCompetencyStatuses(
        competency_statuses=[
            LearnerCompetencyStatus(
                tag_id=skills.root_cause.id, status=MasteryStatus.PARTIALLY_ATTEMPTED, demonstrated_in_course=None,
            ),
            LearnerCompetencyStatus(
                tag_id=skills.communication.id, status=MasteryStatus.DEMONSTRATED, demonstrated_in_course=None,
            ),
        ],
        criterion_statuses=[
            LearnerCriterionStatus(criterion_id=first.id, status=MasteryStatus.DEMONSTRATED),
            LearnerCriterionStatus(criterion_id=second.id, status=MasteryStatus.ATTEMPTED_NOT_DEMONSTRATED),
        ],
    )


def test_status_reads_are_ordered_in_the_database(
    user, now: datetime, course_run: CourseRun, skills: Skills, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    Both lists, and the choice of the earliest group with a row id tie-break, are ordered by the database. The
    unique indexes return rows in id order on SQLite whether or not the query asks for it, so the test checks the
    SQL that runs.
    """
    criterion = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "b1")
    _set_criterion_status(user, criterion, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.root_cause, MasteryStatus.DEMONSTRATED, now)

    with CaptureQueriesContext(connection) as queries:
        get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    def order_by_of(model) -> str:
        """Return the ORDER BY clause of the one captured query that reads `model`'s table."""
        from_table = f"FROM {connection.ops.quote_name(model._meta.db_table)}"
        matching = [q["sql"] for q in queries if from_table in q["sql"]]
        assert len(matching) == 1
        assert "ORDER BY" in matching[0]
        return matching[0].split("ORDER BY")[1]

    order_by_of(StudentCompetencyStatus)
    order_by_of(StudentCompetencyCriterionStatus)
    group_order = order_by_of(StudentCompetencyCriteriaGroupStatus)
    assert group_order.index(connection.ops.quote_name("modified")) < group_order.index(connection.ops.quote_name("id"))


def test_another_learners_statuses_are_not_returned(
    user, now: datetime, course_run: CourseRun, skills: Skills, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Only rows of the requested user are read."""
    other_user = type(user).objects.create(username="other")
    criterion = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "b1")
    _set_criterion_status(other_user, criterion, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(other_user, skills.root_cause, MasteryStatus.PARTIALLY_ATTEMPTED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert result == LearnerCompetencyStatuses(competency_statuses=[], criterion_statuses=[])


def test_ancestor_statuses_follow_the_remaining_lineage(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    competency_taxonomy: CompetencyTaxonomy,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    The statuses of a competency and of its ancestors are returned, and those of tags the course does not
    assess are not. An ancestor's status is dropped once the last competency that has it in its lineage is
    archived, as that competency is dropped from the criteria result.
    """
    make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "r1")
    make_leaf_criterion(skills.fishbone, course_run, default_rule_profile, "f1")
    make_leaf_criterion(skills.communication, course_run, default_rule_profile, "c1")
    for tag in skills:
        _set_competency_status(user, tag, MasteryStatus.PARTIALLY_ATTEMPTED, now)
    unassessed = make_tag(competency_taxonomy, "Unassessed", parent=skills.problem_solving)
    _set_competency_status(user, unassessed, MasteryStatus.PARTIALLY_ATTEMPTED, now)
    key = _key(course_run)

    everything = get_learner_competency_statuses(user_id=user.id, course_key=key)
    Tag.objects.filter(pk=skills.root_cause.pk).update(archived=True)
    one_sibling_left = get_learner_competency_statuses(user_id=user.id, course_key=key)
    Tag.objects.filter(pk=skills.fishbone.pk).update(archived=True)
    no_sibling_left = get_learner_competency_statuses(user_id=user.id, course_key=key)

    assert _tag_ids(everything) == sorted(
        [skills.problem_solving.id, skills.problem_definition.id, skills.root_cause.id, skills.fishbone.id]
        + [skills.communication.id]
    )
    assert unassessed.id not in _tag_ids(everything)
    assert _tag_ids(one_sibling_left) == sorted(
        [skills.problem_solving.id, skills.problem_definition.id, skills.fishbone.id, skills.communication.id]
    )
    assert _tag_ids(no_sibling_left) == [skills.communication.id]


def test_competency_at_maximum_tag_depth_returns_the_status_of_every_ancestor(
    user,
    now: datetime,
    course_run: CourseRun,
    competency_taxonomy: CompetencyTaxonomy,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A competency at the deepest tag level, with five ancestors, has the status of each of them returned, so the
    ancestor read reaches every level a tag can have above it.
    """
    chain: list[Tag] = []
    for depth in range(TAXONOMY_MAX_DEPTH + 1):
        chain.append(make_tag(competency_taxonomy, f"Level {depth}", parent=chain[-1] if chain else None))
    make_leaf_criterion(chain[-1], course_run, default_rule_profile, "d5")
    for tag in chain:
        _set_competency_status(user, tag, MasteryStatus.PARTIALLY_ATTEMPTED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert _tag_ids(result) == sorted(tag.id for tag in chain)


class _ArchivedStateCase(NamedTuple):
    """A way a record is hidden from get_course_competency_criteria() that the authoring API's cascades prevent."""

    name: str
    # Applies the archived state, given the test's criteria by name and its tags by field name.
    archive: Callable[[dict[str, CompetencyCriterion], dict[str, Tag]], object]
    hidden_criteria: list[str]
    hidden_tags: list[str]


def _archive_group_with_live_children(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive the group of criterion "a1", which has a live child group."""
    del tags
    return CompetencyCriteriaGroup.objects.filter(pk=criteria["a1"].group.pk).update(archived=True)


def _archive_object_tag_under_live_criterion(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive the ObjectTag of criterion "a3", leaving the criterion itself live."""
    del tags
    return ObjectTag.objects.filter(pk=criteria["a3"].object_tag.pk).update(archived=True)


def _archive_all_criteria_of_a_group(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive criterion "f", the only criterion of a live group."""
    del tags
    return CompetencyCriterion.objects.filter(pk=criteria["f"].pk).update(archived=True)


def _archive_course_level_group(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive the course-level group of "Root Cause Analysis", above the bottom-tier groups of "a1", "a2", "a3"."""
    del tags
    return CompetencyCriteriaGroup.objects.filter(pk=_course_level_group(criteria["a1"]).pk).update(archived=True)


def _archive_taxonomy(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive the taxonomy of the "Problem Solving" tags without archiving any of its tags."""
    del criteria
    return Taxonomy.objects.filter(tag=tags["root_cause"]).update(archived=True)


def _disable_taxonomy(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Disable the taxonomy of the "Problem Solving" tags."""
    del criteria
    return Taxonomy.objects.filter(tag=tags["root_cause"]).update(enabled=False)


def _archive_ancestor_tag(criteria: dict[str, CompetencyCriterion], tags: dict[str, Tag]) -> int:
    """Archive "Problem Definition", which sits above two assessed competencies."""
    del criteria
    return Tag.objects.filter(pk=tags["problem_definition"].pk).update(archived=True)


@pytest.mark.parametrize(
    "case",
    [
        _ArchivedStateCase("archived group with live children", _archive_group_with_live_children, ["a1", "a2"], []),
        _ArchivedStateCase(
            "archived object tag under a live criterion", _archive_object_tag_under_live_criterion, ["a3"], [],
        ),
        _ArchivedStateCase(
            "live group with all criteria archived", _archive_all_criteria_of_a_group, ["f"], ["fishbone"],
        ),
        _ArchivedStateCase(
            "live tag under an archived ancestor", _archive_ancestor_tag, ["a1", "a2", "a3", "f"],
            ["problem_solving", "problem_definition", "root_cause", "fishbone"],
        ),
        _ArchivedStateCase(
            "archived course-level group", _archive_course_level_group, ["a1", "a2", "a3"], ["root_cause"],
        ),
        _ArchivedStateCase(
            "archived taxonomy with live tags", _archive_taxonomy, ["a1", "a2", "a3", "f"],
            ["problem_solving", "problem_definition", "root_cause", "fishbone"],
        ),
        _ArchivedStateCase(
            "disabled taxonomy", _disable_taxonomy, ["a1", "a2", "a3", "f"],
            ["problem_solving", "problem_definition", "root_cause", "fishbone"],
        ),
    ],
    ids=lambda case: case.name,
)
def test_statuses_follow_733_for_each_archived_state(
    case: _ArchivedStateCase,
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    For each state the authoring API's cascades prevent, the statuses of the hidden criteria and tags are not
    returned and every other stored status is.
    """
    a1 = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "a1")
    a2_group = CompetencyCriteriaGroup.objects.create(tag=skills.root_cause, parent=a1.group)
    a2 = _add_criterion(a2_group, skills.root_cause, course_run, "a2", default_rule_profile)
    a3 = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "a3")
    fishbone = make_leaf_criterion(skills.fishbone, course_run, default_rule_profile, "f")
    communication = make_leaf_criterion(skills.communication, course_run, default_rule_profile, "c")
    criteria = {"a1": a1, "a2": a2, "a3": a3, "f": fishbone, "c": communication}
    for criterion in criteria.values():
        _set_criterion_status(user, criterion, MasteryStatus.DEMONSTRATED, now)
    tags = skills._asdict()
    for tag in tags.values():
        _set_competency_status(user, tag, MasteryStatus.PARTIALLY_ATTEMPTED, now)

    case.archive(criteria, tags)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert not {criteria[name].id for name in case.hidden_criteria} & set(_criterion_ids(result))
    assert not {tags[name].id for name in case.hidden_tags} & set(_tag_ids(result))
    assert len(_criterion_ids(result)) == len(criteria) - len(case.hidden_criteria)
    assert len(_tag_ids(result)) == len(tags) - len(case.hidden_tags)


def test_deep_subtree_returns_statuses_at_every_depth(
    user, now: datetime, course_run: CourseRun, skills: Skills, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """Criteria in groups nested deeper than three levels below the course-level group have their statuses returned."""
    criteria = [make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "d0")]
    group = criteria[0].group
    for depth in range(1, 5):
        group = CompetencyCriteriaGroup.objects.create(tag=skills.root_cause, parent=group)
        criteria.append(_add_criterion(group, skills.root_cause, course_run, f"d{depth}", default_rule_profile))
    for criterion in criteria:
        _set_criterion_status(user, criterion, MasteryStatus.DEMONSTRATED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert _criterion_ids(result) == [criterion.id for criterion in criteria]


# =================================================================================================
# demonstrated_in_course
# =================================================================================================


def test_demonstrated_in_course_names_the_other_run(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A Demonstrated competency carries the key and title of another course run in which the learner's
    course-level group for it is Demonstrated. Other groups of that run do not count, and an ancestor is named in
    the same way.
    """
    other_run = make_course_run(organization, "Python200", "Fall2026")
    make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "r1")
    in_other_run = make_leaf_criterion(skills.root_cause, other_run, default_rule_profile, "r2")
    ancestor_in_other_run = make_leaf_criterion(skills.problem_definition, other_run, default_rule_profile, "p1")
    for tag in (skills.root_cause, skills.problem_definition, skills.problem_solving):
        _set_competency_status(user, tag, MasteryStatus.DEMONSTRATED, now)
    # Rows that must not be named even though they are older: the other run's root group and bottom-tier group.
    earlier = now - timedelta(days=30)
    root = _course_level_group(in_other_run).parent
    assert root is not None
    _set_group_status(user, root, MasteryStatus.DEMONSTRATED, earlier)
    # A bottom-tier group that carries a third course run, which only its depth tells apart from a course-level group.
    third_run = make_course_run(organization, "Python300", "Fall2026")
    CompetencyCriteriaGroup.objects.filter(pk=in_other_run.group.pk).update(course=third_run)
    _set_group_status(user, in_other_run.group, MasteryStatus.DEMONSTRATED, earlier)
    _set_group_status(user, _course_level_group(in_other_run), MasteryStatus.DEMONSTRATED, now)
    _set_group_status(user, _course_level_group(ancestor_in_other_run), MasteryStatus.DEMONSTRATED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    named = LearnerCourseRun(course_key=_key(other_run), title=other_run.title)
    assert result.competency_statuses == [
        LearnerCompetencyStatus(
            tag_id=skills.problem_solving.id, status=MasteryStatus.DEMONSTRATED, demonstrated_in_course=None,
        ),
        LearnerCompetencyStatus(
            tag_id=skills.problem_definition.id, status=MasteryStatus.DEMONSTRATED, demonstrated_in_course=named,
        ),
        LearnerCompetencyStatus(
            tag_id=skills.root_cause.id, status=MasteryStatus.DEMONSTRATED, demonstrated_in_course=named,
        ),
    ]


def test_demonstrated_in_course_is_the_earliest_demonstration_and_breaks_ties_by_row_id(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    With several other runs, the one whose group row was modified first is named. Rows modified at the same
    instant are told apart by row id, which differs from the order the groups were created in.
    """
    make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "r1")
    make_leaf_criterion(skills.communication, course_run, default_rule_profile, "c1")
    runs = [make_course_run(organization, f"Other{index}", "Fall2026") for index in range(3)]
    groups_by_run = {
        run: _course_level_group(make_leaf_criterion(skills.root_cause, run, default_rule_profile, f"o{index}"))
        for index, run in enumerate(runs)
    }
    tie_groups = {
        run: _course_level_group(make_leaf_criterion(skills.communication, run, default_rule_profile, f"t{index}"))
        for index, run in enumerate(runs[:2])
    }
    _set_competency_status(user, skills.root_cause, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.communication, MasteryStatus.DEMONSTRATED, now)
    # Middle run first by time, and the first run last: neither group order nor row order is the answer.
    _set_group_status(user, groups_by_run[runs[2]], MasteryStatus.DEMONSTRATED, now - timedelta(days=1))
    _set_group_status(user, groups_by_run[runs[0]], MasteryStatus.DEMONSTRATED, now - timedelta(days=20))
    _set_group_status(user, groups_by_run[runs[1]], MasteryStatus.DEMONSTRATED, now - timedelta(days=10))
    # A tie: the group of the second run was created later, but its status row was stored first.
    _set_group_status(user, tie_groups[runs[1]], MasteryStatus.DEMONSTRATED, now)
    _set_group_status(user, tie_groups[runs[0]], MasteryStatus.DEMONSTRATED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    by_tag = {row.tag_id: row.demonstrated_in_course for row in result.competency_statuses}
    assert by_tag[skills.root_cause.id] == LearnerCourseRun(course_key=_key(runs[0]), title=runs[0].title)
    assert by_tag[skills.communication.id] == LearnerCourseRun(course_key=_key(runs[1]), title=runs[1].title)


@pytest.mark.parametrize("here_first", [True, False], ids=["here first, then elsewhere", "elsewhere first, then here"])
def test_demonstrated_in_course_is_null_when_the_requested_run_is_also_demonstrated(
    here_first: bool,
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    A competency whose course-level group is Demonstrated in the requested run names no other run, whichever
    run was demonstrated first, because the learner earned it through this run's own criteria.
    """
    other_run = make_course_run(organization, "Python200", "Fall2026")
    here = _course_level_group(make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "r1"))
    elsewhere = _course_level_group(make_leaf_criterion(skills.root_cause, other_run, default_rule_profile, "r2"))
    earlier, later = now - timedelta(days=10), now
    _set_group_status(user, here, MasteryStatus.DEMONSTRATED, earlier if here_first else later)
    _set_group_status(user, elsewhere, MasteryStatus.DEMONSTRATED, later if here_first else earlier)
    _set_competency_status(user, skills.root_cause, MasteryStatus.DEMONSTRATED, now)

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert result.competency_statuses == [
        LearnerCompetencyStatus(
            tag_id=skills.root_cause.id, status=MasteryStatus.DEMONSTRATED, demonstrated_in_course=None,
        ),
    ]


def test_demonstrated_in_course_is_null_without_another_runs_demonstrated_group(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    The field is null for a competency Demonstrated only in the requested run, for one Demonstrated only
    through sub-competencies, so with no course-level group row of its own, for one whose group in the
    other run is not Demonstrated, for one that only another learner demonstrated in the other run, and for one
    whose status is not Demonstrated although its group in the other run is.
    """
    other_run = make_course_run(organization, "Python200", "Fall2026")
    only_here = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "r1")
    make_leaf_criterion(skills.fishbone, course_run, default_rule_profile, "f1")
    make_leaf_criterion(skills.communication, course_run, default_rule_profile, "c1")
    not_demonstrated_elsewhere = _course_level_group(
        make_leaf_criterion(skills.communication, other_run, default_rule_profile, "c2")
    )
    # Another learner's Demonstrated group in the other run must not be attributed to the requested learner.
    other_user = type(user).objects.create(username="other")
    _set_group_status(
        other_user,
        _course_level_group(make_leaf_criterion(skills.root_cause, other_run, default_rule_profile, "r2")),
        MasteryStatus.DEMONSTRATED,
        now,
    )
    _set_group_status(user, _course_level_group(only_here), MasteryStatus.DEMONSTRATED, now)
    _set_group_status(user, not_demonstrated_elsewhere, MasteryStatus.PARTIALLY_ATTEMPTED, now)
    # A status below Demonstrated here, while the same tag's group in the other run is Demonstrated.
    partial_here = make_leaf_criterion(skills.fishbone, other_run, default_rule_profile, "f2")
    _set_group_status(user, _course_level_group(partial_here), MasteryStatus.DEMONSTRATED, now)
    for tag in skills:
        _set_competency_status(
            user, tag, MasteryStatus.PARTIALLY_ATTEMPTED if tag == skills.fishbone else MasteryStatus.DEMONSTRATED, now,
        )

    result = get_learner_competency_statuses(user_id=user.id, course_key=_key(course_run))

    assert _tag_ids(result) == sorted(tag.id for tag in skills)
    assert [row.demonstrated_in_course for row in result.competency_statuses] == [None] * len(skills)


# =================================================================================================
# Query counts
# =================================================================================================


def test_course_run_without_criteria_costs_one_query_whatever_the_learner_has_elsewhere(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
    django_assert_num_queries,
) -> None:
    """
    A course run with no criteria costs the one query that finds no course-level group. Every later read has an
    empty ``IN`` list, which Django does not send.
    """
    other_run = make_course_run(organization, "Python200", "Fall2026")
    criterion = make_leaf_criterion(skills.root_cause, other_run, default_rule_profile, "o1")
    _set_criterion_status(user, criterion, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.root_cause, MasteryStatus.DEMONSTRATED, now)
    key = _key(course_run)

    with django_assert_num_queries(1):
        result = get_learner_competency_statuses(user_id=user.id, course_key=key)

    assert result == LearnerCompetencyStatuses(competency_statuses=[], criterion_statuses=[])


def test_query_count_does_not_grow_with_rows(
    *,
    user,
    now: datetime,
    course_run: CourseRun,
    organization: Organization,
    competency_taxonomy: CompetencyTaxonomy,
    skills: Skills,
    default_rule_profile: CompetencyRuleProfile,
    django_assert_num_queries,
) -> None:
    """A course with many competencies, criteria and status rows, and other runs, costs what a small one does."""
    extra_queries = QUERIES_FOR_STATUSES + QUERIES_FOR_DEMONSTRATED_IN_COURSE
    key = _key(course_run)
    small_criterion = make_leaf_criterion(skills.root_cause, course_run, default_rule_profile, "s1")
    _set_criterion_status(user, small_criterion, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.root_cause, MasteryStatus.DEMONSTRATED, now)

    with django_assert_num_queries(_criteria_queries(key) + extra_queries):
        small = get_learner_competency_statuses(user_id=user.id, course_key=key)
    assert len(small.criterion_statuses) == 1

    other_runs = [make_course_run(organization, f"Other{index}", "Fall2026") for index in range(3)]
    top = make_tag(competency_taxonomy, "Top")
    for index in range(6):
        tag = make_tag(competency_taxonomy, f"Skill {index}", parent=top if index % 2 else skills.problem_definition)
        for block in range(3):
            criterion = make_leaf_criterion(tag, course_run, default_rule_profile, f"k{index}-{block}")
            _set_criterion_status(user, criterion, MasteryStatus.DEMONSTRATED, now)
        _set_competency_status(user, tag, MasteryStatus.DEMONSTRATED, now)
        for run in other_runs:
            other = make_leaf_criterion(tag, run, default_rule_profile, f"x{index}")
            _set_group_status(user, _course_level_group(other), MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.problem_definition, MasteryStatus.DEMONSTRATED, now)
    _set_competency_status(user, skills.problem_solving, MasteryStatus.DEMONSTRATED, now)

    # The big dataset has the same group depth, so the criteria read costs what it did for the small one.
    with django_assert_num_queries(_criteria_queries(key) + extra_queries):
        big = get_learner_competency_statuses(user_id=user.id, course_key=key)

    assert len(big.criterion_statuses) == 1 + 6 * 3
    # The six new competencies, plus "Root Cause Analysis" and its two ancestors. "Top" has no stored row.
    assert len(big.competency_statuses) == 6 + 3
