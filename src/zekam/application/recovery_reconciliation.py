"""Receiptless eski effect'leri exact authorization ile uzlastirma servisi.

Prepare yalniz canonical kayitlari dogrular ve digest uretir. Apply, exact
one-shot authorization'i effect'ten once tuketir; recovery job/claim/receipt,
checkpoint ve eski claim finalization'ini tek PostgreSQL transaction'inda yazar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from zekam.application.execution import ExecutionHost
from zekam.application.governance import EffectRequest, GovernanceService
from zekam.application.legacy_repository_provider import legacy_repository
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import Checkpoint, EvidenceReference
from zekam.domain.errors import AuthorizationRequired, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.realm import Realm
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import (
    AttemptOutcome,
    ClaimedWork,
    EffectClaim,
    FailureCategory,
    Job,
    JobKind,
    JobState,
    ReceiptStatus,
    ReconciledCompletionRequest,
    ReconciledFailureRequest,
    RecoveryFinalization,
)
from zekam.domain.security import Authorization
from zekam.domain.work import EffectKind, TaskPlan

RECOVERY_SCHEMA = "zekam-recovery-reconciliation/v1"
RECOVERY_OPERATION = "reconcile-recovery"
RECOVERY_CONSUMER = "cli:worker-reconcile-recovery"
FAILED_RECOVERY_OPERATION = "reconcile-failed-receipt"
FAILED_RECOVERY_CONSUMER = "cli:worker-reconcile-failed-receipt"


@dataclass(frozen=True, slots=True)
class FailedReceiptReconciliationPlan:
    """Terminal failed receipt'i yarim kalmis job ile exact baglayan plan."""

    project_id: UUID
    work_item_id: UUID
    task_plan_id: UUID
    request: ReconciledFailureRequest

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-failed-receipt-reconciliation/v1",
                "project_id": str(self.project_id),
                "work_item_id": str(self.work_item_id),
                "task_plan_id": str(self.task_plan_id),
                "request": self.request.as_dict(),
            }
        )

    @property
    def resource(self) -> str:
        return (
            f"work:{self.project_id}:failed-receipt:{self.work_item_id}:"
            f"{self.request.job_id}:{self.plan_digest.removeprefix('sha256:')}"
        )

    @property
    def effect_request(self) -> EffectRequest:
        return EffectRequest(
            action=FAILED_RECOVERY_OPERATION,
            effects=(EffectKind.DATABASE_WRITE,),
            resources=(self.resource,),
            required_capabilities=("database.write",),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-failed-receipt-reconciliation/v1",
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "task_plan_id": str(self.task_plan_id),
            "request": self.request.as_dict(),
            "plan_digest": self.plan_digest,
            "resource": self.resource,
            "effect_digest": self.effect_request.effect_digest,
        }


@dataclass(frozen=True, slots=True)
class FailedReceiptReconciliationService:
    connection: Any
    realm: Realm

    @property
    def governance(self) -> GovernanceService:
        return GovernanceService(self.connection, self.realm)

    def prepare(
        self, *, job_id: UUID, claim_id: UUID, receipt_id: UUID
    ) -> FailedReceiptReconciliationPlan:
        host = ExecutionHost(self.connection, self.realm.id, worker_label=FAILED_RECOVERY_CONSUMER)
        job = host.jobs.get(job_id)
        if (
            job.state is not JobState.RECOVERY_REQUIRED
            or job.project_id is None
            or job.work_item_id is None
            or job.plan_id is None
        ):
            raise PolicyViolation("Failed receipt reconciliation exact bound recovery job ister")
        claims = tuple(item for item in host.ledger.claims_for_job(job_id) if item.id == claim_id)
        if len(claims) != 1:
            raise NotFound("Failed receipt reconciliation claim bulunamadi")
        claim = claims[0]
        receipt = host.ledger.receipt_for_claim(claim_id)
        if (
            receipt is None
            or receipt.id != receipt_id
            or receipt.status is not ReceiptStatus.FAILED
            or receipt.failure_digest is None
        ):
            raise PolicyViolation("Failed receipt reconciliation exact failed receipt ister")
        plan = FailedReceiptReconciliationPlan(
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            task_plan_id=job.plan_id,
            request=ReconciledFailureRequest(
                job_id=job.id,
                attempt_id=claim.attempt_id,
                claim_id=claim.id,
                receipt_id=receipt.id,
                fencing_token=claim.fencing_token,
                claim_digest=claim.claim_digest,
                effect_digest=claim.effect_digest,
                authorization_digest=claim.authorization_digest,
                failure_digest=receipt.failure_digest,
            ),
        )
        return plan

    def issue_authorization(
        self,
        plan: FailedReceiptReconciliationPlan,
        *,
        actor_id: UUID,
        now: dt.datetime | None = None,
    ) -> Authorization:
        current = self.prepare(
            job_id=plan.request.job_id,
            claim_id=plan.request.claim_id,
            receipt_id=plan.request.receipt_id,
        )
        if current.plan_digest != plan.plan_digest:
            raise PolicyViolation("Failed receipt reconciliation plan drift")
        return self.governance.issue_authorization(
            request=plan.effect_request,
            actor_id=actor_id,
            plan_digest=plan.plan_digest,
            work_item_id=plan.work_item_id,
            plan_id=plan.task_plan_id,
            lifetime=dt.timedelta(minutes=15),
            now=now,
        )

    def apply(
        self,
        plan: FailedReceiptReconciliationPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.transaction():
            current = self.prepare(
                job_id=plan.request.job_id,
                claim_id=plan.request.claim_id,
                receipt_id=plan.request.receipt_id,
            )
            if current.plan_digest != plan.plan_digest:
                raise PolicyViolation("Failed receipt reconciliation apply drift")
            authorization = self.governance.authorizations.get(authorization_id)
            if (
                authorization.plan_digest != plan.plan_digest
                or authorization.work_item_id != plan.work_item_id
                or authorization.plan_id != plan.task_plan_id
            ):
                raise AuthorizationRequired("Failed receipt authorization binding eslesmiyor")
            consumed = self.governance.require_authorized(
                plan.effect_request,
                authorization=authorization,
                consumed_by=FAILED_RECOVERY_CONSUMER,
                now=moment,
            )
            capability = f"recovery.failed.{plan.plan_digest[-16:]}"
            host = ExecutionHost(
                self.connection, self.realm.id, worker_label=FAILED_RECOVERY_CONSUMER
            )
            recovery_job, created = host.jobs.enqueue(
                Job.create(
                    realm_id=self.realm.id,
                    project_id=plan.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=f"reconcile-failed:{plan.plan_digest}",
                    required_capabilities=("database.write", capability),
                    max_attempts=1,
                    payload={"old_job_id": str(plan.request.job_id)},
                    now=moment,
                )
            )
            if not created:
                raise PolicyViolation("Failed receipt recovery job replay reddedildi")
            work = host.acquire_work(capabilities=("database.write", capability), now=moment)
            if work is None or work.job.id != recovery_job.id:
                raise PolicyViolation("Failed receipt recovery job claim edilemedi")
            recovery_claim = host.claim_effect(
                work,
                operation=FAILED_RECOVERY_OPERATION,
                effect_digest=plan.effect_request.effect_digest,
                authorization_digest=consumed.authorization_digest,
                authorization_id=consumed.id,
                resources=parse_requests(write=(plan.resource,)),
                adapter_digest=digest(
                    {"adapter": "failed-receipt-reconciliation/v1", "plan": plan.plan_digest}
                ),
                now=moment,
            )
            old = host.finalize_reconciled_failure(plan.request, now=moment)
            result_digest = digest(
                {
                    "plan_digest": plan.plan_digest,
                    "old_job_id": str(plan.request.job_id),
                    "old_receipt_id": str(old.receipt.id),
                }
            )
            recovery_receipt = host.record_success(
                recovery_claim,
                result_digest=result_digest,
                adapter_evidence_digest=plan.request.failure_digest,
                now=moment,
            )
            if not host.finish(work, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest):
                raise PolicyViolation("Failed receipt recovery job tamamlanamadi")
        return {
            "schema": "zekam-failed-receipt-reconciliation-result/v1",
            "old_job_id": str(plan.request.job_id),
            "old_state": "failed",
            "old_receipt_id": str(old.receipt.id),
            "recovery_job_id": str(recovery_job.id),
            "recovery_claim_id": str(recovery_claim.id),
            "recovery_receipt_id": str(recovery_receipt.id),
            "result_digest": result_digest,
        }


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValidationFailed(
            f"{label} alanlari exact olmali; eksik={sorted(expected - actual)},"
            f" fazla={sorted(actual - expected)}"
        )


def _uuid(value: object, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{label} UUID olmali") from exc


def _timestamp(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed("checkpoint.created_at ISO-8601 olmali") from exc
    if parsed.tzinfo is None:
        raise ValidationFailed("checkpoint.created_at timezone ister")
    return parsed


@dataclass(frozen=True, slots=True)
class RecoveryReconciliationPlan:
    """Portable evidence ref'leri ve exact canonical kimlikleri baglayan plan."""

    project_id: UUID
    work_item_id: UUID
    task_plan_id: UUID
    task_plan_digest: str
    old_completion: ReconciledCompletionRequest
    checkpoint: Checkpoint
    evidence_refs: tuple[EvidenceReference, ...]
    outcome: str = "completed"

    def __post_init__(self) -> None:
        parse_digest(self.task_plan_digest)
        if self.outcome not in {"completed", "failed-no-effect"}:
            raise ValidationFailed("Recovery outcome desteklenmiyor")
        if not self.evidence_refs:
            raise ValidationFailed("Recovery en az bir portable evidence ref ister")
        if self.checkpoint.project_id != str(self.project_id):
            raise ValidationFailed("Checkpoint project kimligi eslesmiyor")
        if self.checkpoint.work_item_id != str(self.work_item_id):
            raise ValidationFailed("Checkpoint work kimligi eslesmiyor")
        if self.checkpoint.plan_revision_id != str(self.task_plan_id):
            raise ValidationFailed("Checkpoint plan kimligi eslesmiyor")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> RecoveryReconciliationPlan:
        normalized = dict(document)
        normalized.setdefault("outcome", "completed")
        _exact_keys(
            normalized,
            {
                "schema",
                "project_id",
                "work_item_id",
                "task_plan_id",
                "task_plan_digest",
                "old_completion",
                "checkpoint",
                "evidence_refs",
                "outcome",
            },
            "Recovery document",
        )
        if document["schema"] != RECOVERY_SCHEMA:
            raise ValidationFailed("Recovery schema desteklenmiyor")
        project_id = _uuid(document["project_id"], "project_id")
        work_item_id = _uuid(document["work_item_id"], "work_item_id")
        task_plan_id = _uuid(document["task_plan_id"], "task_plan_id")

        old = document["old_completion"]
        if not isinstance(old, Mapping):
            raise ValidationFailed("old_completion object olmali")
        _exact_keys(
            old,
            {
                "job_id",
                "attempt_id",
                "claim_id",
                "fencing_token",
                "claim_digest",
                "effect_digest",
                "authorization_digest",
                "result_digest",
            },
            "old_completion",
        )

        raw_checkpoint = document["checkpoint"]
        if not isinstance(raw_checkpoint, Mapping):
            raise ValidationFailed("checkpoint object olmali")
        _exact_keys(
            raw_checkpoint,
            {
                "checkpoint_id",
                "source_revision",
                "plan_steps",
                "completed_steps",
                "pending_steps",
                "step_results",
                "context_manifest_digest",
                "journal_head_digest",
                "next_safe_action",
                "created_at",
            },
            "checkpoint",
        )
        raw_results = raw_checkpoint["step_results"]
        if not isinstance(raw_results, Mapping):
            raise ValidationFailed("checkpoint.step_results object olmali")

        raw_evidence = document["evidence_refs"]
        if not isinstance(raw_evidence, list):
            raise ValidationFailed("evidence_refs array olmali")
        evidence: list[EvidenceReference] = []
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"evidence_refs[{index}] object olmali")
            allowed = {"kind", "ref", "digest", "revision"}
            if not set(item).issubset(allowed) or not {"kind", "ref", "digest"}.issubset(item):
                raise ValidationFailed(f"evidence_refs[{index}] alanlari gecersiz")
            evidence.append(
                EvidenceReference(
                    kind=str(item["kind"]),
                    ref=str(item["ref"]),
                    evidence_digest=str(item["digest"]),
                    revision=(None if item.get("revision") is None else int(item["revision"])),
                )
            )
        evidence_refs = tuple(sorted(evidence, key=lambda item: (item.kind, item.ref)))
        evidence_digest = digest([item.as_dict() for item in evidence_refs])

        checkpoint = Checkpoint(
            checkpoint_id=str(raw_checkpoint["checkpoint_id"]),
            project_id=str(project_id),
            work_item_id=str(work_item_id),
            plan_revision_id=str(task_plan_id),
            source_revision=str(raw_checkpoint["source_revision"]),
            plan_steps=tuple(str(item) for item in raw_checkpoint["plan_steps"]),
            completed_steps=tuple(str(item) for item in raw_checkpoint["completed_steps"]),
            pending_steps=tuple(str(item) for item in raw_checkpoint["pending_steps"]),
            step_results=tuple(
                sorted((str(key), str(value)) for key, value in raw_results.items())
            ),
            context_manifest_digest=str(raw_checkpoint["context_manifest_digest"]),
            journal_head_digest=str(raw_checkpoint["journal_head_digest"]),
            next_safe_action=str(raw_checkpoint["next_safe_action"]),
            created_at=_timestamp(raw_checkpoint["created_at"]),
        )
        return cls(
            project_id=project_id,
            work_item_id=work_item_id,
            task_plan_id=task_plan_id,
            task_plan_digest=str(document["task_plan_digest"]),
            old_completion=ReconciledCompletionRequest(
                job_id=_uuid(old["job_id"], "old_completion.job_id"),
                attempt_id=_uuid(old["attempt_id"], "old_completion.attempt_id"),
                claim_id=_uuid(old["claim_id"], "old_completion.claim_id"),
                fencing_token=int(old["fencing_token"]),
                claim_digest=str(old["claim_digest"]),
                effect_digest=str(old["effect_digest"]),
                authorization_digest=str(old["authorization_digest"]),
                result_digest=str(old["result_digest"]),
                adapter_evidence_digest=evidence_digest,
            ),
            checkpoint=checkpoint,
            evidence_refs=evidence_refs,
            outcome=str(normalized["outcome"]),
        )

    @property
    def evidence_digest(self) -> str:
        return digest([item.as_dict() for item in self.evidence_refs])

    def body(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_SCHEMA,
            "operation": RECOVERY_OPERATION,
            "outcome": self.outcome,
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "task_plan_id": str(self.task_plan_id),
            "task_plan_digest": self.task_plan_digest,
            "old_completion": self.old_completion.as_dict(),
            "checkpoint": self.checkpoint.body(),
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "evidence_digest": self.evidence_digest,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    @property
    def resource(self) -> str:
        return (
            f"work:{self.project_id}:recovery:{self.work_item_id}:{self.task_plan_id}:"
            f"{self.old_completion.job_id}:{self.old_completion.claim_id}:"
            f"{self.plan_digest.removeprefix('sha256:')}"
        )

    @property
    def effect_request(self) -> EffectRequest:
        return EffectRequest(
            action=RECOVERY_OPERATION,
            effects=(EffectKind.DATABASE_WRITE,),
            resources=(self.resource,),
            required_capabilities=("database.write",),
        )

    @property
    def adapter_digest(self) -> str:
        return digest({"adapter": "recovery-reconciliation/v1", "plan": self.plan_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "plan_digest": self.plan_digest,
            "resource": self.resource,
            "effect_digest": self.effect_request.effect_digest,
            "authorization_scope": {
                "work_item_id": str(self.work_item_id),
                "task_plan_id": str(self.task_plan_id),
                "plan_digest": self.plan_digest,
                "effects": [EffectKind.DATABASE_WRITE.value],
                "resources": [self.resource],
                "max_uses": 1,
            },
            "dry_run": True,
        }


@dataclass(frozen=True, slots=True)
class RecoveryReconciliationResult:
    recovery_job_id: UUID
    recovery_attempt_id: UUID
    recovery_claim_id: UUID
    recovery_receipt_id: UUID
    checkpoint_record_id: UUID
    old_finalization: RecoveryFinalization
    result_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": False,
            "recovery_job_id": str(self.recovery_job_id),
            "recovery_attempt_id": str(self.recovery_attempt_id),
            "recovery_claim_id": str(self.recovery_claim_id),
            "recovery_receipt_id": str(self.recovery_receipt_id),
            "checkpoint_record_id": str(self.checkpoint_record_id),
            "old_receipt_id": str(self.old_finalization.receipt.id),
            "old_receipt_created": self.old_finalization.created,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True, slots=True)
class RecoveryReconciliationService:
    connection: Any
    realm: Realm

    @property
    def governance(self) -> GovernanceService:
        return GovernanceService(self.connection, self.realm)

    def validate(self, plan: RecoveryReconciliationPlan) -> TaskPlan:
        item = legacy_repository("work_item", self.connection, self.realm.id).get(plan.work_item_id)
        if item.project_id != plan.project_id:
            raise PolicyViolation("Recovery work/project kimligi eslesmiyor")
        candidates = legacy_repository("task_plan", self.connection, self.realm.id).history(
            plan.work_item_id
        )
        task_plan = cast(
            TaskPlan | None,
            next((item for item in candidates if item.id == plan.task_plan_id), None),
        )
        if task_plan is None:
            raise NotFound("Recovery task plan bulunamadi")
        if (
            task_plan.project_id != plan.project_id
            or task_plan.plan_digest != plan.task_plan_digest
        ):
            raise PolicyViolation("Recovery task plan digest veya project drift")
        if task_plan.source_revision != plan.checkpoint.source_revision:
            raise PolicyViolation("Recovery source revision drift")
        if tuple(sorted(plan.checkpoint.plan_steps)) != tuple(sorted(task_plan.execution_order)):
            raise PolicyViolation("Recovery checkpoint exact task plan partition eslesmiyor")

        host = ExecutionHost(self.connection, self.realm.id, worker_label=RECOVERY_CONSUMER)
        old_job = host.jobs.get(plan.old_completion.job_id)
        if (
            old_job.project_id != plan.project_id
            or old_job.work_item_id != plan.work_item_id
            or old_job.plan_id != plan.task_plan_id
            or old_job.step_id not in task_plan.execution_order
        ):
            raise PolicyViolation("Recovery old job work/plan/step kimligi eslesmiyor")
        if old_job.state not in {JobState.RUNNING, JobState.RECOVERY_REQUIRED}:
            raise PolicyViolation("Recovery old job durumu uygun degil")
        claims = tuple(
            claim
            for claim in host.ledger.claims_for_job(old_job.id)
            if claim.id == plan.old_completion.claim_id
        )
        if len(claims) != 1:
            raise NotFound("Recovery old claim bulunamadi")
        claim = claims[0]
        if (
            claim.attempt_id != plan.old_completion.attempt_id
            or claim.fencing_token != plan.old_completion.fencing_token
            or claim.claim_digest != plan.old_completion.claim_digest
            or claim.effect_digest != plan.old_completion.effect_digest
            or claim.authorization_digest != plan.old_completion.authorization_digest
        ):
            raise PolicyViolation("Recovery old claim exact identity drift")
        if host.ledger.receipt_for_claim(claim.id) is not None:
            raise PolicyViolation("Recovery old claim receiptless olmali")
        return task_plan

    def issue_authorization(
        self,
        plan: RecoveryReconciliationPlan,
        *,
        actor_id: UUID,
        now: dt.datetime | None = None,
    ) -> Authorization:
        self.validate(plan)
        verdict = self.governance.evaluate(plan.effect_request, now=now)
        if verdict.denial_reason != "authorization-required":
            raise PolicyViolation(
                f"Recovery authorization policy/capability reddi: {verdict.denial_reason}"
            )
        return self.governance.issue_authorization(
            request=plan.effect_request,
            actor_id=actor_id,
            plan_digest=plan.plan_digest,
            work_item_id=plan.work_item_id,
            plan_id=plan.task_plan_id,
            lifetime=dt.timedelta(minutes=15),
            now=now,
        )

    def apply(
        self,
        plan: RecoveryReconciliationPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
        effect_request_override: EffectRequest | None = None,
        before_old_finalization: (
            Callable[[ClaimedWork, EffectClaim, dt.datetime], None] | None
        ) = None,
        after_old_finalization: (
            Callable[[RecoveryFinalization, UUID, dt.datetime], None] | None
        ) = None,
    ) -> RecoveryReconciliationResult:
        moment = now or dt.datetime.now(dt.UTC)
        effect_request = effect_request_override or plan.effect_request
        with self.connection.transaction():
            self.validate(plan)
            authorization = self.governance.authorizations.get(authorization_id)
            if (
                authorization.plan_digest != plan.plan_digest
                or authorization.work_item_id != plan.work_item_id
                or authorization.plan_id != plan.task_plan_id
            ):
                raise AuthorizationRequired("Recovery authorization plan/work binding eslesmiyor")
            consumed = self.governance.require_authorized(
                effect_request,
                authorization=authorization,
                consumed_by=RECOVERY_CONSUMER,
                now=moment,
            )

            unique_capability = f"recovery.reconcile.{plan.plan_digest[-16:]}"
            host = ExecutionHost(self.connection, self.realm.id, worker_label=RECOVERY_CONSUMER)
            recovery_job, created = host.jobs.enqueue(
                Job.create(
                    realm_id=self.realm.id,
                    project_id=plan.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=f"reconcile-recovery:{plan.plan_digest}",
                    # Eski expired job proje kilidini tasiyabilir. Recovery job yeni bir
                    # logical lock edinmez; exact old job row lock finalizer tarafindan
                    # tutulur ve claim recovery resource'unu yine kayda baglar.
                    resources=(),
                    required_capabilities=("database.write", unique_capability),
                    max_attempts=1,
                    payload={
                        "recovery_plan_digest": plan.plan_digest,
                        "old_job_id": str(plan.old_completion.job_id),
                        "old_claim_id": str(plan.old_completion.claim_id),
                        "checkpoint_digest": plan.checkpoint.checkpoint_digest,
                        "evidence_digest": plan.evidence_digest,
                        "source_revision": plan.checkpoint.source_revision,
                    },
                    now=moment,
                )
            )
            if not created:
                raise PolicyViolation("Recovery plan daha once runtime job uretmis")
            work = host.acquire_work(capabilities=("database.write", unique_capability), now=moment)
            if work is None or work.job.id != recovery_job.id:
                raise PolicyViolation("Exact recovery runtime job claim edilemedi")
            recovery_claim = host.claim_effect(
                work,
                operation=RECOVERY_OPERATION,
                effect_digest=effect_request.effect_digest,
                authorization_digest=consumed.authorization_digest,
                authorization_id=consumed.id,
                resources=parse_requests(write=(plan.resource,)),
                adapter_digest=plan.adapter_digest,
                now=moment,
            )
            if before_old_finalization is not None:
                before_old_finalization(work, recovery_claim, moment)
            checkpoint_record_id = legacy_repository(
                "context_continuity",
                self.connection,
                self.realm.id,
                plan.project_id,
                plan.work_item_id,
            ).store_checkpoint(
                plan.checkpoint,
                task_plan_id=plan.task_plan_id,
                job_id=plan.old_completion.job_id,
            )
            old_claims = tuple(
                claim
                for claim in host.ledger.claims_for_job(plan.old_completion.job_id)
                if claim.id == plan.old_completion.claim_id
            )
            if len(old_claims) != 1:
                raise PolicyViolation("Recovery checkpoint old claim exact degil")
            if plan.outcome == "failed-no-effect":
                old_receipt = host.record_failure(
                    old_claims[0],
                    category=FailureCategory.ADAPTER,
                    failure_digest=plan.old_completion.result_digest,
                    now=moment,
                )
                old_finalization = host.finalize_reconciled_failure(
                    ReconciledFailureRequest(
                        job_id=plan.old_completion.job_id,
                        attempt_id=plan.old_completion.attempt_id,
                        claim_id=plan.old_completion.claim_id,
                        receipt_id=old_receipt.id,
                        fencing_token=plan.old_completion.fencing_token,
                        claim_digest=plan.old_completion.claim_digest,
                        effect_digest=plan.old_completion.effect_digest,
                        authorization_digest=plan.old_completion.authorization_digest,
                        failure_digest=plan.old_completion.result_digest,
                    ),
                    now=moment,
                )
                old_finalization = RecoveryFinalization(
                    receipt=old_finalization.receipt,
                    created=True,
                )
            else:
                old_receipt = host.record_success(
                    old_claims[0],
                    result_digest=plan.old_completion.result_digest,
                    adapter_evidence_digest=plan.old_completion.adapter_evidence_digest,
                    now=moment,
                )
                old_job = host.jobs.get(plan.old_completion.job_id)
                if old_job.run_id is not None:
                    lifecycle_repository = legacy_repository(
                        "client_lifecycle", self.connection, self.realm.id
                    )
                    recovered_execution = lifecycle_repository.reconciled_execution(
                        job_id=plan.old_completion.job_id,
                        attempt_id=plan.old_completion.attempt_id,
                        claim_id=plan.old_completion.claim_id,
                        result_digest=plan.old_completion.result_digest,
                        journal_head_digest=plan.checkpoint.journal_head_digest,
                    )
                    lifecycle_repository.store_job_checkpoint(
                        execution=recovered_execution,
                        job_id=plan.old_completion.job_id,
                        step_id=old_job.step_id or "",
                        result_digest=(
                            old_receipt.result_digest or plan.old_completion.result_digest
                        ),
                        now=moment,
                        require_lifecycle_admission=False,
                        allow_terminal_recovery=True,
                    )
                old_finalization = host.finalize_reconciled_completion(
                    plan.old_completion, now=moment
                )
                old_finalization = RecoveryFinalization(
                    receipt=old_finalization.receipt,
                    created=True,
                )
            result_digest = digest(
                {
                    "recovery_plan_digest": plan.plan_digest,
                    "checkpoint_digest": plan.checkpoint.checkpoint_digest,
                    "evidence_digest": plan.evidence_digest,
                    "old_receipt_id": str(old_finalization.receipt.id),
                }
            )
            recovery_receipt = host.record_success(
                recovery_claim,
                result_digest=result_digest,
                adapter_evidence_digest=plan.evidence_digest,
                now=moment,
            )
            if not host.finish(
                work,
                outcome=AttemptOutcome.SUCCEEDED,
                result_digest=result_digest,
                now=moment,
            ):
                raise PolicyViolation("Recovery runtime job terminal tamamlanamadi")
            if after_old_finalization is not None:
                after_old_finalization(old_finalization, checkpoint_record_id, moment)
            return RecoveryReconciliationResult(
                recovery_job_id=recovery_job.id,
                recovery_attempt_id=work.attempt_id,
                recovery_claim_id=recovery_claim.id,
                recovery_receipt_id=recovery_receipt.id,
                checkpoint_record_id=checkpoint_record_id,
                old_finalization=old_finalization,
                result_digest=result_digest,
            )
