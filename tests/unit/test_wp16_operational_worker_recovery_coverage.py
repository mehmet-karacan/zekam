"""Failure-focused coverage for operational, worker, and recovery transaction seams."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest

from zekam.application.execution import ExecutionHost
from zekam.application.worker import (
    SchedulerGateway,
    ShutdownSignal,
    Worker,
    WorkerSettings,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.runtime import AttemptOutcome, FailureCategory, JobKind
from zekam.domain.scheduler import JobDefinition, Schedule
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_runtime_recovery_tx import (
    EffectRecoveryCaseSpec,
    EffectRecoveryResolutionSpec,
    LockRow,
    RecoveryReconcileSpec,
    RecoveryTransitionSpec,
    _canonical_payload,
    _digest,
    insert_effect_recovery_case_tx,
    insert_effect_recovery_resolution_tx,
    reconcile_effect_recovery_job_tx,
    require_outbox_capacity_tx,
    transition_running_job_to_recovery_tx,
)
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit

NOW_TEXT = "2026-09-03T12:00:00+00:00"
NOW = dt.datetime.fromisoformat(NOW_TEXT)
JOB = "018f0000-0000-7000-8000-000000000801"
LEASE = "018f0000-0000-7000-8000-000000000802"
CLAIM = "018f0000-0000-7000-8000-000000000803"
CASE = "018f0000-0000-7000-8000-000000000804"
RESOLUTION = "018f0000-0000-7000-8000-000000000805"
OUTBOX = "018f0000-0000-7000-8000-000000000806"


def _store(tmp_path: Path) -> tuple[Path, SQLiteOperationalStore]:
    path = tmp_path / "operational.db"
    operational_schema.bootstrap(path)
    return path, SQLiteOperationalStore(path)


def test_operational_uow_exception_nested_and_inactive_boundaries(tmp_path: Path) -> None:
    path, store = _store(tmp_path)
    with pytest.raises(RuntimeError, match="rollback"), store.unit_of_work() as uow:
        uow.create_project(slug="rolled-back", display_name="Rolled Back")
        with pytest.raises(ConfigurationError, match="Nested"), store.unit_of_work():
            pass
        raise RuntimeError("rollback")
    restarted = SQLiteOperationalStore(path)
    with restarted.unit_of_work() as uow:
        assert uow.list_projects() == ()
        uow.rollback()
        with pytest.raises(ConfigurationError, match="aktif degil"):
            uow.commit()


def test_operational_config_and_project_replay_drift_fail_closed(tmp_path: Path) -> None:
    _path, store = _store(tmp_path)
    body = {"network": "deny"}
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(body), task_digest=digest("task-a"), sanitized_config=body
        )
        project = uow.create_project(slug="project", display_name="Project")
        uow.add_project_alias(project_id=project.id, alias="alias")
        binding = uow.bind_source(
            project_id=project.id, portable_ref="project/source", source_kind="git"
        )
        uow.commit()
    with store.unit_of_work() as uow:
        with pytest.raises(ValidationFailed, match="Config revision replay"):
            uow.activate_config(
                config_digest=config.config_digest,
                task_digest=digest("task-b"),
                sanitized_config=body,
            )
        with pytest.raises(ValidationFailed, match="Project slug replay"):
            uow.create_project(slug="project", display_name="Different")
        with pytest.raises(ValidationFailed, match="alias ile"):
            uow.create_project(slug="alias", display_name="Collision")
        other = uow.create_project(slug="other", display_name="Other")
        with pytest.raises(ValidationFailed, match="baska project"):
            uow.add_project_alias(project_id=other.id, alias="alias")
        with pytest.raises(ValidationFailed, match="Source binding replay"):
            uow.bind_source(
                project_id=project.id,
                portable_ref=binding.portable_ref,
                source_kind="directory",
            )


def test_operational_work_validation_and_duplicate_external_number_roll_back(
    tmp_path: Path,
) -> None:
    _path, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="work", display_name="Work")
        with pytest.raises(ValidationFailed, match="state"):
            uow.create_work(project_id=project.id, kind="task", title="Bad", state="unknown")
        with pytest.raises(ValidationFailed, match="evidence"):
            uow.create_work(project_id=project.id, kind="task", title="Done", state="completed")
        first = uow.create_work(
            project_id=project.id,
            kind="task",
            title="One",
            state="ready",
            external_number="EXT-1",
        )
        with pytest.raises(ValidationFailed, match="constraint"):
            uow.create_work(
                project_id=project.id,
                kind="task",
                title="Two",
                state="ready",
                external_number="EXT-1",
            )
        assert uow.get_work(first.id).external_number == "EXT-1"
        with pytest.raises(ValidationFailed, match="bulunamadi"):
            uow.get_work(str(uuid4()))


def test_shutdown_install_tolerates_missing_and_rejected_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_module = __import__("zekam.application.worker", fromlist=["signal"]).signal
    original_int = signal_module.SIGINT
    monkeypatch.setattr(signal_module, "SIGINT", None)
    monkeypatch.setattr(signal_module, "signal", Mock(side_effect=ValueError("not-main")))
    shutdown = ShutdownSignal()
    shutdown.install()
    assert shutdown.requested is False
    monkeypatch.setattr(signal_module, "SIGINT", original_int)


def _scheduled_worker(*, name: str, handler: object = None) -> tuple[Worker, Mock]:
    scheduler = Mock(spec=SchedulerGateway)
    scheduler.definitions.return_value = (
        (uuid4(), JobDefinition(job_name=name, schedule=Schedule(interval="5m")), None),
    )
    scheduler.is_running.return_value = False
    scheduler.known_keys.return_value = frozenset()
    scheduler.record_trigger.return_value = uuid4()
    handlers = {} if handler is None else {name: handler}
    worker = Worker(
        cast(ExecutionHost, Mock(spec=ExecutionHost)),
        WorkerSettings("schedule", ("read",)),
        scheduler=cast(SchedulerGateway, scheduler),
        scheduled_handlers=cast(dict[str, Any], handlers),
        consume_queue=False,
    )
    return worker, scheduler


def test_worker_plan_and_schedule_missing_or_failing_handler_paths() -> None:
    worker, scheduler = _scheduled_worker(name="ordinary")
    assert worker.plan(now=NOW).triggered_jobs == ("ordinary",)
    assert worker._run_schedules(NOW) == ("ordinary",)
    scheduler.finish_run.assert_called_with(
        scheduler.record_trigger.return_value,
        state="succeeded",
        detail="tetikleme kaydedildi",
        now=NOW,
    )

    special, special_scheduler = _scheduled_worker(name="chaos-campaign")
    assert special._run_schedules(NOW) == ()
    special_scheduler.record_incident.assert_called_once()

    def fail(_moment: dt.datetime) -> str:
        raise RuntimeError("private detail")

    failing, failing_scheduler = _scheduled_worker(name="report", handler=fail)
    assert failing._run_schedules(NOW) == ()
    assert failing_scheduler.finish_run.call_args.kwargs["detail"].endswith("RuntimeError")


def _work(kind: JobKind = JobKind.READ_ONLY) -> Any:
    return SimpleNamespace(job=SimpleNamespace(id=uuid4(), kind=kind))


def test_worker_process_cancel_missing_handler_and_handler_failures() -> None:
    work = _work()
    host = Mock(spec=ExecutionHost)
    host.finish.return_value = True
    host.ledger.claims_for_job.return_value = ()
    worker = Worker(cast(ExecutionHost, host), WorkerSettings("worker", ("read",)))
    worker.cancel(work.job.id, now=NOW, force=True)
    assert worker._process(work, NOW) is AttemptOutcome.ABANDONED
    assert worker._active == 0

    work = _work(JobKind.MUTATION)
    assert worker._process(work, NOW) is AttemptOutcome.FAILED
    assert host.finish.call_args.kwargs["failure_category"] is FailureCategory.POLICY

    work = _work()

    def fail(_work: object) -> str:
        raise OSError("private")

    worker.handlers[str(JobKind.READ_ONLY)] = fail
    assert worker._process(work, NOW) is AttemptOutcome.FAILED
    host.ledger.claims_for_job.return_value = (object(),)
    assert worker._process(_work(), NOW) is AttemptOutcome.RECOVERY_REQUIRED


def test_worker_finish_false_and_exception_require_visible_recovery() -> None:
    work = _work()
    host = Mock(spec=ExecutionHost)
    worker = Worker(
        cast(ExecutionHost, host),
        WorkerSettings("worker", ("read",)),
        handlers={str(JobKind.READ_ONLY): lambda _work: digest("result")},
    )
    host.finish.side_effect = [False, True]
    with pytest.raises(PolicyViolation, match="recovery-required"):
        worker._process(work, NOW)
    host.finish.side_effect = [OSError("commit"), False]
    with pytest.raises(PolicyViolation, match="gorunurlugu olusmadi"):
        worker._process(_work(), NOW)
    assert worker._active == 0


def _recovery_db(tmp_path: Path, *, receipt: str | None = None) -> sqlite3.Connection:
    path = tmp_path / f"recovery-{uuid4()}.db"
    operational_schema.bootstrap_v4(path)
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys=on")
    db.execute("insert into local_runtime_config values(1,8)")
    db.execute(
        "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,max_attempts,"
        "fencing_counter,terminal_evidence_digest,available_at,timeout_at,created_at,updated_at)"
        " values(?,?,?,'running',1,1,1,null,?,null,?,?)",
        (JOB, "job", canonical_json({"operation": "test"}), NOW_TEXT, NOW_TEXT, NOW_TEXT),
    )
    db.execute(
        "insert into local_lease values(?,?,?,?,?,?,?,?)",
        (LEASE, JOB, "owner", 1, "token", 1, NOW_TEXT, "2026-09-03T12:01:00+00:00"),
    )
    db.execute(
        "insert into local_resource_lock values(?,?,?,?,?)",
        ("resource/a", JOB, LEASE, 1, NOW_TEXT),
    )
    db.execute(
        "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
        (CLAIM, JOB, LEASE, 1, "test", digest("effect"), "effect", NOW_TEXT),
    )
    if receipt is not None:
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (str(uuid4()), CLAIM, receipt, digest("receipt"), NOW_TEXT),
        )
    return db


def _case_spec(route: str, *, fence: int | None = 1) -> EffectRecoveryCaseSpec:
    body: dict[str, object] = {
        "case_kind": "effect-unknown",
        "claim_id": CLAIM,
        "effect_digest": digest("effect"),
    }
    if route == "sweep-receiptless":
        body["recovered_fence"] = fence
    return EffectRecoveryCaseSpec(
        route,
        CASE,
        JOB,
        CLAIM,
        None,
        digest("effect"),
        fence,
        digest(body),
        NOW_TEXT,
    )


def test_recovery_helpers_reject_wrong_transaction_payload_and_digest(tmp_path: Path) -> None:
    db = _recovery_db(tmp_path)
    with pytest.raises(ValidationFailed, match="active transaction"):
        require_outbox_capacity_tx(db, max_pending_outbox=8)
    db.execute("begin immediate")
    db.row_factory = None
    with pytest.raises(ValidationFailed, match="row factory"):
        require_outbox_capacity_tx(db, max_pending_outbox=8)
    db.row_factory = sqlite3.Row
    for payload, value in (("{", digest({})), ("[]", digest([])), ("{} ", digest({}))):
        with pytest.raises(ValidationFailed):
            _canonical_payload(payload, value)
    with pytest.raises(PolicyViolation, match="digest drift"):
        _canonical_payload("{}", digest("wrong"))
    with pytest.raises(ValidationFailed, match="digest metin"):
        _digest(True, "bad")
    db.rollback()
    db.close()


@pytest.mark.parametrize(
    ("route", "receipt", "effect", "fence", "error"),
    [
        ("bad-route", None, digest("effect"), 1, ValidationFailed),
        ("finish-receiptless", "completed", digest("effect"), None, ConcurrencyConflict),
        ("finish-receiptless", None, digest("wrong"), None, PolicyViolation),
        ("finish-receiptless", None, digest("effect"), 1, ValidationFailed),
        ("sweep-receiptless", None, digest("effect"), 2, PolicyViolation),
    ],
)
def test_recovery_case_route_receipt_effect_and_fence_drift_are_atomic(
    tmp_path: Path,
    route: str,
    receipt: str | None,
    effect: str,
    fence: int | None,
    error: type[Exception],
) -> None:
    db = _recovery_db(tmp_path, receipt=receipt)
    db.execute("begin immediate")
    spec = _case_spec(route, fence=fence)
    spec = spec._replace(effect_digest=effect)
    before = tuple(db.iterdump())
    with pytest.raises(error):
        insert_effect_recovery_case_tx(db, spec)
    assert tuple(db.iterdump()) == before
    db.rollback()
    db.close()


def test_recovery_transition_lock_order_fence_and_half_transaction_rollback(
    tmp_path: Path,
) -> None:
    db = _recovery_db(tmp_path)
    db.execute("begin immediate")
    case = _case_spec("sweep-receiptless")
    insert_effect_recovery_case_tx(db, case)
    terminal = digest([case.expected_case_evidence_digest])
    payload = {"job_id": JOB, "state": "recovery-required", "fencing_token": 1}

    def transition(locks: object, fence: int = 1) -> None:
        transition_running_job_to_recovery_tx(
            db,
            RecoveryTransitionSpec(
                "sweep-recovery-required",
                JOB,
                LEASE,
                fence,
                cast(Any, locks),
                (case.expected_case_evidence_digest,),
                terminal,
                NOW_TEXT,
                OUTBOX,
                8,
                digest(payload),
            ),
        )

    with pytest.raises(ValidationFailed, match="lock tuple"):
        transition([])
    wrong_order = (
        LockRow("resource/z", JOB, LEASE, 1, NOW_TEXT),
        LockRow("resource/a", JOB, LEASE, 1, NOW_TEXT),
    )
    with pytest.raises(ValidationFailed, match="noncanonical"):
        transition(wrong_order)
    with pytest.raises((ValidationFailed, PolicyViolation, ConcurrencyConflict)):
        transition((LockRow("resource/a", JOB, LEASE, 2, NOW_TEXT),), fence=2)
    db.rollback()
    db.close()


def test_recovery_resolution_and_reconcile_reject_stale_or_duplicate_state(tmp_path: Path) -> None:
    db = _recovery_db(tmp_path)
    db.execute("begin immediate")
    with pytest.raises(ConcurrencyConflict, match="open effect case"):
        insert_effect_recovery_resolution_tx(
            db,
            EffectRecoveryResolutionSpec(RESOLUTION, CASE, "completed", digest("r"), NOW_TEXT),
        )
    with pytest.raises(ValidationFailed, match="case tuple"):
        reconcile_effect_recovery_job_tx(
            db,
            RecoveryReconcileSpec(
                JOB, (), "completed", digest([]), NOW_TEXT, OUTBOX, 8, digest({"job_id": JOB})
            ),
        )
    db.rollback()
    db.close()
