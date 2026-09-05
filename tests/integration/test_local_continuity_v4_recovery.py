from __future__ import annotations

import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import ModuleType
from typing import Any, cast
from uuid import uuid5

import pytest

import zekam.infrastructure.sqlite.local_continuity_v4_recovery as recovery_module
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_internal import EffectClaimRequest
from zekam.application.local_continuity_v4_recovery import (
    B2_EVENT_NS,
    FrozenEffectRecoveryResolutionSnapshot,
    FrozenReceiptlessRecoverySnapshot,
    FrozenUnknownEffectSnapshot,
    ReceiptlessRecoveryRequest,
    ResolveEffectRecoveryRequest,
    UnknownEffectRequest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError, PolicyViolation
from zekam.infrastructure.sqlite.local_continuity_v4_internal import (
    verify_b1_b2_internal_producers,
)
from zekam.infrastructure.sqlite.local_continuity_v4_recovery import (
    SQLiteDormantV4Recovery,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

B1_TEST = Path(__file__).with_name("test_local_continuity_v4_internal.py")
UNKNOWN_AT = "2026-09-03T23:00:02+00:00"
EXPIRED_AT = "2026-09-04T00:00:01+00:00"
RESOLVED_AT = "2026-09-04T00:00:02+00:00"


def _fault_trigger(store: SQLiteDormantV4Recovery, *, action: str, table: str) -> None:
    assert action in {"insert", "update", "delete"}
    assert table.replace("_", "").isalnum()
    original = store._connect

    def connect(*, read_only: bool = False) -> sqlite3.Connection:
        db = original(read_only=read_only)
        if not read_only:
            db.execute(
                f"create temp trigger b2_injected_fault after {action} on {table} "
                "begin select raise(abort,'injected B2 statement fault'); end"
            )
        return db

    cast(Any, store)._connect = connect


def _logical_digest(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return digest(tuple(db.iterdump()))


def _fill_pending_outbox(path: Path, *, created_at: str) -> None:
    with sqlite3.connect(path) as db:
        pending = int(
            db.execute(
                "select count(*) from local_outbox_delivery where state in "
                "('pending','claimed','recovery-required')"
            ).fetchone()[0]
        )
        for index in range(pending, 64):
            job_id = f"018f0000-0000-7000-8000-{index + 900:012d}"
            outbox_id = f"018f0000-0000-7000-8001-{index + 900:012d}"
            payload = {"fixture": index}
            db.execute(
                "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,"
                "max_attempts,fencing_counter,terminal_evidence_digest,available_at,timeout_at,"
                "created_at,updated_at) values(?,?,?,'completed',0,1,0,?, ?,null,?,?)",
                (
                    job_id,
                    f"capacity-fixture-{index}",
                    "{}",
                    digest(f"fixture-{index}"),
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (
                    outbox_id,
                    job_id,
                    f"capacity-{index}",
                    "job.completed",
                    canonical_json(payload),
                    digest(payload),
                    created_at,
                ),
            )
            db.execute(
                "insert into local_outbox_delivery("
                "outbox_id,state,claim_id,fencing_counter,updated_at) "
                "values(?,'pending',null,0,?)",
                (outbox_id, created_at),
            )


def _load_b1() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b2_b1_fixture", B1_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue[SnapshotT](kind: type[SnapshotT], **values: object) -> SnapshotT:
    value = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(value, name, values[name])
    value.__post_init__()  # type: ignore[attr-defined]
    return value


class _UnknownIssuer:
    def __init__(self, path: Path, binding: ContinuityBinding) -> None:
        self.path = path
        self.binding = binding
        self.fail = False
        self.last: FrozenUnknownEffectSnapshot | None = None

    def snapshot(self, request: UnknownEffectRequest) -> FrozenUnknownEffectSnapshot:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            claim = db.execute(
                "select * from local_effect_claim where id=?", (request.claim_id,)
            ).fetchone()
            lease = db.execute(
                "select * from local_lease where id=?", (claim["lease_id"],)
            ).fetchone()
        assert claim is not None and lease is not None
        self.last = _issue(
            FrozenUnknownEffectSnapshot,
            binding_digest=self.binding.binding_digest,
            job_id=claim["job_id"],
            claim_id=claim["id"],
            lease_id=lease["id"],
            lease_owner_id=lease["owner_id"],
            lease_owner_pid=lease["owner_pid"],
            lease_owner_token=lease["owner_token"],
            fencing_token=claim["fencing_token"],
            operation=claim["operation"],
            effect_commitment_digest=claim["effect_digest"],
            claimed_at=claim["claimed_at"],
            unknown_category="executor-ambiguous",
            unknown_commitment_digest=digest("sealed-unknown-envelope"),
            observed_at=UNKNOWN_AT,
            issuer_receipt_id="unknown-issuer-private",
        )
        return self.last

    def recheck(self, snapshot: FrozenUnknownEffectSnapshot) -> None:
        if self.fail or snapshot != self.last:
            raise PolicyViolation("injected B2 unknown issuer drift")


class _ReceiptlessIssuer:
    def __init__(self, path: Path, binding: ContinuityBinding) -> None:
        self.path = path
        self.binding = binding
        self.fail = False
        self.reason = "lease-expired"
        self.observed_at = EXPIRED_AT
        self.last: FrozenReceiptlessRecoverySnapshot | None = None

    def snapshot(self, request: ReceiptlessRecoveryRequest) -> FrozenReceiptlessRecoverySnapshot:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            claim = db.execute(
                "select * from local_effect_claim where id=?", (request.claim_id,)
            ).fetchone()
            lease = db.execute(
                "select * from local_lease where id=?", (claim["lease_id"],)
            ).fetchone()
        assert claim is not None and lease is not None
        self.last = _issue(
            FrozenReceiptlessRecoverySnapshot,
            binding_digest=self.binding.binding_digest,
            job_id=claim["job_id"],
            claim_id=claim["id"],
            lease_id=lease["id"],
            lease_owner_id=lease["owner_id"],
            lease_owner_pid=lease["owner_pid"],
            lease_owner_token=lease["owner_token"],
            fencing_token=claim["fencing_token"],
            operation=claim["operation"],
            effect_commitment_digest=claim["effect_digest"],
            claimed_at=claim["claimed_at"],
            recovery_reason=self.reason,
            observed_at=self.observed_at,
            issuer_receipt_id="recovery-manager-private",
        )
        return self.last

    def recheck(self, snapshot: FrozenReceiptlessRecoverySnapshot) -> None:
        if self.fail or snapshot != self.last:
            raise PolicyViolation("injected B2 recovery manager drift")


class _Adjudicator:
    def __init__(self, path: Path, binding: ContinuityBinding) -> None:
        self.path = path
        self.binding = binding
        self.outcome = "completed"
        self.fail = False
        self.barrier: Barrier | None = None
        self.last: FrozenEffectRecoveryResolutionSnapshot | None = None

    def snapshot(
        self, request: ResolveEffectRecoveryRequest
    ) -> FrozenEffectRecoveryResolutionSnapshot:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            case = db.execute(
                "select * from local_recovery_case where id=?", (request.recovery_case_id,)
            ).fetchone()
            claim = db.execute(
                "select * from local_effect_claim where id=?", (case["effect_claim_id"],)
            ).fetchone()
        assert case is not None and claim is not None
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        self.last = _issue(
            FrozenEffectRecoveryResolutionSnapshot,
            binding_digest=self.binding.binding_digest,
            job_id=case["job_id"],
            claim_id=claim["id"],
            recovery_case_id=case["id"],
            outcome=self.outcome,
            resolution_commitment_digest=digest(f"sealed-resolution-{self.outcome}"),
            resolved_at=RESOLVED_AT,
            issuer_receipt_id="adjudicator-private",
        )
        return self.last

    def recheck(self, snapshot: FrozenEffectRecoveryResolutionSnapshot) -> None:
        if self.fail or snapshot != self.last:
            raise PolicyViolation("injected B2 adjudicator drift")


def _prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, SQLiteDormantV4Recovery, str]:
    b1 = _load_b1()
    path, binding, producer, _turn, _claim, _outcome = b1._prepared(tmp_path, monkeypatch)
    _runtime, work = b1._running_work(path, binding, monkeypatch)
    claim_result = producer.claim_effect(
        EffectClaimRequest(binding, work.job.id, b1._tail(path, binding))
    )
    store = SQLiteDormantV4Recovery(
        path,
        binding,
        unknown_issuer=_UnknownIssuer(path, binding),
        receiptless_issuer=_ReceiptlessIssuer(path, binding),
        adjudicator=_Adjudicator(path, binding),
    )
    return path, binding, b1, store, claim_result.producer_ref


def _revision(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "order by revision_number desc limit 1"
            ).fetchone()[0]
        )


def _tail(path: Path, binding: ContinuityBinding) -> ContinuityTail:
    b1 = _load_b1()
    return cast(ContinuityTail, b1._tail(path, binding))


def test_unknown_entry_completed_resolution_restart_and_exact_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, _revision(path))
    entered = store.enter_unknown(request)
    assert entered.body()["status"] == "fresh"
    assert entered.body()["event_written"] is False
    assert store.enter_unknown(request).body()["status"] == "replayed"
    case_id = str(entered.body()["recovery_case_id"])
    resolved = store.resolve(
        ResolveEffectRecoveryRequest(binding, case_id, _revision(path), _tail(path, binding))
    )
    assert resolved.body()["status"] == "fresh"
    assert resolved.body()["job_state"] == "completed"
    restarted = SQLiteDormantV4Recovery(
        path,
        binding,
        unknown_issuer=_UnknownIssuer(path, binding),
        receiptless_issuer=_ReceiptlessIssuer(path, binding),
        adjudicator=_Adjudicator(path, binding),
    )
    assert restarted.enter_unknown(request).body()["status"] == "replayed"
    assert (
        restarted.resolve(
            ResolveEffectRecoveryRequest(binding, case_id, _revision(path), _tail(path, binding))
        ).body()["status"]
        == "replayed"
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("begin")
        verify_b1_b2_internal_producers(db, binding)
        assert (
            db.execute(
                "select count(*) from session_event where event_kind='CRASH_RECOVERED'"
            ).fetchone()[0]
            == 1
        )


def test_receiptless_expiry_failed_resolution_is_attention_without_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    entered = store.enter_receiptless(
        ReceiptlessRecoveryRequest(binding, claim_id, _revision(path))
    )
    assert entered.body()["schema"] == "zekam-v4-receiptless-recovery-entry-result/v1"
    adjudicator = store.adjudicator
    assert isinstance(adjudicator, _Adjudicator)
    adjudicator.outcome = "failed"
    failed = store.resolve(
        ResolveEffectRecoveryRequest(
            binding, str(entered.body()["recovery_case_id"]), _revision(path), _tail(path, binding)
        )
    )
    assert failed.body()["attention"] is True
    assert failed.body()["event_written"] is False
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "select count(*) from session_event where event_kind='CRASH_RECOVERED'"
            ).fetchone()[0]
            == 0
        )


def test_receiptless_owner_loss_has_same_public_shape_and_never_persists_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    issuer = store.receiptless_issuer
    assert isinstance(issuer, _ReceiptlessIssuer)
    issuer.reason = "owner-incarnation-lost"
    issuer.observed_at = UNKNOWN_AT
    result = store.enter_receiptless(ReceiptlessRecoveryRequest(binding, claim_id, _revision(path)))
    assert result.body()["schema"] == "zekam-v4-receiptless-recovery-entry-result/v1"
    with sqlite3.connect(path) as db:
        durable = "\n".join(row[0] for row in db.iterdump())
    assert "owner-incarnation-lost" not in durable
    assert "lease-expired" not in durable


@pytest.mark.parametrize("route", ("unknown", "receiptless"))
def test_issuer_recheck_drift_rolls_back_entire_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    before = _revision(path)
    if route == "unknown":
        unknown = store.unknown_issuer
        assert isinstance(unknown, _UnknownIssuer)
        unknown.fail = True

        def call() -> object:
            return store.enter_unknown(UnknownEffectRequest(binding, claim_id, before))
    else:
        receiptless = store.receiptless_issuer
        assert isinstance(receiptless, _ReceiptlessIssuer)
        receiptless.fail = True

        def call() -> object:
            return store.enter_receiptless(ReceiptlessRecoveryRequest(binding, claim_id, before))

    with pytest.raises(PolicyViolation, match=r"^B2 authority recheck failed$"):
        call()
    assert _revision(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_recovery_case").fetchone()[0] == 0
        assert (
            db.execute(
                "select state from local_job where id=(select job_id "
                "from local_effect_claim where id=?)",
                (claim_id,),
            ).fetchone()[0]
            == "running"
        )


def test_capacity_failure_preserves_lease_case_job_revision_and_event_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    before_revision = _revision(path)
    before_tail = _tail(path, binding)
    _fill_pending_outbox(path, created_at=UNKNOWN_AT)
    with pytest.raises(PolicyViolation, match="backpressure"):
        store.enter_unknown(UnknownEffectRequest(binding, claim_id, before_revision))
    assert _revision(path) == before_revision and _tail(path, binding) == before_tail
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_recovery_case").fetchone()[0] == 0
        assert db.execute("select count(*) from local_lease").fetchone()[0] == 1


def test_resolution_capacity_failure_preserves_open_case_and_recovery_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    case_id = str(entered.body()["recovery_case_id"])
    before_revision = _revision(path)
    before_tail = _tail(path, binding)
    _fill_pending_outbox(path, created_at=UNKNOWN_AT)
    before = _logical_digest(path)
    with pytest.raises(PolicyViolation, match="backpressure"):
        store.resolve(ResolveEffectRecoveryRequest(binding, case_id, before_revision, before_tail))
    assert _logical_digest(path) == before
    assert _revision(path) == before_revision and _tail(path, binding) == before_tail
    with sqlite3.connect(path) as db:
        case = db.execute(
            "select state,resolved_at from local_recovery_case where id=?", (case_id,)
        ).fetchone()
        assert case == ("open", None)
        assert db.execute("select count(*) from local_recovery_resolution").fetchone()[0] == 0


def test_concurrent_unknown_and_receiptless_routes_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    revision = _revision(path)
    calls = (
        lambda: store.enter_unknown(UnknownEffectRequest(binding, claim_id, revision)),
        lambda: store.enter_receiptless(ReceiptlessRecoveryRequest(binding, claim_id, revision)),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call) for call in calls]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(successes) == 1
    assert len(failures) == 1 and isinstance(failures[0], ConcurrencyConflict)


def test_concurrent_completed_and_failed_resolution_has_one_exact_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, first, claim_id = _prepared(tmp_path, monkeypatch)
    entered = first.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    case_id = str(entered.body()["recovery_case_id"])
    barrier = Barrier(2)
    stores = []
    for outcome in ("completed", "failed"):
        adjudicator = _Adjudicator(path, binding)
        adjudicator.outcome = outcome
        adjudicator.barrier = barrier
        stores.append(
            SQLiteDormantV4Recovery(
                path,
                binding,
                unknown_issuer=_UnknownIssuer(path, binding),
                receiptless_issuer=_ReceiptlessIssuer(path, binding),
                adjudicator=adjudicator,
            )
        )
    request = ResolveEffectRecoveryRequest(binding, case_id, _revision(path), _tail(path, binding))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.resolve, request) for store in stores]
    assert sum(future.exception() is None for future in futures) == 1
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(failures) == 1 and isinstance(failures[0], ConcurrencyConflict)


@pytest.mark.parametrize("status", ("delivered", "failed", "unknown"))
def test_entry_outbox_runtime_progression_remains_exactly_verifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    moment = datetime(2026, 9, 4, 0, 1, tzinfo=UTC)
    delivery = runtime.claim_outbox(
        supported_kinds=("job.recovery-required",),
        owner_id="b2-delivery-worker",
        owner_pid=31339,
        owner_token="b2-delivery-incarnation",
        lease_seconds=30,
        now=moment.isoformat(),
    )
    assert delivery is not None
    runtime.record_outbox_receipt(
        delivery,
        status=cast(Any, status),
        evidence_digest=digest(f"b2-delivery:{status}"),
        now=(moment + timedelta(seconds=1)).isoformat(),
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_b2_internal_producers(db, binding)


def test_commit_unknown_reopens_read_only_and_reconstructs_complete_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    original = store._commit

    def lost_ack(db: sqlite3.Connection) -> None:
        original(db)
        raise OSError("private commit ack lost")

    monkeypatch.setattr(store, "_commit", lost_ack)
    result = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    assert result.body()["status"] == "replayed"
    assert "private commit ack lost" not in str(result.body())


def test_commit_failure_with_rollback_returns_exact_unobservable_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)

    def rollback_then_fail(db: sqlite3.Connection) -> None:
        db.rollback()
        raise OSError("private commit failed")

    monkeypatch.setattr(store, "_commit", rollback_then_fail)
    result = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    assert result.body() == {
        "schema": "zekam-v4-recovery-commit-outcome/v1",
        "status": "not-committed-or-unobservable",
        "operation": "unknown-entry",
        "claim_id": claim_id,
        "recovery_case_id": str(uuid5(B2_EVENT_NS, f"effect-case|{claim_id}")),
        "safe_to_retry": True,
        "grants_authority": False,
        "approval_inherited": False,
        "production_activated": False,
    }


def test_resolution_commit_failure_returns_case_bound_unobservable_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    case_id = str(entered.body()["recovery_case_id"])

    def rollback_then_fail(db: sqlite3.Connection) -> None:
        db.rollback()
        raise OSError("private resolution commit failed")

    monkeypatch.setattr(store, "_commit", rollback_then_fail)
    result = store.resolve(
        ResolveEffectRecoveryRequest(binding, case_id, _revision(path), _tail(path, binding))
    )
    assert result.body()["operation"] == "resolve"
    assert result.body()["claim_id"] == claim_id
    assert result.body()["recovery_case_id"] == case_id
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_recovery_resolution").fetchone()[0] == 0


@pytest.mark.parametrize("partial", (False, True))
def test_commit_failure_rejects_baseline_or_partial_graph_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    original = store._commit

    def fail(db: sqlite3.Connection) -> None:
        if partial:
            original(db)
        else:
            db.rollback()
        with sqlite3.connect(path) as other:
            before = other.execute(
                "select updated_at from local_job where id=(select job_id "
                "from local_effect_claim where id=?)",
                (claim_id,),
            ).fetchone()
            assert before is not None
            assert before[0] != "2026-09-04T23:00:01+00:00"
            other.execute(
                "update local_job set updated_at=? where id=(select job_id "
                "from local_effect_claim where id=?)",
                ("2026-09-04T23:00:01+00:00", claim_id),
            )
        raise OSError("PRIVATE-COMMIT-CANARY")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(ConcurrencyConflict, match="partial recovery entry graph") as caught:
        store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    assert "PRIVATE-COMMIT-CANARY" not in str(caught.value)


def test_issuer_exceptions_are_fixed_sanitized_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    before = _logical_digest(path)

    def leak(_request: object) -> object:
        raise RuntimeError("PRIVATE-API-KEY /Users/private prompt")

    monkeypatch.setattr(store.unknown_issuer, "snapshot", leak)
    with pytest.raises(PolicyViolation, match=r"^B2 authority snapshot unavailable$") as caught:
        store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    assert "PRIVATE" not in str(caught.value)
    assert _logical_digest(path) == before


def test_mixed_verifier_reads_trusted_clock_once_per_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return datetime(2026, 9, 3, 23, 0, 3 + calls, tzinfo=UTC)

    monkeypatch.setattr(SQLiteDormantV4CloseWriter, "_trusted_now", staticmethod(clock))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("begin")
        verify_b1_b2_internal_producers(db, binding, selected_b2_claim_id=claim_id)
    assert calls == 1


def test_tampered_case_or_revision_is_rejected_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, _revision(path))
    store.enter_unknown(request)
    with sqlite3.connect(path) as db:
        db.execute("drop trigger local_recovery_case_guard_update")
        db.execute(
            "update local_recovery_case set evidence_digest=? where effect_claim_id=?",
            (digest("forged"), claim_id),
        )
    with pytest.raises((ConfigurationError, PolicyViolation, ConcurrencyConflict)):
        store.enter_unknown(request)


def test_second_recovery_after_restoration_is_rejected_but_mixed_b1_history_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    store.resolve(
        ResolveEffectRecoveryRequest(
            binding, str(entered.body()["recovery_case_id"]), _revision(path), _tail(path, binding)
        )
    )
    runtime, work = b1._running_work(path, binding, monkeypatch, "after-b2")
    claimed = b1.SQLiteDormantV4InternalProducer(
        path,
        binding,
        turn_issuer=b1._TurnIssuer(binding),
        claim_issuer=b1._ClaimIssuer(path, binding),
        outcome_issuer=b1._OutcomeIssuer(path, binding),
    ).claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    second = SQLiteDormantV4Recovery(
        path,
        binding,
        unknown_issuer=_UnknownIssuer(path, binding),
        receiptless_issuer=_ReceiptlessIssuer(path, binding),
        adjudicator=_Adjudicator(path, binding),
    )
    with pytest.raises(
        (PolicyViolation, ConcurrencyConflict), match=r"first recovery|history|drift"
    ):
        second.enter_unknown(UnknownEffectRequest(binding, claimed.producer_ref, _revision(path)))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("begin")
        verify_b1_b2_internal_producers(db, binding)
    assert runtime.status().running_jobs == 1


def test_private_canaries_and_excluded_resources_never_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = _prepared(tmp_path, monkeypatch)
    store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    canaries = ("PRIVATE-CANARY", "provider-key", "/Users/mkaracan/Projeler/akilli-kasa")
    with sqlite3.connect(path) as db:
        text = "\n".join(str(value) for row in db.iterdump() for value in (row,))
    for canary in canaries:
        assert canary not in text


def test_every_entry_mutation_fault_rolls_back_exact_logical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = (
        ("insert", "local_effect_receipt"),
        ("insert", "local_recovery_case"),
        ("delete", "local_resource_lock"),
        ("delete", "local_lease"),
        ("update", "local_job"),
        ("insert", "local_outbox"),
        ("insert", "local_outbox_delivery"),
        ("insert", "continuity_hook_attachment_revision"),
    )
    for action, table in targets:
        case_root = tmp_path / f"entry-{action}-{table}"
        case_root.mkdir()
        path, binding, _fixture, store, claim_id = _prepared(case_root, monkeypatch)
        before = _logical_digest(path)
        _fault_trigger(store, action=action, table=table)
        try:
            store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
        except ConcurrencyConflict as exc:
            assert "concurrency conflict" in str(exc)
        else:
            pytest.fail(f"entry fault did not fire: {action} {table}")
        assert _logical_digest(path) == before


def test_every_resolution_mutation_fault_rolls_back_exact_logical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = (
        ("insert", "local_recovery_resolution"),
        ("update", "local_recovery_case"),
        ("update", "local_job"),
        ("insert", "local_outbox"),
        ("insert", "local_outbox_delivery"),
        ("insert", "continuity_internal_event_receipt"),
        ("insert", "session_event"),
        ("insert", "session_event_detail"),
        ("insert", "continuity_hook_attachment_revision"),
    )
    for action, table in targets:
        case_root = tmp_path / f"resolution-{action}-{table}"
        case_root.mkdir()
        path, binding, _fixture, store, claim_id = _prepared(case_root, monkeypatch)
        entered = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
        case_id = str(entered.body()["recovery_case_id"])
        before = _logical_digest(path)
        _fault_trigger(store, action=action, table=table)
        try:
            store.resolve(
                ResolveEffectRecoveryRequest(
                    binding, case_id, _revision(path), _tail(path, binding)
                )
            )
        except ConcurrencyConflict as exc:
            assert "concurrency conflict" in str(exc)
        else:
            pytest.fail(f"resolution fault did not fire: {action} {table}")
        assert _logical_digest(path) == before


@pytest.mark.parametrize("fail_call", (1, 2))
def test_entry_semantic_verifier_fault_before_or_after_mutations_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_call: int
) -> None:
    path, binding, _fixture, store, claim_id = _prepared(tmp_path, monkeypatch)
    before = _logical_digest(path)
    original = cast(Any, vars(recovery_module)["verify_b1_b2_internal_producers"])
    calls = 0

    def fail_selected_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise PolicyViolation("injected B2 semantic verifier fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "verify_b1_b2_internal_producers", fail_selected_call)
    with pytest.raises(PolicyViolation, match="semantic verifier fault"):
        store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    assert calls == fail_call
    assert _logical_digest(path) == before


@pytest.mark.parametrize("fail_call", (1, 2))
def test_resolution_semantic_verifier_fault_before_or_after_mutations_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_call: int
) -> None:
    path, binding, _fixture, store, claim_id = _prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(UnknownEffectRequest(binding, claim_id, _revision(path)))
    case_id = str(entered.body()["recovery_case_id"])
    before = _logical_digest(path)
    original = cast(Any, vars(recovery_module)["verify_b1_b2_internal_producers"])
    calls = 0

    def fail_selected_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise PolicyViolation("injected B2 semantic verifier fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "verify_b1_b2_internal_producers", fail_selected_call)
    with pytest.raises(PolicyViolation, match="semantic verifier fault"):
        store.resolve(
            ResolveEffectRecoveryRequest(binding, case_id, _revision(path), _tail(path, binding))
        )
    assert calls == fail_call
    assert _logical_digest(path) == before


def test_default_v3_is_rejected_without_database_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.infrastructure.sqlite import operational_schema

    _v4_path, binding, _b1, _store, _claim_id = _prepared(tmp_path, monkeypatch)
    path = tmp_path / "v3.db"
    operational_schema.bootstrap(path)
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="operational-v4"):
        SQLiteDormantV4Recovery(
            path,
            binding,
            unknown_issuer=_UnknownIssuer(path, binding),
            receiptless_issuer=_ReceiptlessIssuer(path, binding),
            adjudicator=_Adjudicator(path, binding),
        )
    assert path.read_bytes() == before
