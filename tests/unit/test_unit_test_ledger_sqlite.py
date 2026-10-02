from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError, PolicyViolation
from zekam.domain.unit_test_engineering import (
    AttemptState,
    CoverageMetric,
    CoverageObservation,
    CoveragePolicy,
    CoverageState,
    RatioThreshold,
    UnitTestAttempt,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.unit_test_ledger import (
    MIGRATION_NAME,
    SQLiteUnitTestLedger,
    apply_unit_test_ledger_ddl,
    migration_digest,
)

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC)
A = "mod-a/src/main/java/p/Service.java"
B = "mod-b/src/main/java/q/Service.java"


def _dg(label: str) -> str:
    return digest({"label": label})


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("pragma foreign_keys=on")
    return connection


def _prepare(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "operational.sqlite"
    result = schema.bootstrap_v6(path)
    assert result.schema_ok and result.schema_version == 6
    store = SQLiteOperationalStore(path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.commit()
    return path, project.id


def _request(project_id: str, *, max_attempts: int = 3) -> UnitTestRequest:
    return UnitTestRequest(
        project_id=project_id,
        source_binding_id="bind-1",
        source_revision="rev1",
        source_files=(A, B),
        metric=CoverageMetric.LINE,
        threshold=RatioThreshold.from_percent("90"),
        policy=CoveragePolicy.PER_FILE,
        budget=UnitTestBudget(max_attempts, 60, 600),
    )


def _attempt(request: UnitTestRequest, ordinal: int, parent: str | None) -> UnitTestAttempt:
    return UnitTestAttempt(
        f"att-{ordinal}",
        request.request_digest,
        ordinal,
        parent,
        _dg("plan"),
        _dg(f"cand-{ordinal}"),
        f"key-{ordinal}",
        ((A.replace("main", "test"), _dg(f"t{ordinal}")),),
    )


def _obs(a: tuple[int, int], b: tuple[int, int]) -> tuple[CoverageObservation, ...]:
    return (
        CoverageObservation.measured(A, CoverageMetric.LINE, covered=a[0], missed=a[1]),
        CoverageObservation.measured(B, CoverageMetric.LINE, covered=b[0], missed=b[1]),
    )


def test_no_postgresql_or_native_driver_needed() -> None:
    code = ";".join(
        [
            "import sys",
            "import zekam.domain.unit_test_engineering",
            "import zekam.application.unit_test_ledger",
            "import zekam.infrastructure.sqlite.unit_test_ledger",
            "legacy = ('psycopg', 'psycopg2', 'asyncpg', 'pg8000', 'sqlalchemy')",
            "print(','.join(m for m in sys.modules if m.split('.')[0] in legacy))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == ""


def test_migration_is_additive_on_real_operational_schema_and_pinned(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite"
    schema.bootstrap_v5(path)
    before = _open(path)
    rows_before = before.execute("select count(*) from project").fetchone()[0]
    names_before = {r[0] for r in before.execute("select name from sqlite_master")}
    before.close()
    path2, _ = _prepare(tmp_path / "second")
    connection = _open(path2)
    names_after = {r[0] for r in connection.execute("select name from sqlite_master")}
    connection.close()
    added = names_after - names_before
    assert {
        "unit_test_request",
        "unit_test_attempt",
        "unit_test_attempt_receipt",
        "unit_test_observation",
        "unit_test_terminal",
    } <= added
    assert all(name.startswith(("unit_test_", "sqlite_autoindex_unit_test_")) for name in added)
    assert rows_before == 0
    assert MIGRATION_NAME == "operational-unit-test-engineering-v6"
    assert migration_digest() == migration_digest()
    with pytest.raises(ConfigurationError):
        bare = sqlite3.connect(":memory:", isolation_level=None)
        apply_unit_test_ledger_ddl(bare)


def test_ledger_survives_reopen_and_replays_are_noops(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    connection = _open(path)
    ledger = SQLiteUnitTestLedger(connection)
    assert not ledger.register_request(request, now=NOW).replayed
    first = _attempt(request, 1, None)
    assert not ledger.claim_attempt(first, now=NOW).replayed
    assert ledger.claim_attempt(first, now=NOW).replayed
    evidence = _dg("run-1")
    assert not ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=evidence,
        observations=_obs((9, 1), (10, 0)),
        now=NOW,
    ).replayed
    connection.close()

    reopened = SQLiteUnitTestLedger(_open(path))
    assert reopened.register_request(request, now=NOW).replayed
    assert reopened.claim_attempt(first, now=NOW).replayed
    assert reopened.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=evidence,
        observations=_obs((9, 1), (10, 0)),
        now=NOW,
    ).replayed
    assert reopened.list_attempts(request.request_digest) == (first,)
    assert reopened.list_observations("att-1") == _obs((9, 1), (10, 0))
    count = reopened._db.execute("select count(*) from unit_test_observation").fetchone()[0]
    assert count == 2
    with pytest.raises(ConcurrencyConflict):
        reopened.record_attempt_receipt(
            attempt_id="att-1",
            status=AttemptState.COMPLETED,
            evidence_digest=_dg("other"),
            observations=_obs((9, 1), (10, 0)),
            now=NOW,
        )
    drifted = UnitTestAttempt(
        "att-1", request.request_digest, 1, None, _dg("plan"), _dg("different"), "key-1"
    )
    with pytest.raises(ConcurrencyConflict):
        reopened.claim_attempt(drifted, now=NOW)


def test_half_effect_blocks_next_attempt_until_recovery_receipt(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    ledger._db.close()  # crash: claim var, receipt yok

    ledger = SQLiteUnitTestLedger(_open(path))
    assert ledger.unreceipted_attempts(request.request_digest) == ("att-1",)
    with pytest.raises(PolicyViolation):
        ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW)
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.UNKNOWN,
        evidence_digest=_dg("recovery"),
        observations=(),
        now=NOW,
    )
    assert ledger.unreceipted_attempts(request.request_digest) == ()
    assert not ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW).replayed


def test_lineage_budget_and_receipt_preconditions(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id, max_attempts=1)
    ledger = SQLiteUnitTestLedger(_open(path))
    with pytest.raises(PolicyViolation):
        ledger.claim_attempt(_attempt(request, 1, None), now=NOW)  # request yok
    ledger.register_request(request, now=NOW)
    with pytest.raises(ConcurrencyConflict):
        ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    with pytest.raises(PolicyViolation):
        ledger.record_attempt_receipt(
            attempt_id="att-missing",
            status=AttemptState.FAILED,
            evidence_digest=_dg("x"),
            observations=(),
            now=NOW,
        )
    with pytest.raises(PolicyViolation):
        ledger.record_attempt_receipt(
            attempt_id="att-1",
            status=AttemptState.COMPLETED,
            evidence_digest=_dg("x"),
            observations=(),
            now=NOW,
        )
    with pytest.raises(PolicyViolation):
        ledger.record_attempt_receipt(
            attempt_id="att-1",
            status=AttemptState.COMPLETED,
            evidence_digest=_dg("x"),
            observations=(
                CoverageObservation.measured(
                    "other/X.java", CoverageMetric.LINE, covered=1, missed=0
                ),
            ),
            now=NOW,
        )
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.FAILED,
        evidence_digest=_dg("fail"),
        observations=(),
        now=NOW,
    )
    with pytest.raises(PolicyViolation):
        ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW)  # butce


def test_unknown_project_is_rejected(tmp_path: Path) -> None:
    path, _ = _prepare(tmp_path)
    ledger = SQLiteUnitTestLedger(_open(path))
    with pytest.raises(PolicyViolation):
        ledger.register_request(_request("proj-yok"), now=NOW)


def test_success_terminal_is_recomputed_from_stored_integer_counters(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    evidence = _dg("run-1")
    # 8999/10000 gosterimde %90 ama hedef alti
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=evidence,
        observations=_obs((8999, 1001), (10, 0)),
        now=NOW,
    )
    with pytest.raises(PolicyViolation):
        ledger.record_terminal(
            UnitTestTerminal(request.request_digest, UnitTestStopReason.TARGET_REACHED, evidence),
            now=NOW,
        )
    with pytest.raises(PolicyViolation):
        ledger.record_terminal(
            UnitTestTerminal(
                request.request_digest, UnitTestStopReason.TARGET_REACHED, _dg("uydurma")
            ),
            now=NOW,
        )
    ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW)
    good = _dg("run-2")
    ledger.record_attempt_receipt(
        attempt_id="att-2",
        status=AttemptState.COMPLETED,
        evidence_digest=good,
        observations=_obs((9000, 1000), (10, 0)),
        now=NOW,
    )
    with pytest.raises(PolicyViolation):  # birden fazla attempt: zaten-hedefte degil
        ledger.record_terminal(
            UnitTestTerminal(request.request_digest, UnitTestStopReason.ALREADY_AT_TARGET, good),
            now=NOW,
        )
    terminal = UnitTestTerminal(request.request_digest, UnitTestStopReason.TARGET_REACHED, good)
    assert not ledger.record_terminal(terminal, now=NOW).replayed
    assert ledger.record_terminal(terminal, now=NOW).replayed
    assert ledger.list_terminals(request.request_digest) == (terminal,)
    with pytest.raises(PolicyViolation):
        ledger.claim_attempt(_attempt(request, 3, "att-2"), now=NOW)


def test_already_at_target_requires_single_baseline_attempt(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    evidence = _dg("baseline")
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=evidence,
        observations=_obs((10, 0), (10, 0)),
        now=NOW,
    )
    terminal = UnitTestTerminal(
        request.request_digest, UnitTestStopReason.ALREADY_AT_TARGET, evidence
    )
    assert not ledger.record_terminal(terminal, now=NOW).replayed


def test_inconclusive_measurement_cannot_become_success(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    evidence = _dg("partial")
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=evidence,
        observations=(
            CoverageObservation.measured(A, CoverageMetric.LINE, covered=10, missed=0),
            CoverageObservation(
                B, CoverageMetric.LINE, CoverageState.MAPPING_ERROR, reason="source map yok"
            ),
        ),
        now=NOW,
    )
    with pytest.raises(PolicyViolation):
        ledger.record_terminal(
            UnitTestTerminal(request.request_digest, UnitTestStopReason.TARGET_REACHED, evidence),
            now=NOW,
        )
    blocked = UnitTestTerminal(
        request.request_digest, UnitTestStopReason.MEASUREMENT_INCOMPLETE, None, evidence
    )
    assert not ledger.record_terminal(blocked, now=NOW).replayed
    stored = ledger.list_observations("att-1")
    assert stored[1].state is CoverageState.MAPPING_ERROR and stored[1].covered is None


def test_pause_is_resumable_cancel_is_final_and_blocks_new_attempts(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.INTERRUPTED,
        evidence_digest=_dg("int"),
        observations=(),
        now=NOW,
    )
    paused = UnitTestTerminal(request.request_digest, UnitTestStopReason.USER_PAUSED, None)
    ledger.record_terminal(paused, now=NOW)
    ledger.claim_attempt(_attempt(request, 2, "att-1"), now=NOW)  # resume serbest
    cancelled = UnitTestTerminal(request.request_digest, UnitTestStopReason.USER_CANCELLED, None)
    ledger.record_terminal(cancelled, now=NOW)  # acik claim varken de kayit korunur
    with pytest.raises(PolicyViolation):
        ledger.claim_attempt(_attempt(request, 3, "att-2"), now=NOW)
    ledger.record_attempt_receipt(  # readback: eldeki sonuc yine kaydedilebilir
        attempt_id="att-2",
        status=AttemptState.UNKNOWN,
        evidence_digest=_dg("late"),
        observations=(),
        now=NOW,
    )
    with pytest.raises(PolicyViolation):
        ledger.record_terminal(
            UnitTestTerminal(request.request_digest, UnitTestStopReason.BUDGET_EXHAUSTED, None),
            now=NOW,
        )
    assert [t.stop_reason for t in ledger.list_terminals(request.request_digest)] == [
        UnitTestStopReason.USER_PAUSED,
        UnitTestStopReason.USER_CANCELLED,
    ]


def test_database_enforces_append_only_and_counter_checks(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    ledger = SQLiteUnitTestLedger(_open(path))
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    ledger.record_attempt_receipt(
        attempt_id="att-1",
        status=AttemptState.COMPLETED,
        evidence_digest=_dg("e"),
        observations=_obs((1, 1), (2, 0)),
        now=NOW,
    )
    db = ledger._db
    for statement in (
        "update unit_test_observation set covered=2",
        "delete from unit_test_observation",
        "update unit_test_attempt set ordinal=9",
        "delete from unit_test_attempt_receipt",
        "update unit_test_request set metric='branch'",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):  # missed+covered != total
        db.execute(
            "insert into unit_test_observation values"
            "('att-1','x/Y.java','line','measured',1,1,3,'')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # olculmemis durumda sahte 0
        db.execute(
            "insert into unit_test_observation values"
            "('att-1','x/Z.java','line','not-measured',0,0,0,'neden')"
        )


def test_caller_transaction_rollback_discards_ledger_writes(tmp_path: Path) -> None:
    path, project_id = _prepare(tmp_path)
    request = _request(project_id)
    connection = _open(path)
    ledger = SQLiteUnitTestLedger(connection)
    connection.execute("begin immediate")
    ledger.register_request(request, now=NOW)
    ledger.claim_attempt(_attempt(request, 1, None), now=NOW)
    connection.rollback()
    assert ledger.unreceipted_attempts(request.request_digest) == ()
    assert connection.execute("select count(*) from unit_test_request").fetchone()[0] == 0
    # savepoint hata yolunda: basarisiz islem onceki yazimi bozmaz
    connection.execute("begin immediate")
    ledger.register_request(request, now=NOW)
    with pytest.raises(ConcurrencyConflict):
        ledger.claim_attempt(_attempt(request, 2, "att-9"), now=NOW)
    connection.commit()
    assert connection.execute("select count(*) from unit_test_request").fetchone()[0] == 1
