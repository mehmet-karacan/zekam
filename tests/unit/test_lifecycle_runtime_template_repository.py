from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
    LifecycleRuntimeTemplateRepository,
)


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.query = ""
        self.params: tuple[object, ...] = ()

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.query, self.params = query, params

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.current_cursor = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.current_cursor


def _row() -> tuple[object, ...]:
    return (
        uuid4(), digest("context"), uuid4(), digest("route"),
        dt.datetime(2030, 1, 1, tzinfo=dt.UTC), uuid4(), digest("target"), "model-a",
        uuid4(), digest("provider"), "provider:model-a", "endpoint:a", "invoke",
        uuid4(), digest("environment"), digest("capability"), digest("tools"),
        digest("config"),
        digest("hooks"),
        digest("hook-config"),
        digest("compiled-tools"),
    )


def test_current_maps_exact_template_and_uses_stale_gates() -> None:
    connection = _Connection([_row()])
    project_id, realm_id = uuid4(), uuid4()
    template = LifecycleRuntimeTemplateRepository(connection, realm_id).current(
        project_id, "source-a", digest("policy")
    )
    assert template.project_id == project_id
    assert template.model_id == "model-a"
    assert template.operation == "invoke"
    assert template.compiled_tool_set_digest == digest("compiled-tools")
    assert "statement_timestamp()" in connection.current_cursor.query
    assert "probe.drift_dimensions='{}'::text[]" in connection.current_cursor.query
    assert connection.current_cursor.params.count(realm_id) == 5


@pytest.mark.parametrize("rows", [[], [_row(), _row()]])
def test_current_fails_closed_when_missing_or_ambiguous(
    rows: list[tuple[object, ...]],
) -> None:
    with pytest.raises(PolicyViolation, match="eksik, stale veya belirsiz"):
        LifecycleRuntimeTemplateRepository(_Connection(rows), uuid4()).current(
            uuid4(), "source-a", digest("policy")
        )


def test_current_rejects_unbound_inputs_before_query() -> None:
    repository = LifecycleRuntimeTemplateRepository(_Connection([]), uuid4())
    with pytest.raises(PolicyViolation, match="source revision"):
        repository.current(uuid4(), " ", digest("policy"))
    with pytest.raises(ValidationFailed):
        repository.current(uuid4(), "source-a", "not-a-digest")


def test_bootstrap_parent_selector_is_exact_and_bounded() -> None:
    parent_id, realm_id = uuid4(), uuid4()
    connection = _Connection([(parent_id,)])
    assert (
        LifecycleRuntimeTemplateRepository(connection, realm_id).next_bootstrap_job_id()
        == parent_id
    )
    assert "limit 2" in connection.current_cursor.query
    assert "cardinality(write_resources)=1" in connection.current_cursor.query


def test_bootstrap_parent_selector_fails_closed_on_ambiguity() -> None:
    repository = LifecycleRuntimeTemplateRepository(
        _Connection([(uuid4(),), (uuid4(),)]), uuid4()
    )
    with pytest.raises(PolicyViolation, match="belirsiz"):
        repository.next_bootstrap_job_id()
