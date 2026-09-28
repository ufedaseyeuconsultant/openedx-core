"""Tests pinning #613's whole-feature acceptance criteria across the models #641 and #642 add."""
import inspect
from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.apps import apps
from django.core.management import call_command
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet

from openedx_learning.applets.cbe.models import criteria, learner_status

pytestmark = pytest.mark.django_db

MODELS_613_ADDS = [
    "CompetencyCriteriaGroup",
    "CompetencyRuleProfile",
    "CompetencyCriterion",
    "CompetencyMasteryStatus",
    "StudentCompetencyStatus",
    "StudentCompetencyCriteriaStatus",
    "StudentCompetencyCriteriaGroupStatus",
]
# The PII scan counts the models django-simple-history generates for #641 as models too.
HISTORICAL_MODELS_613_ADDS = [
    "HistoricalCompetencyCriteriaGroup",
    "HistoricalCompetencyCriterion",
    "HistoricalCompetencyRuleProfile",
]
SAFE_LIST_PATH = Path(__file__).resolve().parents[4] / ".annotation_safe_list.yml"


def test_migrations_are_present_for_every_model_change() -> None:
    """
    Migrations are present: `makemigrations --check` finds no model change that lacks a migration.
    """
    call_command("makemigrations", "openedx_learning", "--check", "--dry-run", stdout=StringIO())


def test_migrations_apply_cleanly_from_scratch() -> None:
    """
    Migrations apply cleanly from scratch: the test database, built from every app's first migration up,
    has no conflicting leaf and nothing left unapplied.
    """
    # This proves "from scratch" only when pytest runs without --reuse-db, as the gate runs do.
    executor = MigrationExecutor(connection)

    assert not executor.loader.detect_conflicts()
    assert not executor.migration_plan(executor.loader.graph.leaf_nodes())


@pytest.mark.parametrize("model_name", MODELS_613_ADDS + HISTORICAL_MODELS_613_ADDS)
def test_every_model_613_adds_is_annotated_no_pii(model_name: str) -> None:
    """
    All new models are registered in `.annotation_safe_list.yml` (or inline docstrings), and every one of
    them, the three `StudentCompetency*Status` models included, is annotated `.. no_pii:`.
    """
    model = apps.get_model("openedx_learning", model_name)
    safe_list_entry = yaml.safe_load(SAFE_LIST_PATH.read_text()).get(f"openedx_learning.{model_name}") or {}

    assert ".. no_pii:" in (model.__doc__ or "") or ".. no_pii:" in safe_list_entry


@pytest.mark.parametrize("model_name", MODELS_613_ADDS + HISTORICAL_MODELS_613_ADDS)
def test_no_model_613_adds_uses_pii_retirement_consumer_api(model_name: str) -> None:
    """
    `pii_retirement: consumer_api` is not used, because it asserts a consumer-facing retirement API that
    openedx-core does not have.
    """
    model = apps.get_model("openedx_learning", model_name)
    safe_list_entry = yaml.safe_load(SAFE_LIST_PATH.read_text()).get(f"openedx_learning.{model_name}") or {}

    assert "consumer_api" not in (model.__doc__ or "")
    assert "consumer_api" not in str(safe_list_entry)


def test_no_todo_comment_is_attached_to_any_on_delete_value() -> None:
    """
    No `TODO` comment is attached to any of these nine `on_delete` values; all nine are final.
    """
    for module in (criteria, learner_status):
        assert "TODO" not in inspect.getsource(module)


@pytest.mark.parametrize("model_name", MODELS_613_ADDS)
def test_no_model_613_adds_overrides_delete(model_name: str) -> None:
    """
    No `delete()` override and no archive-versus-delete branch lands in this issue: nothing here implements
    deletion behavior in code.
    """
    model = apps.get_model("openedx_learning", model_name)

    assert model.delete is models.Model.delete
    default_manager = model._meta.default_manager
    assert default_manager is not None
    assert type(default_manager.get_queryset()).delete is QuerySet.delete


@pytest.mark.parametrize("model_name", MODELS_613_ADDS)
def test_no_model_613_adds_a_deletion_lock_field(model_name: str) -> None:
    """
    No deletion-lock field lands in this issue.
    """
    model = apps.get_model("openedx_learning", model_name)

    assert not [field.name for field in model._meta.concrete_fields if "lock" in field.name]
