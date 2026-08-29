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
    def __init__(self, *, newer: bool, current_source: str, ready_close: bool) -> None:
        self.newer = newer
        self.current_source = current_source
        self.ready_close = ready_close
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, _parameters: object) -> None:
        if "from runtime.execution_run" in statement and "created_at>%s" not in statement:
            self.rows = [(PROJECT_ID, WORK_ID, PLAN_ID, NOW, "git:old")]
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
                    ["client.lifecycle.codex-bootstrap"],
                )
            ]
            if self.ready_close:
                self.rows.append(
                    (
                        UUID("00000000-0000-8000-8000-00000000000b"),
                        "ready",
                        "projection-aware-close",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        ["client.lifecycle.projection-close"],
                    )
                )
        elif "from runtime.lease" in statement:
            self.rows = [(0,)]
        elif "created_at>%s" in statement:
            self.rows = [(NEWER_RUN_ID,)] if self.newer else []
        elif "from projects.source_binding" in statement:
            self.rows = [(self.current_source,)]
        else:  # pragma: no cover - sorgu drift'i testi acikca bozsun
            raise AssertionError(statement)

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self.rows else self.rows[0]

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _Connection:
    def __init__(
        self, *, newer: bool, current_source: str = "git:old", ready_close: bool = False
    ) -> None:
        self.newer = newer
        self.current_source = current_source
        self.ready_close = ready_close

    def cursor(self) -> _Cursor:
        return _Cursor(
            newer=self.newer,
            current_source=self.current_source,
            ready_close=self.ready_close,
        )


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


def test_completed_only_run_without_superseding_run_or_source_fails_closed() -> None:
    service = TerminalRunReconciliationService(_Connection(newer=False), _realm())
    with pytest.raises(PolicyViolation, match="newer run veya superseding source"):
        service.prepare(run_id=RUN_ID, now=NOW + dt.timedelta(minutes=1))


def test_completed_only_run_accepts_newer_canonical_source() -> None:
    plan = TerminalRunReconciliationService(
        _Connection(newer=False, current_source="git:new"), _realm()
    ).prepare(run_id=RUN_ID, now=NOW + dt.timedelta(minutes=1))
    assert plan.mode == "source-superseded-completed-only"
    assert plan.superseded_by_run_id is None
    assert plan.superseded_by_source_revision == "git:new"


def test_source_superseded_run_can_cancel_exact_unclaimed_ready_close() -> None:
    plan = TerminalRunReconciliationService(
        _Connection(newer=False, current_source="git:new", ready_close=True), _realm()
    ).prepare(run_id=RUN_ID, now=NOW + dt.timedelta(minutes=1))
    assert plan.cancelled_job_ids == (
        UUID("00000000-0000-8000-8000-00000000000b"),
    )
