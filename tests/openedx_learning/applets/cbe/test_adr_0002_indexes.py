"""ADR-0002 Decision 5's ten indexes exist in the migrated schema, on every database backend."""
import pytest
from django.db import connection

pytestmark = pytest.mark.django_db

# (table, columns, unique), in ADR-0002 Decision 5's order. Indexes 2, 4 and 5 are Django's
# automatic foreign key indexes, so no Meta.indexes entry declares them.
ADR_0002_INDEXES = [
    ("openedx_learning_competencycriteriagroup", ["oel_tagging_tag_id", "course_id"], False),
    ("openedx_learning_competencycriteriagroup", ["parent_id"], False),
    ("oel_tagging_objecttag", ["object_id"], False),
    ("openedx_learning_competencycriterion", ["oel_tagging_objecttag_id"], False),
    ("openedx_learning_competencycriterion", ["competency_criteria_group_id"], False),
    ("openedx_learning_studentcompetencycriteriastatus", ["user_id", "competency_criteria_id"], True),
    ("openedx_learning_studentcompetencycriteriagroupstatus", ["user_id", "competency_criteria_group_id"], True),
    ("openedx_learning_studentcompetencystatus", ["user_id", "oel_tagging_tag_id"], True),
    ("openedx_learning_competencyruleprofile", ["scope_code"], True),
    ("openedx_learning_competencymasterystatus", ["status"], True),
]


def _indexes(table: str) -> list[tuple[list[str], bool]]:
    """Return (columns, unique) for every index on `table`."""
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            # Django's SQLite introspection cannot parse a table whose CHECK constraint contains a
            # comma, such as StudentCompetencyStatus's `status_id IN (2, 3)`, so read SQLite's own
            # index list instead. It includes the automatic indexes behind inline UNIQUE constraints.
            cursor.execute(f"PRAGMA index_list({connection.ops.quote_name(table)})")
            indexes = []
            for _seq, name, unique, *_rest in cursor.fetchall():
                cursor.execute(f"PRAGMA index_info({connection.ops.quote_name(name)})")
                indexes.append(([column for _rank, _cid, column in cursor.fetchall()], bool(unique)))
            return indexes
        constraints = connection.introspection.get_constraints(cursor, table)
    return [
        (list(constraint["columns"]), bool(constraint["unique"]))
        for constraint in constraints.values()
        if constraint["index"] or constraint["unique"]
    ]


@pytest.mark.parametrize(
    "table, columns, unique",
    ADR_0002_INDEXES,
    ids=[f"index-{number}" for number in range(1, len(ADR_0002_INDEXES) + 1)],
)
def test_adr_0002_index_exists(table: str, columns: list[str], unique: bool) -> None:
    """
    Every index from ADR-0002 Decision 5 is present, and the unique ones are unique: indexes 6, 7 and 8 are
    unique on `(user_id, node_id)`, index 9 is unique on `scope_code`, and index 10 is unique on the status value.
    """
    matches = [
        index_columns for index_columns, index_unique in _indexes(table)
        if index_columns == columns and (index_unique or not unique)
    ]
    assert matches, f"no {'unique ' if unique else ''}index on {table}({', '.join(columns)})"
