"""Explicit dormant SQLite implementation of WP-08 Slice B1."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityTail,
    logical,
    uuid_text,
)
from zekam.application.local_continuity_v4_internal import (
    EVENT_NS,
    DirectEffectOutcomeIssuer,
    DirectEffectOutcomeRequest,
    EffectClaimIssuer,
    EffectClaimRequest,
    FrozenDirectEffectOutcomeSnapshot,
    FrozenEffectClaimSnapshot,
    FrozenTurnCommitSnapshot,
    InternalProducerResult,
    TurnCommitIssuer,
    TurnCommitRequest,
)
from zekam.application.local_continuity_v4_recovery import B2_EVENT_NS
from zekam.application.local_continuity_v4_writer import (
    event_digest,
    internal_receipt_digest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)

_NATIVE_KINDS = {"SESSION_START", "PRE_COMPACTION", "POST_COMPACTION"}
_B1_KINDS = {
    "USER_TURN_COMMITTED",
    "ASSISTANT_TURN_COMMITTED",
    "TOOL_EFFECT_CLAIMED",
    "TOOL_EFFECT_COMPLETED",
}


def _runtime_time(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise PolicyViolation(f"B1 exact {label} timestamp required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation(f"B1 malformed {label} timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.astimezone(UTC).isoformat() != value
    ):
        raise PolicyViolation(f"B1 canonical UTC {label} timestamp required")
    return parsed


def _whole_second(value: object, label: str) -> datetime:
    parsed = _runtime_time(value, label)
    if type(value) is not str or len(value) != 25 or "." in value:
        raise PolicyViolation(f"B1 whole-second {label} timestamp required")
    return parsed


def _uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise PolicyViolation(f"B1 exact {label} UUID required")
    try:
        uuid_text(value, label)
        if str(UUID(value)) != value:
            raise ValidationFailed(f"{label} noncanonical")
    except ValidationFailed as exc:
        raise PolicyViolation(f"B1 canonical {label} UUID required") from exc
    return value


def _bounded_runtime_identity(value: object, label: str) -> str:
    if type(value) is not str:
        raise PolicyViolation(f"B1 exact {label} required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PolicyViolation(f"B1 valid UTF-8 {label} required") from exc
    if (
        not encoded
        or len(encoded) > 512
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise PolicyViolation(f"B1 bounded canonical {label} required")
    try:
        if logical(value, label) != value:
            raise PolicyViolation(f"B1 canonical {label} required")
    except ValidationFailed as exc:
        raise PolicyViolation(f"B1 canonical {label} required") from exc
    return value


def _row_body(row: sqlite3.Row, *, receipt_digest: bool = False) -> dict[str, Any]:
    try:
        body = json.loads(row["body_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise PolicyViolation("B1 durable producer JSON malformed") from exc
    if type(body) is not dict or canonical_json(body) != row["body_json"]:
        raise PolicyViolation("B1 durable producer body noncanonical")
    if receipt_digest and digest(body) != row["receipt_digest"]:
        raise PolicyViolation("B1 durable producer body digest drift")
    return cast(dict[str, Any], body)


def _event_id(value: str) -> str:
    return str(uuid5(EVENT_NS, f"event|{value}"))


def _claim_id(binding: ContinuityBinding, job_id: str) -> str:
    return str(uuid5(EVENT_NS, f"effect-claim|{binding.binding_digest}|{job_id}"))


def _receipt_id(claim_id: str) -> str:
    return str(uuid5(EVENT_NS, f"effect-receipt|{claim_id}"))


def _operation_for_turn(role: str, item_ref: str) -> str:
    return f"turn-commit:{role}:{item_ref.removeprefix('turn/')}"


def _claim_operation(claim_id: str) -> str:
    return f"effect-claim:{claim_id}"


def _terminal_operation(claim_id: str) -> str:
    return f"effect-terminal:{claim_id}"


def _producer_rows(
    db: sqlite3.Connection, binding: ContinuityBinding
) -> tuple[tuple[Any, ...], ...]:
    statements = (
        (
            "select 'turn',t.receipt_digest,t.body_json from continuity_turn_commit_receipt t "
            "where t.session_id=? order by t.receipt_digest",
            (binding.session_id,),
        ),
        (
            "select 'claim',c.id,c.job_id,c.effect_digest,c.claimed_at from local_effect_claim c "
            "join continuity_effect_binding b on b.claim_id=c.id "
            "where b.session_id=? order by c.id",
            (binding.session_id,),
        ),
        (
            "select 'receipt',r.id,r.claim_id,r.status,r.evidence_digest,r.created_at "
            "from local_effect_receipt r join continuity_effect_binding b on b.claim_id=r.claim_id "
            "where b.session_id=? order by r.id",
            (binding.session_id,),
        ),
        (
            "select 'internal',i.receipt_digest,i.event_digest,i.body_json "
            "from continuity_internal_event_receipt i where i.session_id=? "
            "and i.event_kind in ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED',"
            "'TOOL_EFFECT_CLAIMED','TOOL_EFFECT_COMPLETED') order by i.receipt_digest",
            (binding.session_id,),
        ),
        (
            "select 'event',e.id,e.event_kind,e.event_digest,e.created_at,d.sequence,"
            "d.previous_digest,d.idempotency_key,d.spool_digest,d.body_json "
            "from continuity_internal_event_receipt i "
            "join session_event_detail d on d.event_digest=i.event_digest "
            "and d.session_id=i.session_id join session_event e on e.id=d.event_id "
            "and e.session_id=d.session_id where i.session_id=? "
            "and i.event_kind in ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED',"
            "'TOOL_EFFECT_CLAIMED','TOOL_EFFECT_COMPLETED') order by d.sequence",
            (binding.session_id,),
        ),
    )
    result: list[tuple[Any, ...]] = []
    for statement, parameters in statements:
        result.extend(tuple(row) for row in db.execute(statement, parameters).fetchall())
    return tuple(result)


def _event_for_receipt(
    db: sqlite3.Connection, receipt: sqlite3.Row
) -> tuple[sqlite3.Row, dict[str, Any]]:
    detail = db.execute(
        "select d.*,e.event_kind,e.created_at as event_created_at from session_event_detail d "
        "join session_event e on e.id=d.event_id and e.session_id=d.session_id "
        "where d.event_digest=? and d.session_id=?",
        (receipt["event_digest"], receipt["session_id"]),
    ).fetchone()
    if detail is None or detail["event_id"] != _event_id(str(receipt["event_digest"])):
        raise PolicyViolation("B1 deterministic event identity drift")
    return cast(sqlite3.Row, detail), _row_body(detail)


def _verify_internal(
    receipt: sqlite3.Row,
    detail: sqlite3.Row,
    *,
    binding: ContinuityBinding,
    producer_kind: str,
    producer_ref: str,
) -> None:
    body = _row_body(receipt)
    expected = {
        "attachment_revision_digest": receipt["attachment_revision_digest"],
        "binding_digest": binding.binding_digest,
        "created_at": receipt["created_at"],
        "event_digest": detail["event_digest"],
        "event_kind": detail["event_kind"],
        "expected_previous_event_digest": detail["previous_digest"],
        "operation_key": detail["idempotency_key"],
        "session_id": binding.session_id,
    }
    populated = [
        name
        for name in (
            "turn_commit_digest",
            "effect_claim_id",
            "effect_receipt_id",
            "native_event_receipt_digest",
            "close_request_digest",
            "close_receipt_digest",
            "hook_recovery_resolution_id",
            "local_recovery_resolution_id",
        )
        if receipt[name] is not None
    ]
    if (
        body != expected
        or receipt["binding_digest"] != binding.binding_digest
        or receipt["session_id"] != binding.session_id
        or receipt["created_at"] != detail["event_created_at"]
        or populated != [producer_kind]
        or receipt[producer_kind] != producer_ref
        or receipt["receipt_digest"]
        != internal_receipt_digest(expected, producer_kind=producer_kind, producer_ref=producer_ref)
    ):
        raise PolicyViolation("B1 internal receipt parity drift")


def _expected_event(
    *,
    kind: str,
    operation_key: str,
    occurred_at: str,
    source_refs: list[str],
    evidence_digests: list[str],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "idempotency_key": operation_key,
        "occurred_at": occurred_at,
        "source_refs": source_refs,
        "evidence_digests": evidence_digests,
        "spool_digest": None,
    }


def _verify_event_body(
    detail: sqlite3.Row,
    envelope: dict[str, Any],
    *,
    binding: ContinuityBinding,
    expected: dict[str, Any],
) -> None:
    expected_envelope = {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "sequence": detail["sequence"],
        "previous_digest": detail["previous_digest"],
        "event": expected,
    }
    if (
        envelope != expected_envelope
        or digest(envelope) != detail["event_digest"]
        or detail["event_kind"] != expected["kind"]
        or detail["idempotency_key"] != expected["idempotency_key"]
        or detail["event_created_at"] != expected["occurred_at"]
        or detail["spool_digest"] is not None
    ):
        raise PolicyViolation("B1 event body/evidence parity drift")


def _verify_turns(db: sqlite3.Connection, binding: ContinuityBinding) -> None:
    rows = db.execute(
        "select i.*,d.sequence from continuity_internal_event_receipt i "
        "join session_event_detail d on d.event_digest=i.event_digest "
        "and d.session_id=i.session_id where i.session_id=? "
        "and i.event_kind in ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED') "
        "order by d.sequence",
        (binding.session_id,),
    ).fetchall()
    turn_count = int(
        db.execute(
            "select count(*) from continuity_turn_commit_receipt where session_id=?",
            (binding.session_id,),
        ).fetchone()[0]
    )
    if turn_count != len(rows):
        raise PolicyViolation("B1 turn producer/internal receipt cardinality drift")
    previous: str | None = None
    for receipt in rows:
        turn = db.execute(
            "select * from continuity_turn_commit_receipt where receipt_digest=? and session_id=?",
            (receipt["turn_commit_digest"], binding.session_id),
        ).fetchone()
        if turn is None:
            raise PolicyViolation("B1 turn producer missing")
        body = _row_body(turn)
        expected_body = {
            "binding_digest": binding.binding_digest,
            "content_digest": turn["content_digest"],
            "created_at": turn["created_at"],
            "item_ref": turn["item_ref"],
            "previous_turn_commit_digest": previous,
            "role": turn["role"],
            "session_id": binding.session_id,
            "store_generation_digest": turn["store_generation_digest"],
        }
        expected_digest = digest({"schema": "zekam-turn-commit-receipt/v1", "body": expected_body})
        kind = "USER_TURN_COMMITTED" if turn["role"] == "user" else "ASSISTANT_TURN_COMMITTED"
        detail, envelope = _event_for_receipt(db, receipt)
        expected_event = _expected_event(
            kind=kind,
            operation_key=_operation_for_turn(str(turn["role"]), str(turn["item_ref"])),
            occurred_at=str(turn["created_at"]),
            source_refs=[str(turn["item_ref"])],
            evidence_digests=[
                expected_digest,
                str(turn["content_digest"]),
                str(turn["store_generation_digest"]),
            ],
        )
        item_ref = turn["item_ref"]
        if (
            turn["role"] not in {"user", "assistant"}
            or type(item_ref) is not str
            or not item_ref.startswith("turn/")
        ):
            raise PolicyViolation("B1 canonical turn selector drift")
        _uuid(item_ref.removeprefix("turn/"), "turn item")
        if (
            body != expected_body
            or turn["receipt_digest"] != expected_digest
            or turn["binding_digest"] != binding.binding_digest
            or turn["session_id"] != binding.session_id
            or turn["previous_turn_commit_digest"] != previous
            or receipt["event_kind"] != kind
        ):
            raise PolicyViolation("B1 turn receipt predecessor/body drift")
        _whole_second(turn["created_at"], "turn receipt")
        _verify_internal(
            receipt,
            detail,
            binding=binding,
            producer_kind="turn_commit_digest",
            producer_ref=expected_digest,
        )
        _verify_event_body(detail, envelope, binding=binding, expected=expected_event)
        previous = expected_digest


def _job_payload(
    db: sqlite3.Connection, job: sqlite3.Row, binding: ContinuityBinding
) -> dict[str, Any]:
    try:
        payload = json.loads(job["payload_json"])
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("B1 selected job payload malformed") from exc
    if type(payload) is not dict or canonical_json(payload) != job["payload_json"]:
        raise PolicyViolation("B1 selected job payload noncanonical")
    if (
        payload.get("session_id") != binding.session_id
        or payload.get("binding_digest") != binding.binding_digest
        or payload.get("run_id") != binding.run_id
        or type(payload.get("operation")) is not str
    ):
        raise PolicyViolation("B1 selected job scope drift")
    return cast(dict[str, Any], payload)


def _ambiguous_other_jobs(
    db: sqlite3.Connection, binding: ContinuityBinding, selected_job_id: str
) -> int:
    return int(
        db.execute(
            "select count(*) from local_job j where j.id<>? "
            "and json_extract(j.payload_json,'$.session_id')=? and ("
            "j.state in ('ready','running','recovery-required','quarantined') or ("
            "j.state in ('completed','failed') and not exists ("
            "select 1 from local_effect_claim c "
            "join continuity_effect_binding b on b.claim_id=c.id and b.job_id=c.job_id "
            "join local_effect_receipt er on er.claim_id=c.id "
            "join continuity_internal_event_receipt ir on ir.effect_receipt_id=er.id "
            "where c.job_id=j.id and b.session_id=?) and not exists ("
            "select 1 from local_effect_claim c "
            "join continuity_effect_binding b on b.claim_id=c.id and b.job_id=c.job_id "
            "join local_recovery_case rc on rc.effect_claim_id=c.id and rc.job_id=c.job_id "
            "join local_recovery_resolution rr on rr.recovery_case_id=rc.id "
            "where c.job_id=j.id and b.session_id=? and rc.state='resolved' "
            "and rr.outcome=j.state)))",
            (selected_job_id, binding.session_id, binding.session_id, binding.session_id),
        ).fetchone()[0]
    )


def _verify_enqueue_outbox(
    db: sqlite3.Connection, job: sqlite3.Row, *, trusted_now: datetime
) -> None:
    rows = db.execute(
        "select * from local_outbox where job_id=? and idempotency_key=?",
        (job["id"], f"job:{job['id']}:enqueued"),
    ).fetchall()
    if len(rows) != 1:
        raise PolicyViolation("B1 selected job enqueue outbox missing")
    row = rows[0]
    payload = {"job_id": job["id"], "idempotency_key": job["idempotency_key"]}
    if (
        row["event_kind"] != "job.enqueued"
        or row["payload_json"] != canonical_json(payload)
        or row["payload_digest"] != digest(payload)
        or _runtime_time(row["created_at"], "enqueue outbox")
        < _runtime_time(job["created_at"], "job creation")
    ):
        raise PolicyViolation("B1 enqueue outbox parity drift")
    SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted_now)


def _terminal_outbox(
    db: sqlite3.Connection,
    job: sqlite3.Row,
    claim: sqlite3.Row,
    receipt: sqlite3.Row | None,
    *,
    trusted_now: datetime,
) -> None:
    rows = db.execute(
        "select * from local_outbox where job_id=? and idempotency_key<>? order by id",
        (job["id"], f"job:{job['id']}:enqueued"),
    ).fetchall()
    if job["state"] == "running":
        if rows:
            raise PolicyViolation("B1 running job has terminal outbox")
        return
    if receipt is None:
        raise PolicyViolation("B1 terminal job missing direct effect receipt")
    if job["state"] not in {"completed", "failed"} or len(rows) != 1:
        raise PolicyViolation("B1 terminal job/outbox alternative drift")
    row = rows[0]
    state = str(job["state"])
    if state != receipt["status"]:
        raise PolicyViolation("B1 terminal job/direct receipt status drift")
    ordinary_key = f"job:{job['id']}:terminal"
    recovery_key = f"job:{job['id']}:recovery:{claim['fencing_token']}:{state}"
    if row["idempotency_key"] == ordinary_key:
        payload = {"job_id": job["id"], "state": state}
        expected_evidence = receipt["evidence_digest"]
    elif row["idempotency_key"] == recovery_key:
        payload = {
            "job_id": job["id"],
            "state": state,
            "fencing_token": claim["fencing_token"],
        }
        expected_evidence = digest([[state, receipt["evidence_digest"]]])
    else:
        raise PolicyViolation("B1 terminal outbox key unsupported")
    if (
        row["event_kind"] != f"job.{state}"
        or row["payload_json"] != canonical_json(payload)
        or row["payload_digest"] != digest(payload)
        or job["terminal_evidence_digest"] != expected_evidence
        or job["updated_at"] != row["created_at"]
        or _runtime_time(job["updated_at"], "terminal job update")
        < _runtime_time(receipt["created_at"], "direct effect receipt")
    ):
        raise PolicyViolation("B1 terminal job progression drift")
    SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted_now)


def _verify_b2_outbox(
    db: sqlite3.Connection,
    *,
    job_id: str,
    key: str,
    expected_id: str,
    kind: str,
    payload: dict[str, Any],
    created_at: str,
    trusted_now: datetime,
) -> None:
    rows = db.execute(
        "select * from local_outbox where job_id=? and idempotency_key=?", (job_id, key)
    ).fetchall()
    if len(rows) != 1:
        raise PolicyViolation("B2 exact runtime outbox missing")
    row = rows[0]
    if (
        row["id"] != expected_id
        or row["event_kind"] != kind
        or row["payload_json"] != canonical_json(payload)
        or row["payload_digest"] != digest(payload)
        or row["created_at"] != created_at
    ):
        raise PolicyViolation("B2 runtime outbox parity drift")
    SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted_now)


def _verify_b2_crash_event(
    db: sqlite3.Connection,
    binding: ContinuityBinding,
    *,
    resolution: sqlite3.Row,
    recovery_revision: sqlite3.Row,
    restored: sqlite3.Row,
) -> None:
    rows = db.execute(
        "select * from continuity_internal_event_receipt where local_recovery_resolution_id=?",
        (resolution["id"],),
    ).fetchall()
    if len(rows) != 1:
        raise PolicyViolation("B2 CRASH_RECOVERED producer cardinality drift")
    receipt = rows[0]
    detail = db.execute(
        "select d.*,e.event_kind,e.created_at as event_created_at from session_event_detail d "
        "join session_event e on e.id=d.event_id and e.session_id=d.session_id "
        "where d.event_digest=? and d.session_id=?",
        (receipt["event_digest"], receipt["session_id"]),
    ).fetchone()
    if detail is None or detail["event_id"] != str(
        uuid5(B2_EVENT_NS, f"event|{receipt['event_digest']}")
    ):
        raise PolicyViolation("B2 deterministic CRASH event identity drift")
    operation = f"effect-crash-recovered:{resolution['recovery_case_id']}"
    expected = _expected_event(
        kind="CRASH_RECOVERED",
        operation_key=operation,
        occurred_at=str(resolution["created_at"]),
        source_refs=[],
        evidence_digests=[str(resolution["evidence_digest"])],
    )
    if (
        receipt["receipt_digest"] != restored["crash_recovered_receipt_digest"]
        or receipt["attachment_revision_digest"] != recovery_revision["revision_digest"]
    ):
        raise PolicyViolation("B2 CRASH recovery revision/receipt drift")
    _verify_internal(
        receipt,
        detail,
        binding=binding,
        producer_kind="local_recovery_resolution_id",
        producer_ref=str(resolution["id"]),
    )
    _verify_event_body(detail, _row_body(detail), binding=binding, expected=expected)


def _verify_effects(
    db: sqlite3.Connection,
    binding: ContinuityBinding,
    *,
    selected_b2_claim_id: str | None = None,
    trusted_now: datetime,
) -> None:
    claims = db.execute(
        "select c.*,b.binding_digest as continuity_binding_digest,b.session_id as bound_session,"
        "b.job_id as bound_job from local_effect_claim c join continuity_effect_binding b "
        "on b.claim_id=c.id where b.session_id=? order by c.id",
        (binding.session_id,),
    ).fetchall()
    scoped_claim_count = int(
        db.execute(
            "select count(*) from local_effect_claim c join local_job j on j.id=c.job_id "
            "where json_extract(j.payload_json,'$.session_id')=?",
            (binding.session_id,),
        ).fetchone()[0]
    )
    if scoped_claim_count != len(claims):
        raise PolicyViolation("B1 selected claim continuity binding missing")
    for claim in claims:
        is_b2 = claim["id"] == selected_b2_claim_id
        job = db.execute("select * from local_job where id=?", (claim["job_id"],)).fetchone()
        if job is None:
            raise PolicyViolation("B1 selected claim job missing")
        payload = _job_payload(db, job, binding)
        _uuid(job["id"], "selected job")
        _uuid(claim["lease_id"], "selected claim lease")
        if (
            type(claim["fencing_token"]) is not int
            or claim["fencing_token"] <= 0
            or type(job["attempt_count"]) is not int
            or type(job["fencing_counter"]) is not int
            or type(job["max_attempts"]) is not int
            or job["attempt_count"] != claim["fencing_token"]
            or job["fencing_counter"] != claim["fencing_token"]
            or job["max_attempts"] < job["attempt_count"]
            or job["state"]
            not in (
                {"running", "completed", "failed", "recovery-required"}
                if is_b2
                else {"running", "completed", "failed"}
            )
        ):
            raise PolicyViolation("B1 selected job attempt/fence state drift")
        job_created = _runtime_time(job["created_at"], "job creation")
        job_available = _runtime_time(job["available_at"], "job availability")
        claimed_time = _whole_second(claim["claimed_at"], "claim")
        if (
            job_created > claimed_time
            or job_available > claimed_time
            or (
                job["timeout_at"] is not None
                and claimed_time >= _runtime_time(job["timeout_at"], "job timeout")
            )
        ):
            raise PolicyViolation("B1 selected job claim time drift")
        expected_claim = _claim_id(binding, str(job["id"]))
        effect_binding_digest = digest(
            {
                "session_id": binding.session_id,
                "claim_id": expected_claim,
                "job_id": job["id"],
                "binding_digest": binding.binding_digest,
            }
        )
        claim_receipts = db.execute(
            "select i.* from continuity_internal_event_receipt i where i.effect_claim_id=?",
            (claim["id"],),
        ).fetchall()
        if len(claim_receipts) != 1:
            raise PolicyViolation("B1 claimed event cardinality drift")
        internal = claim_receipts[0]
        detail, envelope = _event_for_receipt(db, internal)
        expected_event = _expected_event(
            kind="TOOL_EFFECT_CLAIMED",
            operation_key=_claim_operation(expected_claim),
            occurred_at=str(claim["claimed_at"]),
            source_refs=[f"effect-claim/{expected_claim}"],
            evidence_digests=[str(claim["effect_digest"]), effect_binding_digest],
        )
        if claim["id"] != expected_claim or claim["bound_job"] != job["id"]:
            raise PolicyViolation("B1 deterministic claim identity drift")
        if claim["continuity_binding_digest"] != effect_binding_digest:
            raise PolicyViolation("B1 deterministic claim binding drift")
        if claim["idempotency_key"] != f"continuity-effect:{expected_claim}":
            raise PolicyViolation("B1 deterministic claim idempotency drift")
        if claim["operation"] != payload["operation"]:
            raise PolicyViolation("B1 deterministic claim operation drift")
        _verify_internal(
            internal,
            detail,
            binding=binding,
            producer_kind="effect_claim_id",
            producer_ref=expected_claim,
        )
        _verify_event_body(detail, envelope, binding=binding, expected=expected_event)
        _verify_enqueue_outbox(db, job, trusted_now=trusted_now)
        if is_b2:
            continue
        receipts = db.execute(
            "select * from local_effect_receipt where claim_id=?", (claim["id"],)
        ).fetchall()
        cases = db.execute(
            "select * from local_recovery_case where effect_claim_id=?", (claim["id"],)
        ).fetchall()
        if len(receipts) > 1 or cases:
            raise PolicyViolation("B1 effect recovery/receipt cardinality drift")
        lease = db.execute("select * from local_lease where job_id=?", (job["id"],)).fetchall()
        locks = db.execute(
            "select * from local_resource_lock where job_id=? order by resource", (job["id"],)
        ).fetchall()
        if not receipts:
            if job["state"] != "running" or len(lease) != 1:
                raise PolicyViolation("B1 claim without running lease")
            _verify_running_lease(job, claim, lease[0], locks)
            _terminal_outbox(db, job, claim, None, trusted_now=trusted_now)
            continue
        receipt = receipts[0]
        terminal_receipts = db.execute(
            "select i.* from continuity_internal_event_receipt i where i.effect_receipt_id=?",
            (receipt["id"],),
        ).fetchall()
        if len(terminal_receipts) != 1 or receipt["status"] not in {"completed", "failed"}:
            raise PolicyViolation("B1 direct terminal event missing")
        terminal = terminal_receipts[0]
        terminal_detail, terminal_envelope = _event_for_receipt(db, terminal)
        status_digest = digest(
            {
                "schema": "zekam-direct-effect-terminal-status/v1",
                "claim_id": claim["id"],
                "receipt_id": receipt["id"],
                "status": receipt["status"],
            }
        )
        terminal_event = _expected_event(
            kind="TOOL_EFFECT_COMPLETED",
            operation_key=_terminal_operation(str(claim["id"])),
            occurred_at=str(receipt["created_at"]),
            source_refs=[f"effect-claim/{claim['id']}"],
            evidence_digests=[str(receipt["evidence_digest"]), status_digest],
        )
        if receipt["id"] != _receipt_id(str(claim["id"])):
            raise PolicyViolation("B1 deterministic effect receipt identity drift")
        _whole_second(receipt["created_at"], "direct effect receipt")
        if _runtime_time(receipt["created_at"], "direct receipt") < _runtime_time(
            claim["claimed_at"], "claim"
        ):
            raise PolicyViolation("B1 direct receipt preceded claim")
        _verify_internal(
            terminal,
            terminal_detail,
            binding=binding,
            producer_kind="effect_receipt_id",
            producer_ref=str(receipt["id"]),
        )
        _verify_event_body(
            terminal_detail, terminal_envelope, binding=binding, expected=terminal_event
        )
        if job["state"] == "running":
            if len(lease) != 1:
                raise PolicyViolation("B1 direct outcome lost current lease")
            _verify_running_lease(job, claim, lease[0], locks)
        elif lease or locks:
            raise PolicyViolation("B1 terminal job retained lease/locks")
        _terminal_outbox(db, job, claim, receipt, trusted_now=trusted_now)


def _verify_running_lease(
    job: sqlite3.Row,
    claim: sqlite3.Row,
    lease: sqlite3.Row,
    locks: list[sqlite3.Row],
) -> None:
    _uuid(lease["id"], "retained lease")
    _uuid(lease["job_id"], "retained lease job")
    _bounded_runtime_identity(lease["owner_id"], "retained lease owner")
    _bounded_runtime_identity(lease["owner_token"], "retained lease owner token")
    heartbeat = _runtime_time(lease["heartbeat_at"], "lease heartbeat")
    expiry = _runtime_time(lease["expires_at"], "lease expiry")
    claimed = _runtime_time(claim["claimed_at"], "claim")
    job_updated = _runtime_time(job["updated_at"], "job update")
    if (
        lease["id"] != claim["lease_id"]
        or lease["job_id"] != job["id"]
        or lease["fencing_token"] != claim["fencing_token"]
        or job["fencing_counter"] != claim["fencing_token"]
        or job["attempt_count"] != claim["fencing_token"]
        or type(lease["owner_pid"]) is not int
        or not 1 <= lease["owner_pid"] <= 2_147_483_647
        or job_updated > claimed
        or job_updated > heartbeat
        or heartbeat >= expiry
        or claimed >= expiry
    ):
        raise PolicyViolation("B1 running lease/fence/time drift")
    if len(locks) > 64:
        raise PolicyViolation("B1 retained resource lock bound exceeded")
    resources: list[str] = []
    for lock in locks:
        resource = _bounded_runtime_identity(lock["resource"], "retained resource")
        resources.append(resource)
        if (
            lock["lease_id"] != lease["id"]
            or lock["job_id"] != job["id"]
            or lock["fencing_token"] != claim["fencing_token"]
            or _runtime_time(lock["acquired_at"], "resource lock") > claimed
        ):
            raise PolicyViolation("B1 resource lock scope/time drift")
    if resources != sorted(resources) or len(resources) != len(set(resources)):
        raise PolicyViolation("B1 retained resource locks noncanonical")


def _carried_b2_claim(db: sqlite3.Connection, binding: ContinuityBinding) -> str | None:
    row = db.execute(
        "select c.effect_claim_id from continuity_hook_attachment a "
        "join continuity_hook_attachment_revision v on v.attachment_id=a.attachment_id "
        "join local_recovery_case c on c.id=v.local_recovery_case_id "
        "where a.session_id=? and v.revision_number=(select max(x.revision_number) "
        "from continuity_hook_attachment_revision x where x.attachment_id=a.attachment_id)",
        (binding.session_id,),
    ).fetchall()
    if len(row) > 1:
        raise PolicyViolation("B2 carried recovery claim cardinality drift")
    return None if not row else str(row[0][0])


def verify_b1_b2_internal_producers(
    db: sqlite3.Connection,
    binding: ContinuityBinding,
    *,
    selected_b2_claim_id: str | None = None,
) -> tuple[tuple[Any, ...], ...]:
    """Verify mixed direct-B1 and one internally selected B2 recovery graph."""

    if type(binding) is not ContinuityBinding or db.row_factory is not sqlite3.Row:
        raise ValidationFailed("B1 verifier exact binding and sqlite row factory required")
    owned = not db.in_transaction
    if owned:
        db.execute("begin")
    try:
        trusted_now = SQLiteDormantV4CloseWriter._trusted_now()
        carried = _carried_b2_claim(db, binding)
        selected = selected_b2_claim_id or carried
        if carried is not None and selected != carried:
            raise PolicyViolation("B2 selected recovery claim conflicts with carried history")
        before = _producer_rows(db, binding)
        _verify_turns(db, binding)
        _verify_effects(db, binding, selected_b2_claim_id=selected, trusted_now=trusted_now)
        if selected is not None:
            from zekam.infrastructure.sqlite.local_continuity_v4_recovery import (
                verify_selected_b2_graph,
            )

            verify_selected_b2_graph(db, binding, selected, trusted_now=trusted_now)
        SQLiteDormantV4CloseWriter._events(db, binding)
        _verify_turns(db, binding)
        _verify_effects(db, binding, selected_b2_claim_id=selected, trusted_now=trusted_now)
        if selected is not None:
            verify_selected_b2_graph(db, binding, selected, trusted_now=trusted_now)
        after = _producer_rows(db, binding)
        if before != after:
            raise ConcurrencyConflict("B1 producer graph changed during verification")
        return after
    finally:
        if owned:
            db.rollback()


def verify_b1_internal_producers(
    db: sqlite3.Connection, binding: ContinuityBinding
) -> tuple[tuple[Any, ...], ...]:
    """Compatibility entry point; internally derives carried B2 history."""

    return verify_b1_b2_internal_producers(db, binding)


class SQLiteDormantV4InternalProducer:
    """Dormant B1 builder.  It is not reachable from production composition."""

    def __init__(
        self,
        path: Path,
        binding: ContinuityBinding,
        *,
        turn_issuer: TurnCommitIssuer,
        claim_issuer: EffectClaimIssuer,
        outcome_issuer: DirectEffectOutcomeIssuer,
    ) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValidationFailed("B1 exact absolute operational path required")
        if type(binding) is not ContinuityBinding:
            raise ValidationFailed("B1 exact binding required")
        binding.__post_init__()
        for issuer in (turn_issuer, claim_issuer, outcome_issuer):
            if not callable(getattr(issuer, "snapshot", None)) or not callable(
                getattr(issuer, "recheck", None)
            ):
                raise ValidationFailed("B1 fixed issuer handle required")
        self.path = path
        self.binding = binding
        self.turn_issuer = turn_issuer
        self.claim_issuer = claim_issuer
        self.outcome_issuer = outcome_issuer
        self._schema()

    def _schema(self) -> None:
        state = operational_schema.status(self.path)
        if (
            not state.exists
            or not state.integrity_ok
            or not state.schema_ok
            or state.schema_version != 4
        ):
            raise ConfigurationError("B1 explicit operational-v4 schema required")

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        self._schema()
        uri = self.path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        if read_only:
            db.execute("pragma query_only=on")
        return db

    @staticmethod
    def _commit(db: sqlite3.Connection) -> None:
        db.commit()

    def _request(self, request: object, expected: type[Any]) -> None:
        if type(request) is not expected:
            raise ValidationFailed("B1 exact public selector request required")
        checked = cast(Any, request)
        checked.__post_init__()
        if checked.binding != self.binding:
            raise PolicyViolation("B1 request binding scope drift")

    def _attachment_revision(self, db: sqlite3.Connection) -> sqlite3.Row:
        binding = db.execute(
            "select * from continuity_session_binding where session_id=?",
            (self.binding.session_id,),
        ).fetchone()
        session = db.execute(
            "select status from session where id=?", (self.binding.session_id,)
        ).fetchone()
        attachment = db.execute(
            "select * from continuity_hook_attachment where session_id=?",
            (self.binding.session_id,),
        ).fetchall()
        if (
            binding is None
            or session is None
            or session["status"] != "open"
            or len(attachment) != 1
            or any(
                binding[name] != getattr(self.binding, name)
                for name in (
                    "external_session_id",
                    "project_id",
                    "realm_id",
                    "work_item_id",
                    "run_id",
                    "client_id",
                    "device_id",
                    "source_snapshot_id",
                    "task_digest",
                    "plan_digest",
                    "policy_digest",
                    "binding_digest",
                )
            )
        ):
            raise PolicyViolation("B1 active binding/session scope drift")
        revision = SQLiteDormantV4CloseWriter._current_revision(
            db, str(attachment[0]["attachment_id"])
        )
        if revision["state"] != "hydrated":
            raise PolicyViolation("B1 current hydrated attachment required")
        return revision

    def _tail(self, db: sqlite3.Connection) -> ContinuityTail:
        rows = SQLiteDormantV4CloseWriter._events(db, self.binding)
        if not rows or rows[0]["event_kind"] != "SESSION_START":
            raise PolicyViolation("B1 verified SessionStart event head required")
        return ContinuityTail(len(rows), str(rows[-1]["event_digest"]))

    def _preflight(self, db: sqlite3.Connection, expected_tail: ContinuityTail) -> sqlite3.Row:
        revision = self._attachment_revision(db)
        verify_b1_internal_producers(db, self.binding)
        if self._tail(db) != expected_tail:
            raise ConcurrencyConflict("B1 optimistic event tail drift")
        return revision

    def _partial(self, db: sqlite3.Connection, operation_key: str, producer_ref: str | None) -> int:
        return int(
            db.execute(
                "select (select count(*) from continuity_internal_event_receipt "
                "where session_id=? and operation_key=?) + "
                "(select count(*) from session_event_detail where session_id=? "
                "and idempotency_key=?) + "
                "(select count(*) from continuity_turn_commit_receipt where receipt_digest=?) + "
                "(select count(*) from local_effect_claim where id=?) + "
                "(select count(*) from local_effect_receipt where id=?)",
                (
                    self.binding.session_id,
                    operation_key,
                    self.binding.session_id,
                    operation_key,
                    producer_ref,
                    producer_ref,
                    producer_ref,
                ),
            ).fetchone()[0]
        )

    def _replay_result(
        self, db: sqlite3.Connection, operation_key: str, producer_ref: str | None
    ) -> InternalProducerResult | None:
        receipts = db.execute(
            "select * from continuity_internal_event_receipt "
            "where session_id=? and operation_key=?",
            (self.binding.session_id, operation_key),
        ).fetchall()
        if not receipts:
            if self._partial(db, operation_key, producer_ref):
                raise ConcurrencyConflict("B1 partial deterministic producer graph")
            return None
        if len(receipts) != 1:
            raise ConcurrencyConflict("B1 duplicate deterministic producer graph")
        verify_b1_internal_producers(db, self.binding)
        receipt = receipts[0]
        expected_ref = (
            receipt["turn_commit_digest"]
            or receipt["effect_claim_id"]
            or receipt["effect_receipt_id"]
        )
        if producer_ref is not None and expected_ref != producer_ref:
            raise ConcurrencyConflict("B1 replay producer identity drift")
        return InternalProducerResult(
            str(receipt["event_kind"]),
            str(receipt["event_digest"]),
            str(expected_ref),
            True,
        )

    def _classify(self, operation_key: str, producer_ref: str) -> InternalProducerResult:
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            result = self._replay_result(db, operation_key, producer_ref)
            if result is None:
                raise ConcurrencyConflict("B1 not-committed-or-unobservable")
            return result

    @staticmethod
    def _insert_event(
        db: sqlite3.Connection,
        *,
        binding: ContinuityBinding,
        revision_digest: str,
        tail: ContinuityTail,
        kind: str,
        operation_key: str,
        occurred_at: str,
        source_refs: list[str],
        evidence_digests: list[str],
        producer_kind: str,
        producer_ref: str,
    ) -> str:
        body = _expected_event(
            kind=kind,
            operation_key=operation_key,
            occurred_at=occurred_at,
            source_refs=source_refs,
            evidence_digests=evidence_digests,
        )
        sequence = tail.sequence + 1
        value = event_digest(
            binding,
            sequence=sequence,
            previous_digest=tail.event_digest,
            event_body=body,
        )
        event_id = _event_id(value)
        receipt_body = {
            "attachment_revision_digest": revision_digest,
            "binding_digest": binding.binding_digest,
            "created_at": occurred_at,
            "event_digest": value,
            "event_kind": kind,
            "expected_previous_event_digest": tail.event_digest,
            "operation_key": operation_key,
            "session_id": binding.session_id,
        }
        receipt_digest = internal_receipt_digest(
            receipt_body, producer_kind=producer_kind, producer_ref=producer_ref
        )
        producer_columns: dict[str, str | None] = {
            "turn_commit_digest": None,
            "effect_claim_id": None,
            "effect_receipt_id": None,
            "native_event_receipt_digest": None,
            "close_request_digest": None,
            "close_receipt_digest": None,
            "hook_recovery_resolution_id": None,
            "local_recovery_resolution_id": None,
        }
        producer_columns[producer_kind] = producer_ref
        db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
            "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
            "hook_recovery_resolution_id,local_recovery_resolution_id,attachment_revision_digest,"
            "body_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_digest,
                value,
                binding.session_id,
                binding.binding_digest,
                kind,
                operation_key,
                tail.event_digest,
                *producer_columns.values(),
                revision_digest,
                canonical_json(receipt_body),
                occurred_at,
            ),
        )
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (event_id, binding.session_id, kind, value, occurred_at),
        )
        envelope = {
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "sequence": sequence,
            "previous_digest": tail.event_digest,
            "event": body,
        }
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
            (
                event_id,
                binding.session_id,
                sequence,
                tail.event_digest,
                operation_key,
                value,
                None,
                canonical_json(envelope),
            ),
        )
        return value

    def commit_turn(self, request: TurnCommitRequest) -> InternalProducerResult:
        self._request(request, TurnCommitRequest)
        operation_key = _operation_for_turn(request.role, request.item_ref)
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            replay = self._replay_result(db, operation_key, None)
            if replay is not None:
                return replay
            if db.execute(
                "select 1 from continuity_turn_commit_receipt "
                "where session_id=? and role=? and item_ref=?",
                (self.binding.session_id, request.role, request.item_ref),
            ).fetchone():
                raise ConcurrencyConflict("B1 partial turn producer graph")
        snapshot = self.turn_issuer.snapshot(request)
        if type(snapshot) is not FrozenTurnCommitSnapshot:
            raise ValidationFailed("B1 exact turn authority snapshot required")
        snapshot.__post_init__()
        if (
            snapshot.binding_digest != self.binding.binding_digest
            or snapshot.role != request.role
            or snapshot.item_ref != request.item_ref
        ):
            raise PolicyViolation("B1 turn authority selector drift")
        body_seed = {
            "binding_digest": self.binding.binding_digest,
            "content_digest": snapshot.content_commitment_digest,
            "created_at": snapshot.committed_at,
            "item_ref": snapshot.item_ref,
            "previous_turn_commit_digest": None,
            "role": snapshot.role,
            "session_id": self.binding.session_id,
            "store_generation_digest": snapshot.store_generation_commitment_digest,
        }
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            previous = db.execute(
                "select t.receipt_digest from continuity_turn_commit_receipt t "
                "join continuity_internal_event_receipt i on i.turn_commit_digest=t.receipt_digest "
                "join session_event_detail d on d.event_digest=i.event_digest "
                "and d.session_id=i.session_id where t.session_id=? "
                "order by d.sequence desc limit 1",
                (self.binding.session_id,),
            ).fetchone()
            body_seed["previous_turn_commit_digest"] = None if previous is None else previous[0]
            producer_ref = digest({"schema": "zekam-turn-commit-receipt/v1", "body": body_seed})
        db = self._connect()
        try:
            db.execute("begin immediate")
            revision = self._preflight(db, request.expected_tail)
            previous = db.execute(
                "select t.receipt_digest,t.store_generation_digest "
                "from continuity_turn_commit_receipt t "
                "join continuity_internal_event_receipt i on i.turn_commit_digest=t.receipt_digest "
                "join session_event_detail d on d.event_digest=i.event_digest "
                "and d.session_id=i.session_id where t.session_id=? "
                "order by d.sequence desc limit 1",
                (self.binding.session_id,),
            ).fetchone()
            body = dict(body_seed)
            body["previous_turn_commit_digest"] = None if previous is None else previous[0]
            expected_generation = None if previous is None else previous[1]
            if snapshot.previous_store_generation_commitment_digest != expected_generation:
                raise PolicyViolation("B1 turn generation predecessor drift")
            producer_ref = digest({"schema": "zekam-turn-commit-receipt/v1", "body": body})
            db.execute(
                "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
                (
                    producer_ref,
                    self.binding.session_id,
                    self.binding.binding_digest,
                    snapshot.role,
                    snapshot.item_ref,
                    snapshot.content_commitment_digest,
                    snapshot.store_generation_commitment_digest,
                    body["previous_turn_commit_digest"],
                    canonical_json(body),
                    snapshot.committed_at,
                ),
            )
            value = self._insert_event(
                db,
                binding=self.binding,
                revision_digest=str(revision["revision_digest"]),
                tail=request.expected_tail,
                kind="USER_TURN_COMMITTED"
                if snapshot.role == "user"
                else "ASSISTANT_TURN_COMMITTED",
                operation_key=operation_key,
                occurred_at=snapshot.committed_at,
                source_refs=[snapshot.item_ref],
                evidence_digests=[
                    producer_ref,
                    snapshot.content_commitment_digest,
                    snapshot.store_generation_commitment_digest,
                ],
                producer_kind="turn_commit_digest",
                producer_ref=producer_ref,
            )
            self.turn_issuer.recheck(snapshot)
            verify_b1_internal_producers(db, self.binding)
            self._commit(db)
            return InternalProducerResult(
                "USER_TURN_COMMITTED" if snapshot.role == "user" else "ASSISTANT_TURN_COMMITTED",
                value,
                producer_ref,
                False,
            )
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
                raise ConcurrencyConflict("B1 turn commit concurrency conflict") from exc
            db.close()
            return self._classify(operation_key, producer_ref)
        except Exception:
            if db.in_transaction:
                db.rollback()
                raise
            db.close()
            return self._classify(operation_key, producer_ref)
        finally:
            db.close()

    def claim_effect(self, request: EffectClaimRequest) -> InternalProducerResult:
        self._request(request, EffectClaimRequest)
        claim_id = _claim_id(self.binding, request.job_id)
        operation_key = _claim_operation(claim_id)
        with closing(self._connect(read_only=True)) as read:
            read.execute("begin")
            replay = self._replay_result(read, operation_key, claim_id)
            if replay is not None:
                return replay
        snapshot = self.claim_issuer.snapshot(request)
        if type(snapshot) is not FrozenEffectClaimSnapshot:
            raise ValidationFailed("B1 exact claim authority snapshot required")
        snapshot.__post_init__()
        db = self._connect()
        try:
            db.execute("begin immediate")
            revision = self._preflight(db, request.expected_tail)
            self._verify_fresh_claim_snapshot(db, request, snapshot)
            db.execute(
                "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    request.job_id,
                    snapshot.lease_id,
                    snapshot.fencing_token,
                    snapshot.operation,
                    snapshot.effect_commitment_digest,
                    f"continuity-effect:{claim_id}",
                    snapshot.claimed_at,
                ),
            )
            binding_digest = digest(
                {
                    "session_id": self.binding.session_id,
                    "claim_id": claim_id,
                    "job_id": request.job_id,
                    "binding_digest": self.binding.binding_digest,
                }
            )
            db.execute(
                "insert into continuity_effect_binding values(?,?,?,?)",
                (claim_id, self.binding.session_id, request.job_id, binding_digest),
            )
            value = self._insert_event(
                db,
                binding=self.binding,
                revision_digest=str(revision["revision_digest"]),
                tail=request.expected_tail,
                kind="TOOL_EFFECT_CLAIMED",
                operation_key=operation_key,
                occurred_at=snapshot.claimed_at,
                source_refs=[f"effect-claim/{claim_id}"],
                evidence_digests=[snapshot.effect_commitment_digest, binding_digest],
                producer_kind="effect_claim_id",
                producer_ref=claim_id,
            )
            self.claim_issuer.recheck(snapshot)
            verify_b1_internal_producers(db, self.binding)
            self._commit(db)
            return InternalProducerResult("TOOL_EFFECT_CLAIMED", value, claim_id, False)
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
                raise ConcurrencyConflict("B1 claim concurrency conflict") from exc
            db.close()
            return self._classify(operation_key, claim_id)
        except Exception:
            if db.in_transaction:
                db.rollback()
                raise
            db.close()
            return self._classify(operation_key, claim_id)
        finally:
            db.close()

    def _verify_fresh_claim_snapshot(
        self,
        db: sqlite3.Connection,
        request: EffectClaimRequest,
        snapshot: FrozenEffectClaimSnapshot,
    ) -> None:
        job = db.execute("select * from local_job where id=?", (request.job_id,)).fetchone()
        lease = db.execute("select * from local_lease where id=?", (snapshot.lease_id,)).fetchone()
        if job is None or lease is None:
            raise PolicyViolation("B1 selected running job/lease missing")
        payload = _job_payload(db, job, self.binding)
        locks = db.execute(
            "select resource,acquired_at from local_resource_lock where job_id=? order by resource",
            (request.job_id,),
        ).fetchall()
        actual_locks = tuple((str(row[0]), str(row[1])) for row in locks)
        ambiguous = _ambiguous_other_jobs(db, self.binding, request.job_id)
        terminal_rows = db.execute(
            "select count(*) from local_outbox where job_id=? and event_kind in "
            "('job.completed','job.failed','job.recovery-required','job.quarantined')",
            (request.job_id,),
        ).fetchone()[0]
        existing_claims = db.execute(
            "select count(*) from local_effect_claim where job_id=?",
            (request.job_id,),
        ).fetchone()[0]
        if (
            snapshot.binding_digest != self.binding.binding_digest
            or snapshot.job_id != request.job_id
            or snapshot.job_state != "running"
            or job["state"] != "running"
            or snapshot.job_payload_digest != digest(payload)
            or snapshot.job_updated_at != job["updated_at"]
            or snapshot.operation != payload["operation"]
            or lease["job_id"] != request.job_id
            or snapshot.lease_owner_id != lease["owner_id"]
            or snapshot.lease_owner_pid != lease["owner_pid"]
            or snapshot.lease_owner_token != lease["owner_token"]
            or snapshot.fencing_token != lease["fencing_token"]
            or snapshot.lease_heartbeat_at != lease["heartbeat_at"]
            or snapshot.lease_expires_at != lease["expires_at"]
            or snapshot.resource_locks != actual_locks
            or ambiguous
            or terminal_rows
            or existing_claims
        ):
            raise PolicyViolation("B1 fresh claim authority/runtime drift")
        claimed = _whole_second(snapshot.claimed_at, "claim authority")
        if (
            _runtime_time(job["updated_at"], "job update") > claimed
            or _runtime_time(lease["heartbeat_at"], "lease heartbeat") > claimed
            or claimed >= _runtime_time(lease["expires_at"], "lease expiry")
            or any(_runtime_time(item[1], "resource lock") > claimed for item in actual_locks)
        ):
            raise PolicyViolation("B1 fresh claim causal time drift")
        _verify_enqueue_outbox(db, job, trusted_now=claimed)

    def record_direct_outcome(self, request: DirectEffectOutcomeRequest) -> InternalProducerResult:
        self._request(request, DirectEffectOutcomeRequest)
        receipt_id = _receipt_id(request.claim_id)
        operation_key = _terminal_operation(request.claim_id)
        with closing(self._connect(read_only=True)) as read:
            read.execute("begin")
            replay = self._replay_result(read, operation_key, receipt_id)
            if replay is not None:
                return replay
        snapshot = self.outcome_issuer.snapshot(request)
        if type(snapshot) is not FrozenDirectEffectOutcomeSnapshot:
            raise ValidationFailed("B1 exact outcome authority snapshot required")
        snapshot.__post_init__()
        db = self._connect()
        try:
            db.execute("begin immediate")
            revision = self._preflight(db, request.expected_tail)
            self._verify_fresh_outcome_snapshot(db, request, snapshot)
            db.execute(
                "insert into local_effect_receipt values(?,?,?,?,?)",
                (
                    receipt_id,
                    request.claim_id,
                    snapshot.status,
                    snapshot.outcome_commitment_digest,
                    snapshot.completed_at,
                ),
            )
            status_digest = digest(
                {
                    "schema": "zekam-direct-effect-terminal-status/v1",
                    "claim_id": request.claim_id,
                    "receipt_id": receipt_id,
                    "status": snapshot.status,
                }
            )
            value = self._insert_event(
                db,
                binding=self.binding,
                revision_digest=str(revision["revision_digest"]),
                tail=request.expected_tail,
                kind="TOOL_EFFECT_COMPLETED",
                operation_key=operation_key,
                occurred_at=snapshot.completed_at,
                source_refs=[f"effect-claim/{request.claim_id}"],
                evidence_digests=[snapshot.outcome_commitment_digest, status_digest],
                producer_kind="effect_receipt_id",
                producer_ref=receipt_id,
            )
            self.outcome_issuer.recheck(snapshot)
            verify_b1_internal_producers(db, self.binding)
            self._commit(db)
            return InternalProducerResult("TOOL_EFFECT_COMPLETED", value, receipt_id, False)
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
                raise ConcurrencyConflict("B1 direct outcome concurrency conflict") from exc
            db.close()
            return self._classify(operation_key, receipt_id)
        except Exception:
            if db.in_transaction:
                db.rollback()
                raise
            db.close()
            return self._classify(operation_key, receipt_id)
        finally:
            db.close()

    def _verify_fresh_outcome_snapshot(
        self,
        db: sqlite3.Connection,
        request: DirectEffectOutcomeRequest,
        snapshot: FrozenDirectEffectOutcomeSnapshot,
    ) -> None:
        claim = db.execute(
            "select * from local_effect_claim where id=?", (request.claim_id,)
        ).fetchone()
        bound = db.execute(
            "select * from continuity_effect_binding where claim_id=? and session_id=?",
            (request.claim_id, self.binding.session_id),
        ).fetchone()
        if claim is None or bound is None:
            raise PolicyViolation("B1 direct outcome exact claimed effect missing")
        job = db.execute("select * from local_job where id=?", (claim["job_id"],)).fetchone()
        lease = db.execute("select * from local_lease where id=?", (claim["lease_id"],)).fetchone()
        if job is None or lease is None or job["state"] != "running":
            raise PolicyViolation("B1 direct outcome current running lease missing")
        payload = _job_payload(db, job, self.binding)
        expected_binding_digest = digest(
            {
                "session_id": self.binding.session_id,
                "claim_id": claim["id"],
                "job_id": job["id"],
                "binding_digest": self.binding.binding_digest,
            }
        )
        ambiguous = _ambiguous_other_jobs(db, self.binding, str(job["id"]))
        if (
            snapshot.binding_digest != self.binding.binding_digest
            or snapshot.job_id != job["id"]
            or snapshot.claim_id != claim["id"]
            or snapshot.lease_id != claim["lease_id"]
            or snapshot.lease_owner_id != lease["owner_id"]
            or snapshot.lease_owner_pid != lease["owner_pid"]
            or snapshot.lease_owner_token != lease["owner_token"]
            or snapshot.fencing_token != claim["fencing_token"]
            or snapshot.operation != claim["operation"]
            or snapshot.operation != payload["operation"]
            or snapshot.effect_commitment_digest != claim["effect_digest"]
            or snapshot.claimed_at != claim["claimed_at"]
            or bound["binding_digest"] != expected_binding_digest
            or bound["job_id"] != job["id"]
            or ambiguous
        ):
            raise PolicyViolation("B1 direct outcome authority/runtime drift")
        completed = _whole_second(snapshot.completed_at, "direct outcome authority")
        claimed = _whole_second(snapshot.claimed_at, "persisted claim")
        if claimed > completed or completed >= _runtime_time(lease["expires_at"], "lease expiry"):
            raise PolicyViolation("B1 direct outcome causal time drift")
        _verify_enqueue_outbox(db, job, trusted_now=completed)
