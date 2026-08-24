"""Execution Plane icin PostgreSQL adapterleri.

Kritik davranislar:

- Claim secimi `for update skip locked` ile yapilir ve **yalnizca** kuyruk
  sorgusunda kullanilir.
- Enqueue ile outbox olayi ayni transaction'da yazilir.
- Her claim fencing token'i artirir.
- Heartbeat, complete ve fail islemleri owner digest + fence + state uclusuyle
  eslesmezse 0 satir etkiler; caller hemen durur.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.resources import LockMode, ResourceRequest, lock_order
from zekam.domain.runtime import (
    AttemptOutcome,
    EffectClaim,
    EffectReceipt,
    FailureCategory,
    Job,
    JobKind,
    JobState,
    Lease,
    ReceiptStatus,
    ReconciledCompletionRequest,
    ReconciledFailureRequest,
    owner_digest,
)

_JOB_COLUMNS = (
    "id, realm_id, project_id, work_item_id, plan_id, step_id, kind, state, priority,"
    " attempt_count, max_attempts, fencing_token, idempotency_key, required_capabilities,"
    " read_resources, write_resources, payload, available_at, created_at, assignment_id"
)


def _job_from_row(row: Sequence[Any]) -> Job:
    read = tuple(ResourceRequest.parse(text, LockMode.READ) for text in row[14] or ())
    write = tuple(ResourceRequest.parse(text, LockMode.WRITE) for text in row[15] or ())
    return Job(
        id=row[0],
        realm_id=row[1],
        project_id=row[2],
        work_item_id=row[3],
        plan_id=row[4],
        step_id=row[5],
        kind=JobKind(row[6]),
        state=JobState(row[7]),
        priority=row[8],
        attempt_count=row[9],
        max_attempts=row[10],
        fencing_token=row[11],
        idempotency_key=row[12],
        required_capabilities=tuple(row[13] or ()),
        resources=lock_order(read + write),
        payload=dict(row[16] or {}),
        available_at=row[17],
        created_at=row[18],
        assignment_id=row[19],
    )


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    """Bir worker'a verilen is paketi."""

    job: Job
    attempt_id: UUID
    lease: Lease
    owner_token: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.as_dict(),
            "attempt_id": str(self.attempt_id),
            "lease": self.lease.as_dict(),
            # Owner token asla raporlanmaz.
        }


@dataclass(frozen=True, slots=True)
class RecoveryFinalization:
    """Exact recovery finalization sonucu; replay yeni receipt üretmez."""

    receipt: EffectReceipt
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.as_dict(), "created": self.created}


@dataclass(frozen=True, slots=True)
class JobRepository:
    """Durable kuyruk."""

    connection: Any
    realm_id: UUID

    # -- enqueue -----------------------------------------------------------------

    def enqueue(self, job: Job, *, outbox_event: str = "job.enqueued") -> tuple[Job, bool]:
        """Job'i ve outbox olayini ayni transaction'da yazar.

        Ayni idempotency key ile ikinci cagri yeni job uretmez; mevcut job ve
        `False` doner.
        """
        if job.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm job reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.job"
                " (id, realm_id, project_id, work_item_id, plan_id, step_id, kind, state,"
                "  priority, attempt_count, max_attempts, fencing_token, idempotency_key,"
                "  required_capabilities, read_resources, write_resources, payload,"
                "  available_at, created_at, assignment_id)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s::jsonb, %s, %s, %s)"
                " on conflict (realm_id, idempotency_key) do nothing"
                f" returning {_JOB_COLUMNS}",
                (
                    job.id,
                    job.realm_id,
                    job.project_id,
                    job.work_item_id,
                    job.plan_id,
                    job.step_id,
                    job.kind.value,
                    job.state.value,
                    job.priority,
                    job.attempt_count,
                    job.max_attempts,
                    job.fencing_token,
                    job.idempotency_key,
                    list(job.required_capabilities),
                    [item.resource.text for item in job.resources if not item.is_write],
                    [item.resource.text for item in job.resources if item.is_write],
                    canonical_json(job.payload),
                    job.available_at,
                    job.created_at,
                    job.assignment_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"select {_JOB_COLUMNS} from runtime.job where idempotency_key = %s",
                    (job.idempotency_key,),
                )
                existing = cursor.fetchone()
                if existing is None:  # pragma: no cover - conflict sonrasi kayit vardir
                    raise NotFound("Job olusturulamadi")
                return _job_from_row(existing), False

            cursor.execute(
                "insert into runtime.outbox_event (id, realm_id, job_id, event_type, payload)"
                " values (%s, %s, %s, %s, %s::jsonb)",
                (
                    new_uuid7(),
                    job.realm_id,
                    job.id,
                    outbox_event,
                    canonical_json({"idempotency_key": job.idempotency_key}),
                ),
            )
        return _job_from_row(row), True

    def get(self, job_id: UUID) -> Job:
        with self.connection.cursor() as cursor:
            cursor.execute(f"select {_JOB_COLUMNS} from runtime.job where id = %s", (job_id,))
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Job bulunamadi")
        return _job_from_row(row)

    def list_by_state(self, state: JobState) -> tuple[Job, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_JOB_COLUMNS} from runtime.job where state = %s"
                " order by priority, available_at, id",
                (state.value,),
            )
            rows = cursor.fetchall()
        return tuple(_job_from_row(row) for row in rows)

    # -- claim -------------------------------------------------------------------

    def claim_next(
        self,
        *,
        worker_label: str,
        capabilities: Sequence[str],
        lease_seconds: int = 60,
        now: dt.datetime | None = None,
    ) -> ClaimedWork | None:
        """Hazir bir job'i atomik olarak sahiplenir.

        Kuyruk secimi `for update skip locked` kullanir; iki worker ayni job'i
        alamaz. Worker'in yetenek kumesi job'in gereksinimini kapsamalidir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        token = _new_token()
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_JOB_COLUMNS} from runtime.job"
                " where state = 'ready'"
                "   and available_at <= %s"
                "   and required_capabilities <@ %s"
                "   and attempt_count < max_attempts"
                " order by priority, available_at, id"
                " for update skip locked"
                " limit 1",
                (moment, list(capabilities)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            job = _job_from_row(row)

            fencing_token = job.fencing_token + 1
            attempt_number = job.attempt_count + 1
            attempt_id = new_uuid7(now=moment)

            cursor.execute(
                "update runtime.job set state = 'running', attempt_count = %s,"
                " fencing_token = %s where id = %s and state = 'ready'",
                (attempt_number, fencing_token, job.id),
            )
            if cursor.rowcount == 0:  # pragma: no cover - skip locked bunu engeller
                raise ConcurrencyConflict("Job baska bir worker tarafindan alindi")

            cursor.execute(
                "insert into runtime.job_attempt"
                " (id, realm_id, job_id, attempt_number, fencing_token, worker_label, started_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    attempt_id,
                    job.realm_id,
                    job.id,
                    attempt_number,
                    fencing_token,
                    worker_label,
                    moment,
                ),
            )

            lease_id = new_uuid7(now=moment)
            expires_at = moment + dt.timedelta(seconds=lease_seconds)
            cursor.execute(
                "insert into runtime.lease"
                " (id, realm_id, job_id, attempt_id, owner_digest, fencing_token, worker_label,"
                "  expires_at, heartbeat_at, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    lease_id,
                    job.realm_id,
                    job.id,
                    attempt_id,
                    owner_digest(token),
                    fencing_token,
                    worker_label,
                    expires_at,
                    moment,
                    moment,
                ),
            )

        lease = Lease(
            id=lease_id,
            realm_id=job.realm_id,
            job_id=job.id,
            attempt_id=attempt_id,
            owner_digest=owner_digest(token),
            fencing_token=fencing_token,
            expires_at=expires_at,
            heartbeat_at=moment,
            worker_label=worker_label,
        )
        claimed_job = Job(
            id=job.id,
            realm_id=job.realm_id,
            project_id=job.project_id,
            kind=job.kind,
            state=JobState.RUNNING,
            idempotency_key=job.idempotency_key,
            priority=job.priority,
            attempt_count=attempt_number,
            max_attempts=job.max_attempts,
            fencing_token=fencing_token,
            required_capabilities=job.required_capabilities,
            resources=job.resources,
            work_item_id=job.work_item_id,
            plan_id=job.plan_id,
            step_id=job.step_id,
            assignment_id=job.assignment_id,
            payload=job.payload,
            available_at=job.available_at,
            created_at=job.created_at,
        )
        return ClaimedWork(job=claimed_job, attempt_id=attempt_id, lease=lease, owner_token=token)

    # -- heartbeat ve tamamlama ------------------------------------------------------

    def heartbeat(
        self,
        job_id: UUID,
        *,
        token: str,
        fencing_token: int,
        lease_seconds: int = 60,
        now: dt.datetime | None = None,
    ) -> bool:
        """Lease'i tazeler. Sahiplik eskimisse `False` doner."""
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.lease set heartbeat_at = %s, expires_at = %s"
                " where job_id = %s and owner_digest = %s and fencing_token = %s"
                "   and expires_at > %s"
                "   and exists (select 1 from runtime.job j"
                "               where j.id = runtime.lease.job_id and j.state = 'running')",
                (
                    moment,
                    moment + dt.timedelta(seconds=lease_seconds),
                    job_id,
                    owner_digest(token),
                    fencing_token,
                    moment,
                ),
            )
            return bool(cursor.rowcount)

    def complete(
        self,
        job_id: UUID,
        *,
        token: str,
        fencing_token: int,
        outcome: AttemptOutcome,
        result_digest: str | None = None,
        failure_category: FailureCategory | None = None,
        now: dt.datetime | None = None,
    ) -> bool:
        """Isi terminal duruma alir. Eski fence ile cagri reddedilir."""
        moment = now or dt.datetime.now(dt.UTC)
        state = {
            AttemptOutcome.SUCCEEDED: JobState.COMPLETED,
            AttemptOutcome.FAILED: JobState.FAILED,
            AttemptOutcome.ABANDONED: JobState.READY,
            AttemptOutcome.RECOVERY_REQUIRED: JobState.RECOVERY_REQUIRED,
        }[outcome]

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, attempt_id from runtime.lease"
                " where job_id = %s and owner_digest = %s and fencing_token = %s",
                (job_id, owner_digest(token), fencing_token),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            lease_id, attempt_id = row

            cursor.execute(
                "update runtime.job set state = %s where id = %s and fencing_token = %s"
                "   and state = 'running'",
                (state.value, job_id, fencing_token),
            )
            if cursor.rowcount == 0:
                return False

            cursor.execute(
                "update runtime.job_attempt set outcome = %s, failure_category = %s,"
                " result_digest = %s, finished_at = %s where id = %s",
                (
                    outcome.value,
                    None if failure_category is None else failure_category.value,
                    result_digest,
                    moment,
                    attempt_id,
                ),
            )
            cursor.execute("delete from runtime.resource_lock where job_id = %s", (job_id,))
            cursor.execute("delete from runtime.lease where id = %s", (lease_id,))
            cursor.execute(
                "insert into runtime.execution_event"
                " (id, realm_id, job_id, attempt_id, event_type, payload, occurred_at)"
                " values (%s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    new_uuid7(now=moment),
                    self.realm_id,
                    job_id,
                    attempt_id,
                    f"job.{state.value}",
                    canonical_json({"outcome": outcome.value}),
                    moment,
                ),
            )
        return True

    def mark_recovery_required(self, job_id: UUID, reason: str) -> None:
        """Isi recovery-required yapar; sessiz retry engellenir."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.job set state = 'recovery-required' where id = %s", (job_id,)
            )
            cursor.execute(
                "insert into runtime.execution_event"
                " (id, realm_id, job_id, event_type, payload) values (%s, %s, %s, %s, %s::jsonb)",
                (
                    new_uuid7(),
                    self.realm_id,
                    job_id,
                    "job.recovery-required",
                    canonical_json({"reason": reason}),
                ),
            )

    # -- lease bakimi -------------------------------------------------------------------

    def expired_leases(self, *, now: dt.datetime | None = None) -> tuple[UUID, ...]:
        """Suresi dolmus lease'lerin job kimliklerini dondurur."""
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select job_id from runtime.lease where expires_at <= %s order by job_id",
                (moment,),
            )
            return tuple(row[0] for row in cursor.fetchall())

    def reclaim_expired(self, *, now: dt.datetime | None = None) -> tuple[UUID, ...]:
        """Suresi dolmus lease'leri temizler.

        Effect claim'i olup receipt'i olmayan isler `recovery-required` olur;
        digerleri attempt limiti icinde yeniden `ready` yapilir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        affected: list[UUID] = []
        for job_id in self.expired_leases(now=moment):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from runtime.claim_without_receipt where job_id = %s",
                    (job_id,),
                )
                pending_claims = int(cursor.fetchone()[0])
                cursor.execute("delete from runtime.lease where job_id = %s", (job_id,))
                cursor.execute("delete from runtime.resource_lock where job_id = %s", (job_id,))
                if pending_claims:
                    cursor.execute(
                        "update runtime.job set state = 'recovery-required'"
                        " where id = %s and state = 'running'",
                        (job_id,),
                    )
                else:
                    cursor.execute(
                        "update runtime.job set state = case"
                        "   when attempt_count < max_attempts then 'ready' else 'blocked' end"
                        " where id = %s and state = 'running'",
                        (job_id,),
                    )
            affected.append(job_id)
        return tuple(affected)


@dataclass(frozen=True, slots=True)
class ResourceLockRepository:
    """Logical kilitler."""

    connection: Any
    realm_id: UUID

    def acquire(
        self, job_id: UUID, requests: Sequence[ResourceRequest], *, lease_id: UUID | None = None
    ) -> tuple[UUID, ...]:
        """Kilitleri lexical sirada edinir. Catisma varsa hicbiri alinmaz."""
        acquired: list[UUID] = []
        with self.connection.cursor() as cursor:
            for request in lock_order(requests):
                lock_id = new_uuid7()
                cursor.execute(
                    "insert into runtime.resource_lock"
                    " (id, realm_id, resource, mode, job_id, lease_id)"
                    " values (%s, %s, %s, %s, %s, %s)"
                    " on conflict (realm_id, resource, mode, job_id) do nothing",
                    (
                        lock_id,
                        self.realm_id,
                        request.resource.text,
                        request.mode.value,
                        job_id,
                        lease_id,
                    ),
                )
                acquired.append(lock_id)
        return tuple(acquired)

    def held_by(self, job_id: UUID) -> tuple[ResourceRequest, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select resource, mode from runtime.resource_lock where job_id = %s"
                " order by resource, mode",
                (job_id,),
            )
            rows = cursor.fetchall()
        return tuple(ResourceRequest.parse(row[0], LockMode(row[1])) for row in rows)

    def release_all(self, job_id: UUID) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute("delete from runtime.resource_lock where job_id = %s", (job_id,))
            return int(cursor.rowcount)

    def all_locks(self) -> tuple[tuple[str, str, UUID], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select resource, mode, job_id from runtime.resource_lock order by resource"
            )
            return tuple((row[0], row[1], row[2]) for row in cursor.fetchall())


@dataclass(frozen=True, slots=True)
class EffectLedger:
    """Claim ve receipt kayitlari."""

    connection: Any
    realm_id: UUID

    def claim(self, claim: EffectClaim, *, authorization_id: UUID | None = None) -> EffectClaim:
        """Claim'i yazar. Ayni idempotency key ile ikinci claim reddedilir."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.effect_claim"
                " (id, realm_id, job_id, attempt_id, operation, effect_digest,"
                "  authorization_digest, authorization_id, idempotency_key, resources,"
                "  execution_identity, fencing_token, adapter_digest, claim_digest, claimed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
                (
                    claim.id,
                    claim.realm_id,
                    claim.job_id,
                    claim.attempt_id,
                    claim.operation,
                    claim.effect_digest,
                    claim.authorization_digest,
                    authorization_id,
                    claim.idempotency_key,
                    canonical_json([item.as_dict() for item in claim.resources]),
                    claim.execution_identity,
                    claim.fencing_token,
                    claim.adapter_digest,
                    claim.claim_digest,
                    claim.claimed_at,
                ),
            )
        return claim

    def receipt(self, receipt: EffectReceipt) -> EffectReceipt:
        """Terminal receipt yazar. Ayni claim icin ikinci receipt reddedilir."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.effect_receipt"
                " (id, realm_id, claim_id, status, result_digest, failure_category,"
                "  failure_digest, adapter_evidence_digest, token_count, cost_micros,"
                "  latency_ms, completed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    receipt.id,
                    receipt.realm_id,
                    receipt.claim_id,
                    receipt.status.value,
                    receipt.result_digest,
                    None if receipt.failure_category is None else receipt.failure_category.value,
                    receipt.failure_digest,
                    receipt.adapter_evidence_digest,
                    receipt.token_count,
                    receipt.cost_micros,
                    receipt.latency_ms,
                    receipt.completed_at,
                ),
            )
        return receipt

    def finalize_reconciled_completion(
        self,
        request: ReconciledCompletionRequest,
        *,
        now: dt.datetime | None = None,
    ) -> RecoveryFinalization:
        """Expired lease'li receiptless effect'i tek transactionda tamamlar.

        Caller connection transaction'ını yönetir. Bu metot owner token'ı
        atlamaz; yalnız süresi dolmuş eski attempt'i bağımsız adapter kanıtıyla
        uzlaştıran ayrı recovery yoludur. Exact replay mevcut receipt'i döndürür,
        herhangi bir kimlik veya digest farkı fail-closed reddedilir.
        """

        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.transaction():
            return self._finalize_reconciled_completion_in_transaction(request, moment=moment)

    def finalize_reconciled_failure(
        self,
        request: ReconciledFailureRequest,
        *,
        now: dt.datetime | None = None,
    ) -> RecoveryFinalization:
        """Expired lease'li job'i mevcut exact failed receipt ile kapatir."""

        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select state, fencing_token from runtime.job"
                " where id = %s and realm_id = %s for update",
                (request.job_id, self.realm_id),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise NotFound("Failure reconciliation job bulunamadi")
            state = JobState(job_row[0])
            if int(job_row[1]) != request.fencing_token:
                raise ConcurrencyConflict("Failure reconciliation fencing token eslesmiyor")

            completion = ReconciledCompletionRequest(
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                claim_id=request.claim_id,
                fencing_token=request.fencing_token,
                claim_digest=request.claim_digest,
                effect_digest=request.effect_digest,
                authorization_digest=request.authorization_digest,
                result_digest=request.failure_digest,
                adapter_evidence_digest=request.failure_digest,
            )
            self._exact_recovery_claim(cursor, completion)
            receipt = self.receipt_for_claim(request.claim_id)
            if (
                receipt is None
                or receipt.id != request.receipt_id
                or receipt.status is not ReceiptStatus.FAILED
                or receipt.failure_digest != request.failure_digest
                or receipt.failure_category is None
            ):
                raise PolicyViolation("Failure reconciliation exact failed receipt ister")

            if state is JobState.FAILED:
                cursor.execute(
                    "select outcome, failure_category, result_digest from runtime.job_attempt"
                    " where id = %s and realm_id = %s and job_id = %s",
                    (request.attempt_id, self.realm_id, request.job_id),
                )
                attempt = cursor.fetchone()
                if attempt not in {
                    (AttemptOutcome.FAILED.value, receipt.failure_category.value, None),
                    (
                        AttemptOutcome.RECOVERY_REQUIRED.value,
                        receipt.failure_category.value,
                        request.failure_digest,
                    ),
                }:
                    raise PolicyViolation("Failure reconciliation replay attempt drift")
                return RecoveryFinalization(receipt=receipt, created=False)
            if state is not JobState.RECOVERY_REQUIRED:
                raise PolicyViolation(
                    f"Failure reconciliation job durumu uygun degil: {state.value}"
                )

            cursor.execute(
                "select outcome, fencing_token, failure_category, result_digest"
                " from runtime.job_attempt"
                " where id = %s and realm_id = %s and job_id = %s for update",
                (request.attempt_id, self.realm_id, request.job_id),
            )
            attempt = cursor.fetchone()
            if attempt is None:
                raise NotFound("Failure reconciliation attempt bulunamadi")
            if int(attempt[1]) != request.fencing_token:
                raise ConcurrencyConflict("Failure reconciliation attempt fence drift")
            terminal_recovery = attempt[0] == AttemptOutcome.RECOVERY_REQUIRED.value
            if terminal_recovery:
                if (
                    attempt[2] != receipt.failure_category.value
                    or attempt[3] != request.failure_digest
                ):
                    raise PolicyViolation("Failure reconciliation recovery attempt drift")
                cursor.execute(
                    "select count(*) from runtime.lease"
                    " where realm_id = %s and job_id = %s and attempt_id = %s",
                    (self.realm_id, request.job_id, request.attempt_id),
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise PolicyViolation("Failure reconciliation terminal attempt lease tasiyamaz")
                lease_id = None
            elif attempt[0] is None:
                cursor.execute(
                    "select id, expires_at from runtime.lease"
                    " where realm_id = %s and job_id = %s and attempt_id = %s"
                    " and fencing_token = %s for update",
                    (
                        self.realm_id,
                        request.job_id,
                        request.attempt_id,
                        request.fencing_token,
                    ),
                )
                lease = cursor.fetchone()
                if lease is None or lease[1] > moment:
                    raise PolicyViolation("Failure reconciliation exact expired lease ister")
                lease_id = lease[0]
                cursor.execute(
                    "update runtime.job_attempt set outcome = 'failed', failure_category = %s,"
                    " finished_at = %s where id = %s and outcome is null and fencing_token = %s",
                    (
                        receipt.failure_category.value,
                        moment,
                        request.attempt_id,
                        request.fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyConflict("Failure reconciliation attempt update reddedildi")
            else:
                raise PolicyViolation("Failure reconciliation attempt durumu uygun degil")
            cursor.execute("delete from runtime.resource_lock where job_id = %s", (request.job_id,))
            if lease_id is not None:
                cursor.execute("delete from runtime.lease where id = %s", (lease_id,))
            cursor.execute(
                "update runtime.job set state = 'failed'"
                " where id = %s and realm_id = %s and fencing_token = %s"
                " and state = 'recovery-required'",
                (request.job_id, self.realm_id, request.fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Failure reconciliation job update reddedildi")
            cursor.execute(
                "insert into runtime.execution_event"
                " (id, realm_id, job_id, attempt_id, event_type, payload, occurred_at)"
                " values (%s, %s, %s, %s, 'job.reconciled-failed', %s::jsonb, %s)",
                (
                    new_uuid7(now=moment),
                    self.realm_id,
                    request.job_id,
                    request.attempt_id,
                    canonical_json(
                        {
                            "claim_id": str(request.claim_id),
                            "receipt_id": str(request.receipt_id),
                            "failure_digest": request.failure_digest,
                        }
                    ),
                    moment,
                ),
            )
        return RecoveryFinalization(receipt=receipt, created=False)

    def _finalize_reconciled_completion_in_transaction(
        self,
        request: ReconciledCompletionRequest,
        *,
        moment: dt.datetime,
    ) -> RecoveryFinalization:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select state, fencing_token, work_item_id, plan_id, step_id, payload"
                " from runtime.job where id = %s and realm_id = %s for update",
                (request.job_id, self.realm_id),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise NotFound("Recovery job bulunamadi")
            state = JobState(job_row[0])
            if int(job_row[1]) != request.fencing_token:
                raise ConcurrencyConflict("Recovery job fencing token eslesmiyor")

            claim = self._exact_recovery_claim(cursor, request)
            existing = self.receipt_for_claim(request.claim_id)
            if state is JobState.COMPLETED:
                return self._validate_recovery_replay(cursor, request, claim, existing)
            if state not in {JobState.RUNNING, JobState.RECOVERY_REQUIRED}:
                raise PolicyViolation(f"Recovery job durumu kapatilamaz: {state.value}")
            if existing is not None:
                raise PolicyViolation("Aktif recovery job receipt ile celisiyor")

            cursor.execute(
                "select outcome, fencing_token from runtime.job_attempt"
                " where id = %s and realm_id = %s and job_id = %s for update",
                (request.attempt_id, self.realm_id, request.job_id),
            )
            attempt = cursor.fetchone()
            if attempt is None:
                raise NotFound("Recovery attempt bulunamadi")
            if attempt[0] is not None:
                raise PolicyViolation("Recovery attempt zaten terminal")
            if int(attempt[1]) != request.fencing_token:
                raise ConcurrencyConflict("Recovery attempt fencing token eslesmiyor")

            cursor.execute(
                "select id, expires_at from runtime.lease"
                " where realm_id = %s and job_id = %s and attempt_id = %s"
                "   and fencing_token = %s for update",
                (
                    self.realm_id,
                    request.job_id,
                    request.attempt_id,
                    request.fencing_token,
                ),
            )
            lease = cursor.fetchone()
            if lease is None:
                raise PolicyViolation("Recovery exact expired lease ister")
            if lease[1] > moment:
                raise PolicyViolation("Recovery lease henuz sona ermedi")

            payload = dict(job_row[5] or {})
            meaningful = bool(
                str(payload.get("meaningful_step", "false")).casefold() == "true"
                or (job_row[2] is not None and job_row[3] is not None and job_row[4] is not None)
            )
            if meaningful:
                cursor.execute(
                    "select 1 from work.checkpoint where realm_id = %s and job_id = %s limit 1",
                    (self.realm_id, request.job_id),
                )
                if cursor.fetchone() is None:
                    raise PolicyViolation("Meaningful recovery finalization checkpoint ister")

            receipt = EffectReceipt.completed(
                realm_id=self.realm_id,
                claim=claim,
                result_digest=request.result_digest,
                adapter_evidence_digest=request.adapter_evidence_digest,
                now=moment,
            )
            cursor.execute(
                "insert into runtime.effect_receipt"
                " (id, realm_id, claim_id, status, result_digest, failure_category,"
                "  failure_digest, adapter_evidence_digest, token_count, cost_micros,"
                "  latency_ms, completed_at)"
                " values (%s, %s, %s, %s, %s, null, null, %s, 0, 0, 0, %s)",
                (
                    receipt.id,
                    receipt.realm_id,
                    receipt.claim_id,
                    receipt.status.value,
                    receipt.result_digest,
                    receipt.adapter_evidence_digest,
                    receipt.completed_at,
                ),
            )
            cursor.execute(
                "update runtime.job_attempt set outcome = 'succeeded', result_digest = %s,"
                " finished_at = %s where id = %s and outcome is null and fencing_token = %s",
                (
                    request.result_digest,
                    moment,
                    request.attempt_id,
                    request.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Recovery attempt terminal update reddedildi")
            cursor.execute("delete from runtime.resource_lock where job_id = %s", (request.job_id,))
            cursor.execute("delete from runtime.lease where id = %s", (lease[0],))
            cursor.execute(
                "update runtime.job set state = 'completed'"
                " where id = %s and realm_id = %s and fencing_token = %s"
                "   and state in ('running', 'recovery-required')",
                (request.job_id, self.realm_id, request.fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Recovery job terminal update reddedildi")
            cursor.execute(
                "insert into runtime.execution_event"
                " (id, realm_id, job_id, attempt_id, event_type, payload, occurred_at)"
                " values (%s, %s, %s, %s, 'job.reconciled-completed', %s::jsonb, %s)",
                (
                    new_uuid7(now=moment),
                    self.realm_id,
                    request.job_id,
                    request.attempt_id,
                    canonical_json(
                        {
                            "claim_id": str(request.claim_id),
                            "receipt_id": str(receipt.id),
                            "fencing_token": request.fencing_token,
                            "claim_digest": request.claim_digest,
                            "effect_digest": request.effect_digest,
                            "authorization_digest": request.authorization_digest,
                            "result_digest": request.result_digest,
                            "adapter_evidence_digest": request.adapter_evidence_digest,
                        }
                    ),
                    moment,
                ),
            )
        return RecoveryFinalization(receipt=receipt, created=True)

    def _exact_recovery_claim(
        self, cursor: Any, request: ReconciledCompletionRequest
    ) -> EffectClaim:
        cursor.execute(
            f"select {_CLAIM_COLUMNS}, claim_digest from runtime.effect_claim"
            " where id = %s and realm_id = %s and job_id = %s and attempt_id = %s",
            (request.claim_id, self.realm_id, request.job_id, request.attempt_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Recovery claim bulunamadi")
        claim = _claim_from_row(row)
        if claim.fencing_token != request.fencing_token:
            raise ConcurrencyConflict("Recovery claim fencing token eslesmiyor")
        stored_claim_digest = str(row[13])
        if stored_claim_digest != request.claim_digest or claim.claim_digest != stored_claim_digest:
            raise PolicyViolation("Recovery claim digest eslesmiyor")
        if claim.effect_digest != request.effect_digest:
            raise PolicyViolation("Recovery effect digest eslesmiyor")
        if claim.authorization_digest != request.authorization_digest:
            raise PolicyViolation("Recovery authorization digest eslesmiyor")
        return claim

    def _validate_recovery_replay(
        self,
        cursor: Any,
        request: ReconciledCompletionRequest,
        claim: EffectClaim,
        receipt: EffectReceipt | None,
    ) -> RecoveryFinalization:
        del claim
        if receipt is None or receipt.status is not ReceiptStatus.COMPLETED:
            raise PolicyViolation("Completed recovery job terminal receipt ile eslesmiyor")
        if (
            receipt.result_digest != request.result_digest
            or receipt.adapter_evidence_digest != request.adapter_evidence_digest
        ):
            raise PolicyViolation("Recovery replay result veya adapter evidence drift")
        cursor.execute(
            "select outcome, result_digest, fencing_token from runtime.job_attempt"
            " where id = %s and realm_id = %s and job_id = %s",
            (request.attempt_id, self.realm_id, request.job_id),
        )
        attempt = cursor.fetchone()
        if attempt is None:
            raise NotFound("Recovery replay attempt bulunamadi")
        if (
            attempt[0] != AttemptOutcome.SUCCEEDED.value
            or attempt[1] != request.result_digest
            or int(attempt[2]) != request.fencing_token
        ):
            raise PolicyViolation("Recovery replay attempt sonucu drift")
        cursor.execute("select 1 from runtime.lease where job_id = %s", (request.job_id,))
        if cursor.fetchone() is not None:
            raise PolicyViolation("Completed recovery job lease tasiyamaz")
        cursor.execute("select 1 from runtime.resource_lock where job_id = %s", (request.job_id,))
        if cursor.fetchone() is not None:
            raise PolicyViolation("Completed recovery job logical lock tasiyamaz")
        cursor.execute(
            "select count(*) from runtime.execution_event"
            " where realm_id = %s and job_id = %s and attempt_id = %s"
            "   and event_type = 'job.reconciled-completed'"
            "   and payload = %s::jsonb",
            (
                self.realm_id,
                request.job_id,
                request.attempt_id,
                canonical_json(
                    {
                        "claim_id": str(request.claim_id),
                        "receipt_id": str(receipt.id),
                        "fencing_token": request.fencing_token,
                        "claim_digest": request.claim_digest,
                        "effect_digest": request.effect_digest,
                        "authorization_digest": request.authorization_digest,
                        "result_digest": request.result_digest,
                        "adapter_evidence_digest": request.adapter_evidence_digest,
                    }
                ),
            ),
        )
        if int(cursor.fetchone()[0]) != 1:
            raise PolicyViolation("Recovery replay terminal execution event drift")
        return RecoveryFinalization(receipt=receipt, created=False)

    def find_claim_by_key(self, idempotency_key: str) -> EffectClaim | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_CLAIM_COLUMNS} from runtime.effect_claim where idempotency_key = %s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
        return None if row is None else _claim_from_row(row)

    def claims_for_job(self, job_id: UUID) -> tuple[EffectClaim, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_CLAIM_COLUMNS} from runtime.effect_claim where job_id = %s"
                " order by claimed_at",
                (job_id,),
            )
            rows = cursor.fetchall()
        return tuple(_claim_from_row(row) for row in rows)

    def receipt_for_claim(self, claim_id: UUID) -> EffectReceipt | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, claim_id, status, result_digest, failure_category,"
                " failure_digest, adapter_evidence_digest, token_count, cost_micros,"
                " latency_ms, completed_at from runtime.effect_receipt where claim_id = %s",
                (claim_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return EffectReceipt(
            id=row[0],
            realm_id=row[1],
            claim_id=row[2],
            status=ReceiptStatus(row[3]),
            result_digest=row[4],
            failure_category=None if row[5] is None else FailureCategory(row[5]),
            failure_digest=row[6],
            adapter_evidence_digest=row[7],
            token_count=row[8],
            cost_micros=row[9],
            latency_ms=row[10],
            completed_at=row[11],
        )

    def claims_without_receipt(self) -> tuple[dict[str, Any], ...]:
        """Recovery gerektiren kayitlar."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select claim_id, job_id, operation, effect_digest, claimed_at, job_state"
                " from runtime.claim_without_receipt order by claimed_at"
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "claim_id": str(row[0]),
                "job_id": str(row[1]),
                "operation": row[2],
                "effect_digest": row[3],
                "claimed_at": row[4],
                "job_state": row[5],
            }
            for row in rows
        )


_CLAIM_COLUMNS = (
    "id, realm_id, job_id, attempt_id, operation, effect_digest, authorization_digest,"
    " idempotency_key, resources, execution_identity, fencing_token, adapter_digest, claimed_at"
)


def _claim_from_row(row: Sequence[Any]) -> EffectClaim:
    return EffectClaim(
        id=row[0],
        realm_id=row[1],
        job_id=row[2],
        attempt_id=row[3],
        operation=row[4],
        effect_digest=row[5],
        authorization_digest=row[6],
        idempotency_key=row[7],
        resources=tuple(
            ResourceRequest.parse(item["resource"], LockMode(item["mode"])) for item in row[8] or ()
        ),
        execution_identity=row[9],
        fencing_token=row[10],
        adapter_digest=row[11],
        claimed_at=row[12],
    )


def _new_token() -> str:
    from zekam.domain.runtime import new_owner_token

    return new_owner_token()
