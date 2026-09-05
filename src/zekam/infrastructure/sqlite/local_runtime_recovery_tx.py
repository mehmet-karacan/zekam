from __future__ import annotations

# ruff: noqa: UP014
import json
import sqlite3
from typing import NamedTuple

from zekam.application.local_continuity_v4_internal import (
    _key,
    _runtime_time,
    _text,
    _uuid,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed

_required = _text


PendingOutboxCount = NamedTuple("PendingOutboxCount", [("pending", int), ("maximum", int)])
EffectRecoveryCaseRows = NamedTuple(
    "EffectRecoveryCaseRows", [("case_id", str), ("inserted", bool), ("evidence_digest", str)]
)
RecoveryTransitionRows = NamedTuple(
    "RecoveryTransitionRows",
    [
        ("job_id", str),
        ("lease_id", str),
        ("deleted_locks", int),
        ("outbox_id", str),
        ("old_state", str),
        ("new_state", str),
    ],
)
EffectRecoveryResolutionRows = NamedTuple(
    "EffectRecoveryResolutionRows",
    [("resolution_id", str), ("case_id", str), ("outcome", str)],
)
RecoveryReconcileRows = NamedTuple(
    "RecoveryReconcileRows",
    [
        ("job_id", str),
        ("case_ids", tuple[str, ...]),
        ("outbox_id", str),
        ("old_state", str),
        ("new_state", str),
    ],
)


class LockRow(NamedTuple):
    resource: str
    job_id: str
    lease_id: str
    fencing_token: int
    acquired_at: str


class EffectRecoveryCaseSpec(NamedTuple):
    route: str
    case_id: str
    job_id: str
    claim_id: str
    receipt_evidence_digest: str | None
    effect_digest: str | None
    recovered_fence: int | None
    expected_case_evidence_digest: str
    created_at: str


class RecoveryTransitionSpec(NamedTuple):
    route: str
    job_id: str
    lease_id: str
    fencing_token: int
    expected_locks: tuple[LockRow, ...]
    ordered_case_evidence_digests: tuple[str, ...]
    expected_terminal_evidence_digest: str
    updated_at: str
    outbox_id: str
    max_pending_outbox: int
    expected_outbox_payload_digest: str


class EffectRecoveryResolutionSpec(NamedTuple):
    resolution_id: str
    case_id: str
    outcome: str
    evidence_digest: str
    created_at: str


class RecoveryReconcileSpec(NamedTuple):
    job_id: str
    expected_case_ids: tuple[str, ...]
    expected_terminal_state: str
    expected_terminal_evidence_digest: str
    updated_at: str
    outbox_id: str
    max_pending_outbox: int
    expected_outbox_payload_digest: str


class _PreparedOutbox(NamedTuple):
    outbox_id: str
    job_id: str
    key: str
    event_kind: str
    payload_json: str
    payload_digest: str
    created_at: str


def _digest(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} digest metin olmali")
    parse_digest(value)
    return value


def _bounded_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationFailed(f"{label} {minimum}..{maximum} olmali")
    return value


def _transaction(db: sqlite3.Connection) -> None:
    if type(db) is not sqlite3.Connection or not db.in_transaction:
        raise ValidationFailed("Recovery helper requires caller-owned active transaction")
    if db.row_factory is not sqlite3.Row:
        raise ValidationFailed("Recovery helper requires sqlite row factory")


def _canonical_payload(payload_json: str, payload_digest: str) -> dict[str, object]:
    if type(payload_json) is not str or len(payload_json.encode("utf-8")) > 32_768:
        raise ValidationFailed("Recovery outbox payload outside bound")
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationFailed("Recovery outbox payload malformed") from exc
    if type(payload) is not dict or canonical_json(payload) != payload_json:
        raise ValidationFailed("Recovery outbox payload noncanonical")
    _digest(payload_digest, "Recovery outbox payload")
    if digest(payload) != payload_digest:
        raise PolicyViolation("Recovery outbox payload digest drift")
    return payload


def require_outbox_capacity_tx(
    db: sqlite3.Connection, *, max_pending_outbox: int
) -> PendingOutboxCount:
    _transaction(db)
    _bounded_int(max_pending_outbox, "Max pending outbox", minimum=1, maximum=100_000)
    stored = db.execute(
        "select max_pending_outbox from local_runtime_config where singleton=1"
    ).fetchall()
    if len(stored) != 1 or type(stored[0][0]) is not int or stored[0][0] != max_pending_outbox:
        raise PolicyViolation("Persisted max pending outbox config drift")
    pending = int(
        db.execute(
            "select count(*) from local_outbox_delivery where state in "
            "('pending','claimed','recovery-required')"
        ).fetchone()[0]
    )
    if pending >= max_pending_outbox:
        raise PolicyViolation("Local outbox backpressure limit dolu")
    return PendingOutboxCount(pending, max_pending_outbox)


def insert_effect_recovery_case_tx(
    db: sqlite3.Connection,
    spec: EffectRecoveryCaseSpec,
) -> EffectRecoveryCaseRows:
    _transaction(db)
    if type(spec) is not EffectRecoveryCaseSpec or spec.route not in {
        "unknown-receipt",
        "finish-receiptless",
        "sweep-receiptless",
    }:
        raise ValidationFailed("Recovery exact case spec required")
    case_id, job_id, claim_id = spec.case_id, spec.job_id, spec.claim_id
    _uuid(case_id, "Recovery case")
    _uuid(job_id, "Recovery job")
    _uuid(claim_id, "Recovery claim")
    _runtime_time(spec.created_at, "Recovery created_at")
    _digest(spec.expected_case_evidence_digest, "Recovery case evidence")
    claim = db.execute(
        "select effect_digest,fencing_token from local_effect_claim where id=? and job_id=?",
        (claim_id, job_id),
    ).fetchone()
    if claim is None:
        raise ConcurrencyConflict("Effect recovery claim scope drift")
    if spec.route == "unknown-receipt":
        receipt = db.execute(
            "select status,evidence_digest,created_at from local_effect_receipt where claim_id=?",
            (claim_id,),
        ).fetchall()
        evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": claim_id,
                "receipt_evidence": _digest(spec.receipt_evidence_digest, "Receipt evidence"),
            }
        )
        if (
            len(receipt) != 1
            or tuple(receipt[0]) != ("unknown", spec.receipt_evidence_digest, spec.created_at)
            or spec.effect_digest is not None
            or spec.recovered_fence is not None
        ):
            raise ValidationFailed("Unknown receipt case spec drift")
    else:
        if db.execute(
            "select 1 from local_effect_receipt where claim_id=?", (claim_id,)
        ).fetchone():
            raise ConcurrencyConflict("Receiptless recovery found receipt")
        effect = _digest(spec.effect_digest, "Recovery effect")
        if effect != claim["effect_digest"]:
            raise PolicyViolation("Recovery effect digest drift")
        body: dict[str, object] = {
            "case_kind": "effect-unknown",
            "claim_id": claim_id,
            "effect_digest": effect,
        }
        if spec.route == "sweep-receiptless":
            body["recovered_fence"] = _bounded_int(
                spec.recovered_fence, "Recovered fence", minimum=1, maximum=2_147_483_647
            )
            if spec.recovered_fence != claim["fencing_token"]:
                raise PolicyViolation("Recovery claim fence drift")
        elif spec.recovered_fence is not None:
            raise ValidationFailed("Finish recovery fence must be absent")
        evidence = digest(body)
    if evidence != spec.expected_case_evidence_digest:
        raise PolicyViolation("Recovery case evidence drift")
    existing = db.execute(
        "select * from local_recovery_case where id=? or effect_claim_id=?",
        (case_id, claim_id),
    ).fetchall()
    expected = (
        case_id,
        job_id,
        claim_id,
        None,
        "effect-unknown",
        evidence,
        "open",
        spec.created_at,
        None,
    )
    if existing:
        row = existing[0]
        if len(existing) != 1 or tuple(row) != expected:
            raise ConcurrencyConflict("Effect recovery case collision")
        return EffectRecoveryCaseRows(case_id, False, evidence)
    cursor = db.execute(
        "insert into local_recovery_case(id,job_id,effect_claim_id,outbox_id,case_kind,"
        "evidence_digest,state,created_at,resolved_at) values(?,?,?,null,'effect-unknown',?,"
        "'open',?,null) on conflict(effect_claim_id) do nothing",
        (case_id, job_id, claim_id, evidence, spec.created_at),
    )
    rows = db.execute(
        "select * from local_recovery_case where effect_claim_id=?", (claim_id,)
    ).fetchall()
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ConcurrencyConflict("Effect recovery case cardinality drift")
    return EffectRecoveryCaseRows(case_id, cursor.rowcount == 1, evidence)


def _prepare_outbox(
    *,
    outbox_id: str,
    job_id: str,
    key: str,
    event_kind: str,
    payload_json: str,
    payload_digest: str,
    created_at: str,
) -> _PreparedOutbox:
    _uuid(outbox_id, "Recovery outbox")
    _uuid(job_id, "Recovery outbox job")
    _key(key, "Recovery outbox key")
    _key(event_kind, "Recovery outbox event")
    _runtime_time(created_at, "Recovery outbox created_at")
    payload = _canonical_payload(payload_json, payload_digest)
    if payload.get("job_id") != job_id:
        raise PolicyViolation("Recovery outbox job payload drift")
    return _PreparedOutbox(
        outbox_id, job_id, key, event_kind, payload_json, payload_digest, created_at
    )


def _require_fresh_outbox(db: sqlite3.Connection, value: _PreparedOutbox) -> None:
    rows = db.execute(
        "select id,idempotency_key,event_kind,payload_json,payload_digest,created_at "
        "from local_outbox where id=? or idempotency_key=? order by id",
        (value.outbox_id, value.key),
    ).fetchall()
    if rows:
        raise ConcurrencyConflict("Recovery outbox identity/key collision")


def _insert_outbox(db: sqlite3.Connection, value: _PreparedOutbox) -> None:
    db.execute(
        "insert into local_outbox(id,job_id,idempotency_key,event_kind,payload_json,"
        "payload_digest,created_at) values(?,?,?,?,?,?,?)",
        value,
    )
    db.execute(
        "insert into local_outbox_delivery(outbox_id,state,claim_id,fencing_counter,updated_at)"
        " values(?,'pending',null,0,?)",
        (value.outbox_id, value.created_at),
    )


def transition_running_job_to_recovery_tx(
    db: sqlite3.Connection,
    spec: RecoveryTransitionSpec,
) -> RecoveryTransitionRows:
    _transaction(db)
    if type(spec) is not RecoveryTransitionSpec or spec.route not in {
        "finish-recovery-required",
        "sweep-recovery-required",
    }:
        raise ValidationFailed("Recovery exact transition spec required")
    job_id, lease_id, fencing_token = spec.job_id, spec.lease_id, spec.fencing_token
    _bounded_int(fencing_token, "Recovery fencing token", minimum=1, maximum=2_147_483_647)
    _uuid(job_id, "Recovery job")
    _uuid(lease_id, "Recovery lease")
    _runtime_time(spec.updated_at, "Recovery updated_at")
    _uuid(spec.outbox_id, "Recovery outbox")
    if type(spec.expected_locks) is not tuple or len(spec.expected_locks) > 64:
        raise ValidationFailed("Recovery exact bounded lock tuple required")
    resources: list[str] = []
    for item in spec.expected_locks:
        if type(item) is not LockRow:
            raise ValidationFailed("Recovery exact lock row required")
        resource, lock_job, lock_lease, lock_fence, acquired_at = item
        resources.append(_key(resource, "Recovery lock resource"))
        if (
            _uuid(lock_job, "Recovery lock job") != job_id
            or _uuid(lock_lease, "Recovery lock lease") != lease_id
            or _bounded_int(lock_fence, "Recovery lock fence", minimum=1, maximum=2_147_483_647)
            != fencing_token
        ):
            raise ValidationFailed("Recovery lock scope drift")
        _runtime_time(acquired_at, "Recovery lock acquired_at")
    if resources != sorted(resources) or len(resources) != len(set(resources)):
        raise ValidationFailed("Recovery lock tuple noncanonical")
    if type(spec.ordered_case_evidence_digests) is not tuple:
        raise ValidationFailed("Recovery exact case evidence tuple required")
    cases = tuple(
        _digest(value, "Recovery case evidence") for value in spec.ordered_case_evidence_digests
    )
    terminal_evidence_digest = digest(list(cases))
    if terminal_evidence_digest != _digest(
        spec.expected_terminal_evidence_digest, "Recovery terminal evidence"
    ):
        raise PolicyViolation("Recovery terminal evidence drift")
    expected_payload: dict[str, object] = {
        "job_id": job_id,
        "state": "recovery-required",
    }
    key = (
        f"job:{job_id}:terminal"
        if spec.route == "finish-recovery-required"
        else f"job:{job_id}:recovery:{fencing_token}:recovery-required"
    )
    if spec.route == "sweep-recovery-required":
        expected_payload["fencing_token"] = fencing_token
    payload_json = canonical_json(expected_payload)
    payload_digest = digest(expected_payload)
    if payload_digest != _digest(spec.expected_outbox_payload_digest, "Recovery outbox payload"):
        raise PolicyViolation("Recovery outbox payload drift")
    outbox = _prepare_outbox(
        outbox_id=spec.outbox_id,
        job_id=job_id,
        key=key,
        event_kind="job.recovery-required",
        payload_json=payload_json,
        payload_digest=payload_digest,
        created_at=spec.updated_at,
    )
    actual_cases_list: list[str] = []
    case_rows = db.execute(
        "select rc.evidence_digest,c.id,c.effect_digest,r.status,r.evidence_digest,"
        "r.created_at,rc.created_at from local_recovery_case rc join local_effect_claim c "
        "on c.id=rc.effect_claim_id left join local_effect_receipt r on r.claim_id=c.id "
        "where rc.job_id=? and rc.state='open' order by case when ?="
        "'sweep-recovery-required' then c.id else rc.id end",
        (job_id, spec.route),
    ).fetchall()
    for row in case_rows:
        value = str(row[0])
        if row[3] == "unknown":
            stored = digest(
                {"case_kind": "effect-unknown", "claim_id": row[1], "receipt_evidence": row[4]}
            )
            if value != stored or row[5] != row[6]:
                raise ConcurrencyConflict("Unknown receipt recovery case drift")
            if spec.route == "sweep-recovery-required":
                value = digest(
                    {
                        "case_kind": "effect-unknown",
                        "claim_id": row[1],
                        "effect_digest": row[2],
                        "recovered_fence": fencing_token,
                    }
                )
        elif row[3] is not None:
            raise ConcurrencyConflict("Recovery case receipt state drift")
        actual_cases_list.append(value)
    actual_cases = tuple(actual_cases_list)
    if actual_cases != cases:
        raise ConcurrencyConflict("Recovery case evidence tuple drift")
    require_outbox_capacity_tx(db, max_pending_outbox=spec.max_pending_outbox)
    actual = tuple(
        tuple(row)
        for row in db.execute(
            "select resource,job_id,lease_id,fencing_token,acquired_at from local_resource_lock "
            "where job_id=? order by resource",
            (job_id,),
        ).fetchall()
    )
    if actual != tuple(tuple(item) for item in spec.expected_locks):
        raise ConcurrencyConflict("Recovery resource lock tuple drift")
    lease = db.execute("select * from local_lease where id=?", (lease_id,)).fetchone()
    job = db.execute("select * from local_job where id=?", (job_id,)).fetchone()
    if (
        lease is None
        or job is None
        or job["state"] != "running"
        or lease["job_id"] != job_id
        or lease["fencing_token"] != fencing_token
        or job["fencing_counter"] != fencing_token
    ):
        raise ConcurrencyConflict("Recovery running job/lease fence drift")
    _require_fresh_outbox(db, outbox)
    removed = db.execute(
        "delete from local_resource_lock where lease_id=? and job_id=?", (lease_id, job_id)
    )
    if removed.rowcount != len(spec.expected_locks):
        raise ConcurrencyConflict("Recovery resource lock delete drift")
    if (
        db.execute(
            "delete from local_lease where id=? and job_id=? and fencing_token=?",
            (lease_id, job_id, fencing_token),
        ).rowcount
        != 1
    ):
        raise ConcurrencyConflict("Recovery lease delete drift")
    if (
        db.execute(
            "update local_job set state='recovery-required',"
            "terminal_evidence_digest=?,updated_at=? "
            "where id=? and state='running' and fencing_counter=?",
            (terminal_evidence_digest, spec.updated_at, job_id, fencing_token),
        ).rowcount
        != 1
    ):
        raise ConcurrencyConflict("Recovery running job transition drift")
    _insert_outbox(db, outbox)
    return RecoveryTransitionRows(
        job_id, lease_id, removed.rowcount, spec.outbox_id, "running", "recovery-required"
    )


def insert_effect_recovery_resolution_tx(
    db: sqlite3.Connection,
    spec: EffectRecoveryResolutionSpec,
) -> EffectRecoveryResolutionRows:
    _transaction(db)
    if type(spec) is not EffectRecoveryResolutionSpec:
        raise ValidationFailed("Recovery exact resolution spec required")
    resolution_id, case_id, outcome = spec.resolution_id, spec.case_id, spec.outcome
    if type(outcome) is not str or outcome not in {"completed", "failed"}:
        raise ValidationFailed("Recovery resolution outcome invalid")
    _uuid(resolution_id, "Recovery resolution")
    _uuid(case_id, "Recovery case")
    _runtime_time(spec.created_at, "Recovery resolution created_at")
    _digest(spec.evidence_digest, "Recovery resolution evidence")
    case = db.execute("select * from local_recovery_case where id=?", (case_id,)).fetchone()
    if case is None or case["case_kind"] != "effect-unknown" or case["state"] != "open":
        raise ConcurrencyConflict("Recovery resolution exact open effect case required")
    if db.execute(
        "select 1 from local_recovery_resolution where id=? or recovery_case_id=?",
        (resolution_id, case_id),
    ).fetchone():
        raise ConcurrencyConflict("Recovery resolution collision")
    db.execute(
        "insert into local_recovery_resolution values(?,?,?,?,?)",
        (resolution_id, case_id, outcome, spec.evidence_digest, spec.created_at),
    )
    if (
        db.execute(
            "update local_recovery_case set state='resolved',resolved_at=? "
            "where id=? and state='open'",
            (spec.created_at, case_id),
        ).rowcount
        != 1
    ):
        raise ConcurrencyConflict("Recovery case resolution update drift")
    return EffectRecoveryResolutionRows(resolution_id, case_id, outcome)


def reconcile_effect_recovery_job_tx(
    db: sqlite3.Connection,
    spec: RecoveryReconcileSpec,
) -> RecoveryReconcileRows:
    _transaction(db)
    if type(spec) is not RecoveryReconcileSpec:
        raise ValidationFailed("Recovery exact reconcile spec required")
    job_id, expected_case_ids = spec.job_id, spec.expected_case_ids
    terminal_state = spec.expected_terminal_state
    if type(terminal_state) is not str or terminal_state not in {"completed", "failed"}:
        raise ValidationFailed("Recovery reconcile terminal state invalid")
    if type(expected_case_ids) is not tuple or not expected_case_ids:
        raise ValidationFailed("Recovery reconcile exact case tuple required")
    _uuid(job_id, "Recovery reconcile job")
    _runtime_time(spec.updated_at, "Recovery reconcile updated_at")
    _uuid(spec.outbox_id, "Recovery reconcile outbox")
    if tuple(_uuid(item, "Recovery reconcile case") for item in expected_case_ids) != tuple(
        sorted(set(expected_case_ids))
    ):
        raise ValidationFailed("Recovery reconcile case tuple noncanonical")
    _digest(spec.expected_terminal_evidence_digest, "Recovery reconcile terminal evidence")
    expected_payload = {"job_id": job_id, "state": terminal_state, "reconciled": True}
    payload_json, payload_digest = canonical_json(expected_payload), digest(expected_payload)
    if payload_digest != _digest(spec.expected_outbox_payload_digest, "Recovery outbox payload"):
        raise PolicyViolation("Recovery reconcile payload drift")
    outbox = _prepare_outbox(
        outbox_id=spec.outbox_id,
        job_id=job_id,
        key=f"job:{job_id}:reconciled",
        event_kind=f"job.{terminal_state}",
        payload_json=payload_json,
        payload_digest=payload_digest,
        created_at=spec.updated_at,
    )
    require_outbox_capacity_tx(db, max_pending_outbox=spec.max_pending_outbox)
    rows = db.execute(
        "select c.id,r.outcome,r.evidence_digest from local_recovery_case c "
        "join local_recovery_resolution r on r.recovery_case_id=c.id "
        "where c.job_id=? and c.state='resolved' order by c.id",
        (job_id,),
    ).fetchall()
    if tuple(str(row["id"]) for row in rows) != expected_case_ids:
        raise ConcurrencyConflict("Recovery reconcile case tuple drift")
    receipts = db.execute(
        "select er.status,er.evidence_digest,rr.outcome,rr.evidence_digest as resolution_evidence "
        "from local_effect_claim c left join local_effect_receipt er on er.claim_id=c.id "
        "left join local_recovery_case rc on rc.effect_claim_id=c.id "
        "left join local_recovery_resolution rr on rr.recovery_case_id=rc.id "
        "where c.job_id=? order by c.id",
        (job_id,),
    ).fetchall()
    computed = digest([(row[0], row[1], row[2], row[3]) for row in receipts])
    if computed != spec.expected_terminal_evidence_digest:
        raise PolicyViolation("Recovery reconcile terminal evidence drift")
    job = db.execute("select state from local_job where id=?", (job_id,)).fetchall()
    if len(job) != 1 or job[0][0] != "recovery-required":
        raise ConcurrencyConflict("Recovery reconcile job state drift")
    _require_fresh_outbox(db, outbox)
    if (
        db.execute(
            "update local_job set state=?,terminal_evidence_digest=?,updated_at=? "
            "where id=? and state='recovery-required'",
            (terminal_state, spec.expected_terminal_evidence_digest, spec.updated_at, job_id),
        ).rowcount
        != 1
    ):
        raise ConcurrencyConflict("Recovery reconcile job transition drift")
    _insert_outbox(db, outbox)
    return RecoveryReconcileRows(
        job_id, expected_case_ids, spec.outbox_id, "recovery-required", terminal_state
    )
