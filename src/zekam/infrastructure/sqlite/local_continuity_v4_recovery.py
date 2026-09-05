from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid5

from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_recovery import (
    B2_EVENT_NS,
    EffectRecoveryAdjudicator,
    FrozenEffectRecoveryResolutionSnapshot,
    FrozenReceiptlessRecoverySnapshot,
    FrozenUnknownEffectSnapshot,
    ReceiptlessRecoveryIssuer,
    ReceiptlessRecoveryRequest,
    RecoveryResult,
    ResolveEffectRecoveryRequest,
    UnknownEffectIssuer,
    UnknownEffectRequest,
    commit_outcome,
    entry_result,
    resolution_result,
)
from zekam.application.local_continuity_v4_writer import (
    event_digest,
    internal_receipt_digest,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity_v4_internal import (
    _job_payload,
    _runtime_time,
    _verify_b2_crash_event,
    _verify_b2_outbox,
    _whole_second,
    verify_b1_b2_internal_producers,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter
from zekam.infrastructure.sqlite.local_runtime_recovery_tx import (
    EffectRecoveryCaseSpec,
    EffectRecoveryResolutionSpec,
    LockRow,
    RecoveryReconcileSpec,
    RecoveryTransitionSpec,
    insert_effect_recovery_case_tx,
    insert_effect_recovery_resolution_tx,
    reconcile_effect_recovery_job_tx,
    require_outbox_capacity_tx,
    transition_running_job_to_recovery_tx,
)

_RECOVERY_FIELDS = (
    "hook_recovery_case_id",
    "hook_recovery_resolution_id",
    "local_recovery_case_id",
    "local_recovery_resolution_id",
    "crash_recovered_event_digest",
    "crash_recovered_receipt_digest",
)

type _EntryRoute = Literal["unknown", "receiptless"]
type _EntryRequest = UnknownEffectRequest | ReceiptlessRecoveryRequest
type _EntrySnapshot = FrozenUnknownEffectSnapshot | FrozenReceiptlessRecoverySnapshot


def _b2_id(label: str, *parts: object) -> str:
    return str(uuid5(B2_EVENT_NS, "|".join((label, *(str(part) for part in parts)))))


def _current_revision(db: sqlite3.Connection, binding: ContinuityBinding) -> sqlite3.Row:
    attachment = db.execute(
        "select attachment_id from continuity_hook_attachment where session_id=?",
        (binding.session_id,),
    ).fetchall()
    if len(attachment) != 1:
        raise PolicyViolation("B2 exact hook attachment required")
    return SQLiteDormantV4CloseWriter._current_revision(db, str(attachment[0][0]))


def _entry_revision(db: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    rows = db.execute(
        "select * from continuity_hook_attachment_revision where local_recovery_case_id=? "
        "and state='recovery-required' order by revision_number",
        (case_id,),
    ).fetchall()
    if len(rows) != 1:
        raise PolicyViolation("B2 recovery-required revision cardinality drift")
    return SQLiteDormantV4CloseWriter._verified_revision(rows[0])


def _next_revision_body(
    predecessor: sqlite3.Row,
    *,
    state: str,
    operation_key: str,
    created_at: str,
    local_case: str,
    local_resolution: str | None,
    crash_event: str | None,
    crash_receipt: str | None,
) -> dict[str, Any]:
    body = SQLiteDormantV4CloseWriter._revision_body(
        predecessor,
        revision_number=int(predecessor["revision_number"]) + 1,
        operation_key=operation_key,
        state=state,
        created_at=created_at,
        checkpoint_digest=predecessor["checkpoint_digest"],
        close_request_digest=predecessor["close_request_digest"],
        pre_close_event_digest=predecessor["pre_close_event_digest"],
        close_receipt_digest=predecessor["close_receipt_digest"],
        session_closed_event_digest=predecessor["session_closed_event_digest"],
    )
    body.update(
        {
            "local_recovery_case_id": local_case,
            "local_recovery_resolution_id": local_resolution,
            "crash_recovered_event_digest": crash_event,
            "crash_recovered_receipt_digest": crash_receipt,
            "created_at": created_at,
        }
    )
    return body


def _rows(
    db: sqlite3.Connection, sql: str, values: tuple[object, ...]
) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in db.execute(sql, values).fetchall())


@dataclass(frozen=True, slots=True, repr=False)
class _CommitBaseline:
    binding: tuple[tuple[Any, ...], ...]
    claim: tuple[tuple[Any, ...], ...]
    job: tuple[tuple[Any, ...], ...]
    lease: tuple[tuple[Any, ...], ...]
    locks: tuple[tuple[Any, ...], ...]
    revision: tuple[Any, ...]
    tail: tuple[int, str | None]
    outbox_graph: tuple[tuple[Any, ...], ...]
    operation_graph: tuple[tuple[Any, ...], ...]


def _baseline(
    db: sqlite3.Connection,
    binding: ContinuityBinding,
    claim_id: str,
    case_id: str,
) -> _CommitBaseline:
    claim = _rows(
        db,
        "select c.*,b.* from local_effect_claim c left join continuity_effect_binding b "
        "on b.claim_id=c.id where c.id=?",
        (claim_id,),
    )
    job_id = "" if not claim else str(claim[0][1])
    revision = _current_revision(db, binding)
    events = SQLiteDormantV4CloseWriter._events(db, binding)
    tail = (len(events), None if not events else str(events[-1]["event_digest"]))
    return _CommitBaseline(
        _rows(
            db,
            "select * from continuity_session_binding where session_id=?",
            (binding.session_id,),
        ),
        claim,
        _rows(db, "select * from local_job where id=?", (job_id,)),
        _rows(db, "select * from local_lease where job_id=? order by id", (job_id,)),
        _rows(db, "select * from local_resource_lock where job_id=? order by resource", (job_id,)),
        tuple(revision),
        tail,
        _rows(
            db,
            "select o.*,d.*,r.*,x.*,z.* "
            "from local_outbox o left join local_outbox_delivery d on d.outbox_id=o.id "
            "left join local_outbox_receipt r on r.outbox_id=o.id "
            "left join local_recovery_case x on x.outbox_id=o.id "
            "left join local_recovery_resolution z on z.recovery_case_id=x.id "
            "where o.job_id=? order by o.id,r.id,x.id,z.id",
            (job_id,),
        ),
        _rows(
            db,
            "select 'receipt',id,status,evidence_digest,created_at from local_effect_receipt "
            "where claim_id=? union all select 'case',id,state,evidence_digest,created_at "
            "from local_recovery_case where id=? or effect_claim_id=? union all "
            "select 'resolution',r.id,r.outcome,r.evidence_digest,r.created_at "
            "from local_recovery_resolution r join local_recovery_case c "
            "on c.id=r.recovery_case_id where c.id=? union all "
            "select 'revision',revision_digest,state,operation_key,created_at "
            "from continuity_hook_attachment_revision where local_recovery_case_id=? union all "
            "select 'internal',receipt_digest,event_kind,operation_key,created_at "
            "from continuity_internal_event_receipt where local_recovery_resolution_id in "
            "(select id from local_recovery_resolution where recovery_case_id=?) union all "
            "select 'event',e.id,e.event_kind,e.event_digest,e.created_at from session_event e "
            "join continuity_internal_event_receipt i on i.event_digest=e.event_digest "
            "where i.local_recovery_resolution_id in "
            "(select id from local_recovery_resolution where recovery_case_id=?) union all "
            "select 'detail',d.event_id,cast(d.sequence as text),d.event_digest,d.idempotency_key "
            "from session_event_detail d join continuity_internal_event_receipt i "
            "on i.event_digest=d.event_digest where i.local_recovery_resolution_id in "
            "(select id from local_recovery_resolution where recovery_case_id=?)",
            (claim_id, case_id, claim_id, case_id, case_id, case_id, case_id, case_id),
        ),
    )


def verify_selected_b2_graph(
    db: sqlite3.Connection,
    binding: ContinuityBinding,
    claim_id: str,
    *,
    trusted_now: datetime,
) -> None:
    claim = db.execute(
        "select c.*,b.binding_digest as continuity_binding_digest,b.session_id as bound_session,"
        "b.job_id as bound_job from local_effect_claim c join continuity_effect_binding b "
        "on b.claim_id=c.id where c.id=? and b.session_id=?",
        (claim_id, binding.session_id),
    ).fetchone()
    if claim is None:
        raise PolicyViolation("B2 selected continuity claim missing")
    job = db.execute("select * from local_job where id=?", (claim["job_id"],)).fetchone()
    cases = db.execute(
        "select * from local_recovery_case where effect_claim_id=?", (claim_id,)
    ).fetchall()
    if job is None or len(cases) != 1:
        raise PolicyViolation("B2 selected job/case cardinality drift")
    case = cases[0]
    if (
        case["id"] != _b2_id("effect-case", claim_id)
        or case["job_id"] != job["id"]
        or case["outbox_id"] is not None
        or case["case_kind"] != "effect-unknown"
    ):
        raise PolicyViolation("B2 selected recovery case identity/scope drift")
    try:
        parse_digest(str(claim["effect_digest"]))
        parse_digest(str(case["evidence_digest"]))
        claimed_at = _runtime_time(claim["claimed_at"], "B2 selected claim")
        case_at = _whole_second(case["created_at"], "B2 recovery case")
    except (ValidationFailed, PolicyViolation) as exc:
        raise PolicyViolation("B2 durable commitment/time drift") from exc
    if claimed_at > case_at:
        raise PolicyViolation("B2 recovery case preceded claim")
    receipts = db.execute(
        "select * from local_effect_receipt where claim_id=?", (claim_id,)
    ).fetchall()
    if len(receipts) > 1:
        raise PolicyViolation("B2 selected receipt cardinality drift")
    if receipts:
        receipt = receipts[0]
        try:
            parse_digest(str(receipt["evidence_digest"]))
            receipt_at = _whole_second(receipt["created_at"], "B2 unknown receipt")
        except (ValidationFailed, PolicyViolation) as exc:
            raise PolicyViolation("B2 unknown receipt commitment/time drift") from exc
        case_evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": claim_id,
                "receipt_evidence": receipt["evidence_digest"],
            }
        )
        if (
            receipt["id"] != _b2_id("unknown-receipt", claim_id)
            or receipt["status"] != "unknown"
            or receipt["created_at"] != case["created_at"]
            or receipt_at != case_at
        ):
            raise PolicyViolation("B2 unknown receipt parity drift")
        entry_id = _b2_id("terminal-outbox", job["id"])
        key = f"job:{job['id']}:terminal"
        payload = {"job_id": job["id"], "state": "recovery-required"}
    else:
        case_evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": claim_id,
                "effect_digest": claim["effect_digest"],
                "recovered_fence": claim["fencing_token"],
            }
        )
        entry_id = _b2_id("recovery-outbox", job["id"], claim["fencing_token"])
        key = f"job:{job['id']}:recovery:{claim['fencing_token']}:recovery-required"
        payload = {
            "job_id": job["id"],
            "state": "recovery-required",
            "fencing_token": claim["fencing_token"],
        }
    if case["evidence_digest"] != case_evidence:
        raise PolicyViolation("B2 recovery case evidence drift")
    entry_revision = _entry_revision(db, str(case["id"]))
    predecessor = SQLiteDormantV4CloseWriter._verified_revision(
        db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (entry_revision["previous_revision_digest"],),
        ).fetchone()
    )
    if (
        predecessor["state"] != "hydrated"
        or any(predecessor[name] is not None for name in _RECOVERY_FIELDS)
        or entry_revision["operation_key"] != f"effect-recovery-required:{case['id']}"
        or entry_revision["created_at"] != case["created_at"]
    ):
        raise PolicyViolation("B2 first recovery revision predecessor drift")
    if (
        db.execute("select count(*) from local_lease where job_id=?", (job["id"],)).fetchone()[0]
        or db.execute(
            "select count(*) from local_resource_lock where job_id=?", (job["id"],)
        ).fetchone()[0]
    ):
        raise PolicyViolation("B2 recovery job retained lease/locks")
    _verify_b2_outbox(
        db,
        job_id=str(job["id"]),
        key=key,
        expected_id=entry_id,
        kind="job.recovery-required",
        payload=payload,
        created_at=str(case["created_at"]),
        trusted_now=trusted_now,
    )
    resolutions = db.execute(
        "select * from local_recovery_resolution where recovery_case_id=?", (case["id"],)
    ).fetchall()
    terminal_keys = {
        str(row[0])
        for row in db.execute(
            "select idempotency_key from local_outbox where job_id=? and event_kind in "
            "('job.completed','job.failed','job.recovery-required','job.quarantined')",
            (job["id"],),
        ).fetchall()
    }
    expected_keys = {key}
    if resolutions:
        expected_keys.add(f"job:{job['id']}:reconciled")
    if terminal_keys != expected_keys:
        raise PolicyViolation("B2 terminal/recovery outbox route drift")
    if not resolutions:
        if (
            case["state"] != "open"
            or job["state"] != "recovery-required"
            or job["terminal_evidence_digest"] != digest([case_evidence])
            or case["created_at"] != job["updated_at"]
            or _current_revision(db, binding)["revision_digest"]
            != entry_revision["revision_digest"]
        ):
            raise PolicyViolation("B2 open recovery graph drift")
        return
    if len(resolutions) != 1:
        raise PolicyViolation("B2 recovery resolution cardinality drift")
    resolution = resolutions[0]
    try:
        parse_digest(str(resolution["evidence_digest"]))
        resolution_at = _whole_second(resolution["created_at"], "B2 resolution")
    except (ValidationFailed, PolicyViolation) as exc:
        raise PolicyViolation("B2 resolution commitment/time drift") from exc
    if (
        resolution["id"] != _b2_id("effect-resolution", case["id"])
        or resolution["outcome"] not in {"completed", "failed"}
        or case["state"] != "resolved"
        or case["resolved_at"] != resolution["created_at"]
        or case_at > resolution_at
    ):
        raise PolicyViolation("B2 recovery resolution parity drift")
    state = str(resolution["outcome"])
    terminal = digest(
        [
            (
                receipts[0]["status"] if receipts else None,
                receipts[0]["evidence_digest"] if receipts else None,
                state,
                resolution["evidence_digest"],
            )
        ]
    )
    if (
        job["state"] != state
        or job["terminal_evidence_digest"] != terminal
        or job["updated_at"] != resolution["created_at"]
    ):
        raise PolicyViolation("B2 reconciled job evidence drift")
    reconciled = {"job_id": job["id"], "state": state, "reconciled": True}
    _verify_b2_outbox(
        db,
        job_id=str(job["id"]),
        key=f"job:{job['id']}:reconciled",
        expected_id=_b2_id("reconciled-outbox", job["id"]),
        kind=f"job.{state}",
        payload=reconciled,
        created_at=str(resolution["created_at"]),
        trusted_now=trusted_now,
    )
    if state == "failed":
        if _current_revision(db, binding)["revision_digest"] != entry_revision["revision_digest"]:
            raise PolicyViolation("B2 failed recovery changed attachment")
        if db.execute(
            "select count(*) from continuity_internal_event_receipt "
            "where local_recovery_resolution_id=?",
            (resolution["id"],),
        ).fetchone()[0]:
            raise PolicyViolation("B2 failed recovery forged CRASH event")
        return
    restored_rows = db.execute(
        "select * from continuity_hook_attachment_revision where previous_revision_digest=? "
        "and local_recovery_resolution_id=?",
        (entry_revision["revision_digest"], resolution["id"]),
    ).fetchall()
    if len(restored_rows) != 1:
        raise PolicyViolation("B2 restored revision cardinality drift")
    restored = SQLiteDormantV4CloseWriter._verified_revision(restored_rows[0])
    if (
        restored["state"] != "hydrated"
        or restored["operation_key"] != f"effect-restored-revision:{case['id']}"
        or restored["created_at"] != resolution["created_at"]
        or _current_revision(db, binding)["revision_digest"] != restored["revision_digest"]
    ):
        raise PolicyViolation("B2 restored attachment revision drift")
    _verify_b2_crash_event(
        db, binding, resolution=resolution, recovery_revision=entry_revision, restored=restored
    )


def _safe_snapshot(port: object, request: object, expected: type[Any]) -> Any:
    try:
        value = cast(Any, port).snapshot(request)
    except Exception:
        raise PolicyViolation("B2 authority snapshot unavailable") from None
    if type(value) is not expected:
        raise PolicyViolation("B2 authority snapshot invalid")
    try:
        value.__post_init__()
    except Exception:
        raise PolicyViolation("B2 authority snapshot invalid") from None
    return value


def _safe_recheck(port: object, snapshot: object) -> None:
    try:
        cast(Any, port).recheck(snapshot)
    except Exception:
        raise PolicyViolation("B2 authority recheck failed") from None


class SQLiteDormantV4Recovery:
    def __init__(
        self,
        path: Path,
        binding: ContinuityBinding,
        *,
        unknown_issuer: UnknownEffectIssuer,
        receiptless_issuer: ReceiptlessRecoveryIssuer,
        adjudicator: EffectRecoveryAdjudicator,
    ) -> None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or type(binding) is not ContinuityBinding
        ):
            raise ValidationFailed("B2 exact absolute path and binding required")
        binding.__post_init__()
        for issuer in (unknown_issuer, receiptless_issuer, adjudicator):
            if not callable(getattr(issuer, "snapshot", None)) or not callable(
                getattr(issuer, "recheck", None)
            ):
                raise ValidationFailed("B2 fixed issuer handle required")
        self.path = path
        self.binding = binding
        self.unknown_issuer = unknown_issuer
        self.receiptless_issuer = receiptless_issuer
        self.adjudicator = adjudicator
        self._schema()

    def _schema(self) -> None:
        state = operational_schema.status(self.path)
        if (
            not state.exists
            or not state.integrity_ok
            or not state.schema_ok
            or state.schema_version != 4
        ):
            raise ConfigurationError("B2 explicit operational-v4 schema required")

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
            raise ValidationFailed("B2 exact public recovery request required")
        cast(Any, request).__post_init__()
        if cast(Any, request).binding != self.binding:
            raise PolicyViolation("B2 request binding scope drift")

    def _replay_entry(
        self, db: sqlite3.Connection, claim_id: str, route: _EntryRoute
    ) -> RecoveryResult | None:
        case_id = _b2_id("effect-case", claim_id)
        cases = db.execute(
            "select 1 from local_recovery_case where id=? or effect_claim_id=?", (case_id, claim_id)
        ).fetchall()
        if not cases:
            return None
        verify_b1_b2_internal_producers(db, self.binding, selected_b2_claim_id=claim_id)
        case = db.execute("select * from local_recovery_case where id=?", (case_id,)).fetchone()
        receipt = db.execute(
            "select * from local_effect_receipt where claim_id=?", (claim_id,)
        ).fetchone()
        actual_route = "unknown" if receipt is not None else "receiptless"
        if case is None or actual_route != route:
            raise ConcurrencyConflict("B2 recovery entry route conflict")
        outboxes = db.execute(
            "select id from local_outbox where job_id=? and event_kind='job.recovery-required'",
            (case["job_id"],),
        ).fetchall()
        fence = db.execute(
            "select fencing_token from local_effect_claim where id=?", (claim_id,)
        ).fetchone()
        expected_outbox = (
            _b2_id("terminal-outbox", case["job_id"])
            if route == "unknown"
            else _b2_id("recovery-outbox", case["job_id"], fence[0])
        )
        if fence is None or len(outboxes) != 1 or outboxes[0][0] != expected_outbox:
            raise ConcurrencyConflict("B2 recovery entry outbox census drift")
        revision = _entry_revision(db, case_id)
        return entry_result(
            route=route,
            status="replayed",
            claim_id=claim_id,
            recovery_case_id=case_id,
            attachment_revision_digest=str(revision["revision_digest"]),
        )

    def enter_unknown(self, request: UnknownEffectRequest) -> RecoveryResult:
        self._request(request, UnknownEffectRequest)
        case_id = _b2_id("effect-case", request.claim_id)
        with closing(self._connect(read_only=True)) as read:
            read.execute("begin")
            replay = self._replay_entry(read, request.claim_id, "unknown")
            if replay is not None:
                return replay
            baseline = _baseline(read, self.binding, request.claim_id, case_id)
        snapshot = _safe_snapshot(self.unknown_issuer, request, FrozenUnknownEffectSnapshot)
        return self._enter(request, snapshot, baseline=baseline, route="unknown")

    def enter_receiptless(self, request: ReceiptlessRecoveryRequest) -> RecoveryResult:
        self._request(request, ReceiptlessRecoveryRequest)
        case_id = _b2_id("effect-case", request.claim_id)
        with closing(self._connect(read_only=True)) as read:
            read.execute("begin")
            replay = self._replay_entry(read, request.claim_id, "receiptless")
            if replay is not None:
                return replay
            baseline = _baseline(read, self.binding, request.claim_id, case_id)
        snapshot = _safe_snapshot(
            self.receiptless_issuer, request, FrozenReceiptlessRecoverySnapshot
        )
        return self._enter(request, snapshot, baseline=baseline, route="receiptless")

    def _enter(
        self,
        request: _EntryRequest,
        snapshot: _EntrySnapshot,
        *,
        baseline: _CommitBaseline,
        route: _EntryRoute,
    ) -> RecoveryResult:
        if route == "unknown":
            if type(snapshot) is not FrozenUnknownEffectSnapshot:
                raise ValidationFailed("B2 unknown snapshot route drift")
            unknown_commitment = snapshot.unknown_commitment_digest
            recovery_reason = None
        else:
            if type(snapshot) is not FrozenReceiptlessRecoverySnapshot:
                raise ValidationFailed("B2 receiptless snapshot route drift")
            unknown_commitment = None
            recovery_reason = snapshot.recovery_reason
        case_id = _b2_id("effect-case", request.claim_id)
        db = self._connect()
        committing = False
        try:
            db.execute("begin immediate")
            replay = self._replay_entry(db, request.claim_id, route)
            if replay is not None:
                db.commit()
                return replay
            if _baseline(db, self.binding, request.claim_id, case_id) != baseline:
                raise ConcurrencyConflict("B2 recovery entry baseline drift")
            verify_b1_b2_internal_producers(db, self.binding)
            revision = _current_revision(db, self.binding)
            if (
                revision["revision_digest"] != request.expected_revision_digest
                or revision["state"] != "hydrated"
                or any(revision[name] is not None for name in _RECOVERY_FIELDS)
            ):
                raise ConcurrencyConflict("B2 first recovery attachment revision drift")
            claim = db.execute(
                "select * from local_effect_claim where id=?", (request.claim_id,)
            ).fetchone()
            if claim is None:
                raise PolicyViolation("B2 selected effect claim missing")
            job = db.execute("select * from local_job where id=?", (claim["job_id"],)).fetchone()
            lease = db.execute(
                "select * from local_lease where id=?", (claim["lease_id"],)
            ).fetchone()
            bound = db.execute(
                "select * from continuity_effect_binding where claim_id=?", (claim["id"],)
            ).fetchone()
            payload = None if job is None else _job_payload(db, job, self.binding)
            if (
                job is None
                or lease is None
                or bound is None
                or payload is None
                or snapshot.binding_digest != self.binding.binding_digest
                or snapshot.job_id != job["id"]
                or snapshot.claim_id != claim["id"]
                or snapshot.lease_id != lease["id"]
                or snapshot.lease_owner_id != lease["owner_id"]
                or snapshot.lease_owner_pid != lease["owner_pid"]
                or snapshot.lease_owner_token != lease["owner_token"]
                or snapshot.fencing_token != claim["fencing_token"]
                or snapshot.operation != claim["operation"]
                or snapshot.operation != payload["operation"]
                or snapshot.effect_commitment_digest != claim["effect_digest"]
                or snapshot.claimed_at != claim["claimed_at"]
                or job["state"] != "running"
                or bound["session_id"] != self.binding.session_id
            ):
                raise PolicyViolation("B2 fresh recovery authority/runtime drift")
            observed = _whole_second(snapshot.observed_at, "recovery observation")
            claimed = _whole_second(claim["claimed_at"], "selected claim")
            expiry = _runtime_time(lease["expires_at"], "selected lease expiry")
            if (
                claimed > observed
                or (route == "unknown" and observed >= expiry)
                or (
                    route == "receiptless"
                    and recovery_reason == "lease-expired"
                    and observed < expiry
                )
            ):
                raise PolicyViolation("B2 recovery causal authority drift")
            existing_receipts = db.execute(
                "select * from local_effect_receipt where claim_id=?", (claim["id"],)
            ).fetchall()
            if existing_receipts:
                raise ConcurrencyConflict("B2 fresh recovery receipt already exists")
            maximum = db.execute(
                "select max_pending_outbox from local_runtime_config where singleton=1"
            ).fetchone()
            if maximum is None:
                raise PolicyViolation("B2 persisted outbox config missing")
            require_outbox_capacity_tx(db, max_pending_outbox=int(maximum[0]))
            _safe_recheck(
                self.unknown_issuer if route == "unknown" else self.receiptless_issuer,
                snapshot,
            )
            if route == "unknown":
                db.execute(
                    "insert into local_effect_receipt values(?,?,?,?,?)",
                    (
                        _b2_id("unknown-receipt", request.claim_id),
                        request.claim_id,
                        "unknown",
                        unknown_commitment,
                        snapshot.observed_at,
                    ),
                )
                case_evidence = digest(
                    {
                        "case_kind": "effect-unknown",
                        "claim_id": request.claim_id,
                        "receipt_evidence": unknown_commitment,
                    }
                )
                outbox_id = _b2_id("terminal-outbox", job["id"])
                outbox_payload = {"job_id": job["id"], "state": "recovery-required"}
            else:
                case_evidence = digest(
                    {
                        "case_kind": "effect-unknown",
                        "claim_id": request.claim_id,
                        "effect_digest": claim["effect_digest"],
                        "recovered_fence": claim["fencing_token"],
                    }
                )
                outbox_id = _b2_id("recovery-outbox", job["id"], claim["fencing_token"])
                outbox_payload = {
                    "job_id": job["id"],
                    "state": "recovery-required",
                    "fencing_token": claim["fencing_token"],
                }
            case_rows = insert_effect_recovery_case_tx(
                db,
                EffectRecoveryCaseSpec(
                    "unknown-receipt" if route == "unknown" else "sweep-receiptless",
                    case_id,
                    str(job["id"]),
                    request.claim_id,
                    unknown_commitment,
                    None if route == "unknown" else str(claim["effect_digest"]),
                    None if route == "unknown" else int(claim["fencing_token"]),
                    case_evidence,
                    snapshot.observed_at,
                ),
            )
            if (
                not case_rows.inserted
                or case_rows.case_id != case_id
                or case_rows.evidence_digest != case_evidence
            ):
                raise ConcurrencyConflict("B2 recovery case collision")
            locks = tuple(
                tuple(row)
                for row in db.execute(
                    "select resource,job_id,lease_id,fencing_token,acquired_at "
                    "from local_resource_lock where job_id=? order by resource",
                    (job["id"],),
                ).fetchall()
            )
            transition_running_job_to_recovery_tx(
                db,
                RecoveryTransitionSpec(
                    "finish-recovery-required" if route == "unknown" else "sweep-recovery-required",
                    str(job["id"]),
                    str(lease["id"]),
                    int(claim["fencing_token"]),
                    tuple(LockRow(*row) for row in locks),
                    (case_evidence,),
                    digest([case_evidence]),
                    snapshot.observed_at,
                    outbox_id,
                    int(maximum[0]),
                    digest(outbox_payload),
                ),
            )
            revision_body = _next_revision_body(
                revision,
                state="recovery-required",
                operation_key=f"effect-recovery-required:{case_id}",
                created_at=snapshot.observed_at,
                local_case=case_id,
                local_resolution=None,
                crash_event=None,
                crash_receipt=None,
            )
            value = SQLiteDormantV4CloseWriter._insert_revision(db, revision_body)
            verify_b1_b2_internal_producers(db, self.binding, selected_b2_claim_id=request.claim_id)
            committing = True
            self._commit(db)
            return entry_result(
                route=route,
                status="fresh",
                claim_id=request.claim_id,
                recovery_case_id=case_id,
                attachment_revision_digest=value,
            )
        except Exception as exc:
            if db.in_transaction and not committing:
                db.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise ConcurrencyConflict("B2 recovery entry concurrency conflict") from exc
                raise
            if db.in_transaction:
                db.rollback()
            db.close()
            return self._classify_entry(request.claim_id, case_id, route, baseline)
        finally:
            db.close()

    def _classify_entry(
        self, claim_id: str, case_id: str, route: _EntryRoute, baseline: _CommitBaseline
    ) -> RecoveryResult:
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            try:
                replay = self._replay_entry(db, claim_id, route)
            except (ConcurrencyConflict, PolicyViolation, ValidationFailed):
                raise ConcurrencyConflict("B2 partial recovery entry graph") from None
            if replay is not None:
                return replay
            try:
                current = _baseline(db, self.binding, claim_id, case_id)
            except (ConcurrencyConflict, PolicyViolation, ValidationFailed):
                raise ConcurrencyConflict("B2 partial recovery entry graph") from None
            if current != baseline:
                raise ConcurrencyConflict("B2 partial recovery entry graph")
            operation = "unknown-entry" if route == "unknown" else "receiptless-entry"
            return commit_outcome(operation=operation, claim_id=claim_id, recovery_case_id=case_id)

    def resolve(self, request: ResolveEffectRecoveryRequest) -> RecoveryResult:
        self._request(request, ResolveEffectRecoveryRequest)
        with closing(self._connect(read_only=True)) as read:
            read.execute("begin")
            replay = self._replay_resolution(read, request.recovery_case_id)
            if replay is not None:
                return replay
            case = read.execute(
                "select effect_claim_id from local_recovery_case where id=?",
                (request.recovery_case_id,),
            ).fetchone()
            if case is None or case[0] is None:
                raise PolicyViolation("B2 exact recovery case missing")
            claim_id = str(case[0])
            baseline = _baseline(read, self.binding, claim_id, request.recovery_case_id)
        snapshot = _safe_snapshot(self.adjudicator, request, FrozenEffectRecoveryResolutionSnapshot)
        db = self._connect()
        case_id = request.recovery_case_id
        committing = False
        try:
            db.execute("begin immediate")
            replay = self._replay_resolution(db, case_id)
            if replay is not None:
                if replay.body()["job_state"] != snapshot.outcome:
                    raise ConcurrencyConflict("B2 concurrent recovery outcome drift")
                db.commit()
                return replay
            if _baseline(db, self.binding, claim_id, case_id) != baseline:
                raise ConcurrencyConflict("B2 recovery resolution baseline drift")
            case = db.execute("select * from local_recovery_case where id=?", (case_id,)).fetchone()
            if case is None or case["effect_claim_id"] is None:
                raise PolicyViolation("B2 exact recovery case missing")
            claim_id = str(case["effect_claim_id"])
            verify_b1_b2_internal_producers(db, self.binding, selected_b2_claim_id=claim_id)
            revision = _current_revision(db, self.binding)
            if (
                revision["revision_digest"] != request.expected_revision_digest
                or revision["state"] != "recovery-required"
                or revision["local_recovery_case_id"] != case_id
                or self._tail(db) != request.expected_tail
            ):
                raise ConcurrencyConflict("B2 recovery resolution revision/tail drift")
            job = db.execute("select * from local_job where id=?", (case["job_id"],)).fetchone()
            if (
                job is None
                or job["state"] != "recovery-required"
                or snapshot.binding_digest != self.binding.binding_digest
                or snapshot.job_id != job["id"]
                or snapshot.claim_id != claim_id
                or snapshot.recovery_case_id != case_id
            ):
                raise PolicyViolation("B2 recovery adjudication scope drift")
            if (
                db.execute(
                    "select count(*) from local_lease where job_id=?", (job["id"],)
                ).fetchone()[0]
                or db.execute(
                    "select count(*) from local_resource_lock where job_id=?", (job["id"],)
                ).fetchone()[0]
            ):
                raise PolicyViolation("B2 recovery resolution found live authority")
            resolved = _whole_second(snapshot.resolved_at, "recovery resolution")
            if _runtime_time(case["created_at"], "recovery case") > resolved:
                raise PolicyViolation("B2 resolution preceded recovery case")
            maximum = db.execute(
                "select max_pending_outbox from local_runtime_config where singleton=1"
            ).fetchone()
            if maximum is None:
                raise PolicyViolation("B2 persisted outbox config missing")
            require_outbox_capacity_tx(db, max_pending_outbox=int(maximum[0]))
            _safe_recheck(self.adjudicator, snapshot)
            resolution_id = _b2_id("effect-resolution", case_id)
            rows = insert_effect_recovery_resolution_tx(
                db,
                EffectRecoveryResolutionSpec(
                    resolution_id,
                    case_id,
                    snapshot.outcome,
                    snapshot.resolution_commitment_digest,
                    snapshot.resolved_at,
                ),
            )
            if rows.resolution_id != resolution_id:
                raise ConcurrencyConflict("B2 recovery resolution identity drift")
            receipt = db.execute(
                "select * from local_effect_receipt where claim_id=?", (claim_id,)
            ).fetchone()
            terminal = digest(
                [
                    (
                        None if receipt is None else receipt["status"],
                        None if receipt is None else receipt["evidence_digest"],
                        snapshot.outcome,
                        snapshot.resolution_commitment_digest,
                    )
                ]
            )
            payload = {"job_id": job["id"], "state": snapshot.outcome, "reconciled": True}
            reconcile_effect_recovery_job_tx(
                db,
                RecoveryReconcileSpec(
                    str(job["id"]),
                    (case_id,),
                    snapshot.outcome,
                    terminal,
                    snapshot.resolved_at,
                    _b2_id("reconciled-outbox", job["id"]),
                    int(maximum[0]),
                    digest(payload),
                ),
            )
            crash_event = restored = None
            if snapshot.outcome == "completed":
                crash_event, crash_receipt = self._insert_crash(
                    db, revision, request.expected_tail, resolution_id, case_id, snapshot
                )
                restored = SQLiteDormantV4CloseWriter._insert_revision(
                    db,
                    _next_revision_body(
                        revision,
                        state="hydrated",
                        operation_key=f"effect-restored-revision:{case_id}",
                        created_at=snapshot.resolved_at,
                        local_case=case_id,
                        local_resolution=resolution_id,
                        crash_event=crash_event,
                        crash_receipt=crash_receipt,
                    ),
                )
            verify_b1_b2_internal_producers(db, self.binding, selected_b2_claim_id=claim_id)
            committing = True
            self._commit(db)
            return resolution_result(
                status="fresh",
                outcome=snapshot.outcome,
                claim_id=claim_id,
                recovery_case_id=case_id,
                recovery_resolution_id=resolution_id,
                crash_recovered_event_digest=crash_event,
                restored_revision_digest=restored,
            )
        except Exception as exc:
            if db.in_transaction and not committing:
                db.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise ConcurrencyConflict(
                        "B2 recovery resolution concurrency conflict"
                    ) from exc
                raise
            if db.in_transaction:
                db.rollback()
            db.close()
            return self._classify_resolution(case_id, baseline)
        finally:
            db.close()

    def _tail(self, db: sqlite3.Connection) -> ContinuityTail:
        rows = SQLiteDormantV4CloseWriter._events(db, self.binding)
        return ContinuityTail(len(rows), None if not rows else str(rows[-1]["event_digest"]))

    def _insert_crash(
        self,
        db: sqlite3.Connection,
        revision: sqlite3.Row,
        tail: ContinuityTail,
        resolution_id: str,
        case_id: str,
        snapshot: FrozenEffectRecoveryResolutionSnapshot,
    ) -> tuple[str, str]:
        operation = f"effect-crash-recovered:{case_id}"
        event = {
            "kind": "CRASH_RECOVERED",
            "idempotency_key": operation,
            "occurred_at": snapshot.resolved_at,
            "source_refs": [],
            "evidence_digests": [snapshot.resolution_commitment_digest],
            "spool_digest": None,
        }
        sequence = tail.sequence + 1
        value = event_digest(
            self.binding, sequence=sequence, previous_digest=tail.event_digest, event_body=event
        )
        receipt_body = {
            "attachment_revision_digest": revision["revision_digest"],
            "binding_digest": self.binding.binding_digest,
            "created_at": snapshot.resolved_at,
            "event_digest": value,
            "event_kind": "CRASH_RECOVERED",
            "expected_previous_event_digest": tail.event_digest,
            "operation_key": operation,
            "session_id": self.binding.session_id,
        }
        receipt = internal_receipt_digest(
            receipt_body,
            producer_kind="local_recovery_resolution_id",
            producer_ref=resolution_id,
        )
        db.execute(
            "insert into continuity_internal_event_receipt(receipt_digest,event_digest,"
            "session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,local_recovery_resolution_id,"
            "attachment_revision_digest,body_json,created_at) "
            "values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt,
                value,
                self.binding.session_id,
                self.binding.binding_digest,
                "CRASH_RECOVERED",
                operation,
                tail.event_digest,
                resolution_id,
                revision["revision_digest"],
                canonical_json(receipt_body),
                snapshot.resolved_at,
            ),
        )
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (
                _b2_id("event", value),
                self.binding.session_id,
                "CRASH_RECOVERED",
                value,
                snapshot.resolved_at,
            ),
        )
        envelope = {
            "session_id": self.binding.session_id,
            "binding_digest": self.binding.binding_digest,
            "sequence": sequence,
            "previous_digest": tail.event_digest,
            "event": event,
        }
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
            (
                _b2_id("event", value),
                self.binding.session_id,
                sequence,
                tail.event_digest,
                operation,
                value,
                None,
                canonical_json(envelope),
            ),
        )
        return value, receipt

    def _replay_resolution(self, db: sqlite3.Connection, case_id: str) -> RecoveryResult | None:
        case = db.execute("select * from local_recovery_case where id=?", (case_id,)).fetchone()
        resolutions = db.execute(
            "select * from local_recovery_resolution where recovery_case_id=?", (case_id,)
        ).fetchall()
        if not resolutions:
            return None
        if case is None or len(resolutions) != 1 or case["effect_claim_id"] is None:
            raise ConcurrencyConflict("B2 partial recovery resolution graph")
        claim_id = str(case["effect_claim_id"])
        verify_b1_b2_internal_producers(db, self.binding, selected_b2_claim_id=claim_id)
        resolution = resolutions[0]
        if resolution["outcome"] == "failed":
            return resolution_result(
                status="replayed",
                outcome="failed",
                claim_id=claim_id,
                recovery_case_id=case_id,
                recovery_resolution_id=str(resolution["id"]),
            )
        current = _current_revision(db, self.binding)
        return resolution_result(
            status="replayed",
            outcome="completed",
            claim_id=claim_id,
            recovery_case_id=case_id,
            recovery_resolution_id=str(resolution["id"]),
            crash_recovered_event_digest=str(current["crash_recovered_event_digest"]),
            restored_revision_digest=str(current["revision_digest"]),
        )

    def _classify_resolution(self, case_id: str, baseline: _CommitBaseline) -> RecoveryResult:
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            try:
                replay = self._replay_resolution(db, case_id)
            except (ConcurrencyConflict, PolicyViolation, ValidationFailed):
                raise ConcurrencyConflict("B2 partial recovery resolution graph") from None
            if replay is not None:
                return replay
            case = db.execute(
                "select effect_claim_id from local_recovery_case where id=?", (case_id,)
            ).fetchone()
            if case is None or case[0] is None:
                raise ConcurrencyConflict("B2 recovery resolution case disappeared")
            claim_id = str(case[0])
            try:
                current = _baseline(db, self.binding, claim_id, case_id)
            except (ConcurrencyConflict, PolicyViolation, ValidationFailed):
                raise ConcurrencyConflict("B2 partial recovery resolution graph") from None
            if current != baseline:
                raise ConcurrencyConflict("B2 partial recovery resolution graph")
            return commit_outcome(operation="resolve", claim_id=claim_id, recovery_case_id=case_id)
