"""Crash-safe SQLite adapter for the local runtime contracts."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from zekam.application.local_runtime import (
    RESERVED_JOB_OPERATIONS,
    LocalClaim,
    LocalClaimedWork,
    LocalJob,
    LocalLease,
    LocalOutboxClaim,
    LocalOutboxEvent,
    LocalReceipt,
    LocalRecoveryCase,
    LocalRecoveryResolution,
    LocalRuntimeStatus,
    RecoverySweep,
    validate_job_operations,
    validate_outbox_kinds,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
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
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, bootstrap, status

# Matches str.strip() used by the legacy worker before invoking its executor.
# Padding must not disguise a reserved operation as an ordinary legacy job.
_OPERATION_WHITESPACE = (
    " \t\n\r\v\f\x1c\x1d\x1e\x1f\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _moment(value: str | None = None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed("Local runtime timestamp ISO-8601 olmali")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailed("Local runtime timestamp ISO-8601 olmali") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationFailed("Local runtime timestamp timezone tasimali")
    return parsed.astimezone(dt.UTC)


def _text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValidationFailed(f"{name} bos/gecersiz")
    return value.strip()


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{name} digest metin olmali")
    parse_digest(value)
    return value


def _bounded_int(value: int, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationFailed(f"{name} {minimum}..{maximum} olmali")
    return value


def _payload_json(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ValidationFailed("Local payload object olmali")
    try:
        encoded = canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Local payload canonical JSON olmali") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValidationFailed("Local payload 1 MiB sinirini asiyor")
    return encoded


def _process_probe_value(
    process_token_for: Callable[[int], str | None],
    owner_pid: int,
) -> str | None:
    observed = process_token_for(owner_pid)
    if observed is not None and (
        not isinstance(observed, str)
        or not observed.strip()
        or observed != observed.strip()
        or len(observed) > 512
    ):
        raise ValidationFailed("Process token probe string veya null donmeli")
    return observed


class SQLiteLocalRuntimeStore:
    def __init__(
        self,
        path: Path,
        *,
        max_pending_outbox: int | None = None,
        existing_only: bool = False,
    ) -> None:
        if type(existing_only) is not bool:
            raise ValidationFailed("Local runtime existing_only bool olmali")
        if max_pending_outbox is not None:
            _bounded_int(
                max_pending_outbox,
                "Max pending outbox",
                minimum=1,
                maximum=100_000,
            )
        self.path = path
        self.existing_only = existing_only
        if existing_only:
            observed = status(path)
            if not (
                observed.exists
                and observed.schema_version == SCHEMA_VERSION
                and observed.integrity_ok
                and observed.schema_ok
            ):
                raise PolicyViolation("Local runtime requires existing current operational schema")
        else:
            bootstrap(path)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select max_pending_outbox from local_runtime_config where singleton=1"
            ).fetchone()
            if row is None:
                if existing_only:
                    raise PolicyViolation("Local runtime requires existing admitted config")
                configured = 1000 if max_pending_outbox is None else max_pending_outbox
                connection.execute("insert into local_runtime_config values(1,?)", (configured,))
            else:
                if existing_only:
                    _bounded_int(
                        row["max_pending_outbox"],
                        "Persisted max pending outbox",
                        minimum=1,
                        maximum=100_000,
                    )
                configured = int(row["max_pending_outbox"])
                if max_pending_outbox is not None and max_pending_outbox != configured:
                    raise PolicyViolation("Persisted max pending outbox config drift")
            connection.commit()
            self.max_pending_outbox = configured
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self) -> LocalRuntimeStatus:
        connection = self._connect()
        try:
            jobs = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "select state,count(*) as count from local_job group by state"
                ).fetchall()
            }
            outbox = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "select state,count(*) as count from local_outbox_delivery group by state"
                ).fetchall()
            }
            cases = int(
                connection.execute(
                    "select count(*) from local_recovery_case where state='open'"
                ).fetchone()[0]
            )
            return LocalRuntimeStatus(
                jobs.get("ready", 0),
                jobs.get("running", 0),
                jobs.get("recovery-required", 0),
                jobs.get("quarantined", 0),
                outbox.get("pending", 0),
                outbox.get("claimed", 0),
                outbox.get("recovery-required", 0),
                cases,
            )
        finally:
            connection.close()

    def recovery_cases(self, *, open_only: bool = True) -> tuple[LocalRecoveryCase, ...]:
        if not isinstance(open_only, bool):
            raise ValidationFailed("Recovery open_only bool olmali")
        connection = self._connect()
        try:
            where = " where state='open'" if open_only else ""
            rows = connection.execute(
                "select id,job_id,case_kind,evidence_digest,state from local_recovery_case"
                + where
                + " order by created_at,id"
            ).fetchall()
            return tuple(
                LocalRecoveryCase(
                    str(row["id"]),
                    str(row["job_id"]),
                    row["case_kind"],
                    str(row["evidence_digest"]),
                    row["state"],
                )
                for row in rows
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        # Existing-state callers must never recreate a database lost after admission.
        connection = (
            sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5.0)
            if self.existing_only
            else sqlite3.connect(self.path, timeout=5.0)
        )
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        if connection.execute("pragma foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise PolicyViolation("SQLite foreign key enforcement acilamadi")
        connection.execute("pragma busy_timeout=5000")
        return connection

    def _emit_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        created_at: str,
    ) -> bool:
        payload_json = _payload_json(payload)
        pending = int(
            connection.execute(
                "select count(*) from local_outbox_delivery where state in"
                " ('pending','claimed','recovery-required')"
            ).fetchone()[0]
        )
        if pending >= self.max_pending_outbox:
            raise PolicyViolation("Local outbox backpressure limit dolu")
        outbox_id = str(new_uuid7())
        connection.execute(
            "insert into local_outbox(id,job_id,idempotency_key,event_kind,payload_json,"
            "payload_digest,created_at) values(?,?,?,?,?,?,?)",
            (
                outbox_id,
                job_id,
                _required(idempotency_key, "Outbox idempotency key"),
                _required(event_kind, "Outbox event kind"),
                payload_json,
                digest(payload),
                created_at,
            ),
        )
        connection.execute(
            "insert into local_outbox_delivery(outbox_id,state,updated_at) values(?,'pending',?)",
            (outbox_id, created_at),
        )
        return True

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        key: str,
        payload_json: str,
        max_attempts: int,
        available_at: str,
        timeout_at: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,"
            "max_attempts,available_at,timeout_at,created_at,updated_at)"
            " values(?,?,?,'ready',0,?,?,?,?,?)",
            (
                job_id,
                key,
                payload_json,
                max_attempts,
                available_at,
                timeout_at,
                created_at,
                created_at,
            ),
        )
        self._emit_outbox(
            connection,
            job_id=job_id,
            event_kind="job.enqueued",
            payload={"job_id": job_id, "idempotency_key": key},
            idempotency_key=f"job:{job_id}:enqueued",
            created_at=created_at,
        )

    def enqueue(
        self,
        *,
        idempotency_key: str,
        payload: dict[str, Any],
        max_attempts: int = 1,
        available_at: str | None = None,
        timeout_at: str | None = None,
    ) -> tuple[LocalJob, bool]:
        key = _required(idempotency_key, "Idempotency key")
        _bounded_int(max_attempts, "Local job max_attempts", minimum=1, maximum=100)
        available = _moment(available_at)
        timeout = None if timeout_at is None else _moment(timeout_at)
        timeout_text = None if timeout is None else _text(timeout)
        if timeout is not None and timeout <= available:
            raise ValidationFailed("Local job timeout available_at sonrasinda olmali")
        payload_json = _payload_json(payload)
        job_id = str(new_uuid7())
        now = _text(dt.datetime.now(dt.UTC))
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select * from local_job where idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] != payload_json
                    or int(existing["max_attempts"]) != max_attempts
                    or (available_at is not None and existing["available_at"] != _text(available))
                    or (timeout_at is not None and existing["timeout_at"] != timeout_text)
                ):
                    raise ConcurrencyConflict("Local job idempotency replay payload drift")
                connection.commit()
                return _job(existing), False
            self._insert_job(
                connection,
                job_id=job_id,
                key=key,
                payload_json=payload_json,
                max_attempts=max_attempts,
                available_at=_text(available),
                timeout_at=timeout_text,
                created_at=now,
            )
            row = connection.execute("select * from local_job where id=?", (job_id,)).fetchone()
            connection.commit()
            return _job(row), True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next(
        self,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int,
        resources: tuple[str, ...] = (),
        supported_operations: tuple[str, ...] | None = None,
        job_id: str | None = None,
        now: str | None = None,
    ) -> LocalClaimedWork | None:
        operations = (
            None if supported_operations is None else validate_job_operations(supported_operations)
        )
        if job_id is not None and (
            operations is None or _required(job_id, "Exact job id") != job_id
        ):
            raise ValidationFailed("Exact job id explicit supported operations ister")
        owner = _required(owner_id, "Owner")
        _bounded_int(owner_pid, "Owner PID", minimum=1, maximum=2_147_483_647)
        token = _required(owner_token, "Owner token")
        _bounded_int(lease_seconds, "Lease suresi", minimum=1, maximum=3600)
        normalized = tuple(sorted({_required(item, "Resource") for item in resources}))
        if len(normalized) != len(resources):
            raise ValidationFailed("Resource listesi duplicate tasiyamaz")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            self._recover(connection, moment)
            parameters: tuple[object, ...] = (_text(moment), _text(moment))
            if operations is None:
                placeholders = ",".join("?" for _ in RESERVED_JOB_OPERATIONS)
                selection = (
                    " and (json_type(payload_json,'$.operation') is not 'text'"
                    f" or trim(json_extract(payload_json,'$.operation'), ?)"
                    f" not in ({placeholders}))"
                )
                parameters += (_OPERATION_WHITESPACE, *RESERVED_JOB_OPERATIONS)
            else:
                placeholders = ",".join("?" for _ in operations)
                selection = (
                    " and json_type(payload_json,'$.operation')='text'"
                    f" and json_extract(payload_json,'$.operation') in ({placeholders})"
                )
                parameters += operations
            if job_id is not None:
                selection += " and id=?"
                parameters += (job_id,)
            row = connection.execute(
                "select * from local_job where state='ready' and available_at<=?"
                " and (timeout_at is null or timeout_at>?) and attempt_count<max_attempts"
                + selection
                + " order by available_at,created_at,id limit 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            fencing = int(row["fencing_counter"]) + 1
            lease_id = str(new_uuid7())
            expires = moment + dt.timedelta(seconds=lease_seconds)
            connection.execute(
                "update local_job set state='running',attempt_count=attempt_count+1,"
                " fencing_counter=?,updated_at=? where id=? and state='ready'",
                (fencing, _text(moment), row["id"]),
            )
            connection.execute(
                "insert into local_lease values(?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    row["id"],
                    owner,
                    owner_pid,
                    token,
                    fencing,
                    _text(moment),
                    _text(expires),
                ),
            )
            for resource in normalized:
                connection.execute(
                    "insert into local_resource_lock values(?,?,?,?,?)",
                    (resource, row["id"], lease_id, fencing, _text(moment)),
                )
            job_row = connection.execute(
                "select * from local_job where id=?", (row["id"],)
            ).fetchone()
            lease_row = connection.execute(
                "select * from local_lease where id=?", (lease_id,)
            ).fetchone()
            connection.commit()
            return LocalClaimedWork(_job(job_row), _lease(lease_row))
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "local_resource_lock.resource" in str(exc):
                return None
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(
        self,
        lease_id: str,
        *,
        owner_id: str,
        owner_token: str,
        fencing_token: int,
        lease_seconds: int,
        now: str | None = None,
    ) -> LocalLease:
        lease_key = _required(lease_id, "Lease id")
        owner = _required(owner_id, "Owner")
        token = _required(owner_token, "Owner token")
        _bounded_int(fencing_token, "Fencing token", minimum=1, maximum=2_147_483_647)
        _bounded_int(lease_seconds, "Lease suresi", minimum=1, maximum=3600)
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from local_lease where id=?", (lease_key,)
            ).fetchone()
            if row is None:
                raise NotFound("Local lease bulunamadi")
            if (
                row["owner_id"] != owner
                or row["owner_token"] != token
                or int(row["fencing_token"]) != fencing_token
                or _moment(row["expires_at"]) <= moment
            ):
                raise ConcurrencyConflict("Local lease owner/fence/expiry drift")
            expires = moment + dt.timedelta(seconds=lease_seconds)
            connection.execute(
                "update local_lease set heartbeat_at=?,expires_at=? where id=?",
                (_text(moment), _text(expires), lease_key),
            )
            updated = connection.execute(
                "select * from local_lease where id=?", (lease_key,)
            ).fetchone()
            connection.commit()
            return _lease(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_effect(
        self,
        work: LocalClaimedWork,
        *,
        operation: str,
        effect_digest: str,
        idempotency_key: str,
        now: str | None = None,
    ) -> tuple[LocalClaim, bool]:
        effect = _digest(effect_digest, "Effect")
        operation_key = _required(operation, "Operation")
        idempotency = _required(idempotency_key, "Effect idempotency key")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            lease = connection.execute(
                "select * from local_lease where id=?", (work.lease.id,)
            ).fetchone()
            if (
                lease is None
                or lease["job_id"] != work.job.id
                or lease["owner_id"] != work.lease.owner_id
                or lease["owner_token"] != work.lease.owner_token
                or int(lease["fencing_token"]) != work.lease.fencing_token
                or _moment(lease["expires_at"]) <= moment
            ):
                raise ConcurrencyConflict("Effect claim current lease/fence ister")
            existing = connection.execute(
                "select * from local_effect_claim where idempotency_key=?",
                (idempotency,),
            ).fetchone()
            if existing is not None:
                expected = (work.job.id, operation_key, effect)
                actual = (existing["job_id"], existing["operation"], existing["effect_digest"])
                if actual != expected:
                    raise ConcurrencyConflict("Effect claim idempotency drift")
                connection.commit()
                return _claim(existing), False
            claim_id = str(new_uuid7())
            connection.execute(
                "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    work.job.id,
                    work.lease.id,
                    work.lease.fencing_token,
                    operation_key,
                    effect,
                    idempotency,
                    _text(moment),
                ),
            )
            row = connection.execute(
                "select * from local_effect_claim where id=?", (claim_id,)
            ).fetchone()
            connection.commit()
            return _claim(row), True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_receipt(
        self,
        claim: LocalClaim,
        *,
        status: Literal["completed", "failed", "unknown"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalReceipt:
        if status not in {"completed", "failed", "unknown"}:
            raise ValidationFailed("Local receipt status gecersiz")
        evidence = _digest(evidence_digest, "Receipt evidence")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            stored = connection.execute(
                "select * from local_effect_claim where id=?", (claim.id,)
            ).fetchone()
            if stored is None or _claim(stored) != claim:
                raise ConcurrencyConflict("Local receipt exact claim drift")
            existing = connection.execute(
                "select * from local_effect_receipt where claim_id=?", (claim.id,)
            ).fetchone()
            if existing is not None:
                if (existing["status"], existing["evidence_digest"]) != (
                    status,
                    evidence,
                ):
                    raise ConcurrencyConflict("Local receipt replay drift")
                connection.commit()
                return _receipt(existing)
            receipt_id = str(new_uuid7())
            connection.execute(
                "insert into local_effect_receipt values(?,?,?,?,?)",
                (receipt_id, claim.id, status, evidence, _text(moment)),
            )
            if status == "unknown":
                case_evidence = digest(
                    {
                        "case_kind": "effect-unknown",
                        "claim_id": claim.id,
                        "receipt_evidence": evidence,
                    }
                )
                insert_effect_recovery_case_tx(
                    connection,
                    EffectRecoveryCaseSpec(
                        "unknown-receipt",
                        str(new_uuid7()),
                        claim.job_id,
                        claim.id,
                        evidence,
                        None,
                        None,
                        case_evidence,
                        _text(moment),
                    ),
                )
            row = connection.execute(
                "select * from local_effect_receipt where id=?", (receipt_id,)
            ).fetchone()
            connection.commit()
            return _receipt(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish(
        self,
        work: LocalClaimedWork,
        *,
        state: Literal["completed", "failed", "recovery-required"],
        evidence_digest: str | None = None,
        now: str | None = None,
    ) -> LocalJob:
        if state not in {"completed", "failed", "recovery-required"}:
            raise ValidationFailed("Local terminal state gecersiz")
        terminal_evidence = None
        if state in {"completed", "failed"}:
            if evidence_digest is None:
                raise PolicyViolation("Terminal local job evidence digest gerektirir")
            terminal_evidence = _digest(evidence_digest, "Terminal job evidence")
        elif evidence_digest is not None:
            raise ValidationFailed("Recovery-required evidence recovery case tarafindan uretilir")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            lease = connection.execute(
                "select * from local_lease where id=?", (work.lease.id,)
            ).fetchone()
            if (
                lease is None
                or lease["job_id"] != work.job.id
                or lease["owner_id"] != work.lease.owner_id
                or lease["owner_token"] != work.lease.owner_token
                or int(lease["fencing_token"]) != work.lease.fencing_token
                or _moment(lease["expires_at"]) <= moment
            ):
                raise ConcurrencyConflict("Local finish current fence ister")
            unresolved = int(
                connection.execute(
                    "select count(*) from local_effect_claim c left join local_effect_receipt r"
                    " on r.claim_id=c.id where c.job_id=? and"
                    " (r.id is null or r.status='unknown')",
                    (work.job.id,),
                ).fetchone()[0]
            )
            if unresolved and state != "recovery-required":
                raise PolicyViolation("Unresolved claim yalniz recovery-required olabilir")
            if not unresolved and state == "recovery-required":
                raise PolicyViolation("Recovery-required receiptless claim ister")
            failed_receipt = bool(
                connection.execute(
                    "select 1 from local_effect_receipt r join local_effect_claim c"
                    " on c.id=r.claim_id where c.job_id=? and r.status='failed' limit 1",
                    (work.job.id,),
                ).fetchone()
            )
            if failed_receipt and state == "completed":
                raise PolicyViolation("Failed effect receipt completed job olamaz")
            if unresolved:
                open_cases = connection.execute(
                    "select evidence_digest from local_recovery_case where job_id=?"
                    " and state='open' order by id",
                    (work.job.id,),
                ).fetchall()
                if not open_cases:
                    claims = connection.execute(
                        "select c.id,c.effect_digest from local_effect_claim c"
                        " left join local_effect_receipt r on r.claim_id=c.id"
                        " where c.job_id=? and (r.id is null or r.status='unknown') order by c.id",
                        (work.job.id,),
                    ).fetchall()
                    for claim_row in claims:
                        case_evidence = digest(
                            {
                                "case_kind": "effect-unknown",
                                "claim_id": claim_row["id"],
                                "effect_digest": claim_row["effect_digest"],
                            }
                        )
                        insert_effect_recovery_case_tx(
                            connection,
                            EffectRecoveryCaseSpec(
                                "finish-receiptless",
                                str(new_uuid7()),
                                work.job.id,
                                str(claim_row["id"]),
                                None,
                                str(claim_row["effect_digest"]),
                                None,
                                case_evidence,
                                _text(moment),
                            ),
                        )
                    open_cases = connection.execute(
                        "select evidence_digest from local_recovery_case where job_id=?"
                        " and state='open' order by id",
                        (work.job.id,),
                    ).fetchall()
                terminal_evidence = digest([row["evidence_digest"] for row in open_cases])
                require_outbox_capacity_tx(connection, max_pending_outbox=self.max_pending_outbox)
                outbox_id = str(new_uuid7())
                lock_rows = tuple(
                    tuple(row)
                    for row in connection.execute(
                        "select resource,job_id,lease_id,fencing_token,acquired_at "
                        "from local_resource_lock where job_id=? order by resource",
                        (work.job.id,),
                    ).fetchall()
                )
                payload = {"job_id": work.job.id, "state": "recovery-required"}
                transition_running_job_to_recovery_tx(
                    connection,
                    RecoveryTransitionSpec(
                        "finish-recovery-required",
                        work.job.id,
                        work.lease.id,
                        work.lease.fencing_token,
                        tuple(LockRow(*row) for row in lock_rows),
                        tuple(str(row["evidence_digest"]) for row in open_cases),
                        terminal_evidence,
                        _text(moment),
                        outbox_id,
                        self.max_pending_outbox,
                        digest(payload),
                    ),
                )
                row = connection.execute(
                    "select * from local_job where id=?", (work.job.id,)
                ).fetchone()
                connection.commit()
                return _job(row)
            connection.execute("delete from local_resource_lock where lease_id=?", (work.lease.id,))
            connection.execute("delete from local_lease where id=?", (work.lease.id,))
            updated = connection.execute(
                "update local_job set state=?,terminal_evidence_digest=?,updated_at=?"
                " where id=? and state='running'",
                (state, terminal_evidence, _text(moment), work.job.id),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict("Local finish running job ister")
            self._emit_outbox(
                connection,
                job_id=work.job.id,
                event_kind=f"job.{state}",
                payload={"job_id": work.job.id, "state": state},
                idempotency_key=f"job:{work.job.id}:terminal",
                created_at=_text(moment),
            )
            row = connection.execute(
                "select * from local_job where id=?", (work.job.id,)
            ).fetchone()
            connection.commit()
            return _job(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired(self, *, now: str | None = None) -> RecoverySweep:
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            result = self._recover(connection, _moment(now))
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_orphans(
        self,
        process_token_for: Callable[[int], str | None],
        *,
        now: str | None = None,
    ) -> RecoverySweep:
        if not callable(process_token_for):
            raise ValidationFailed("Process token probe callable olmali")
        snapshot = self._connect()
        try:
            leases = snapshot.execute(
                "select id,owner_pid,owner_token from local_lease order by id"
            ).fetchall()
        finally:
            snapshot.close()
        orphan_ids: list[str] = []
        for lease in leases:
            observed = _process_probe_value(process_token_for, int(lease["owner_pid"]))
            if observed != lease["owner_token"]:
                orphan_ids.append(str(lease["id"]))
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            result = self._recover(
                connection,
                _moment(now),
                orphan_lease_ids=tuple(orphan_ids),
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover(
        self,
        connection: sqlite3.Connection,
        moment: dt.datetime,
        *,
        orphan_lease_ids: tuple[str, ...] = (),
    ) -> RecoverySweep:
        rows = connection.execute(
            "select * from local_lease where expires_at<=? order by expires_at,id",
            (_text(moment),),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        for lease_id in orphan_lease_ids:
            if lease_id not in by_id:
                row = connection.execute(
                    "select * from local_lease where id=?", (lease_id,)
                ).fetchone()
                if row is not None:
                    by_id[lease_id] = row
        rows = sorted(by_id.values(), key=lambda row: (row["expires_at"], row["id"]))
        requeued = recovery = locks = finalized = timed_out_leases = 0
        for lease in rows:
            lock_rows = tuple(
                tuple(row)
                for row in connection.execute(
                    "select resource,job_id,lease_id,fencing_token,acquired_at "
                    "from local_resource_lock where lease_id=? order by resource",
                    (lease["id"],),
                ).fetchall()
            )
            locks += len(lock_rows)
            unresolved_claims = connection.execute(
                "select c.id,c.effect_digest,r.status,r.evidence_digest as receipt_evidence,"
                "r.created_at as receipt_created_at,rc.id as case_id,"
                "rc.evidence_digest as case_evidence,rc.created_at as case_created_at "
                "from local_effect_claim c"
                " left join local_effect_receipt r on r.claim_id=c.id"
                " left join local_recovery_case rc on rc.effect_claim_id=c.id"
                " where c.job_id=? and (r.id is null or r.status='unknown') order by c.id",
                (lease["job_id"],),
            ).fetchall()
            claim_count = int(
                connection.execute(
                    "select count(*) from local_effect_claim where job_id=?",
                    (lease["job_id"],),
                ).fetchone()[0]
            )
            terminal_evidence: str | None = None
            if unresolved_claims:
                state = "recovery-required"
                case_digests: list[str] = []
                for claim_row in unresolved_claims:
                    if claim_row["status"] == "unknown":
                        # Preserve the legacy UUID stream: the pre-B2 sweep allocated a
                        # candidate case id before its insert-or-ignore replay check.
                        str(new_uuid7())
                        stored_case_evidence = digest(
                            {
                                "case_kind": "effect-unknown",
                                "claim_id": claim_row["id"],
                                "receipt_evidence": claim_row["receipt_evidence"],
                            }
                        )
                        if (
                            claim_row["case_id"] is None
                            or claim_row["case_evidence"] != stored_case_evidence
                            or claim_row["case_created_at"] != claim_row["receipt_created_at"]
                        ):
                            raise ConcurrencyConflict("Unknown receipt recovery case drift")
                        insert_effect_recovery_case_tx(
                            connection,
                            EffectRecoveryCaseSpec(
                                "unknown-receipt",
                                str(claim_row["case_id"]),
                                str(lease["job_id"]),
                                str(claim_row["id"]),
                                str(claim_row["receipt_evidence"]),
                                None,
                                None,
                                stored_case_evidence,
                                str(claim_row["case_created_at"]),
                            ),
                        )
                        case_evidence = digest(
                            {
                                "case_kind": "effect-unknown",
                                "claim_id": claim_row["id"],
                                "effect_digest": claim_row["effect_digest"],
                                "recovered_fence": int(lease["fencing_token"]),
                            }
                        )
                        case_digests.append(case_evidence)
                        continue
                    case_evidence = digest(
                        {
                            "case_kind": "effect-unknown",
                            "claim_id": claim_row["id"],
                            "effect_digest": claim_row["effect_digest"],
                            "recovered_fence": int(lease["fencing_token"]),
                        }
                    )
                    insert_effect_recovery_case_tx(
                        connection,
                        EffectRecoveryCaseSpec(
                            "sweep-receiptless",
                            str(new_uuid7()),
                            str(lease["job_id"]),
                            str(claim_row["id"]),
                            None,
                            str(claim_row["effect_digest"]),
                            int(lease["fencing_token"]),
                            case_evidence,
                            _text(moment),
                        ),
                    )
                    case_digests.append(case_evidence)
                terminal_evidence = digest(case_digests)
                require_outbox_capacity_tx(connection, max_pending_outbox=self.max_pending_outbox)
                outbox_id = str(new_uuid7())
                payload = {
                    "job_id": str(lease["job_id"]),
                    "state": "recovery-required",
                    "fencing_token": int(lease["fencing_token"]),
                }
                transition_running_job_to_recovery_tx(
                    connection,
                    RecoveryTransitionSpec(
                        "sweep-recovery-required",
                        str(lease["job_id"]),
                        str(lease["id"]),
                        int(lease["fencing_token"]),
                        tuple(LockRow(*row) for row in lock_rows),
                        tuple(case_digests),
                        terminal_evidence,
                        _text(moment),
                        outbox_id,
                        self.max_pending_outbox,
                        digest(payload),
                    ),
                )
                recovery += 1
                continue
            elif claim_count:
                receipts = connection.execute(
                    "select r.status,r.evidence_digest from local_effect_receipt r"
                    " join local_effect_claim c on c.id=r.claim_id where c.job_id=?"
                    " order by r.id",
                    (lease["job_id"],),
                ).fetchall()
                failed_receipts = sum(row["status"] == "failed" for row in receipts)
                state = "failed" if failed_receipts else "completed"
                terminal_evidence = digest(
                    [(row["status"], row["evidence_digest"]) for row in receipts]
                )
                connection.execute(
                    "update local_job set state=?,terminal_evidence_digest=?,updated_at=?"
                    " where id=?",
                    (state, terminal_evidence, _text(moment), lease["job_id"]),
                )
                finalized += 1
            else:
                job_row = connection.execute(
                    "select attempt_count,max_attempts,timeout_at from local_job where id=?",
                    (lease["job_id"],),
                ).fetchone()
                timed_out = bool(
                    job_row["timeout_at"] is not None and _moment(job_row["timeout_at"]) <= moment
                )
                exhausted = int(job_row["attempt_count"]) >= int(job_row["max_attempts"])
                state = "failed" if timed_out or exhausted else "ready"
                timed_out_leases += int(timed_out)
                if state == "failed":
                    terminal_evidence = digest(
                        {
                            "reason": "timeout" if timed_out else "attempts-exhausted",
                            "job_id": lease["job_id"],
                            "fencing_token": int(lease["fencing_token"]),
                        }
                    )
                connection.execute(
                    "update local_job set state=?,terminal_evidence_digest=?,updated_at=?"
                    " where id=?",
                    (state, terminal_evidence, _text(moment), lease["job_id"]),
                )
                requeued += 1
            connection.execute("delete from local_resource_lock where lease_id=?", (lease["id"],))
            connection.execute("delete from local_lease where id=?", (lease["id"],))
            self._emit_outbox(
                connection,
                job_id=str(lease["job_id"]),
                event_kind=f"job.{state}",
                payload={
                    "job_id": str(lease["job_id"]),
                    "state": state,
                    "fencing_token": int(lease["fencing_token"]),
                },
                idempotency_key=(
                    f"job:{lease['job_id']}:recovery:{lease['fencing_token']}:{state}"
                ),
                created_at=_text(moment),
            )
        ready_timeouts = connection.execute(
            "select id from local_job where state='ready' and timeout_at is not null"
            " and timeout_at<=? order by id",
            (_text(moment),),
        ).fetchall()
        for job in ready_timeouts:
            timeout_evidence = digest(
                {"reason": "ready-timeout", "job_id": job["id"], "at": _text(moment)}
            )
            connection.execute(
                "update local_job set state='failed',terminal_evidence_digest=?,updated_at=?"
                " where id=? and state='ready'",
                (timeout_evidence, _text(moment), job["id"]),
            )
            self._emit_outbox(
                connection,
                job_id=str(job["id"]),
                event_kind="job.failed",
                payload={
                    "job_id": str(job["id"]),
                    "state": "failed",
                    "reason": "ready-timeout",
                },
                idempotency_key=f"job:{job['id']}:ready-timeout",
                created_at=_text(moment),
            )
        ready_timeout_count = len(ready_timeouts)
        return RecoverySweep(
            requeued,
            recovery,
            locks,
            finalized,
            timed_out_leases + ready_timeout_count,
        )

    def schedule_once(
        self,
        *,
        slot_key: str,
        schedule_digest: str,
        idempotency_key: str,
        payload: dict[str, Any],
        now: str | None = None,
    ) -> tuple[LocalJob, bool]:
        _digest(schedule_digest, "Schedule")
        slot = _required(slot_key, "Scheduler slot")
        key = _required(idempotency_key, "Idempotency key")
        payload_json = _payload_json(payload)
        moment = _text(_moment(now))
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select schedule_digest,job_id from local_scheduler_slot where slot_key=?",
                (slot,),
            ).fetchone()
            if row is not None:
                if row["schedule_digest"] != schedule_digest:
                    raise ConcurrencyConflict("Scheduler slot digest drift")
                job = connection.execute(
                    "select * from local_job where id=?", (row["job_id"],)
                ).fetchone()
                if job is None:
                    raise ConcurrencyConflict("Scheduler slot orphan job")
                if job["idempotency_key"] != key or job["payload_json"] != payload_json:
                    raise ConcurrencyConflict("Scheduler slot replay payload drift")
                connection.commit()
                return _job(job), False
            collision = connection.execute(
                "select id from local_job where idempotency_key=?", (key,)
            ).fetchone()
            if collision is not None:
                raise ConcurrencyConflict("Scheduler job idempotency key zaten kullanilmis")
            job_id = str(new_uuid7())
            self._insert_job(
                connection,
                job_id=job_id,
                key=key,
                payload_json=payload_json,
                max_attempts=1,
                available_at=moment,
                timeout_at=None,
                created_at=moment,
            )
            connection.execute(
                "insert into local_scheduler_slot values(?,?,?,?)",
                (slot, schedule_digest, job_id, moment),
            )
            stored = connection.execute("select * from local_job where id=?", (job_id,)).fetchone()
            connection.commit()
            return _job(stored), True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_outbox(self, *, limit: int = 100) -> tuple[LocalOutboxEvent, ...]:
        _bounded_int(limit, "Outbox limit", minimum=1, maximum=1000)
        connection = self._connect()
        try:
            rows = connection.execute(
                "select o.*,d.state from local_outbox o join local_outbox_delivery d"
                " on d.outbox_id=o.id where d.state='pending' order by o.created_at,o.id limit ?",
                (limit,),
            ).fetchall()
            return tuple(_outbox(row) for row in rows)
        finally:
            connection.close()

    def claim_outbox(
        self,
        *,
        supported_kinds: tuple[str, ...],
        outbox_id: str | None = None,
        require_completed_job: bool = False,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int,
        now: str | None = None,
    ) -> LocalOutboxClaim | None:
        kinds = validate_outbox_kinds(supported_kinds)
        if type(require_completed_job) is not bool:
            raise ValidationFailed("Outbox completed job selector exact bool required")
        if outbox_id is not None and _required(outbox_id, "Exact outbox id") != outbox_id:
            raise ValidationFailed("Outbox id exact required")
        owner = _required(owner_id, "Outbox owner")
        token = _required(owner_token, "Outbox owner token")
        _bounded_int(owner_pid, "Outbox owner PID", minimum=1, maximum=2_147_483_647)
        _bounded_int(lease_seconds, "Outbox lease", minimum=1, maximum=3600)
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            self._recover_outbox(connection, moment)
            placeholders = ",".join("?" for _ in kinds)
            selection = ""
            parameters: tuple[object, ...] = kinds
            if outbox_id is not None:
                selection += " and o.id=?"
                parameters += (outbox_id,)
            if require_completed_job:
                selection += " and o.job_id in (select id from local_job where state='completed')"
            row = connection.execute(
                "select o.*,d.state,d.fencing_counter from local_outbox o"
                " join local_outbox_delivery d on d.outbox_id=o.id"
                f" where d.state='pending' and o.event_kind in ({placeholders})"
                + selection
                + " order by o.created_at,o.id limit 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            fence = int(row["fencing_counter"]) + 1
            claim_id = str(new_uuid7())
            expires = moment + dt.timedelta(seconds=lease_seconds)
            connection.execute(
                "update local_outbox_delivery set state='claimed',fencing_counter=?,claim_id=?,"
                "owner_id=?,owner_pid=?,owner_token=?,expires_at=?,updated_at=?"
                " where outbox_id=? and state='pending'",
                (
                    fence,
                    claim_id,
                    owner,
                    owner_pid,
                    token,
                    _text(expires),
                    _text(moment),
                    row["id"],
                ),
            )
            event_row = connection.execute(
                "select o.*,d.state from local_outbox o join local_outbox_delivery d"
                " on d.outbox_id=o.id where o.id=?",
                (row["id"],),
            ).fetchone()
            event = _outbox(event_row)
            connection.commit()
            return LocalOutboxClaim(
                event,
                claim_id,
                owner,
                owner_pid,
                token,
                fence,
                _text(expires),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_outbox_receipt(
        self,
        claim: LocalOutboxClaim,
        *,
        status: Literal["delivered", "failed", "unknown"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalOutboxEvent:
        if not isinstance(claim, LocalOutboxClaim) or not isinstance(claim.event, LocalOutboxEvent):
            raise ValidationFailed("Outbox receipt typed claim ister")
        if digest(claim.event.payload) != claim.event.payload_digest:
            raise ConcurrencyConflict("Outbox receipt exact claim/fence drift (payload)")
        if status not in {"delivered", "failed", "unknown"}:
            raise ValidationFailed("Outbox receipt status gecersiz")
        evidence = _digest(evidence_digest, "Outbox receipt evidence")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            stored = connection.execute(
                "select o.*,d.state,d.claim_id,d.owner_id,d.owner_pid,d.owner_token,"
                "d.fencing_counter,d.expires_at from local_outbox o"
                " join local_outbox_delivery d on d.outbox_id=o.id where o.id=?",
                (claim.event.id,),
            ).fetchone()
            if (
                stored is None
                or stored["claim_id"] != claim.claim_id
                or stored["owner_id"] != claim.owner_id
                or int(stored["owner_pid"]) != claim.owner_pid
                or stored["owner_token"] != claim.owner_token
                or int(stored["fencing_counter"]) != claim.fencing_token
                or stored["payload_digest"] != claim.event.payload_digest
                or stored["event_kind"] != claim.event.event_kind
                or stored["job_id"] != claim.event.job_id
                or stored["idempotency_key"] != claim.event.idempotency_key
                or stored["expires_at"] != claim.expires_at
            ):
                raise ConcurrencyConflict("Outbox receipt exact claim/fence drift")
            _outbox(stored)
            existing = connection.execute(
                "select status,evidence_digest from local_outbox_receipt where outbox_id=?",
                (claim.event.id,),
            ).fetchone()
            if existing is not None:
                if (existing["status"], existing["evidence_digest"]) != (status, evidence):
                    raise ConcurrencyConflict("Outbox receipt replay drift")
            else:
                if stored["state"] != "claimed" or _moment(stored["expires_at"]) <= moment:
                    raise ConcurrencyConflict("Outbox receipt live claim ister")
                connection.execute(
                    "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
                    (
                        str(new_uuid7()),
                        claim.event.id,
                        claim.claim_id,
                        claim.fencing_token,
                        status,
                        evidence,
                        _text(moment),
                    ),
                )
                delivery_state = "recovery-required" if status == "unknown" else status
                connection.execute(
                    "update local_outbox_delivery set state=?,updated_at=? where outbox_id=?",
                    (delivery_state, _text(moment), claim.event.id),
                )
                if status == "unknown":
                    case_evidence = digest(
                        {
                            "case_kind": "outbox-delivery-unknown",
                            "outbox_id": claim.event.id,
                            "claim_id": claim.claim_id,
                            "receipt_evidence": evidence,
                        }
                    )
                    connection.execute(
                        "insert into local_recovery_case(id,job_id,effect_claim_id,outbox_id,"
                        "case_kind,evidence_digest,state,created_at,resolved_at)"
                        " values(?,?,null,?,'outbox-delivery-unknown',?,'open',?,null)"
                        " on conflict(outbox_id) do nothing",
                        (
                            str(new_uuid7()),
                            claim.event.job_id,
                            claim.event.id,
                            case_evidence,
                            _text(moment),
                        ),
                    )
            event_row = connection.execute(
                "select o.*,d.state from local_outbox o join local_outbox_delivery d"
                " on d.outbox_id=o.id where o.id=?",
                (claim.event.id,),
            ).fetchone()
            connection.commit()
            return _outbox(event_row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover_outbox(
        self,
        connection: sqlite3.Connection,
        moment: dt.datetime,
        process_token_for: Callable[[int], str | None] | None = None,
    ) -> int:
        rows = connection.execute(
            "select o.id,o.job_id,d.claim_id,d.fencing_counter,d.owner_pid,d.owner_token,"
            " d.expires_at from local_outbox o"
            " join local_outbox_delivery d on d.outbox_id=o.id"
            " where d.state='claimed' order by o.id"
        ).fetchall()
        observed = {
            str(row["id"]): (
                None
                if process_token_for is None
                else _process_probe_value(process_token_for, int(row["owner_pid"]))
            )
            for row in rows
        }
        abandoned = [
            row
            for row in rows
            if _moment(row["expires_at"]) <= moment
            or (process_token_for is not None and observed[str(row["id"])] != row["owner_token"])
        ]
        for row in abandoned:
            evidence = digest(
                {
                    "case_kind": "outbox-delivery-unknown",
                    "outbox_id": row["id"],
                    "claim_id": row["claim_id"],
                    "fencing_token": int(row["fencing_counter"]),
                }
            )
            connection.execute(
                "insert into local_outbox_receipt values(?,?,?,?,? ,?,?)",
                (
                    str(new_uuid7()),
                    row["id"],
                    row["claim_id"],
                    row["fencing_counter"],
                    "unknown",
                    evidence,
                    _text(moment),
                ),
            )
            connection.execute(
                "update local_outbox_delivery set state='recovery-required',updated_at=?"
                " where outbox_id=?",
                (_text(moment), row["id"]),
            )
            connection.execute(
                "insert into local_recovery_case(id,job_id,effect_claim_id,outbox_id,case_kind,"
                "evidence_digest,state,created_at,resolved_at)"
                " values(?,?,null,?,'outbox-delivery-unknown',?,'open',?,null)",
                (str(new_uuid7()), row["job_id"], row["id"], evidence, _text(moment)),
            )
        return len(abandoned)

    def recover_outbox(
        self,
        process_token_for: Callable[[int], str | None] | None = None,
        *,
        now: str | None = None,
    ) -> int:
        if process_token_for is not None and not callable(process_token_for):
            raise ValidationFailed("Process token probe callable olmali")
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            count = self._recover_outbox(connection, _moment(now), process_token_for)
            connection.commit()
            return count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_recovery(
        self,
        recovery_case_id: str,
        *,
        outcome: Literal["completed", "failed", "delivered"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalRecoveryResolution:
        case_id = _required(recovery_case_id, "Recovery case id")
        if outcome not in {"completed", "failed", "delivered"}:
            raise ValidationFailed("Recovery outcome gecersiz")
        evidence = _digest(evidence_digest, "Recovery resolution evidence")
        moment = _text(_moment(now))
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            case = connection.execute(
                "select * from local_recovery_case where id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise NotFound("Recovery case bulunamadi")
            allowed = (
                {"completed", "failed"}
                if case["case_kind"] == "effect-unknown"
                else {"delivered", "failed"}
            )
            if outcome not in allowed:
                raise ValidationFailed("Recovery outcome case kind ile uyumsuz")
            if case["case_kind"] == "effect-unknown":
                receipt = connection.execute(
                    "select r.status from local_effect_receipt r where r.claim_id=?",
                    (case["effect_claim_id"],),
                ).fetchone()
                if (
                    receipt is not None
                    and receipt["status"] in {"completed", "failed"}
                    and receipt["status"] != outcome
                ):
                    raise ConcurrencyConflict("Recovery resolution terminal receipt ile celisiyor")
            existing = connection.execute(
                "select * from local_recovery_resolution where recovery_case_id=?",
                (case_id,),
            ).fetchone()
            if existing is not None:
                if (existing["outcome"], existing["evidence_digest"]) != (
                    outcome,
                    evidence,
                ):
                    raise ConcurrencyConflict("Recovery resolution replay drift")
                connection.commit()
                return _resolution(existing)
            if case["state"] != "open":
                raise ConcurrencyConflict("Recovery case acik degil")
            resolution_id = str(new_uuid7())
            if case["case_kind"] == "effect-unknown":
                insert_effect_recovery_resolution_tx(
                    connection,
                    EffectRecoveryResolutionSpec(resolution_id, case_id, outcome, evidence, moment),
                )
            else:
                connection.execute(
                    "insert into local_recovery_resolution values(?,?,?,?,?)",
                    (resolution_id, case_id, outcome, evidence, moment),
                )
                connection.execute(
                    "update local_recovery_case set state='resolved',resolved_at=? where id=?",
                    (moment, case_id),
                )
            if case["case_kind"] == "outbox-delivery-unknown":
                delivery_state = "delivered" if outcome == "delivered" else "failed"
                connection.execute(
                    "update local_outbox_delivery set state=?,updated_at=? where outbox_id=?"
                    " and state='recovery-required'",
                    (delivery_state, moment, case["outbox_id"]),
                )
            row = connection.execute(
                "select * from local_recovery_resolution where id=?", (resolution_id,)
            ).fetchone()
            connection.commit()
            return _resolution(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reconcile_recovery(self, job_id: str, *, now: str | None = None) -> LocalJob:
        job_key = _required(job_id, "Job id")
        moment = _text(_moment(now))
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            job = connection.execute("select * from local_job where id=?", (job_key,)).fetchone()
            if job is None:
                raise NotFound("Local job bulunamadi")
            if job["state"] != "recovery-required":
                raise PolicyViolation("Reconcile recovery-required job ister")
            unresolved = int(
                connection.execute(
                    "select count(*) from local_effect_claim c"
                    " left join local_effect_receipt r on r.claim_id=c.id"
                    " left join local_recovery_case rc on rc.effect_claim_id=c.id"
                    " left join local_recovery_resolution rr on rr.recovery_case_id=rc.id"
                    " where c.job_id=? and ((rc.id is not null and rr.id is null)"
                    " or (rc.id is null and (r.id is null or r.status='unknown')))",
                    (job_key,),
                ).fetchone()[0]
            )
            if unresolved:
                connection.commit()
                return _job(job)
            failed = bool(
                connection.execute(
                    "select 1 from local_effect_claim c"
                    " left join local_effect_receipt r on r.claim_id=c.id"
                    " left join local_recovery_case rc on rc.effect_claim_id=c.id"
                    " left join local_recovery_resolution rr on rr.recovery_case_id=rc.id"
                    " where c.job_id=? and (r.status='failed' or rr.outcome='failed') limit 1",
                    (job_key,),
                ).fetchone()
            )
            state = "failed" if failed else "completed"
            receipts = connection.execute(
                "select r.status,r.evidence_digest,rr.outcome as resolution_outcome,"
                " rr.evidence_digest as resolution_evidence from local_effect_claim c"
                " left join local_effect_receipt r on r.claim_id=c.id"
                " left join local_recovery_case rc on rc.effect_claim_id=c.id"
                " left join local_recovery_resolution rr on rr.recovery_case_id=rc.id"
                " where c.job_id=? order by c.id",
                (job_key,),
            ).fetchall()
            terminal_evidence = digest(
                [
                    (
                        row["status"],
                        row["evidence_digest"],
                        row["resolution_outcome"],
                        row["resolution_evidence"],
                    )
                    for row in receipts
                ]
            )
            require_outbox_capacity_tx(connection, max_pending_outbox=self.max_pending_outbox)
            outbox_id = str(new_uuid7())
            payload = {"job_id": job_key, "state": state, "reconciled": True}
            case_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "select id from local_recovery_case where job_id=? and state='resolved' "
                    "order by id",
                    (job_key,),
                ).fetchall()
            )
            reconcile_effect_recovery_job_tx(
                connection,
                RecoveryReconcileSpec(
                    job_key,
                    case_ids,
                    state,
                    terminal_evidence,
                    moment,
                    outbox_id,
                    self.max_pending_outbox,
                    digest(payload),
                ),
            )
            updated = connection.execute(
                "select * from local_job where id=?", (job_key,)
            ).fetchone()
            connection.commit()
            return _job(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def quarantine(
        self,
        work: LocalClaimedWork,
        *,
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalJob:
        evidence = _digest(evidence_digest, "Quarantine evidence")
        moment = _moment(now)
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            lease = connection.execute(
                "select * from local_lease where id=?", (work.lease.id,)
            ).fetchone()
            if (
                lease is None
                or lease["job_id"] != work.job.id
                or lease["owner_id"] != work.lease.owner_id
                or lease["owner_token"] != work.lease.owner_token
                or int(lease["fencing_token"]) != work.lease.fencing_token
                or _moment(lease["expires_at"]) <= moment
            ):
                raise ConcurrencyConflict("Quarantine current live fence ister")
            if connection.execute(
                "select 1 from local_effect_claim where job_id=? limit 1", (work.job.id,)
            ).fetchone():
                raise PolicyViolation("Effect claim sonrasi poison quarantine yasak")
            connection.execute("delete from local_resource_lock where lease_id=?", (work.lease.id,))
            connection.execute("delete from local_lease where id=?", (work.lease.id,))
            connection.execute(
                "update local_job set state='quarantined',terminal_evidence_digest=?,updated_at=?"
                " where id=? and state='running'",
                (evidence, _text(moment), work.job.id),
            )
            self._emit_outbox(
                connection,
                job_id=work.job.id,
                event_kind="job.quarantined",
                payload={"job_id": work.job.id, "state": "quarantined"},
                idempotency_key=f"job:{work.job.id}:quarantined",
                created_at=_text(moment),
            )
            row = connection.execute(
                "select * from local_job where id=?", (work.job.id,)
            ).fetchone()
            connection.commit()
            return _job(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def destroy_terminal(self, job_id: str) -> None:
        job_key = _required(job_id, "Job id")
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select state from local_job where id=?", (job_key,)
            ).fetchone()
            if row is None:
                raise NotFound("Local job bulunamadi")
            if row["state"] not in {"completed", "failed", "cancelled", "quarantined"}:
                raise PolicyViolation("Yalniz terminal local job destroy edilebilir")
            raise PolicyViolation(
                "Local job/claim/receipt/outbox audit retention nedeniyle silinemez"
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _job(row: sqlite3.Row) -> LocalJob:
    return LocalJob(
        str(row["id"]),
        str(row["state"]),
        str(row["idempotency_key"]),
        int(row["attempt_count"]),
        int(row["max_attempts"]),
        json.loads(str(row["payload_json"])),
    )


def _lease(row: sqlite3.Row) -> LocalLease:
    return LocalLease(
        str(row["id"]),
        str(row["job_id"]),
        str(row["owner_id"]),
        int(row["owner_pid"]),
        str(row["owner_token"]),
        int(row["fencing_token"]),
        str(row["expires_at"]),
    )


def _claim(row: sqlite3.Row) -> LocalClaim:
    return LocalClaim(
        str(row["id"]),
        str(row["job_id"]),
        str(row["lease_id"]),
        int(row["fencing_token"]),
        str(row["operation"]),
        str(row["effect_digest"]),
    )


def _receipt(row: sqlite3.Row) -> LocalReceipt:
    return LocalReceipt(
        str(row["id"]),
        str(row["claim_id"]),
        row["status"],
        str(row["evidence_digest"]),
    )


def _resolution(row: sqlite3.Row) -> LocalRecoveryResolution:
    return LocalRecoveryResolution(
        str(row["id"]),
        str(row["recovery_case_id"]),
        row["outcome"],
        str(row["evidence_digest"]),
    )


def _outbox(row: sqlite3.Row) -> LocalOutboxEvent:
    raw_payload = str(row["payload_json"])
    try:
        payload = json.loads(raw_payload)
    except (ValueError, RecursionError) as exc:
        raise ValidationFailed("Outbox persisted payload JSON bozuk") from exc
    if _payload_json(payload) != raw_payload or digest(payload) != row["payload_digest"]:
        raise ValidationFailed("Outbox persisted payload canonical/digest drift")
    return LocalOutboxEvent(
        str(row["id"]),
        str(row["job_id"]),
        str(row["idempotency_key"]),
        str(row["event_kind"]),
        str(row["payload_digest"]),
        payload,
        row["state"],
    )
