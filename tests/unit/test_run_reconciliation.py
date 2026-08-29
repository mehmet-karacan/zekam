from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from zekam.application.run_reconciliation import TerminalRunReconciliationService
from zekam.domain.errors import PolicyViolation
from zekam.domain.realm import Realm

REALM_ID = UUID("00000000-0000-8000-8000-000000000001")
PROJECT_ID = UUID("00000000-0000-8000-8000-000000000002")
WORK_ID = UUID("00000000-0000-8000-8000-000000000003")
PLAN_ID = UUID("00000000-0000-8000-8000-000000000004")
RUN_ID = UUID("00000000-0000-8000-8000-000000000005")
NEWER_RUN_ID = UUID("00000000-0000-8000-8000-000000000006")
JOB_ID = UUID("00000000-0000-8000-8000-000000000007")
ATTEMPT_ID = UUID("00000000-0000-8000-8000-000000000008")
CLAIM_ID = UUID("00000000-0000-8000-8000-000000000009")
RECEIPT_ID = UUID("00000000-0000-8000-8000-00000000000a")
NOW = dt.datetime(2026, 8, 29, tzinfo=dt.UTC)


class _Cursor:
    def __init__(self, *, newer: bool) -> None:
        self.newer = newer
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, _parameters: object) -> None:
        if "from runtime.execution_run" in statement and "created_at>%s" not in statement:
            self.rows = [(PROJECT_ID, WORK_ID, PLAN_ID, NOW)]
        elif "from runtime.job job" in statement and "left join" in statement:
            self.rows = [
                (
                    JOB_ID,
                    "completed",
                    "client-lifecycle-bootstrap",
                    ATTEMPT_ID,
                    "succeeded",
                    "sha256:" + "1" * 64,
                    CLAIM_ID,
                    RECEIPT_ID,
                    "completed",
                    "sha256:" + "2" * 64,
                )
            ]
        elif "from runtime.lease" in statement:
            self.rows = [(0,)]
        elif "created_at>%s" in statement:
            self.rows = [(NEWER_RUN_ID,)] if self.newer else []
        else:  # pragma: no cover - sorgu drift'i testi acikca bozsun
            raise AssertionError(statement)

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self.rows else self.rows[0]

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _Connection:
    def __init__(self, *, newer: bool) -> None:
        self.newer = newer

    def cursor(self) -> _Cursor:
        return _Cursor(newer=self.newer)


def _realm() -> Realm:
    return Realm(id=REALM_ID, slug="yerel", display_name="Yerel", created_at=NOW)


def test_completed_only_run_requires_and_binds_newer_superseding_run() -> None:
    plan = TerminalRunReconciliationService(_Connection(newer=True), _realm()).prepare(
        run_id=RUN_ID, now=NOW + dt.timedelta(minutes=1)
    )
    assert plan.mode == "superseded-completed-only"
    assert plan.superseded_by_run_id == NEWER_RUN_ID
    assert plan.as_dict()["target_state"] == "failed"
    assert plan.as_dict()["superseded_by_run_id"] == str(NEWER_RUN_ID)


def test_completed_only_run_without_newer_run_fails_closed() -> None:
    service = TerminalRunReconciliationService(_Connection(newer=False), _realm())
    with pytest.raises(PolicyViolation, match="newer superseding run"):
        service.prepare(run_id=RUN_ID, now=NOW + dt.timedelta(minutes=1))
