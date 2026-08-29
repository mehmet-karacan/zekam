"""Terminal job kanitiyla stale active execution run uzlastirmasi."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.execution import ExecutionHost
from zekam.application.governance import EffectRequest, GovernanceService
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.realm import Realm
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, Job, JobKind
from zekam.domain.security import Authorization
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository

RUN_RECONCILIATION_OPERATION = "reconcile-terminal-execution-run"
RUN_RECONCILIATION_CONSUMER = "cli:worker-reconcile-terminal-run"


@dataclass(frozen=True, slots=True)
class TerminalRunReconciliationPlan:
    run_id: UUID
    project_id: UUID
    work_item_id: UUID
    task_plan_id: UUID
    evidence_digest: str

    @property
    def resource(self) -> str:
        return (
            f"work:{self.project_id}:execution-run:{self.work_item_id}:{self.run_id}"
        )

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-terminal-run-reconciliation/v1",
                "run_id": str(self.run_id),
                "project_id": str(self.project_id),
                "work_item_id": str(self.work_item_id),
                "task_plan_id": str(self.task_plan_id),
                "target_state": "failed",
                "evidence_digest": self.evidence_digest,
            }
        )

    @property
    def effect_request(self) -> EffectRequest:
        return EffectRequest(
            action=RUN_RECONCILIATION_OPERATION,
            effects=(EffectKind.DATABASE_WRITE,),
            resources=(self.resource,),
            required_capabilities=("database.write",),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-terminal-run-reconciliation/v1",
            "run_id": str(self.run_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "task_plan_id": str(self.task_plan_id),
            "target_state": "failed",
            "evidence_digest": self.evidence_digest,
            "resource": self.resource,
            "plan_digest": self.plan_digest,
            "effect_digest": self.effect_request.effect_digest,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class TerminalRunReconciliationService:
    connection: Any
    realm: Realm

    @property
    def governance(self) -> GovernanceService:
        return GovernanceService(self.connection, self.realm)

    def prepare(
        self, *, run_id: UUID, now: dt.datetime | None = None
    ) -> TerminalRunReconciliationPlan:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,plan_id from runtime.execution_run"
                " where realm_id=%s and id=%s and state='active'",
                (self.realm.id, run_id),
            )
            header = cursor.fetchone()
            if header is None or header[1] is None or header[2] is None:
                raise PolicyViolation("Run reconciliation exact active work/plan run ister")
            cursor.execute(
                "select job.id,job.state,job.step_id,attempt.id,attempt.outcome,"
                " attempt.result_digest,claim.id,receipt.id,receipt.status,"
                " coalesce(receipt.result_digest,receipt.failure_digest,"
                " receipt.adapter_evidence_digest)"
                " from runtime.job job"
                " left join runtime.job_attempt attempt on attempt.realm_id=job.realm_id"
                " and attempt.job_id=job.id"
                " left join runtime.effect_claim claim on claim.realm_id=job.realm_id"
                " and claim.job_id=job.id and claim.attempt_id=attempt.id"
                " left join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
                " and receipt.claim_id=claim.id"
                " where job.realm_id=%s and job.run_id=%s"
                " order by job.id,attempt.id,claim.id",
                (self.realm.id, run_id),
            )
            rows = cursor.fetchall()
            if not rows or not any(str(row[1]) == "failed" for row in rows):
                raise PolicyViolation("Run reconciliation en az bir failed terminal job ister")
            if any(str(row[1]) not in {"completed", "failed", "cancelled"} for row in rows):
                raise PolicyViolation("Run reconciliation live job varken reddedildi")
            if any(row[3] is None or row[4] is None for row in rows):
                raise PolicyViolation("Run reconciliation terminal attempt kaniti eksik")
            if any(row[6] is not None and row[7] is None for row in rows):
                raise PolicyViolation("Run reconciliation receiptless claim varken reddedildi")
            cursor.execute(
                "select count(*) from runtime.lease lease"
                " join runtime.job job on job.realm_id=lease.realm_id and job.id=lease.job_id"
                " where lease.realm_id=%s and job.run_id=%s and lease.expires_at>%s",
                (self.realm.id, run_id, moment),
            )
            if int(cursor.fetchone()[0]) != 0:
                raise PolicyViolation("Run reconciliation live lease varken reddedildi")
        evidence = [
            {
                "job_id": str(row[0]),
                "job_state": str(row[1]),
                "step_id": None if row[2] is None else str(row[2]),
                "attempt_id": str(row[3]),
                "attempt_outcome": str(row[4]),
                "attempt_result_digest": None if row[5] is None else str(row[5]),
                "claim_id": None if row[6] is None else str(row[6]),
                "receipt_id": None if row[7] is None else str(row[7]),
                "receipt_status": None if row[8] is None else str(row[8]),
                "receipt_digest": None if row[9] is None else str(row[9]),
            }
            for row in rows
        ]
        return TerminalRunReconciliationPlan(
            run_id=run_id,
            project_id=UUID(str(header[0])),
            work_item_id=UUID(str(header[1])),
            task_plan_id=UUID(str(header[2])),
            evidence_digest=digest(evidence),
        )

    def issue_authorization(
        self, plan: TerminalRunReconciliationPlan, *, actor_id: UUID, now: dt.datetime | None = None
    ) -> Authorization:
        current = self.prepare(run_id=plan.run_id, now=now)
        if current.plan_digest != plan.plan_digest:
            raise PolicyViolation("Run reconciliation plan drift")
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
        plan: TerminalRunReconciliationPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.transaction():
            current = self.prepare(run_id=plan.run_id, now=moment)
            if current.plan_digest != plan.plan_digest:
                raise PolicyViolation("Run reconciliation apply drift")
            authorization = self.governance.authorizations.get(authorization_id)
            if (
                authorization.plan_digest != plan.plan_digest
                or authorization.work_item_id != plan.work_item_id
                or authorization.plan_id != plan.task_plan_id
            ):
                raise AuthorizationRequired("Run reconciliation authorization binding eslesmiyor")
            consumed = self.governance.require_authorized(
                plan.effect_request,
                authorization=authorization,
                consumed_by=RUN_RECONCILIATION_CONSUMER,
                now=moment,
            )
            capability = f"recovery.run.{plan.plan_digest[-16:]}"
            host = ExecutionHost(
                self.connection, self.realm.id, worker_label=RUN_RECONCILIATION_CONSUMER
            )
            job, created = host.jobs.enqueue(
                Job.create(
                    realm_id=self.realm.id,
                    project_id=plan.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=f"reconcile-run:{plan.plan_digest}",
                    required_capabilities=("database.write", capability),
                    max_attempts=1,
                    payload={
                        "schema": "zekam-terminal-run-reconciliation-job/v1",
                        "run_id": str(plan.run_id),
                    },
                    now=moment,
                )
            )
            if not created:
                raise PolicyViolation("Run reconciliation job replay reddedildi")
            work = host.acquire_work(capabilities=("database.write", capability), now=moment)
            if work is None or work.job.id != job.id:
                raise PolicyViolation("Run reconciliation job claim edilemedi")
            claim = host.claim_effect(
                work,
                operation=RUN_RECONCILIATION_OPERATION,
                effect_digest=plan.effect_request.effect_digest,
                authorization_digest=consumed.authorization_digest,
                authorization_id=consumed.id,
                resources=parse_requests(write=(plan.resource,)),
                adapter_digest=digest({"adapter": "terminal-run-reconciliation/v1"}),
                now=moment,
            )
            ExecutionRunRepository(self.connection, self.realm.id).finish_run(
                plan.run_id, state="failed", terminal_at=moment
            )
            result_digest = digest(
                {"run_id": str(plan.run_id), "state": "failed", "evidence": plan.evidence_digest}
            )
            receipt = host.record_success(
                claim,
                result_digest=result_digest,
                adapter_evidence_digest=plan.evidence_digest,
                now=moment,
            )
            if not host.finish(work, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest):
                raise PolicyViolation("Run reconciliation job tamamlanamadi")
        return {
            "schema": "zekam-terminal-run-reconciliation-result/v1",
            "run_id": str(plan.run_id),
            "state": "failed",
            "reconciliation_job_id": str(job.id),
            "claim_id": str(claim.id),
            "receipt_id": str(receipt.id),
            "result_digest": result_digest,
        }
