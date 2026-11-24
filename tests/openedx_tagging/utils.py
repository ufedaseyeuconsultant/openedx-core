"""
Useful utilities for testing tagging and taxonomy code.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from django.db import connection
from django.db.models import Model, QuerySet
from django.dispatch import Signal


class WritesToPrimaryRouter:
    """
    Sends reads to a database alias that is not configured, and writes to ``default``.

    A query that runs on the router's choice for reads instead of on the alias it was told to use fails.
    """

    def db_for_read(self, _model, **_hints):
        return "replica"

    def db_for_write(self, _model, **_hints):
        return "default"


@contextmanager
def received(signal: Signal, sender: type | None = None) -> Iterator[list[dict[str, Any]]]:
    """
    Connect a throwaway receiver to ``signal`` and yield the list of keyword arguments of each send.
    """
    calls: list[dict[str, Any]] = []

    def receiver(**kwargs: Any) -> None:
        calls.append(kwargs)

    signal.connect(receiver, sender=sender, weak=False, dispatch_uid=f"test-{id(receiver)}")
    try:
        yield calls
    finally:
        signal.disconnect(receiver, sender=sender, dispatch_uid=f"test-{id(receiver)}")


@contextmanager
def concurrent_write(model: type[Model], write: Callable[[], object], before: str = "delete") -> Iterator[None]:
    """
    Make `write` look like another transaction committing just before a queryset delete or update of `model`.

    `before` names the queryset method, "delete" or "update". `write` runs just before each such call, and again
    after each savepoint rollback. A rollback here would otherwise undo it, which it could not do to another
    transaction's commit. This relies on the test running inside a transaction, as `TestCase` arranges, so that
    each attempt is a savepoint. `write` has to be safe to run more than once.
    """
    real_method = getattr(QuerySet, before)
    real_rollback = connection.savepoint_rollback

    def write_then_call(queryset: QuerySet[Any], **kwargs: Any) -> Any:
        if queryset.model is model:
            with patch.object(QuerySet, before, real_method):
                write()
        return real_method(queryset, **kwargs)

    def rollback_then_write(sid: str) -> None:
        real_rollback(sid)
        write()

    with patch.object(QuerySet, before, write_then_call):
        with patch.object(connection, "savepoint_rollback", rollback_then_write):
            yield


def pretty_format_tags(result, parent=True, external_id=False) -> list[str]:
    """
    Format the result of get_filtered_tags() to be more human readable.

    Also works with other wrappers around get_filtered_tags, like api.get_tags()
    Also works with serialized TagData from the REST API.
    """
    pretty_results = []
    for t in result:
        line = f"{t['depth'] * '  '}{t['value']} "
        if external_id:
            line += f"({t['external_id']}) "
        if parent:
            line += f"({t['parent_value']}) "
        line += "("
        if "usage_count" in t:
            line += f"used: {t['usage_count']}, "
        line += f"children: {t['child_count']}"
        line += ")"
        pretty_results.append(line)
    return pretty_results
