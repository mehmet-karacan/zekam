"""Current-source admission must reject a corrupt admitted config body, not just its ID."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from tests.unit.test_local_continuity_source_plan import source as source
from tests.unit.test_local_continuity_startup import startup as startup

from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest


@pytest.mark.parametrize(
    "raw",
    ["{}", "[]", '{"duplicate":1,"duplicate":2}', "null", '"' + "a" * 1048576 + '"'],
    ids=["wrong-object", "array", "duplicate", "null", "oversize"],
)
@pytest.mark.parametrize("operation", ["apply", "assert-snapshot"])
def test_source_admission_checks_current_config_payload_not_only_digest_columns(
    source: dict[str, Any], raw: str, operation: str
) -> None:
    adapter, store, plan = source["adapter"], source["operational"], source["plan"]
    snapshot = adapter.apply(store, plan, expected_plan_digest=plan.content_digest)
    with sqlite3.connect(source["path"]) as db:
        db.execute("update config_revision set sanitized_json=? where active=1", (raw,))
    before = logical_database_digest(source["path"])
    with pytest.raises(PolicyViolation, match="Source admitted configuration"):
        if operation == "apply":
            adapter.apply(store, plan, expected_plan_digest=plan.content_digest)
        else:
            adapter.assert_snapshot(store, snapshot.id)
    assert logical_database_digest(source["path"]) == before


@pytest.mark.parametrize("operation", ["apply", "assert-snapshot"])
def test_invalid_utf8_config_is_rejected_without_echo_or_source_mutation(
    source: dict[str, Any], operation: str
) -> None:
    adapter, store, plan = source["adapter"], source["operational"], source["plan"]
    snapshot = adapter.apply(store, plan, expected_plan_digest=plan.content_digest)
    poisoned = b'{"private_marker":"do-not-echo-configuration", "broken":"\xff"}'
    with sqlite3.connect(source["path"]) as db:
        db.execute(
            "update config_revision set sanitized_json=cast(? as text) where active=1", (poisoned,)
        )
        before_count = db.execute("select count(*) from source_snapshot").fetchone()[0]
    with pytest.raises(
        PolicyViolation, match="Source admitted configuration payload drift"
    ) as error:
        if operation == "apply":
            adapter.apply(store, plan, expected_plan_digest=plan.content_digest)
        else:
            adapter.assert_snapshot(store, snapshot.id)
    assert "private_marker" not in str(error.value)
    assert "do-not-echo" not in str(error.value)
    assert error.value.__cause__ is None
    with sqlite3.connect(source["path"]) as db:
        assert db.execute("select count(*) from source_snapshot").fetchone()[0] == before_count
        assert (
            db.execute(
                "select cast(sanitized_json as blob) from config_revision where active=1"
            ).fetchone()[0]
            == poisoned
        )
