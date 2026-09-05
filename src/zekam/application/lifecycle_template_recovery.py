"""Exact continuation for a materialized lifecycle template with no receipt."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from zekam.application.control_plane_completion import (
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionService,
)
from zekam.application.legacy_repository_provider import legacy_repository
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleTemplatePreparePlan,
    _bind_prepare_runtime,
    _prepare_manifest,
)
from zekam.application.recovery_reconciliation import (
    RecoveryReconciliationPlan,
    RecoveryReconciliationResult,
    RecoveryReconciliationService,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint, EvidenceReference, JournalEntry
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.realm import Realm
from zekam.domain.runtime import ClaimedWork, Lease, ReconciledCompletionRequest
from zekam.domain.security import Authorization
from zekam.domain.work import AcceptanceCriterion, EffectKind, EvidenceRef, WorkState


def _template_digest(template: Any) -> str:
    return digest(
        {
            "routing_context": template.routing_context_digest,
            "route": template.route_decision_digest,
            "target": template.execution_target_digest,
            "provider": template.provider_binding_digest,
            "environment": template.execution_environment_snapshot_digest,
            "hooks": template.hook_set_digest,
            "tools": template.compiled_tool_set_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class LifecycleTemplateRecoveryPlan:
    lifecycle_plan: LifecycleTemplatePreparePlan
    reconciliation: RecoveryReconciliationPlan
    template_digest: str
    lease_id: UUID
    lease_owner_digest: str
    lease_expires_at: dt.datetime
    lease_heartbeat_at: dt.datetime
    lease_worker_label: str

    @property
    def plan_digest(self) -> str:
        return self.reconciliation.plan_digest

    def as_dict(self) -> dict[str, Any]:
        return self.reconciliation.as_dict() | {
            "lifecycle_template_digest": self.template_digest,
            "old_lease_id": str(self.lease_id),
            "old_lease_expires_at": self.lease_expires_at,
        }


@dataclass(frozen=True, slots=True)
class LifecycleTemplateRecoveryService:
    connection: Any
    realm: Realm

    def prepare(
        self, *, job_id: UUID, actor_id: UUID, now: dt.datetime | None = None
    ) -> LifecycleTemplateRecoveryPlan:
        moment = now or dt.datetime.now(dt.UTC)
        job = legacy_repository("job", self.connection, self.realm.id).get(job_id)
        payload = dict(job.payload)
        if (
            payload.get("schema") != "zekam-lifecycle-template-prepare-job/v1"
            or job.work_item_id is None
            or job.plan_id is None
            or job.run_id is None
            or job.state.value not in {"running", "recovery-required"}
        ):
            raise PolicyViolation("Lifecycle template recovery exact old job ister")
        plan = LifecycleTemplatePreparePlan(
            realm_id=self.realm.id,
            project_id=job.project_id,
            work_item_id=UUID(str(payload["target_work_item_id"])),
            work_revision=int(payload["target_work_revision"]),
            actor_id=UUID(str(payload["actor_id"])),
            source_revision=str(payload["source_revision"]),
            policy_digest=str(payload["policy_digest"]),
            adopt_existing=bool(payload.get("adopt_existing", False)),
            prepared_at=dt.datetime.fromisoformat(str(payload["prepared_at"])),
            expires_at=dt.datetime.fromisoformat(str(payload["expires_at"])),
        )
        if actor_id != plan.actor_id or plan.plan_digest != str(payload["plan_digest"]):
            raise PolicyViolation("Lifecycle template recovery actor/plan drift")
        ledger = legacy_repository("effect_ledger", self.connection, self.realm.id)
        claims = ledger.claims_for_job(job.id)
        if len(claims) != 1 or ledger.receipt_for_claim(claims[0].id) is not None:
            raise PolicyViolation("Lifecycle template recovery exact receiptless claim ister")
        claim = claims[0]
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select attempt.id,lease.id,lease.owner_digest,lease.expires_at,"
                " lease.heartbeat_at,lease.worker_label from runtime.job_attempt attempt"
                " join runtime.lease lease on lease.realm_id=attempt.realm_id"
                " and lease.attempt_id=attempt.id and lease.job_id=attempt.job_id"
                " where attempt.realm_id=%s and attempt.job_id=%s"
                " and attempt.id=%s and attempt.fencing_token=%s",
                (self.realm.id, job.id, claim.attempt_id, claim.fencing_token),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "select count(*) from runtime.execution_envelope"
                " where realm_id=%s and job_id=%s and attempt_id=%s",
                (self.realm.id, job.id, claim.attempt_id),
            )
            envelope_count = int(cursor.fetchone()[0])
        if len(rows) != 1 or rows[0][3] > moment or envelope_count:
            raise PolicyViolation(
                "Lifecycle template recovery expired lease ve eksik envelope ister"
            )
        template = legacy_repository(
            "lifecycle_runtime_template", self.connection, self.realm.id
        ).at_effect(job.id, claim.id)
        template_digest = _template_digest(template)
        result_digest = digest(
            {
                "schema": "zekam-lifecycle-template-recovered-result/v1",
                "plan_digest": plan.plan_digest,
                "template_digest": template_digest,
                "provider_calls": 0,
                "network_calls": 0,
            }
        )
        plans = legacy_repository("task_plan", self.connection, self.realm.id).history(
            job.work_item_id
        )
        task_plan = next((item for item in plans if item.id == job.plan_id), None)
        if task_plan is None:
            raise PolicyViolation("Lifecycle template recovery exact task plan ister")
        evidence = EvidenceReference(
            kind="artifact",
            ref=f"lifecycle-template/{job.project_id}/{plan.source_revision}",
            evidence_digest=template_digest,
        )
        journal_head = legacy_repository(
            "context_continuity",
            self.connection,
            self.realm.id,
            job.project_id,
            job.work_item_id,
        ).journal_head()
        previous_journal_digest = None if journal_head is None else journal_head[1]
        recovered_journal = JournalEntry(
            1 if journal_head is None else journal_head[0] + 1,
            str(job.work_item_id),
            "step-completed",
            result_digest,
            previous_journal_digest,
            False,
            rows[0][3],
        )
        checkpoint = Checkpoint(
            checkpoint_id=f"lifecycle-template-recovery-{job.id}",
            project_id=str(job.project_id),
            work_item_id=str(job.work_item_id),
            plan_revision_id=str(job.plan_id),
            source_revision=plan.source_revision,
            plan_steps=task_plan.execution_order,
            completed_steps=task_plan.execution_order,
            pending_steps=(),
            step_results=((job.step_id or "lifecycle-template-prepare", result_digest),),
            context_manifest_digest=_prepare_manifest(plan, job.work_item_id)[1].manifest_digest,
            journal_head_digest=recovered_journal.entry_digest,
            next_safe_action="target-client-runtime-bootstrap",
            created_at=rows[0][3],
        )
        recovery = RecoveryReconciliationPlan(
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            task_plan_id=job.plan_id,
            task_plan_digest=task_plan.plan_digest,
            old_completion=ReconciledCompletionRequest(
                job_id=job.id,
                attempt_id=claim.attempt_id,
                claim_id=claim.id,
                fencing_token=claim.fencing_token,
                claim_digest=claim.claim_digest,
                effect_digest=claim.effect_digest,
                authorization_digest=claim.authorization_digest,
                result_digest=result_digest,
                adapter_evidence_digest=digest([evidence.as_dict()]),
            ),
            checkpoint=checkpoint,
            evidence_refs=(evidence,),
        )
        row = rows[0]
        return LifecycleTemplateRecoveryPlan(
            plan,
            recovery,
            template_digest,
            UUID(str(row[1])),
            str(row[2]),
            row[3],
            row[4],
            str(row[5]),
        )

    def issue_authorization(
        self, plan: LifecycleTemplateRecoveryPlan, *, actor_id: UUID, now: dt.datetime | None = None
    ) -> Authorization:
        if actor_id != plan.lifecycle_plan.actor_id:
            raise PolicyViolation("Lifecycle template recovery actor drift")
        RecoveryReconciliationService(self.connection, self.realm).validate(plan.reconciliation)
        return RecoveryReconciliationService(
            self.connection, self.realm
        ).governance.issue_authorization(
            request=replace(plan.reconciliation.effect_request, required_capabilities=()),
            actor_id=actor_id,
            plan_digest=plan.plan_digest,
            work_item_id=plan.reconciliation.work_item_id,
            plan_id=plan.reconciliation.task_plan_id,
            lifetime=dt.timedelta(minutes=15),
            now=now,
        )

    def apply(
        self,
        plan: LifecycleTemplateRecoveryPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> RecoveryReconciliationResult:
        moment = now or dt.datetime.now(dt.UTC)
        current = self.prepare(
            job_id=plan.reconciliation.old_completion.job_id,
            actor_id=plan.lifecycle_plan.actor_id,
            now=moment,
        )
        if (
            current.plan_digest != plan.plan_digest
            or current.template_digest != plan.template_digest
        ):
            raise PolicyViolation("Lifecycle template recovery current plan drift")
        job = legacy_repository("job", self.connection, self.realm.id).get(
            plan.reconciliation.old_completion.job_id
        )
        assert job.run_id is not None and job.plan_id is not None and job.work_item_id is not None
        run_id = job.run_id
        task_plan_id = job.plan_id
        prep_work_id = job.work_item_id
        claim = legacy_repository("effect_ledger", self.connection, self.realm.id).claims_for_job(
            job.id
        )[0]
        old_authorization = legacy_repository("authorization", self.connection, self.realm.id).get(
            UUID(str(job.payload["authorization_id"]))
        )
        historical_template = legacy_repository(
            "lifecycle_runtime_template", self.connection, self.realm.id
        ).at_effect(job.id, claim.id)
        if _template_digest(historical_template) != plan.template_digest:
            raise PolicyViolation("Lifecycle template recovery historical template drift")
        runtime: list[Any] = []

        def before(
            recovery_work: ClaimedWork, recovery_claim: Any, terminal_at: dt.datetime
        ) -> None:
            if len(recovery_claim.resources) != 1:
                raise PolicyViolation("Lifecycle template recovery exact tek resource ister")
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "insert into runtime.recovery_envelope_admission"
                    " (id,realm_id,old_job_id,old_attempt_id,old_lease_id,old_fencing_token,"
                    " old_claim_id,recovery_job_id,recovery_attempt_id,recovery_claim_id,"
                    " resource,expires_at) values"
                    " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_uuid7(now=terminal_at),
                        self.realm.id,
                        job.id,
                        claim.attempt_id,
                        plan.lease_id,
                        claim.fencing_token,
                        claim.id,
                        recovery_work.job.id,
                        recovery_work.attempt_id,
                        recovery_claim.id,
                        str(recovery_claim.resources[0].resource),
                        terminal_at + dt.timedelta(minutes=5),
                    ),
                )
            lease = Lease(
                id=plan.lease_id,
                realm_id=self.realm.id,
                job_id=job.id,
                attempt_id=claim.attempt_id,
                owner_digest=plan.lease_owner_digest,
                fencing_token=claim.fencing_token,
                expires_at=plan.lease_expires_at,
                heartbeat_at=plan.lease_heartbeat_at,
                worker_label=plan.lease_worker_label,
            )
            recovered_result = {
                "schema": "zekam-lifecycle-template-recovered-result/v1",
                "plan_digest": plan.lifecycle_plan.plan_digest,
                "template_digest": plan.template_digest,
                "provider_calls": 0,
                "network_calls": 0,
            }
            runtime.extend(
                _bind_prepare_runtime(
                    connection=self.connection,
                    realm=self.realm,
                    claimed=ClaimedWork(job, claim.attempt_id, lease, "recovery-owner-unavailable"),
                    plan=plan.lifecycle_plan,
                    authorization=old_authorization,
                    result=recovered_result,
                    # The missing envelope describes the already-observed historical
                    # effect. Bind it at the immutable claim time; terminal recovery
                    # records continue to use ``terminal_at`` below.
                    now=claim.claimed_at,
                    journal_created_at=plan.reconciliation.checkpoint.created_at,
                    template_override=historical_template,
                )
            )

        def after(finalization: Any, checkpoint_id: UUID, terminal_at: dt.datetime) -> None:
            legacy_repository("execution_run", self.connection, self.realm.id).finish_run(
                run_id, state="completed", terminal_at=terminal_at
            )
            legacy_repository(
                "agent_assignment", self.connection, self.realm.id
            ).complete_terminal_plan(task_plan_id, now=terminal_at)
            graph = WorkGraphService(
                self.connection, self.realm, actor_id=plan.lifecycle_plan.actor_id
            )
            prep_work = graph.items.get(prep_work_id)
            graph.update_details(
                prep_work.id,
                acceptance_criteria=tuple(
                    AcceptanceCriterion(item.text, verified=True)
                    for item in prep_work.acceptance_criteria
                ),
                reason="Lifecycle template recovery receipt verified",
                now=terminal_at,
            )
            graph.transition(prep_work.id, WorkState.VERIFICATION, now=terminal_at)
            ControlPlaneCompletionService(
                legacy_repository("control_plane_completion", self.connection, self.realm.id)
            ).complete(
                ControlPlaneCompletionRequest(
                    project_id=job.project_id,
                    work_item_id=prep_work_id,
                    task_plan_id=task_plan_id,
                    job_id=job.id,
                    attempt_id=claim.attempt_id,
                    checkpoint_id=checkpoint_id,
                    source_authorization_id=old_authorization.id,
                    source_authorization_digest=old_authorization.authorization_digest,
                    source_claim_id=claim.id,
                    source_claim_digest=claim.claim_digest,
                    source_effect_receipt_id=finalization.receipt.id,
                    source_operation=claim.operation,
                    source_consumed_by="worker:lifecycle-template-prepare",
                    source_effect_digest=claim.effect_digest,
                    source_adapter_digest=claim.adapter_digest,
                    source_adapter_evidence_digest=plan.reconciliation.evidence_digest,
                    source_resources=tuple(str(item.resource) for item in claim.resources),
                    source_effects=(EffectKind.DATABASE_WRITE.value,),
                    source_data_classifications=("local-only",),
                    evidence=(
                        EvidenceRef(
                            "runtime-receipt",
                            str(finalization.receipt.id),
                            plan.reconciliation.old_completion.result_digest,
                        ),
                    ),
                )
            )

        return RecoveryReconciliationService(self.connection, self.realm).apply(
            plan.reconciliation,
            authorization_id=authorization_id,
            now=moment,
            effect_request_override=replace(
                plan.reconciliation.effect_request, required_capabilities=()
            ),
            before_old_finalization=before,
            after_old_finalization=after,
        )
