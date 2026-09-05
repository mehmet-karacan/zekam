from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, cast

import pytest

from zekam.application.local_continuity import ContinuityBinding
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import local_continuity_v4_internal as internal
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter

UUID1 = "00000000-0000-4000-8000-000000000001"
UUID2 = "00000000-0000-4000-8000-000000000002"
DIGEST = "sha256:" + "a" * 64
NOW = "2026-09-04T12:00:00+00:00"
TRUSTED_NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        UUID1,
        "external-session",
        UUID2,
        "00000000-0000-4000-8000-000000000003",
        "codex",
        "device",
        "00000000-0000-4000-8000-000000000004",
        DIGEST,
        DIGEST,
        DIGEST,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _Db:
    def __init__(self, *results: list[Any]) -> None:
        self.results = list(results)

    def execute(self, _sql: str, _parameters: object = ()) -> _Result:
        return _Result(self.results.pop(0))


def _job(**changes: Any) -> dict[str, Any]:
    payload = {
        "session_id": UUID1,
        "binding_digest": _binding().binding_digest,
        "run_id": None,
        "operation": "safe-operation",
    }
    row: dict[str, Any] = {
        "id": UUID2,
        "idempotency_key": "job-key",
        "payload_json": canonical_json(payload),
        "state": "running",
        "created_at": NOW,
        "updated_at": NOW,
        "terminal_evidence_digest": DIGEST,
    }
    row.update(changes)
    return row


def test_job_payload_rejects_malformed_noncanonical_and_scope_drift() -> None:
    binding = _binding()
    for payload in (None, "{", "[]", '{"session_id":"x", "operation":"op"}'):
        with pytest.raises(PolicyViolation):
            internal._job_payload(
                cast(sqlite3.Connection, object()),
                cast(sqlite3.Row, {"payload_json": payload}),
                binding,
            )
    wrong = {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "run_id": binding.run_id,
        "operation": 1,
    }
    with pytest.raises(PolicyViolation, match="scope drift"):
        internal._job_payload(
            cast(sqlite3.Connection, object()),
            cast(sqlite3.Row, {"payload_json": canonical_json(wrong)}),
            binding,
        )
    assert (
        internal._job_payload(
            cast(sqlite3.Connection, object()), cast(sqlite3.Row, _job()), binding
        )["operation"]
        == "safe-operation"
    )


def test_enqueue_outbox_rejects_missing_and_parity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_delivery_state",
        lambda *_args, **_kwargs: None,
    )
    job = _job()
    with pytest.raises(PolicyViolation, match="missing"):
        internal._verify_enqueue_outbox(
            cast(sqlite3.Connection, _Db([])), cast(sqlite3.Row, job), trusted_now=TRUSTED_NOW
        )
    payload = {"job_id": job["id"], "idempotency_key": job["idempotency_key"]}
    valid = {
        "event_kind": "job.enqueued",
        "payload_json": canonical_json(payload),
        "payload_digest": digest(payload),
        "created_at": NOW,
    }
    for changed in ({"event_kind": "wrong"}, {"created_at": "2026-09-04T11:59:59+00:00"}):
        with pytest.raises(PolicyViolation, match="parity drift"):
            internal._verify_enqueue_outbox(
                cast(sqlite3.Connection, _Db([{**valid, **changed}])),
                cast(sqlite3.Row, job),
                trusted_now=TRUSTED_NOW,
            )
    internal._verify_enqueue_outbox(
        cast(sqlite3.Connection, _Db([valid])),
        cast(sqlite3.Row, job),
        trusted_now=TRUSTED_NOW,
    )


def test_terminal_outbox_rejects_illegal_state_receipt_and_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_delivery_state",
        lambda *_args, **_kwargs: None,
    )
    claim = {"fencing_token": 1}
    with pytest.raises(PolicyViolation, match="running job"):
        internal._terminal_outbox(
            cast(sqlite3.Connection, _Db([{}])),
            cast(sqlite3.Row, _job()),
            cast(sqlite3.Row, claim),
            None,
            trusted_now=TRUSTED_NOW,
        )
    with pytest.raises(PolicyViolation, match="missing direct"):
        internal._terminal_outbox(
            cast(sqlite3.Connection, _Db([])),
            cast(sqlite3.Row, _job(state="completed")),
            cast(sqlite3.Row, claim),
            None,
            trusted_now=TRUSTED_NOW,
        )
    receipt = {"status": "completed", "evidence_digest": DIGEST, "created_at": NOW}
    with pytest.raises(PolicyViolation, match="alternative drift"):
        internal._terminal_outbox(
            cast(sqlite3.Connection, _Db([])),
            cast(sqlite3.Row, _job(state="quarantined")),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, receipt),
            trusted_now=TRUSTED_NOW,
        )
    with pytest.raises(PolicyViolation, match="status drift"):
        internal._terminal_outbox(
            cast(sqlite3.Connection, _Db([{"idempotency_key": "x"}])),
            cast(sqlite3.Row, _job(state="failed")),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, receipt),
            trusted_now=TRUSTED_NOW,
        )
    with pytest.raises(PolicyViolation, match="key unsupported"):
        internal._terminal_outbox(
            cast(sqlite3.Connection, _Db([{"idempotency_key": "x"}])),
            cast(sqlite3.Row, _job(state="completed")),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, receipt),
            trusted_now=TRUSTED_NOW,
        )


def test_b2_outbox_rejects_missing_and_every_parity_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_delivery_state",
        lambda *_args, **_kwargs: None,
    )
    payload = {"safe": True}
    valid = {
        "id": UUID1,
        "event_kind": "job.recovered",
        "payload_json": canonical_json(payload),
        "payload_digest": digest(payload),
        "created_at": NOW,
    }
    with pytest.raises(PolicyViolation, match="missing"):
        internal._verify_b2_outbox(
            cast(sqlite3.Connection, _Db([])),
            job_id=UUID2,
            key="key",
            expected_id=UUID1,
            kind="job.recovered",
            payload=payload,
            created_at=NOW,
            trusted_now=TRUSTED_NOW,
        )
    for changed in (
        {"id": UUID2},
        {"event_kind": "wrong"},
        {"payload_json": "{}"},
        {"payload_digest": DIGEST},
        {"created_at": "2026-09-04T12:00:01+00:00"},
    ):
        with pytest.raises(PolicyViolation, match="parity drift"):
            internal._verify_b2_outbox(
                cast(sqlite3.Connection, _Db([{**valid, **changed}])),
                job_id=UUID2,
                key="key",
                expected_id=UUID1,
                kind="job.recovered",
                payload=payload,
                created_at=NOW,
                trusted_now=TRUSTED_NOW,
            )
    internal._verify_b2_outbox(
        cast(sqlite3.Connection, _Db([valid])),
        job_id=UUID2,
        key="key",
        expected_id=UUID1,
        kind="job.recovered",
        payload=payload,
        created_at=NOW,
        trusted_now=TRUSTED_NOW,
    )


def test_running_lease_rejects_fence_time_bound_and_noncanonical_locks() -> None:
    job = {
        "id": UUID1,
        "fencing_counter": 1,
        "attempt_count": 1,
        "updated_at": NOW,
    }
    claim = {"lease_id": UUID2, "fencing_token": 1, "claimed_at": NOW}
    lease = {
        "id": UUID2,
        "job_id": UUID1,
        "fencing_token": 1,
        "owner_id": "owner",
        "owner_token": "token",
        "owner_pid": 10,
        "heartbeat_at": "2026-09-04T12:00:01+00:00",
        "expires_at": "2026-09-04T12:00:02+00:00",
    }
    internal._verify_running_lease(
        cast(sqlite3.Row, job), cast(sqlite3.Row, claim), cast(sqlite3.Row, lease), []
    )
    for changed in (
        {"id": UUID1},
        {"owner_pid": True},
        {"heartbeat_at": "2026-09-04T12:00:02+00:00"},
    ):
        with pytest.raises(PolicyViolation, match="lease/fence/time drift"):
            internal._verify_running_lease(
                cast(sqlite3.Row, job),
                cast(sqlite3.Row, claim),
                cast(sqlite3.Row, {**lease, **changed}),
                [],
            )
    with pytest.raises(PolicyViolation, match="bound exceeded"):
        internal._verify_running_lease(
            cast(sqlite3.Row, job),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, lease),
            cast(list[sqlite3.Row], [{}] * 65),
        )
    lock = {
        "resource": "z-resource",
        "lease_id": UUID2,
        "job_id": UUID1,
        "fencing_token": 1,
        "acquired_at": NOW,
    }
    with pytest.raises(PolicyViolation, match="noncanonical"):
        internal._verify_running_lease(
            cast(sqlite3.Row, job),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, lease),
            cast(list[sqlite3.Row], [lock, {**lock, "resource": "a-resource"}]),
        )
    with pytest.raises(PolicyViolation, match="scope/time drift"):
        internal._verify_running_lease(
            cast(sqlite3.Row, job),
            cast(sqlite3.Row, claim),
            cast(sqlite3.Row, lease),
            cast(list[sqlite3.Row], [{**lock, "job_id": UUID2}]),
        )


def test_carried_claim_cardinality_and_verifier_type_gate() -> None:
    binding = _binding()
    with pytest.raises(PolicyViolation, match="cardinality drift"):
        internal._carried_b2_claim(
            cast(sqlite3.Connection, _Db([{"effect_claim_id": UUID1}, {"effect_claim_id": UUID2}])),
            binding,
        )
    assert internal._carried_b2_claim(cast(sqlite3.Connection, _Db([])), binding) is None
    assert internal._carried_b2_claim(cast(sqlite3.Connection, _Db([(UUID1,)])), binding) == UUID1
    db = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValidationFailed, match="exact binding"):
            internal.verify_b1_b2_internal_producers(db, object())  # type: ignore[arg-type]
    finally:
        db.close()
