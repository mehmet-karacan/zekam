from __future__ import annotations

import datetime as dt
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.local_runtime import LocalOutboxClaim
from zekam.application.local_runtime_service import (
    LocalDeliveryResult,
    LocalEffectResult,
    LocalOutboxDispatcher,
    LocalRuntimeService,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

NOW = dt.datetime(2026, 9, 4, tzinfo=dt.UTC)
COMPILE = "continuity.compile"


def _store(tmp_path: Path) -> SQLiteLocalRuntimeStore:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    store.enqueue(idempotency_key="job", payload={"value": 1})
    return store


def _add_event(
    store: SQLiteLocalRuntimeStore,
    kind: str,
    *,
    raw_payload: str | None = None,
    payload_digest: str | None = None,
) -> str:
    """Insert test-owned producer data without mutating any schema or real home."""
    event_id = str(uuid4())
    payload = {"purpose": kind}
    with sqlite3.connect(store.path) as connection:
        connection.execute("pragma foreign_keys=on")
        job_id = connection.execute("select id from local_job").fetchone()[0]
        connection.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (
                event_id,
                job_id,
                event_id,
                kind,
                canonical_json(payload) if raw_payload is None else raw_payload,
                digest(payload) if payload_digest is None else payload_digest,
                "2020-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "insert into local_outbox_delivery(outbox_id,state,updated_at) values(?,'pending',?)",
            (event_id, NOW.isoformat()),
        )
    return event_id


def _claim(
    store: SQLiteLocalRuntimeStore,
    kinds: tuple[str, ...] = (COMPILE,),
    *,
    owner: str = "consumer",
    now: dt.datetime = NOW,
) -> LocalOutboxClaim | None:
    return store.claim_outbox(
        supported_kinds=kinds,
        owner_id=owner,
        owner_pid=12,
        owner_token=owner,
        lease_seconds=30,
        now=now.isoformat(),
    )


def _delivered(claim: LocalOutboxClaim) -> LocalDeliveryResult:
    return LocalDeliveryResult("delivered", digest(claim.event.id))


def _service(store: SQLiteLocalRuntimeStore, handler: Any = _delivered) -> LocalRuntimeService:
    return LocalRuntimeService(
        store,
        effect_executor=lambda _: LocalEffectResult("completed", digest("unused")),
        outbox_dispatcher=LocalOutboxDispatcher(((COMPILE, handler),)),
    )


def _tick(service: LocalRuntimeService) -> LocalOutboxClaim | None:
    return service.publish_outbox_once(owner_id="consumer", owner_pid=12, owner_token="token")


@pytest.mark.parametrize(
    "kinds",
    [
        None,
        "continuity.compile",
        [],
        {},
        (),
        (None,),
        (True,),
        (1,),
        ("",),
        (" ",),
        (" continuity.compile",),
        ("continuity.compile ",),
        ("*",),
        ("a\x00",),
        ("a' OR 1=1--",),
        ("a" * 129,),
        (COMPILE, COMPILE),
        tuple(f"a{i}" for i in range(65)),
    ],
)
def test_invalid_allowlist_fails_before_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kinds: Any,
) -> None:
    store = _store(tmp_path)
    before = store.status()

    def forbidden() -> None:
        pytest.fail("Invalid consumer must fail before any connection/recovery/write")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", forbidden)
        with pytest.raises(ValidationFailed):
            _claim(store, kinds)
    assert store.status() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", None),
        ("owner_id", ""),
        ("owner_id", 12),
        ("owner_token", None),
        ("owner_token", ""),
        ("owner_token", []),
        ("owner_pid", None),
        ("owner_pid", True),
        ("owner_pid", 0),
        ("lease_seconds", None),
        ("lease_seconds", True),
        ("lease_seconds", 0),
    ],
)
def test_invalid_consumer_identity_has_no_claim(tmp_path: Path, field: str, value: Any) -> None:
    store = _store(tmp_path)
    arguments: dict[str, Any] = {
        "supported_kinds": ("job.enqueued",),
        "owner_id": "c",
        "owner_pid": 12,
        "owner_token": "t",
        "lease_seconds": 30,
    }
    arguments[field] = value
    with pytest.raises(ValidationFailed):
        store.claim_outbox(**arguments)
    assert store.status().claimed_outbox == 0
    assert store.status().pending_outbox == 1


def test_mixed_queue_exact_selection_and_unknown_visibility(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unknown = _add_event(store, "future.unsupported")
    expected = _add_event(store, COMPILE)
    assert _claim(store, ("not.registered",)) is None
    claim = _claim(store)
    assert claim is not None and claim.event.id == expected
    store.record_outbox_receipt(
        claim, status="delivered", evidence_digest=digest("ok"), now=NOW.isoformat()
    )
    assert _claim(store) is None
    pending = store.pending_outbox()
    assert {event.event_kind for event in pending} == {"job.enqueued", "future.unsupported"}
    assert next(event for event in pending if event.id == unknown).state == "pending"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "select claim_id,fencing_counter from local_outbox_delivery where outbox_id=?",
            (unknown,),
        ).fetchone() == (None, 0)
        assert connection.execute("select count(*) from local_outbox_receipt").fetchone()[0] == 1


def test_maximum_kind_count_and_token_length_are_supported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kind = "a" * 128
    event_id = _add_event(store, kind)
    claim = _claim(store, (*(f"kind{i}" for i in range(63)), kind))
    assert claim is not None and claim.event.id == event_id


@pytest.mark.parametrize(
    "routes",
    [
        None,
        [],
        (),
        "x",
        ((COMPILE,),),
        ((COMPILE, None),),
        ((COMPILE, 1),),
        ((COMPILE, _delivered, "extra"),),
        ((None, _delivered),),
        ((COMPILE, _delivered), (COMPILE, _delivered)),
    ],
)
def test_dispatcher_rejects_invalid_or_duplicate_routes(routes: Any) -> None:
    with pytest.raises(ValidationFailed):
        LocalOutboxDispatcher(routes)


def test_wrong_dispatcher_cannot_invoke_handler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    claim = _claim(store)
    assert claim is not None
    calls: list[str] = []

    def handler(value: LocalOutboxClaim) -> LocalDeliveryResult:
        calls.append(value.event.id)
        return _delivered(value)

    dispatcher = LocalOutboxDispatcher((("job.enqueued", handler),))
    with pytest.raises(PolicyViolation, match="desteklemiyor"):
        dispatcher(claim)
    assert calls == []
    assert store.status().claimed_outbox == 1


def test_service_legacy_publisher_never_claims_continuity_or_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    _add_event(store, "future.kind")
    calls: list[str] = []

    def publisher(claim: LocalOutboxClaim) -> LocalDeliveryResult:
        calls.append(claim.event.event_kind)
        return _delivered(claim)

    service = LocalRuntimeService(
        store,
        effect_executor=lambda _: LocalEffectResult("completed", digest("unused")),
        outbox_publisher=publisher,
    )
    assert _tick(service) is not None
    assert _tick(service) is None
    assert calls == ["job.enqueued"]
    assert {event.event_kind for event in store.pending_outbox()} == {COMPILE, "future.kind"}
    assert _tick(_service(store, publisher)) is not None
    assert calls == ["job.enqueued", COMPILE]


@pytest.mark.parametrize(
    "bad_result",
    [
        None,
        {},
        "delivered",
        LocalDeliveryResult("completed", digest("x")),  # type: ignore[arg-type]
        LocalDeliveryResult("delivered", "bad"),
    ],
)
def test_invalid_handler_result_is_unknown_never_success(tmp_path: Path, bad_result: Any) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    assert _tick(_service(store, lambda _: bad_result)) is not None
    assert store.status().recovery_outbox == 1
    assert store.status().open_recovery_cases == 1
    assert _tick(_service(store)) is None


def test_partial_delivery_timeout_restart_does_not_redeliver(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    effects: list[str] = []

    def partial(claim: LocalOutboxClaim) -> LocalDeliveryResult:
        effects.append(claim.event.id)
        raise TimeoutError("effect may have completed")

    assert _tick(_service(store, partial)) is not None
    restarted = SQLiteLocalRuntimeStore(store.path)
    assert _tick(_service(restarted, partial)) is None
    assert len(effects) == 1
    assert restarted.status().recovery_outbox == 1


@pytest.mark.parametrize(
    "raw,checksum",
    [
        ("not-json", digest({})),
        ("null", digest(None)),
        ("[]", digest([])),
        ('{"value":NaN}', digest({})),
        ('{"value":1,"value":1}', digest({"value": 1})),
        ('{"purpose":"continuity.compile"}', digest("wrong")),
    ],
)
def test_corrupt_payload_rolls_back_claim(tmp_path: Path, raw: str, checksum: str) -> None:
    store = _store(tmp_path)
    event_id = _add_event(store, COMPILE, raw_payload=raw, payload_digest=checksum)
    with pytest.raises(ValidationFailed):
        _claim(store)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "select state,claim_id,fencing_counter from local_outbox_delivery where outbox_id=?",
            (event_id,),
        ).fetchone() == ("pending", None, 0)
        assert connection.execute("select count(*) from local_outbox_receipt").fetchone()[0] == 0


def test_concurrent_consumers_one_exact_owner_and_receipt_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    other = SQLiteLocalRuntimeStore(store.path)
    barrier = Barrier(2)

    def race(pair: tuple[SQLiteLocalRuntimeStore, str]) -> LocalOutboxClaim | None:
        target, owner = pair
        barrier.wait(timeout=5)
        return _claim(target, owner=owner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(race, ((store, "one"), (other, "two"))))
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    forged = replace(claim, owner_token="wrong")
    with pytest.raises(ConcurrencyConflict):
        store.record_outbox_receipt(
            forged, status="delivered", evidence_digest=digest("ok"), now=NOW.isoformat()
        )
    first = store.record_outbox_receipt(
        claim, status="delivered", evidence_digest=digest("ok"), now=NOW.isoformat()
    )
    replay = other.record_outbox_receipt(
        claim, status="delivered", evidence_digest=digest("ok"), now=NOW.isoformat()
    )
    assert first == replay
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        other.record_outbox_receipt(
            claim, status="delivered", evidence_digest=digest("different"), now=NOW.isoformat()
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_kind", "job.enqueued"),
        ("job_id", "different-job"),
        ("idempotency_key", "different-key"),
        ("payload", {"changed": True}),
    ],
)
def test_forged_claim_event_identity_cannot_receive_receipt(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    claim = _claim(store)
    assert claim is not None
    forged = replace(claim, event=replace(claim.event, **{field: value}))
    with pytest.raises(ConcurrencyConflict):
        store.record_outbox_receipt(
            forged, status="delivered", evidence_digest=digest("forged"), now=NOW.isoformat()
        )
    assert store.status().claimed_outbox == 1


def test_expired_claim_is_unknown_and_never_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_event(store, COMPILE)
    claim = _claim(store)
    assert claim is not None
    later = NOW + dt.timedelta(seconds=31)
    assert _claim(store, owner="replacement", now=later) is None
    assert store.status().recovery_outbox == 1
    with pytest.raises(ConcurrencyConflict):
        store.record_outbox_receipt(
            claim, status="delivered", evidence_digest=digest("late"), now=later.isoformat()
        )


def _claim_job(store: SQLiteLocalRuntimeStore, **selection: Any) -> Any:
    return store.claim_next(
        owner_id="worker",
        owner_pid=12,
        owner_token="worker-token",
        lease_seconds=30,
        now=NOW.isoformat(),
        **selection,
    )


@pytest.mark.parametrize(
    "operation", [COMPILE, f" {COMPILE} ", f"\t{COMPILE}\n", f"\u2003{COMPILE}\u3000"]
)
def test_legacy_worker_never_claims_reserved_compile_job(tmp_path: Path, operation: str) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    reserved, _ = store.enqueue(
        idempotency_key="compile",
        payload={"operation": operation},
        available_at="2020-01-01T00:00:00+00:00",
    )
    ordinary, _ = store.enqueue(
        idempotency_key="legacy", payload={}, available_at="2020-01-02T00:00:00+00:00"
    )
    work = _claim_job(store)
    assert work is not None and work.job.id == ordinary.id
    assert _claim_job(store) is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "select state,attempt_count from local_job where id=?", (reserved.id,)
        ).fetchone() == ("ready", 0)


@pytest.mark.parametrize(
    "selection",
    [
        {"supported_operations": ()},
        {"supported_operations": []},
        {"supported_operations": COMPILE},
        {"supported_operations": (None,)},
        {"supported_operations": (COMPILE, COMPILE)},
        {"supported_operations": ("*",)},
        {"supported_operations": ("a" * 129,)},
        {"job_id": "some-id"},
        {"job_id": "some-id", "supported_operations": None},
        {"job_id": "", "supported_operations": (COMPILE,)},
        {"job_id": 12, "supported_operations": (COMPILE,)},
        {"job_id": " padded ", "supported_operations": (COMPILE,)},
    ],
)
def test_job_selection_rejects_malformed_before_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: dict[str, Any],
) -> None:
    store = _store(tmp_path)

    def forbidden() -> None:
        pytest.fail("Invalid job selection cannot open database")

    monkeypatch.setattr(store, "_connect", forbidden)
    with pytest.raises(ValidationFailed):
        _claim_job(store, **selection)


def test_compile_job_requires_exact_operation_and_job_then_effect_authority(tmp_path: Path) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    first, _ = store.enqueue(
        idempotency_key="first",
        payload={"operation": COMPILE},
        available_at="2020-01-01T00:00:00+00:00",
    )
    second, _ = store.enqueue(
        idempotency_key="second",
        payload={"operation": COMPILE},
        available_at="2020-01-02T00:00:00+00:00",
    )
    ordinary, _ = store.enqueue(idempotency_key="ordinary", payload={"operation": "legacy"})
    assert _claim_job(store, supported_operations=(COMPILE,), job_id=ordinary.id) is None
    assert _claim_job(store, supported_operations=("legacy",), job_id=second.id) is None
    assert _claim_job(store, supported_operations=(COMPILE,), job_id="missing") is None
    work = _claim_job(store, supported_operations=(COMPILE,), job_id=second.id)
    assert work is not None and work.job.id == second.id
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("select state from local_job where id=?", (first.id,)).fetchone()[0]
            == "ready"
        )
        assert connection.execute("select count(*) from local_effect_claim").fetchone()[0] == 0
    request_digest = digest({"compile": second.id})
    forged = replace(work, lease=replace(work.lease, owner_token="wrong"))
    with pytest.raises(ConcurrencyConflict):
        store.claim_effect(
            forged,
            operation=COMPILE,
            effect_digest=request_digest,
            idempotency_key="compile-effect",
            now=NOW.isoformat(),
        )
    effect, created = store.claim_effect(
        work,
        operation=COMPILE,
        effect_digest=request_digest,
        idempotency_key="compile-effect",
        now=NOW.isoformat(),
    )
    assert created
    store.record_receipt(
        effect, status="completed", evidence_digest=digest("files-verified"), now=NOW.isoformat()
    )
    terminal = store.finish(
        work, state="completed", evidence_digest=digest("terminal"), now=NOW.isoformat()
    )
    assert terminal.id == second.id and terminal.state == "completed"
    assert _claim_job(store, supported_operations=(COMPILE,), job_id=second.id) is None


def test_compile_job_exact_claim_concurrency(tmp_path: Path) -> None:
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    job, _ = store.enqueue(
        idempotency_key="compile",
        payload={"operation": COMPILE},
        available_at=NOW.isoformat(),
    )
    second = SQLiteLocalRuntimeStore(store.path)
    barrier = Barrier(2)

    def attempt(target: SQLiteLocalRuntimeStore) -> Any:
        barrier.wait(timeout=5)
        return _claim_job(target, supported_operations=(COMPILE,), job_id=job.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(attempt, (store, second)))
    assert sum(claim is not None for claim in claims) == 1
