from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_runtime_recovery_tx import (
    EffectRecoveryCaseSpec,
    EffectRecoveryResolutionSpec,
    LockRow,
    RecoveryReconcileSpec,
    RecoveryTransitionSpec,
    _canonical_payload,
    _required,
    insert_effect_recovery_case_tx,
    insert_effect_recovery_resolution_tx,
    reconcile_effect_recovery_job_tx,
    require_outbox_capacity_tx,
    transition_running_job_to_recovery_tx,
)

NOW = "2026-09-03T12:00:00+00:00"
JOB = "018f0000-0000-7000-8000-000000000801"
LEASE = "018f0000-0000-7000-8000-000000000802"
CLAIM = "018f0000-0000-7000-8000-000000000803"
CASE = "018f0000-0000-7000-8000-000000000804"
RESOLUTION = "018f0000-0000-7000-8000-000000000805"
OUTBOX = "018f0000-0000-7000-8000-000000000806"
OTHER_OUTBOX = "018f0000-0000-7000-8000-000000000807"
OTHER_CASE = "018f0000-0000-7000-8000-000000000808"


def _db(tmp_path: Path, *, capacity: int = 8) -> sqlite3.Connection:
    path = tmp_path / "operational.db"
    operational_schema.bootstrap_v4(path)
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys=on")
    db.execute("insert into local_runtime_config values(1,?)", (capacity,))
    db.execute(
        "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,max_attempts,"
        "fencing_counter,terminal_evidence_digest,available_at,timeout_at,created_at,updated_at) "
        "values(?,?,?,'running',1,1,1,null,?,null,?,?)",
        (JOB, "job", canonical_json({"operation": "test"}), NOW, NOW, NOW),
    )
    db.execute(
        "insert into local_lease values(?,?,?,?,?,?,?,?)",
        (LEASE, JOB, "owner", 1, "token", 1, NOW, "2026-09-03T12:01:00+00:00"),
    )
    db.execute(
        "insert into local_resource_lock values(?,?,?,?,?)",
        ("resource/a", JOB, LEASE, 1, NOW),
    )
    db.execute(
        "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
        (CLAIM, JOB, LEASE, 1, "test", digest("effect"), "effect", NOW),
    )
    return db


def _enter(db: sqlite3.Connection) -> str:
    case_evidence = digest(
        {
            "case_kind": "effect-unknown",
            "claim_id": CLAIM,
            "effect_digest": digest("effect"),
            "recovered_fence": 1,
        }
    )
    rows = insert_effect_recovery_case_tx(
        db,
        EffectRecoveryCaseSpec(
            "sweep-receiptless",
            CASE,
            JOB,
            CLAIM,
            None,
            digest("effect"),
            1,
            case_evidence,
            NOW,
        ),
    )
    assert rows.inserted
    payload = {"job_id": JOB, "state": "recovery-required", "fencing_token": 1}
    transition_running_job_to_recovery_tx(
        db,
        RecoveryTransitionSpec(
            "sweep-recovery-required",
            JOB,
            LEASE,
            1,
            (LockRow("resource/a", JOB, LEASE, 1, NOW),),
            (case_evidence,),
            digest([case_evidence]),
            NOW,
            OUTBOX,
            8,
            digest(payload),
        ),
    )
    return case_evidence


def _dump(db: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(db.iterdump())


def _case_and_transition_spec(db: sqlite3.Connection) -> RecoveryTransitionSpec:
    case_evidence = digest(
        {
            "case_kind": "effect-unknown",
            "claim_id": CLAIM,
            "effect_digest": digest("effect"),
            "recovered_fence": 1,
        }
    )
    insert_effect_recovery_case_tx(
        db,
        EffectRecoveryCaseSpec(
            "sweep-receiptless",
            CASE,
            JOB,
            CLAIM,
            None,
            digest("effect"),
            1,
            case_evidence,
            NOW,
        ),
    )
    payload = {"job_id": JOB, "state": "recovery-required", "fencing_token": 1}
    return RecoveryTransitionSpec(
        "sweep-recovery-required",
        JOB,
        LEASE,
        1,
        (LockRow("resource/a", JOB, LEASE, 1, NOW),),
        (case_evidence,),
        digest([case_evidence]),
        NOW,
        OUTBOX,
        8,
        digest(payload),
    )


def test_helpers_require_exact_caller_owned_transaction(tmp_path: Path) -> None:
    db = _db(tmp_path, capacity=1)
    try:
        with pytest.raises(ValidationFailed, match="active transaction"):
            require_outbox_capacity_tx(db, max_pending_outbox=1)
        db.execute("begin immediate")
        assert require_outbox_capacity_tx(db, max_pending_outbox=1).pending == 0
        with pytest.raises(PolicyViolation, match="config drift"):
            require_outbox_capacity_tx(db, max_pending_outbox=7)
        db.rollback()
    finally:
        db.close()


def test_entry_helpers_are_atomic_and_return_exact_row_evidence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        require_outbox_capacity_tx(db, max_pending_outbox=8)
        evidence = _enter(db)
        assert (
            db.execute("select state from local_job where id=?", (JOB,)).fetchone()[0]
            == "recovery-required"
        )
        assert db.execute("select count(*) from local_lease").fetchone()[0] == 0
        assert db.execute("select count(*) from local_resource_lock").fetchone()[0] == 0
        assert db.execute("select terminal_evidence_digest from local_job").fetchone()[0] == digest(
            [evidence]
        )
        db.rollback()
        assert (
            db.execute("select state from local_job where id=?", (JOB,)).fetchone()[0] == "running"
        )
    finally:
        db.close()


@pytest.mark.parametrize(
    "mutation",
    (
        (("resource/z", JOB, LEASE, 1, NOW),),
        (("resource/a", JOB, LEASE, 2, NOW),),
        (),
    ),
)
def test_transition_rejects_lock_tuple_drift_without_partial_mutation(
    tmp_path: Path, mutation: tuple[tuple[str, str, str, int, str], ...]
) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        case_evidence = digest(
            {"case_kind": "effect-unknown", "claim_id": CLAIM, "effect_digest": digest("effect")}
        )
        insert_effect_recovery_case_tx(
            db,
            EffectRecoveryCaseSpec(
                "finish-receiptless",
                CASE,
                JOB,
                CLAIM,
                None,
                digest("effect"),
                None,
                case_evidence,
                NOW,
            ),
        )
        payload = {"job_id": JOB, "state": "recovery-required"}
        with pytest.raises((ConcurrencyConflict, ValidationFailed), match=r"lock (tuple|scope)"):
            transition_running_job_to_recovery_tx(
                db,
                RecoveryTransitionSpec(
                    "finish-recovery-required",
                    JOB,
                    LEASE,
                    1,
                    tuple(LockRow(*row) for row in mutation),
                    (case_evidence,),
                    digest([case_evidence]),
                    NOW,
                    OUTBOX,
                    8,
                    digest(payload),
                ),
            )
        db.rollback()
        assert db.execute("select count(*) from local_outbox").fetchone()[0] == 0
    finally:
        db.close()


def test_resolution_and_reconcile_recompute_terminal_evidence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        _enter(db)
        insert_effect_recovery_resolution_tx(
            db,
            EffectRecoveryResolutionSpec(
                RESOLUTION,
                CASE,
                "completed",
                digest("resolution"),
                "2026-09-03T12:00:01+00:00",
            ),
        )
        terminal = digest([(None, None, "completed", digest("resolution"))])
        payload = {"job_id": JOB, "state": "completed", "reconciled": True}
        result = reconcile_effect_recovery_job_tx(
            db,
            RecoveryReconcileSpec(
                JOB,
                (CASE,),
                "completed",
                terminal,
                "2026-09-03T12:00:01+00:00",
                "018f0000-0000-7000-8000-000000000807",
                8,
                digest(payload),
            ),
        )
        assert result.new_state == "completed"
        db.rollback()
    finally:
        db.close()


def test_capacity_and_payload_digest_fail_before_partial_rows(tmp_path: Path) -> None:
    db = _db(tmp_path, capacity=1)
    try:
        db.execute("begin immediate")
        db.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (OUTBOX, JOB, "full", "job.enqueued", "{}", digest({}), NOW),
        )
        db.execute(
            "insert into local_outbox_delivery("
            "outbox_id,state,claim_id,fencing_counter,updated_at) "
            "values(?,'pending',null,0,?)",
            (OUTBOX, NOW),
        )
        with pytest.raises(PolicyViolation, match="backpressure"):
            require_outbox_capacity_tx(db, max_pending_outbox=1)
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("bad", [True, 0, 100_001, "8", None])
def test_capacity_rejects_wrong_types_and_bounds(tmp_path: Path, bad: Any) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        with pytest.raises(ValidationFailed):
            require_outbox_capacity_tx(db, max_pending_outbox=bad)
        db.rollback()
    finally:
        db.close()


def test_helper_text_uses_utf8_bytes_and_rejects_controls_and_surrogates() -> None:
    assert _required("😀" * 128, "boundary") == "😀" * 128
    for bad in ("😀" * 129, "ok\x00bad", "ok\x1fbad", "ok\x7fbad", "\ud800"):
        with pytest.raises(ValidationFailed):
            _required(bad, "boundary")


def test_payload_uses_exact_32kib_utf8_boundary() -> None:
    def payload(size: int) -> tuple[str, str]:
        base = canonical_json({"blob": "", "job_id": JOB})
        body = canonical_json({"blob": "x" * (size - len(base.encode("utf-8"))), "job_id": JOB})
        assert len(body.encode("utf-8")) == size
        return body, digest({"blob": "x" * (size - len(base)), "job_id": JOB})

    body, value = payload(32_768)
    assert _canonical_payload(body, value)["job_id"] == JOB
    with pytest.raises(ValidationFailed, match="outside bound"):
        _canonical_payload(*payload(32_769))


def test_helper_outcomes_require_exact_strings(tmp_path: Path) -> None:
    class StringSubclass(str):
        pass

    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        for outcome in (True, StringSubclass("completed")):
            with pytest.raises(ValidationFailed):
                insert_effect_recovery_resolution_tx(
                    db,
                    EffectRecoveryResolutionSpec(
                        RESOLUTION,
                        CASE,
                        cast(Any, outcome),
                        digest("resolution"),
                        NOW,
                    ),
                )
        for state in (True, StringSubclass("completed")):
            with pytest.raises(ValidationFailed):
                reconcile_effect_recovery_job_tx(
                    db,
                    RecoveryReconcileSpec(
                        JOB,
                        (CASE,),
                        cast(Any, state),
                        digest([]),
                        NOW,
                        OUTBOX,
                        8,
                        digest({"job_id": JOB}),
                    ),
                )
        db.rollback()
    finally:
        db.close()


def test_prevalidation_failure_can_be_caught_without_committing_partial_mutation(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        bad_case = EffectRecoveryCaseSpec(
            "sweep-receiptless",
            CASE,
            JOB,
            CLAIM,
            None,
            digest("effect"),
            1,
            digest("wrong"),
            NOW,
        )
        with pytest.raises(PolicyViolation, match="case evidence drift"):
            insert_effect_recovery_case_tx(db, bad_case)
        db.commit()
        assert db.execute("select count(*) from local_recovery_case").fetchone()[0] == 0

        db.execute("begin immediate")
        case_evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": CLAIM,
                "effect_digest": digest("effect"),
                "recovered_fence": 1,
            }
        )
        insert_effect_recovery_case_tx(
            db,
            EffectRecoveryCaseSpec(
                "sweep-receiptless",
                CASE,
                JOB,
                CLAIM,
                None,
                digest("effect"),
                1,
                case_evidence,
                NOW,
            ),
        )
        bad_transition = RecoveryTransitionSpec(
            "sweep-recovery-required",
            JOB,
            LEASE,
            1,
            (LockRow("resource/a", JOB, LEASE, 1, NOW),),
            (case_evidence,),
            digest([case_evidence]),
            NOW,
            OUTBOX,
            8,
            digest("wrong"),
        )
        with pytest.raises(PolicyViolation, match="payload drift"):
            transition_running_job_to_recovery_tx(db, bad_transition)
        db.commit()
        assert db.execute("select state from local_job").fetchone()[0] == "running"
        assert db.execute("select count(*) from local_outbox").fetchone()[0] == 0
        assert db.execute("select count(*) from local_lease").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "rows",
    (
        ((OUTBOX, "different-key"),),
        ((OTHER_OUTBOX, f"job:{JOB}:recovery:1:recovery-required"),),
        ((OUTBOX, f"job:{JOB}:recovery:1:recovery-required"),),
        (
            (OUTBOX, "different-key"),
            (OTHER_OUTBOX, f"job:{JOB}:recovery:1:recovery-required"),
        ),
    ),
)
def test_transition_outbox_identity_and_key_collisions_precede_every_mutation(
    tmp_path: Path, rows: tuple[tuple[str, str], ...]
) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        spec = _case_and_transition_spec(db)
        for outbox_id, key in rows:
            payload = canonical_json({"job_id": JOB})
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (outbox_id, JOB, key, "job.enqueued", payload, digest({"job_id": JOB}), NOW),
            )
            db.execute(
                "insert into local_outbox_delivery(outbox_id,state,claim_id,"
                "fencing_counter,updated_at) values(?,'pending',null,0,?)",
                (outbox_id, NOW),
            )
        before = _dump(db)
        try:
            transition_running_job_to_recovery_tx(db, spec)
        except Exception as caught:
            failure = caught
        else:
            pytest.fail("collision was accepted")
        db.commit()
        assert type(failure) is ConcurrencyConflict
        assert _dump(db) == before
    finally:
        db.close()


@pytest.mark.parametrize(
    "rows",
    (
        ((OTHER_OUTBOX, f"job:{JOB}:reconciled"),),
        (("018f0000-0000-7000-8000-000000000809", "different-key"),),
        (("018f0000-0000-7000-8000-000000000809", f"job:{JOB}:reconciled"),),
    ),
)
def test_reconcile_outbox_identity_and_key_collisions_precede_every_mutation(
    tmp_path: Path, rows: tuple[tuple[str, str], ...]
) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        _enter(db)
        insert_effect_recovery_resolution_tx(
            db,
            EffectRecoveryResolutionSpec(RESOLUTION, CASE, "completed", digest("resolution"), NOW),
        )
        for outbox_id, key in rows:
            payload = canonical_json({"job_id": JOB})
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (outbox_id, JOB, key, "job.enqueued", payload, digest({"job_id": JOB}), NOW),
            )
            db.execute(
                "insert into local_outbox_delivery(outbox_id,state,claim_id,"
                "fencing_counter,updated_at) values(?,'pending',null,0,?)",
                (outbox_id, NOW),
            )
        before = _dump(db)
        terminal = digest([(None, None, "completed", digest("resolution"))])
        spec = RecoveryReconcileSpec(
            JOB,
            (CASE,),
            "completed",
            terminal,
            NOW,
            "018f0000-0000-7000-8000-000000000809",
            8,
            digest({"job_id": JOB, "state": "completed", "reconciled": True}),
        )
        try:
            reconcile_effect_recovery_job_tx(db, spec)
        except Exception as caught:
            failure = caught
        else:
            pytest.fail("collision was accepted")
        db.commit()
        assert type(failure) is ConcurrencyConflict
        assert _dump(db) == before
    finally:
        db.close()


@pytest.mark.parametrize(
    ("stored_id", "stored_at"),
    ((OTHER_CASE, NOW), (CASE, "2026-09-03T12:00:01+00:00")),
)
def test_existing_effect_case_requires_exact_identity_and_creation_time(
    tmp_path: Path, stored_id: str, stored_at: str
) -> None:
    db = _db(tmp_path)
    try:
        db.execute("begin immediate")
        evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": CLAIM,
                "effect_digest": digest("effect"),
                "recovered_fence": 1,
            }
        )
        db.execute(
            "insert into local_recovery_case values(?,?,?,null,'effect-unknown',?,'open',?,null)",
            (stored_id, JOB, CLAIM, evidence, stored_at),
        )
        spec = EffectRecoveryCaseSpec(
            "sweep-receiptless",
            CASE,
            JOB,
            CLAIM,
            None,
            digest("effect"),
            1,
            evidence,
            NOW,
        )
        before = _dump(db)
        with pytest.raises(ConcurrencyConflict, match="case collision"):
            insert_effect_recovery_case_tx(db, spec)
        db.commit()
        assert _dump(db) == before
    finally:
        db.close()
