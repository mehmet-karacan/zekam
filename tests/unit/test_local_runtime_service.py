from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.application.local_runtime_service import (
    LocalDeliveryResult,
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_runtime_effects import (
    MAINTENANCE_RECONCILE_OPERATION,
    LocalJournalEffectExecutor,
    LocalMaintenanceReconcileExecutor,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


def test_production_worker_and_outbox_services_use_complete_claim_chains(
    tmp_path: Path,
) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    store.enqueue(
        idempotency_key="job",
        payload={"operation": "test/v1", "effect": {"value": 1}},
    )
    effects: list[LocalEffectRequest] = []
    deliveries: list[str] = []

    def execute(request: LocalEffectRequest) -> LocalEffectResult:
        effects.append(request)
        return LocalEffectResult("completed", digest(request.payload))

    def publish(claim):  # type: ignore[no-untyped-def]
        deliveries.append(claim.event.idempotency_key)
        return LocalDeliveryResult("delivered", digest(claim.event.payload))

    service = LocalRuntimeService(
        store, effect_executor=cast(Any, execute), outbox_publisher=publish
    )
    startup = service.startup(lambda pid: "current" if pid == os.getpid() else None)
    assert startup.orphans.recovery_required == startup.recovered_outbox == 0
    work = service.run_worker_once(
        owner_id="worker",
        owner_pid=os.getpid(),
        owner_token="current",
    )
    assert work is not None
    assert len(effects) == 1
    while service.publish_outbox_once(
        owner_id="consumer",
        owner_pid=os.getpid(),
        owner_token="current",
    ):
        pass
    assert len(deliveries) == 2
    assert store.status().running_jobs == store.status().pending_outbox == 0


def test_executor_exception_is_unknown_and_never_implicitly_retried(tmp_path: Path) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    store.enqueue(
        idempotency_key="job",
        payload={"operation": "test/v1", "effect": {"value": 1}},
    )
    calls = 0

    def execute(_request: LocalEffectRequest) -> LocalEffectResult:
        nonlocal calls
        calls += 1
        raise TimeoutError("ambiguous external timeout")

    service = LocalRuntimeService(
        store,
        effect_executor=cast(Any, execute),
        outbox_publisher=lambda _claim: LocalDeliveryResult("delivered", digest("ok")),
    )
    service.run_worker_once(owner_id="worker", owner_pid=1, owner_token="one")
    assert calls == 1
    assert store.status().recovery_jobs == store.status().open_recovery_cases == 1
    assert service.run_worker_once(owner_id="replacement", owner_pid=2, owner_token="two") is None
    assert calls == 1


def test_worker_claims_only_the_executor_operation_allowlist(tmp_path: Path) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    foreign, _ = store.enqueue(
        idempotency_key="foreign",
        payload={"operation": "oracle.metadata.read", "effect": {"project": "gpu"}},
    )
    journal, _ = store.enqueue(
        idempotency_key="journal",
        payload={
            "operation": "local.append-journal/v1",
            "effect": {"relative_path": "scope.log", "line": "transition"},
        },
    )
    service = LocalRuntimeService(
        store,
        effect_executor=LocalJournalEffectExecutor(tmp_path / "effects"),
        outbox_publisher=lambda _claim: LocalDeliveryResult("delivered", digest("ok")),
        supported_operations=("local.append-journal/v1",),
    )

    work = service.run_worker_once(owner_id="journal", owner_pid=1, owner_token="one")

    assert work is not None and work.job.id == journal.id
    assert store.job_snapshot(foreign.id)["state"] == "ready"  # type: ignore[index]
    assert store.status().recovery_jobs == 0


def test_typed_effect_dispatcher_selects_exact_operation_and_rejects_foreign() -> None:
    calls: list[str] = []

    def handled(request: LocalEffectRequest) -> LocalEffectResult:
        calls.append(request.operation)
        return LocalEffectResult("completed", digest(request.payload))

    dispatcher = LocalEffectDispatcher((("report.daily/v1", handled),))
    result = dispatcher(
        LocalEffectRequest("report.daily/v1", "job:one", {"day": "2026-09-06"})
    )
    assert result.status == "completed"
    assert calls == ["report.daily/v1"]
    with pytest.raises(PolicyViolation, match="registry"):
        dispatcher(LocalEffectRequest("shell.exec", "job:two", {"command": "whoami"}))


def test_maintenance_handler_recomputes_exact_schedule_digest(tmp_path: Path) -> None:
    scheduled_for = "2026-09-06T12:05:00Z"
    body = {
        "schema": "zekam-maintenance-reconcile-schedule/v1",
        "interval_minutes": 5,
        "scheduled_for": scheduled_for,
        "operation": MAINTENANCE_RECONCILE_OPERATION,
        "misfire": "run-once",
        "overlap": "skip",
    }
    executor = LocalMaintenanceReconcileExecutor(tmp_path / "effects")
    payload = {
        "scheduled_for": scheduled_for,
        "schedule_digest": digest(body),
        "source": "os-supervisor",
    }
    assert executor(
        LocalEffectRequest(MAINTENANCE_RECONCILE_OPERATION, "job:one", payload)
    ).status == "completed"
    with pytest.raises(PolicyViolation, match="schedule digest"):
        executor(
            LocalEffectRequest(
                MAINTENANCE_RECONCILE_OPERATION,
                "job:two",
                payload | {"schedule_digest": digest("forged")},
            )
        )


def test_local_journal_retries_short_writes_until_every_byte_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write
    calls = 0

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal calls
        calls += 1
        return real_write(descriptor, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(os, "write", short_write)
    executor = LocalJournalEffectExecutor(tmp_path)
    request = LocalEffectRequest(
        "local.append-journal/v1",
        "idem",
        {"relative_path": "calls.log", "line": "payload"},
    )
    assert executor(request).status == "completed"
    assert (tmp_path / "calls.log").read_bytes() == b"idem\tpayload\n"
    assert calls > 1


def test_outbox_startup_can_drain_bound_before_job_recovery_retries(
    tmp_path: Path,
) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db", max_pending_outbox=1)
    store.enqueue(
        idempotency_key="job",
        payload={"operation": "test/v1", "effect": {"value": 1}},
    )
    work = store.claim_next(
        owner_id="dead-worker",
        owner_pid=999_999,
        owner_token="dead",
        lease_seconds=30,
    )
    assert work is not None
    store.claim_effect(
        work,
        operation="test/v1",
        effect_digest=digest({"value": 1}),
        idempotency_key="persisted-effect-claim",
    )
    delivered: list[str] = []

    def publish(claim):  # type: ignore[no-untyped-def]
        delivered.append(claim.event.event_kind)
        return LocalDeliveryResult("delivered", digest(claim.event.id))

    service = LocalRuntimeService(
        store,
        effect_executor=lambda _request: LocalEffectResult("completed", digest("unused")),
        outbox_publisher=publish,
    )
    with pytest.raises(PolicyViolation, match="backpressure"):
        service.startup(lambda _pid: None)
    status = store.status()
    assert (status.running_jobs, status.recovery_jobs, status.pending_outbox) == (1, 0, 1)
    assert service.startup_outbox(lambda _pid: None) == 0
    assert service.publish_outbox_once(
        owner_id="consumer",
        owner_pid=1,
        owner_token="consumer",
    )
    assert delivered == ["job.enqueued"]
    startup = service.startup(lambda _pid: None)
    assert startup.orphans.recovery_required == 1
    assert (store.status().recovery_jobs, store.status().pending_outbox) == (1, 1)
    assert service.publish_outbox_once(
        owner_id="consumer",
        owner_pid=1,
        owner_token="consumer",
    )
    assert delivered == ["job.enqueued", "job.recovery-required"]
    assert store.status().pending_outbox == 0
