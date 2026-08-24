"""Execution host: admission, claim-before-effect ve recovery akisi.

Bu servis, bir worker'in dogru sirayla ilerlemesini zorunlu kilar:

```text
admission -> job claim -> logical lock -> effect claim -> effect -> receipt
```

Claim alinmadan effect baslatilamaz; terminal receipt yazilmadan is basarili
sayilmaz. Claim var ve receipt yoksa is `recovery-required` olur ve sessiz retry
yasaktir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.application.tool_dispatch import ToolDispatchService, ToolRuntimeAdapter
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.resources import ResourceRequest
from zekam.domain.runtime import (
    AttemptOutcome,
    EffectClaim,
    EffectReceipt,
    FailureCategory,
    Job,
    JobKind,
    ReceiptStatus,
    ReconciledCompletionRequest,
    ReconciledFailureRequest,
    RecoveryAssessment,
    assert_no_silent_retry,
    assess_recovery,
)
from zekam.domain.tool_registry import ToolDispatchBinding
from zekam.infrastructure.postgres.runtime_repository import (
    ClaimedWork,
    EffectLedger,
    JobRepository,
    RecoveryFinalization,
    ResourceLockRepository,
)
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository


class AdmissionDecision(StrEnum):
    """Yeni isin simdi baslayip baslayamayacagi."""

    ADMIT = "admit"
    DEFER = "defer"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class AdmissionState:
    """Admission kontrolunun girdileri.

    Admission **yetki degildir**; yalnizca "simdi baslayabilir mi" sorusunu
    yanitlar.
    """

    draining: bool = False
    maintenance: bool = False
    backup_lock: bool = False
    migration_pending: bool = False
    running_jobs: int = 0
    project_concurrency_limit: int = 8
    queue_backlog: int = 0
    max_queue_backlog: int = 10_000
    quota_available: bool = True
    sandbox_available: bool = True
    verifier_available: bool = True
    remaining_cost_micros: int = 1_000_000
    remaining_tokens: int = 1_000_000


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Admission karari ve gerekcesi."""

    decision: AdmissionDecision
    reason: str

    @property
    def admitted(self) -> bool:
        return self.decision is AdmissionDecision.ADMIT

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "reason": self.reason}


def check_admission(job: Job, state: AdmissionState) -> AdmissionResult:
    """Isin simdi baslayip baslayamayacagini soyler."""
    if state.maintenance or state.draining:
        return AdmissionResult(AdmissionDecision.DEFER, "system-draining-or-maintenance")
    if state.backup_lock:
        return AdmissionResult(AdmissionDecision.DEFER, "backup-lock-active")
    if state.migration_pending:
        return AdmissionResult(AdmissionDecision.DEFER, "migration-pending")
    if state.running_jobs >= state.project_concurrency_limit:
        return AdmissionResult(AdmissionDecision.DEFER, "project-concurrency-limit")
    if state.queue_backlog > state.max_queue_backlog:
        return AdmissionResult(AdmissionDecision.DEFER, "queue-backlog")
    if job.kind is JobKind.PROVIDER_CALL and not state.quota_available:
        return AdmissionResult(AdmissionDecision.DEFER, "provider-quota-exhausted")
    if job.produces_effect and not state.sandbox_available:
        return AdmissionResult(AdmissionDecision.DEFER, "sandbox-unavailable")
    if job.kind is JobKind.VERIFICATION and not state.verifier_available:
        return AdmissionResult(AdmissionDecision.DEFER, "verifier-unavailable")
    if state.remaining_cost_micros <= 0 or state.remaining_tokens <= 0:
        return AdmissionResult(AdmissionDecision.DEFER, "budget-exhausted")
    if not job.has_attempts_left():
        return AdmissionResult(AdmissionDecision.REJECT, "max-attempts-reached")
    return AdmissionResult(AdmissionDecision.ADMIT, "admitted")


@dataclass(frozen=True, slots=True)
class ExecutionHost:
    """Bir worker'in kuyruk, kilit ve effect ledger ile etkilesimi."""

    connection: Any
    realm_id: UUID
    worker_label: str = "worker"

    @property
    def jobs(self) -> JobRepository:
        return JobRepository(self.connection, self.realm_id)

    @property
    def locks(self) -> ResourceLockRepository:
        return ResourceLockRepository(self.connection, self.realm_id)

    @property
    def ledger(self) -> EffectLedger:
        return EffectLedger(self.connection, self.realm_id)

    # -- calisma dongusu ------------------------------------------------------------

    def acquire_work(
        self,
        *,
        capabilities: Sequence[str],
        admission: AdmissionState | None = None,
        lease_seconds: int = 60,
        now: dt.datetime | None = None,
    ) -> ClaimedWork | None:
        """Bir is alir ve ilan edilen kilitleri edinir.

        Kilit catismasi varsa is birakilir ve `None` doner; kilitler sizmaz.
        """
        claimed = self.jobs.claim_next(
            worker_label=self.worker_label,
            capabilities=capabilities,
            lease_seconds=lease_seconds,
            now=now,
        )
        if claimed is None:
            return None

        if admission is not None:
            result = check_admission(claimed.job, admission)
            if not result.admitted:
                self.jobs.complete(
                    claimed.job.id,
                    token=claimed.owner_token,
                    fencing_token=claimed.lease.fencing_token,
                    outcome=AttemptOutcome.ABANDONED,
                    now=now,
                )
                return None

        try:
            self.locks.acquire(claimed.job.id, claimed.job.resources, lease_id=claimed.lease.id)
        except Exception:
            # Kilit alinamadiysa is baska bir zamana birakilir.
            self.jobs.complete(
                claimed.job.id,
                token=claimed.owner_token,
                fencing_token=claimed.lease.fencing_token,
                outcome=AttemptOutcome.ABANDONED,
                now=now,
            )
            raise
        return claimed

    # -- claim-before-effect ------------------------------------------------------------

    def claim_effect(
        self,
        work: ClaimedWork,
        *,
        operation: str,
        effect_digest: str,
        authorization_digest: str,
        resources: Sequence[ResourceRequest],
        adapter_digest: str,
        authorization_id: UUID | None = None,
        idempotency_key: str | None = None,
        now: dt.datetime | None = None,
    ) -> EffectClaim:
        """Effect'ten **once** claim yazar.

        Ayni exact effect icin ikinci claim veritabani tarafindan reddedilir.
        """
        key = idempotency_key or digest(
            {
                "job_id": str(work.job.id),
                "operation": operation,
                "effect_digest": effect_digest,
            }
        )
        claim = EffectClaim.create(
            realm_id=self.realm_id,
            job_id=work.job.id,
            attempt_id=work.attempt_id,
            operation=operation,
            effect_digest=effect_digest,
            authorization_digest=authorization_digest,
            idempotency_key=key,
            resources=tuple(resources),
            execution_identity=f"{self.worker_label}:{work.lease.fencing_token}",
            fencing_token=work.lease.fencing_token,
            adapter_digest=adapter_digest,
            now=now,
        )
        return self.ledger.claim(claim, authorization_id=authorization_id)

    def dispatch_tool(
        self,
        binding: ToolDispatchBinding,
        adapter: ToolRuntimeAdapter[Any],
        *,
        now: dt.datetime | None = None,
    ) -> Any:
        """Exact claim/turn/spec/current-runtime kapisindan tool effect'i calistirir."""

        repository = ToolRegistryRepository(self.connection, self.realm_id)
        return ToolDispatchService(repository).dispatch(binding, adapter, now=now)

    def record_success(
        self,
        claim: EffectClaim,
        *,
        result_digest: str,
        adapter_evidence_digest: str | None = None,
        token_count: int = 0,
        cost_micros: int = 0,
        latency_ms: int = 0,
        now: dt.datetime | None = None,
    ) -> EffectReceipt:
        """Terminal `completed` receipt yazar."""
        receipt = EffectReceipt.completed(
            realm_id=self.realm_id,
            claim=claim,
            result_digest=result_digest,
            adapter_evidence_digest=adapter_evidence_digest,
            token_count=token_count,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            now=now,
        )
        return self.ledger.receipt(receipt)

    def record_failure(
        self,
        claim: EffectClaim,
        *,
        category: FailureCategory,
        failure_digest: str | None = None,
        latency_ms: int = 0,
        now: dt.datetime | None = None,
    ) -> EffectReceipt:
        """Terminal `failed` receipt yazar."""
        receipt = EffectReceipt.failed(
            realm_id=self.realm_id,
            claim=claim,
            category=category,
            failure_digest=failure_digest,
            latency_ms=latency_ms,
            now=now,
        )
        return self.ledger.receipt(receipt)

    def finish(
        self,
        work: ClaimedWork,
        *,
        outcome: AttemptOutcome,
        result_digest: str | None = None,
        failure_category: FailureCategory | None = None,
        now: dt.datetime | None = None,
    ) -> bool:
        """Isi terminal duruma alir.

        Terminal receipt'i olmayan claim varsa basarili tamamlama reddedilir.
        """
        if outcome is AttemptOutcome.SUCCEEDED:
            pending = self.pending_claims(work.job.id)
            if pending:
                raise PolicyViolation(
                    f"Terminal receipt'i olmayan {len(pending)} claim var; is completed olamaz"
                )
        return self.jobs.complete(
            work.job.id,
            token=work.owner_token,
            fencing_token=work.lease.fencing_token,
            outcome=outcome,
            result_digest=result_digest,
            failure_category=failure_category,
            now=now,
        )

    # -- recovery ------------------------------------------------------------------------

    def pending_claims(self, job_id: UUID) -> tuple[EffectClaim, ...]:
        """Terminal receipt'i olmayan claim'ler."""
        return tuple(
            claim
            for claim in self.ledger.claims_for_job(job_id)
            if self.ledger.receipt_for_claim(claim.id) is None
        )

    def assess(
        self, job_id: UUID, *, adapter_evidence: ReceiptStatus | None = None
    ) -> RecoveryAssessment:
        """Isin recovery durumunu claim/receipt ledgerinden turetir."""
        claims = self.ledger.claims_for_job(job_id)
        if not claims:
            return assess_recovery(claim=None, receipt=None)
        latest = claims[-1]
        return assess_recovery(
            claim=latest,
            receipt=self.ledger.receipt_for_claim(latest.id),
            adapter_evidence=adapter_evidence,
        )

    def require_no_silent_retry(self, job_id: UUID) -> RecoveryAssessment:
        """Sessiz retry girisimini reddeder ve degerlendirmeyi dondurur."""
        assessment = self.assess(job_id)
        assert_no_silent_retry(assessment)
        return assessment

    def recover(
        self, job_id: UUID, *, adapter_evidence: ReceiptStatus | None = None
    ) -> RecoveryAssessment:
        """Recovery degerlendirmesi yapar ve job durumunu buna gore ayarlar."""
        assessment = self.assess(job_id, adapter_evidence=adapter_evidence)
        if assessment.outcome.value == "recovery-required":
            self.jobs.mark_recovery_required(job_id, assessment.reason)
        return assessment

    def finalize_reconciled_completion(
        self,
        request: ReconciledCompletionRequest,
        *,
        now: dt.datetime | None = None,
    ) -> RecoveryFinalization:
        """Bağımsız completed adapter kanıtını exact terminal kayda dönüştürür."""

        return self.ledger.finalize_reconciled_completion(request, now=now)

    def finalize_reconciled_failure(
        self,
        request: ReconciledFailureRequest,
        *,
        now: dt.datetime | None = None,
    ) -> RecoveryFinalization:
        """Mevcut failed receipt'i exact expired job ile uzlastirir."""

        return self.ledger.finalize_reconciled_failure(request, now=now)


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    """Kuyruk durumunun ozeti."""

    ready: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    recovery_required: int = 0
    pending_claims: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return bool(self.recovery_required or self.blocked or self.pending_claims)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "running": self.running,
            "completed": self.completed,
            "failed": self.failed,
            "blocked": self.blocked,
            "recovery_required": self.recovery_required,
            "pending_claims": self.pending_claims,
            "needs_attention": self.needs_attention,
            "details": self.details,
        }


def summarize(host: ExecutionHost) -> ExecutionSummary:
    """Kuyruk ve recovery durumunu ozetler."""
    from zekam.domain.runtime import JobState

    counts = {state: len(host.jobs.list_by_state(state)) for state in JobState}
    pending = host.ledger.claims_without_receipt()
    return ExecutionSummary(
        ready=counts[JobState.READY],
        running=counts[JobState.RUNNING],
        completed=counts[JobState.COMPLETED],
        failed=counts[JobState.FAILED],
        blocked=counts[JobState.BLOCKED],
        recovery_required=counts[JobState.RECOVERY_REQUIRED],
        pending_claims=len(pending),
        details={"claims_without_receipt": list(pending)},
    )
