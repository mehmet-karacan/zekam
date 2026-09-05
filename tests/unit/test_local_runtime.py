from __future__ import annotations

import datetime as dt
import os
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.process_identity import process_incarnation_token
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

NOW = dt.datetime(2026, 9, 2, 12, tzinfo=dt.UTC)


def _store(tmp_path: Path) -> SQLiteLocalRuntimeStore:
    return SQLiteLocalRuntimeStore(tmp_path / "operational.db")


def _scalar(path: Path, query: str, parameters: tuple[object, ...] = ()) -> object:
    with sqlite3.connect(path) as connection:
        return connection.execute(query, parameters).fetchone()[0]


def test_enqueue_replay_is_exact_and_payload_drift_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, created = store.enqueue(idempotency_key="job-1", payload={"value": 1})
    replay, replay_created = store.enqueue(idempotency_key="job-1", payload={"value": 1})
    assert created is True and replay_created is False and replay == first
    with pytest.raises(ConcurrencyConflict, match="payload drift"):
        store.enqueue(idempotency_key="job-1", payload={"value": 2})
    with pytest.raises(ValidationFailed):
        store.enqueue(idempotency_key="", payload={})
    with pytest.raises(ValidationFailed):
        store.enqueue(idempotency_key="x", payload={}, max_attempts=0)


def test_claim_heartbeat_effect_receipt_and_terminal_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={"kind": "effect"}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker-a",
        owner_pid=101,
        owner_token="process-a",
        lease_seconds=30,
        resources=("project:p",),
        now=NOW.isoformat(),
    )
    assert work is not None and work.job.state == "running" and work.lease.fencing_token == 1
    with pytest.raises(ConcurrencyConflict, match="owner/fence"):
        store.heartbeat(
            work.lease.id,
            owner_id="worker-b",
            owner_token="process-a",
            fencing_token=1,
            lease_seconds=30,
            now=(NOW + dt.timedelta(seconds=1)).isoformat(),
        )
    claim, claim_created = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect-1",
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    replay, replay_created = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect-1",
        now=(NOW + dt.timedelta(seconds=3)).isoformat(),
    )
    assert claim_created is True and replay_created is False and replay == claim
    with pytest.raises(PolicyViolation, match="Unresolved"):
        store.finish(
            work,
            state="completed",
            evidence_digest=digest("job-result"),
            now=(NOW + dt.timedelta(seconds=4)).isoformat(),
        )
    receipt = store.record_receipt(
        claim,
        status="completed",
        evidence_digest=digest("result"),
        now=(NOW + dt.timedelta(seconds=5)).isoformat(),
    )
    assert receipt.status == "completed"
    terminal = store.finish(
        work,
        state="completed",
        evidence_digest=digest("job-result"),
        now=(NOW + dt.timedelta(seconds=6)).isoformat(),
    )
    assert terminal.state == "completed"


def test_expired_receiptless_claim_becomes_recovery_required_and_releases_lock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="uncertain", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=202,
        owner_token="process-worker",
        lease_seconds=1,
        resources=("resource:x",),
        now=NOW.isoformat(),
    )
    assert work is not None
    store.claim_effect(
        work,
        operation="external-write",
        effect_digest=digest("uncertain"),
        idempotency_key="uncertain-effect",
        now=NOW.isoformat(),
    )
    sweep = store.recover_expired(now=(NOW + dt.timedelta(seconds=2)).isoformat())
    assert sweep.recovery_required == 1
    assert sweep.released_locks == 1
    assert (
        store.claim_next(
            owner_id="other",
            owner_pid=303,
            owner_token="process-other",
            lease_seconds=10,
            now=NOW.isoformat(),
        )
        is None
    )


def test_expired_claimless_lease_requeues_with_new_fence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="retry", payload={}, max_attempts=2, available_at=NOW.isoformat())
    first = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="process-one",
        lease_seconds=1,
        now=NOW.isoformat(),
    )
    assert first is not None
    sweep = store.recover_expired(now=(NOW + dt.timedelta(seconds=2)).isoformat())
    assert sweep.requeued == 1
    second = store.claim_next(
        owner_id="worker-2",
        owner_pid=2,
        owner_token="process-two",
        lease_seconds=10,
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert second is not None and second.lease.fencing_token == 2


def test_concurrent_claim_has_exactly_one_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="race", payload={}, available_at=NOW.isoformat())

    def claim(index: int):  # type: ignore[no-untyped-def]
        return store.claim_next(
            owner_id=f"worker-{index}",
            owner_pid=100 + index,
            owner_token=f"process-{index}",
            lease_seconds=30,
            now=NOW.isoformat(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(claim, range(8)))
    assert sum(item is not None for item in results) == 1


def test_scheduler_slot_replay_is_idempotent_and_digest_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, created = store.schedule_once(
        slot_key="hour:2026-09-02T12",
        schedule_digest=digest("schedule"),
        idempotency_key="scheduled-job",
        payload={"kind": "maintenance"},
        now=NOW.isoformat(),
    )
    replay, replay_created = store.schedule_once(
        slot_key="hour:2026-09-02T12",
        schedule_digest=digest("schedule"),
        idempotency_key="scheduled-job",
        payload={"kind": "maintenance"},
        now=NOW.isoformat(),
    )
    assert created is True and replay_created is False and replay == first
    with pytest.raises(ConcurrencyConflict, match="digest drift"):
        store.schedule_once(
            slot_key="hour:2026-09-02T12",
            schedule_digest=digest("changed"),
            idempotency_key="other",
            payload={},
            now=NOW.isoformat(),
        )


@pytest.mark.parametrize("value", [None, True, "1", 1.5, 0, -1, 101])
def test_enqueue_rejects_wrong_max_attempt_types_and_bounds(tmp_path: Path, value: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValidationFailed):
        store.enqueue(idempotency_key=f"job-{value}", payload={}, max_attempts=value)


@pytest.mark.parametrize("value", [None, True, "1", 1.5, 0, -1, 3601])
def test_claim_rejects_wrong_lease_types_and_bounds(tmp_path: Path, value: Any) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    with pytest.raises(ValidationFailed):
        store.claim_next(
            owner_id="worker",
            owner_pid=1,
            owner_token="incarnation",
            lease_seconds=value,
            now=NOW.isoformat(),
        )


@pytest.mark.parametrize("value", [1, "", "2026-09-02T12:00:00", "broken"])
def test_runtime_rejects_null_wrong_or_naive_timestamps(tmp_path: Path, value: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValidationFailed):
        store.enqueue(idempotency_key="job", payload={}, available_at=value)


@pytest.mark.parametrize("payload", [[], None, {"value": float("nan")}, {1: "bad"}])
def test_enqueue_rejects_non_object_or_noncanonical_payload(tmp_path: Path, payload: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValidationFailed):
        store.enqueue(idempotency_key="job", payload=payload)


def test_enqueue_and_outbox_are_atomic_digest_bound_and_backpressured(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, _ = store.enqueue(idempotency_key="one", payload={"value": 1})
    store.enqueue(idempotency_key="two", payload={"value": 2})
    pending = store.pending_outbox(limit=1)
    assert len(pending) == 1 and len(store.pending_outbox()) == 2
    assert pending[0].job_id == first.id and pending[0].event_kind == "job.enqueued"
    claim = store.claim_outbox(
        supported_kinds=RUNTIME_OUTBOX_KINDS,
        owner_id="consumer",
        owner_pid=1,
        owner_token="consumer-one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert claim is not None
    drifted = replace(claim, event=replace(claim.event, payload_digest=digest("wrong")))
    with pytest.raises(ConcurrencyConflict, match="claim/fence drift"):
        store.record_outbox_receipt(
            drifted,
            status="delivered",
            evidence_digest=digest("delivery"),
            now=(NOW + dt.timedelta(seconds=1)).isoformat(),
        )
    delivered = store.record_outbox_receipt(
        claim,
        status="delivered",
        evidence_digest=digest("delivery"),
        now=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    replay = store.record_outbox_receipt(
        claim,
        status="delivered",
        evidence_digest=digest("delivery"),
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert delivered.state == replay.state == "delivered"
    assert len(store.pending_outbox()) == 1


def test_concurrent_scheduler_slot_creates_one_job_and_no_orphan(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def schedule(_: int) -> tuple[object, bool]:
        return store.schedule_once(
            slot_key="minute:2026-09-02T12:00",
            schedule_digest=digest("schedule"),
            idempotency_key="scheduled",
            payload={"kind": "tick"},
            now=NOW.isoformat(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(schedule, range(8)))
    assert sum(created for _, created in results) == 1
    path = tmp_path / "operational.db"
    assert _scalar(path, "select count(*) from local_job") == 1
    assert _scalar(path, "select count(*) from local_scheduler_slot") == 1
    assert _scalar(path, "select count(*) from local_outbox") == 1


def test_dead_pid_pid_reuse_and_same_pid_orphans_recover_without_data_loss(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.enqueue(
        idempotency_key="orphan", payload={}, max_attempts=2, available_at=NOW.isoformat()
    )
    current = process_incarnation_token(os.getpid())
    assert current is not None
    first = store.claim_next(
        owner_id="worker",
        owner_pid=os.getpid(),
        owner_token="prior-process-incarnation",
        lease_seconds=3600,
        resources=("project:one",),
        now=NOW.isoformat(),
    )
    assert first is not None
    sweep = store.recover_orphans(
        lambda pid: current if pid == os.getpid() else None,
        now=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    assert sweep.requeued == 1 and sweep.released_locks == 1
    second = store.claim_next(
        owner_id="worker-new",
        owner_pid=os.getpid(),
        owner_token=current,
        lease_seconds=30,
        resources=("project:one",),
        now=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    assert second is not None and second.lease.fencing_token == 2
    live = store.recover_orphans(
        lambda pid: current if pid == os.getpid() else None,
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert live == type(live)(0, 0, 0, 0, 0)


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL assertion is POSIX-only")
def test_process_kill_after_effect_claim_never_reexecutes_unknown_effect(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    effect_path = tmp_path / "external-effect.log"
    child_code = """
import os
import signal
import sys
from pathlib import Path
from zekam.domain.canonical import digest
from zekam.infrastructure.process_identity import process_incarnation_token
from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
path = Path(sys.argv[1])
effect_path = Path(sys.argv[2])
store = SQLiteLocalRuntimeStore(path)
store.enqueue(idempotency_key='killed', payload={}, max_attempts=2)
token = process_incarnation_token(os.getpid())
assert token is not None
work = store.claim_next(
    owner_id='child',
    owner_pid=os.getpid(),
    owner_token=token,
    lease_seconds=3600,
)
assert work is not None
store.claim_effect(
    work,
    operation='external-write',
    effect_digest=digest('effect'),
    idempotency_key='effect',
)
with effect_path.open('ab') as stream:
    stream.write(b'external-effect-once\\n')
    stream.flush()
    os.fsync(stream.fileno())
os.kill(os.getpid(), signal.SIGKILL)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(path), str(effect_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == -signal.SIGKILL
    assert effect_path.read_bytes() == b"external-effect-once\n"
    store = SQLiteLocalRuntimeStore(path)
    sweep = store.recover_orphans(process_incarnation_token)
    assert sweep.recovery_required == 1
    assert _scalar(path, "select state from local_job") == "recovery-required"
    assert _scalar(path, "select count(*) from local_effect_claim") == 1
    assert _scalar(path, "select count(*) from local_effect_receipt") == 0
    assert _scalar(path, "select count(*) from local_lease") == 0
    assert (
        store.claim_next(
            owner_id="replacement",
            owner_pid=os.getpid(),
            owner_token=process_incarnation_token(os.getpid()) or "missing",
            lease_seconds=30,
        )
        is None
    )
    assert effect_path.read_bytes().count(b"external-effect-once\n") == 1


def test_receipted_effect_is_finalized_on_restart_without_requeue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, max_attempts=2, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="dead",
        lease_seconds=3600,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, created = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    assert created is True
    store.record_receipt(
        claim,
        status="completed",
        evidence_digest=digest("evidence"),
        now=NOW.isoformat(),
    )
    sweep = store.recover_orphans(lambda _pid: None, now=NOW.isoformat())
    assert sweep.finalized == 1
    assert _scalar(tmp_path / "operational.db", "select state from local_job") == "completed"


def test_timeout_and_poison_attempt_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(
        idempotency_key="timeout",
        payload={},
        available_at=NOW.isoformat(),
        timeout_at=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    first = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="dead",
        lease_seconds=3600,
        now=NOW.isoformat(),
    )
    assert first is not None
    sweep = store.recover_orphans(
        lambda _pid: None,
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert sweep.timed_out == 1
    assert _scalar(tmp_path / "operational.db", "select state from local_job") == "failed"


def test_recovery_reconciliation_and_destroy_require_complete_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="dead",
        lease_seconds=1,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, created = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    assert created is True
    store.recover_expired(now=(NOW + dt.timedelta(seconds=2)).isoformat())
    assert store.reconcile_recovery(work.job.id).state == "recovery-required"
    with pytest.raises(PolicyViolation, match="terminal"):
        store.destroy_terminal(work.job.id)
    store.record_receipt(claim, status="completed", evidence_digest=digest("verified"))
    assert store.reconcile_recovery(work.job.id).state == "recovery-required"
    recovery_case = store.recovery_cases()[0]
    store.resolve_recovery(
        recovery_case.id,
        outcome="completed",
        evidence_digest=digest("late-receipt-verified"),
    )
    assert store.reconcile_recovery(work.job.id).state == "completed"
    while store.pending_outbox():
        outbox_claim = store.claim_outbox(
            supported_kinds=RUNTIME_OUTBOX_KINDS,
            owner_id="consumer",
            owner_pid=2,
            owner_token="consumer-two",
            lease_seconds=30,
        )
        assert outbox_claim is not None
        store.record_outbox_receipt(
            outbox_claim,
            status="delivered",
            evidence_digest=digest(outbox_claim.event.id),
        )
    with pytest.raises(PolicyViolation, match="audit retention"):
        store.destroy_terminal(work.job.id)
    assert _scalar(tmp_path / "operational.db", "select count(*) from local_job") == 1
    assert _scalar(tmp_path / "operational.db", "select count(*) from local_effect_claim") == 1
    assert _scalar(tmp_path / "operational.db", "select count(*) from local_effect_receipt") == 1
    outbox_count = _scalar(tmp_path / "operational.db", "select count(*) from local_outbox")
    assert isinstance(outbox_count, int) and outbox_count >= 2


def test_enqueue_and_scheduler_roll_back_whole_transaction_on_outbox_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)

    def fail_outbox(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated-disk-failure")

    monkeypatch.setattr(store, "_emit_outbox", fail_outbox)
    with pytest.raises(OSError, match="disk-failure"):
        store.enqueue(idempotency_key="job", payload={})
    with pytest.raises(OSError, match="disk-failure"):
        store.schedule_once(
            slot_key="slot",
            schedule_digest=digest("schedule"),
            idempotency_key="scheduled",
            payload={},
        )
    path = tmp_path / "operational.db"
    assert _scalar(path, "select count(*) from local_job") == 0
    assert _scalar(path, "select count(*) from local_outbox") == 0
    assert _scalar(path, "select count(*) from local_scheduler_slot") == 0


def test_heartbeat_loss_expires_old_fence_and_rejects_late_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, max_attempts=2, available_at=NOW.isoformat())
    first = store.claim_next(
        owner_id="worker-one",
        owner_pid=1,
        owner_token="one",
        lease_seconds=1,
        now=NOW.isoformat(),
    )
    assert first is not None
    with pytest.raises(ConcurrencyConflict, match="expiry"):
        store.heartbeat(
            first.lease.id,
            owner_id="worker-one",
            owner_token="one",
            fencing_token=first.lease.fencing_token,
            lease_seconds=10,
            now=(NOW + dt.timedelta(seconds=2)).isoformat(),
        )
    store.recover_expired(now=(NOW + dt.timedelta(seconds=2)).isoformat())
    second = store.claim_next(
        owner_id="worker-two",
        owner_pid=2,
        owner_token="two",
        lease_seconds=10,
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert second is not None and second.lease.fencing_token == 2
    with pytest.raises(ConcurrencyConflict, match="current lease"):
        store.claim_effect(
            first,
            operation="late-write",
            effect_digest=digest("late"),
            idempotency_key="late",
            now=(NOW + dt.timedelta(seconds=3)).isoformat(),
        )


def test_duplicate_resource_request_and_live_resource_conflict_are_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="first", payload={}, available_at=NOW.isoformat())
    with pytest.raises(ValidationFailed, match="duplicate"):
        store.claim_next(
            owner_id="worker",
            owner_pid=1,
            owner_token="one",
            lease_seconds=30,
            resources=("resource:x", "resource:x"),
            now=NOW.isoformat(),
        )
    first = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        resources=("resource:x",),
        now=NOW.isoformat(),
    )
    assert first is not None
    store.enqueue(idempotency_key="second", payload={}, available_at=NOW.isoformat())
    assert (
        store.claim_next(
            owner_id="other",
            owner_pid=2,
            owner_token="two",
            lease_seconds=30,
            resources=("resource:x",),
            now=NOW.isoformat(),
        )
        is None
    )
    path = tmp_path / "operational.db"
    assert _scalar(path, "select count(*) from local_lease") == 1
    assert _scalar(path, "select state from local_job where idempotency_key='second'") == "ready"


def test_timeout_with_unknown_external_result_requires_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(
        idempotency_key="job",
        payload={},
        available_at=NOW.isoformat(),
        timeout_at=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=3600,
        now=NOW.isoformat(),
    )
    assert work is not None
    _, created = store.claim_effect(
        work,
        operation="external-write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    assert created is True
    sweep = store.recover_orphans(
        lambda _pid: None,
        now=(NOW + dt.timedelta(seconds=2)).isoformat(),
    )
    assert sweep.recovery_required == 1 and sweep.timed_out == 0
    assert _scalar(tmp_path / "operational.db", "select state from local_job") == (
        "recovery-required"
    )


def test_failed_effect_receipt_cannot_be_completed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, _ = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    store.record_receipt(
        claim,
        status="failed",
        evidence_digest=digest("failure"),
        now=NOW.isoformat(),
    )
    with pytest.raises(PolicyViolation, match="Failed effect"):
        store.finish(
            work,
            state="completed",
            evidence_digest=digest("invalid-completed"),
            now=NOW.isoformat(),
        )
    assert (
        store.finish(
            work,
            state="failed",
            evidence_digest=digest("job-failed"),
            now=NOW.isoformat(),
        ).state
        == "failed"
    )


def test_orphan_probe_wrong_type_fails_before_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None

    def invalid_probe(_pid: int) -> Any:
        return 7

    with pytest.raises(ValidationFailed, match="string veya null"):
        store.recover_orphans(invalid_probe)
    assert _scalar(tmp_path / "operational.db", "select count(*) from local_lease") == 1


@pytest.mark.parametrize("value", [None, 1, "sha256:ABC", "sha256:0", " bad"])
def test_effect_claim_rejects_malformed_digest_without_writes(tmp_path: Path, value: Any) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    with pytest.raises(ValidationFailed):
        store.claim_effect(
            work,
            operation="write",
            effect_digest=value,
            idempotency_key="effect",
            now=NOW.isoformat(),
        )
    assert _scalar(tmp_path / "operational.db", "select count(*) from local_effect_claim") == 0


def test_claim_receipt_recovery_and_outbox_audit_are_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, _ = store.claim_effect(
        work,
        operation="write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    receipt = store.record_receipt(
        claim,
        status="completed",
        evidence_digest=digest("receipt"),
        now=NOW.isoformat(),
    )
    store.finish(
        work,
        state="completed",
        evidence_digest=digest("job-result"),
        now=NOW.isoformat(),
    )
    path = tmp_path / "operational.db"
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        for statement, parameters in (
            ("update local_effect_claim set operation='changed' where id=?", (claim.id,)),
            (
                "update local_effect_receipt set evidence_digest=? where id=?",
                (digest("changed"), receipt.id),
            ),
            ("delete from local_effect_receipt where id=?", (receipt.id,)),
            ("delete from local_effect_claim where id=?", (claim.id,)),
            ("delete from local_outbox where job_id=?", (work.job.id,)),
            ("delete from local_job where id=?", (work.job.id,)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
            connection.rollback()
    assert _scalar(path, "select count(*) from local_effect_claim") == 1
    assert _scalar(path, "select count(*) from local_effect_receipt") == 1
    assert _scalar(path, "select count(*) from local_outbox") == 2


def test_claimless_terminal_job_requires_evidence_and_persists_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    with pytest.raises(PolicyViolation, match="evidence digest"):
        store.finish(work, state="completed", now=NOW.isoformat())
    evidence = digest("claimless-result")
    assert (
        store.finish(
            work,
            state="completed",
            evidence_digest=evidence,
            now=NOW.isoformat(),
        ).state
        == "completed"
    )
    assert (
        _scalar(
            tmp_path / "operational.db",
            "select terminal_evidence_digest from local_job",
        )
        == evidence
    )


def test_ready_timeout_persists_terminal_evidence_and_outbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(
        idempotency_key="timeout",
        payload={},
        available_at=NOW.isoformat(),
        timeout_at=(NOW + dt.timedelta(seconds=1)).isoformat(),
    )
    sweep = store.recover_expired(now=(NOW + dt.timedelta(seconds=2)).isoformat())
    assert sweep.timed_out == 1
    path = tmp_path / "operational.db"
    assert _scalar(path, "select state from local_job") == "failed"
    assert str(_scalar(path, "select terminal_evidence_digest from local_job")).startswith(
        "sha256:"
    )
    with sqlite3.connect(path) as connection:
        kinds = [row[0] for row in connection.execute("select event_kind from local_outbox")]
    assert kinds == ["job.enqueued", "job.failed"]


def test_outbox_consumer_claim_is_fenced_and_expiry_becomes_unknown_recovery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={})

    def claim(index: int):  # type: ignore[no-untyped-def]
        return store.claim_outbox(
            supported_kinds=RUNTIME_OUTBOX_KINDS,
            owner_id=f"consumer-{index}",
            owner_pid=index + 1,
            owner_token=f"token-{index}",
            lease_seconds=1,
            now=NOW.isoformat(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = tuple(executor.map(claim, range(8)))
    assert sum(item is not None for item in claims) == 1
    assert store.recover_outbox(now=(NOW + dt.timedelta(seconds=2)).isoformat()) == 1
    assert (
        store.claim_outbox(
            supported_kinds=RUNTIME_OUTBOX_KINDS,
            owner_id="replacement",
            owner_pid=99,
            owner_token="replacement",
            lease_seconds=30,
            now=(NOW + dt.timedelta(seconds=2)).isoformat(),
        )
        is None
    )
    path = tmp_path / "operational.db"
    assert _scalar(path, "select status from local_outbox_receipt") == "unknown"
    assert _scalar(path, "select state from local_outbox_delivery") == "recovery-required"
    assert _scalar(path, "select case_kind from local_recovery_case") == ("outbox-delivery-unknown")


@pytest.mark.parametrize("probe_value", [7, "", " padded "])
def test_outbox_orphan_probe_invalid_result_rolls_back_without_false_unknown(
    tmp_path: Path,
    probe_value: object,
) -> None:
    path = tmp_path / "operational.db"
    store = SQLiteLocalRuntimeStore(path)
    store.enqueue(idempotency_key="job", payload={})
    claim = store.claim_outbox(
        supported_kinds=RUNTIME_OUTBOX_KINDS,
        owner_id="consumer",
        owner_pid=1,
        owner_token="live",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert claim is not None

    def invalid_probe(_pid: int) -> Any:
        return probe_value

    with pytest.raises(ValidationFailed, match="string veya null"):
        store.recover_outbox(
            invalid_probe,
            now=(NOW + dt.timedelta(seconds=1)).isoformat(),
        )
    assert _scalar(path, "select state from local_outbox_delivery") == "claimed"
    assert _scalar(path, "select count(*) from local_outbox_receipt") == 0
    assert _scalar(path, "select count(*) from local_recovery_case") == 0


def test_outbox_backpressure_rejects_producer_without_partial_job(tmp_path: Path) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db", max_pending_outbox=1)
    store.enqueue(idempotency_key="first", payload={})
    with pytest.raises(PolicyViolation, match="backpressure"):
        store.enqueue(idempotency_key="second", payload={})
    path = tmp_path / "operational.db"
    assert _scalar(path, "select count(*) from local_job") == 1
    assert _scalar(path, "select count(*) from local_outbox") == 1


def test_outbox_backpressure_is_a_hard_bound_for_terminal_producers(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    store = SQLiteLocalRuntimeStore(path, max_pending_outbox=1)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    with pytest.raises(PolicyViolation, match="backpressure"):
        store.finish(
            work,
            state="completed",
            evidence_digest=digest("terminal"),
            now=NOW.isoformat(),
        )
    assert _scalar(path, "select count(*) from local_outbox_delivery where state='pending'") == 1
    assert _scalar(path, "select state from local_job") == "running"
    assert _scalar(path, "select count(*) from local_lease") == 1

    delivery = store.claim_outbox(
        supported_kinds=RUNTIME_OUTBOX_KINDS,
        owner_id="consumer",
        owner_pid=2,
        owner_token="consumer-one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert delivery is not None
    store.record_outbox_receipt(
        delivery,
        status="delivered",
        evidence_digest=digest("delivery"),
        now=NOW.isoformat(),
    )
    assert (
        store.finish(
            work,
            state="completed",
            evidence_digest=digest("terminal"),
            now=NOW.isoformat(),
        ).state
        == "completed"
    )
    assert _scalar(path, "select count(*) from local_outbox_delivery where state='pending'") == 1


def test_outbox_bound_is_persisted_and_config_drift_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    first = SQLiteLocalRuntimeStore(path, max_pending_outbox=1)
    first.enqueue(idempotency_key="first", payload={})
    restarted = SQLiteLocalRuntimeStore(path)
    assert restarted.max_pending_outbox == 1
    with pytest.raises(PolicyViolation, match="backpressure"):
        restarted.enqueue(idempotency_key="second", payload={})
    with pytest.raises(PolicyViolation, match="config drift"):
        SQLiteLocalRuntimeStore(path, max_pending_outbox=2)
    with (
        sqlite3.connect(path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute("update local_runtime_config set max_pending_outbox=2")


def test_poison_job_is_quarantined_and_does_not_block_next_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="poison", payload={}, available_at=NOW.isoformat())
    poisoned = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        resources=("resource:poison",),
        now=NOW.isoformat(),
    )
    assert poisoned is not None
    quarantined = store.quarantine(
        poisoned,
        evidence_digest=digest("invalid-payload-contract"),
        now=NOW.isoformat(),
    )
    assert quarantined.state == "quarantined"
    store.enqueue(idempotency_key="next", payload={}, available_at=NOW.isoformat())
    next_work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        resources=("resource:poison",),
        now=NOW.isoformat(),
    )
    assert next_work is not None and next_work.job.idempotency_key == "next"
    path = tmp_path / "operational.db"
    assert _scalar(path, "select count(*) from local_resource_lock") == 1
    assert _scalar(
        path,
        "select terminal_evidence_digest from local_job where idempotency_key='poison'",
    ) == digest("invalid-payload-contract")


def test_unknown_effect_receipt_creates_immutable_recovery_case(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, _ = store.claim_effect(
        work,
        operation="external-write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    store.record_receipt(
        claim,
        status="unknown",
        evidence_digest=digest("provider-timeout"),
        now=NOW.isoformat(),
    )
    assert store.finish(work, state="recovery-required", now=NOW.isoformat()).state == (
        "recovery-required"
    )
    path = tmp_path / "operational.db"
    assert _scalar(path, "select case_kind from local_recovery_case") == "effect-unknown"
    assert _scalar(path, "select state from local_recovery_case") == "open"


def test_unknown_effect_requires_immutable_resolution_before_reconcile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, _ = store.claim_effect(
        work,
        operation="external-write",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    store.record_receipt(
        claim,
        status="unknown",
        evidence_digest=digest("timeout"),
        now=NOW.isoformat(),
    )
    store.finish(work, state="recovery-required", now=NOW.isoformat())
    case = store.recovery_cases()[0]
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        store.record_receipt(
            claim,
            status="completed",
            evidence_digest=digest("verified"),
            now=NOW.isoformat(),
        )
    assert store.reconcile_recovery(work.job.id).state == "recovery-required"
    resolution = store.resolve_recovery(
        case.id,
        outcome="completed",
        evidence_digest=digest("verified-provider-record"),
        now=NOW.isoformat(),
    )
    assert (
        store.resolve_recovery(
            case.id,
            outcome="completed",
            evidence_digest=digest("verified-provider-record"),
            now=NOW.isoformat(),
        )
        == resolution
    )
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        store.resolve_recovery(
            case.id,
            outcome="failed",
            evidence_digest=digest("different"),
            now=NOW.isoformat(),
        )
    assert store.reconcile_recovery(work.job.id).state == "completed"
    path = tmp_path / "operational.db"
    assert _scalar(path, "select state from local_recovery_case") == "resolved"
    assert _scalar(path, "select count(*) from local_recovery_resolution") == 1


def test_outbox_unknown_resolution_is_typed_and_terminal(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    store = SQLiteLocalRuntimeStore(path)
    store.enqueue(idempotency_key="job", payload={})
    claim = store.claim_outbox(
        supported_kinds=RUNTIME_OUTBOX_KINDS,
        owner_id="consumer",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert claim is not None
    store.record_outbox_receipt(
        claim,
        status="unknown",
        evidence_digest=digest("ambiguous-delivery"),
        now=NOW.isoformat(),
    )
    case = store.recovery_cases()[0]
    with pytest.raises(ValidationFailed, match="case kind"):
        store.resolve_recovery(
            case.id,
            outcome="completed",
            evidence_digest=digest("wrong-kind"),
            now=NOW.isoformat(),
        )
    store.resolve_recovery(
        case.id,
        outcome="delivered",
        evidence_digest=digest("verified-downstream"),
        now=NOW.isoformat(),
    )
    assert _scalar(path, "select state from local_outbox_delivery") == "delivered"
    assert store.recovery_cases() == ()


def test_database_rejects_terminal_without_evidence_and_recovery_evidence_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operational.db"
    store = SQLiteLocalRuntimeStore(path)
    store.enqueue(idempotency_key="job", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=1,
        owner_token="one",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    claim, _ = store.claim_effect(
        work,
        operation="external",
        effect_digest=digest("effect"),
        idempotency_key="effect",
        now=NOW.isoformat(),
    )
    store.record_receipt(
        claim,
        status="unknown",
        evidence_digest=digest("unknown"),
        now=NOW.isoformat(),
    )
    store.finish(work, state="recovery-required", now=NOW.isoformat())
    case = store.recovery_cases()[0]
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "update local_job set state='completed',terminal_evidence_digest=null where id=?",
                (work.job.id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable evidence"):
            connection.execute(
                "update local_recovery_case set evidence_digest=? where id=?",
                (digest("tampered"), case.id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="state transition"):
            connection.execute(
                "update local_recovery_case set state='resolved',resolved_at=? where id=?",
                (NOW.isoformat(), case.id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="case/outcome mismatch"):
            connection.execute(
                "insert into local_recovery_resolution values(?,?,?,?,?)",
                (
                    "forged-resolution",
                    case.id,
                    "delivered",
                    digest("forged-resolution"),
                    NOW.isoformat(),
                ),
            )
        connection.rollback()
        outbox_id = connection.execute("select id from local_outbox limit 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="must start open"):
            connection.execute(
                "insert into local_recovery_case values(?,?,null,?,'outbox-delivery-unknown',"
                "?,'resolved',?,?)",
                (
                    "forged-case",
                    work.job.id,
                    outbox_id,
                    digest("forged"),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL assertion is POSIX-only")
def test_outbox_delivery_kill_before_receipt_never_redelivers(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    delivery_path = tmp_path / "external-delivery.log"
    store = SQLiteLocalRuntimeStore(path)
    store.enqueue(idempotency_key="job", payload={})
    child_code = """
import os
import signal
import sys
from pathlib import Path
from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
database = Path(sys.argv[1])
delivery = Path(sys.argv[2])
store = SQLiteLocalRuntimeStore(database)
claim = store.claim_outbox(
    supported_kinds=RUNTIME_OUTBOX_KINDS,
    owner_id='child-consumer',
    owner_pid=os.getpid(),
    owner_token='child-incarnation',
    lease_seconds=1,
    now='2026-09-02T12:00:00+00:00',
)
assert claim is not None
with delivery.open('ab') as stream:
    stream.write(b'delivered-once\\n')
    stream.flush()
    os.fsync(stream.fileno())
os.kill(os.getpid(), signal.SIGKILL)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(path), str(delivery_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == -signal.SIGKILL
    assert delivery_path.read_bytes() == b"delivered-once\n"
    assert store.recover_outbox(now=(NOW + dt.timedelta(seconds=2)).isoformat()) == 1
    assert (
        store.claim_outbox(
            supported_kinds=RUNTIME_OUTBOX_KINDS,
            owner_id="replacement",
            owner_pid=os.getpid(),
            owner_token="replacement",
            lease_seconds=30,
            now=(NOW + dt.timedelta(seconds=2)).isoformat(),
        )
        is None
    )
    assert delivery_path.read_bytes().count(b"delivered-once\n") == 1
