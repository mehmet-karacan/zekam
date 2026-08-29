"""Durable worker adapter for the staged Codex projection-aware close step."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from zekam.application.client_runtime_bootstrap import _CLOSE_STEP_ID
from zekam.application.execution import ExecutionHost
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.projection_closure import (
    PROJECTION_CLOSURE_ADAPTER_DIGEST,
    PROJECTION_CLOSURE_OPERATION,
    ProjectionAwareClosureService,
)
from zekam.domain.agents import AgentInvocation
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    TurnExecutionSnapshot,
)
from zekam.domain.execution_run import CheckpointDisposition, ExecutionEnvelope
from zekam.domain.resources import parse_requests
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CloseStatus,
    DigestReference,
    SessionCloseReceipt,
    TruthClass,
)
from zekam.domain.work import EvidenceRef, WorkState
from zekam.infrastructure.postgres.agent_assignment_repository import (
    AgentAssignmentRepository,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
    LifecycleRuntimeTemplateRepository,
    template_source_revision,
)
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.projection_closure_repository import (
    ProjectionClosureRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

_CLOSE_NAMESPACE = UUID("24cfdca7-e4d6-5fcc-b171-07f6db7a91d3")
_CLOSE_VERIFIER_NAMESPACE = UUID("8449e4c2-b565-5c90-87e7-cac161d24d5d")
_CAPABILITY = "client.lifecycle.projection-close"


@dataclass(frozen=True, slots=True)
class ProjectionCloseRuntimeService:
    connection: Any
    realm_id: UUID

    def next_ready_job_id(self) -> UUID | None:
        """Return only a close job whose immutable prerequisites are complete."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select close_job.id from runtime.job close_job"
                " join work.work_item item on item.realm_id=close_job.realm_id"
                " and item.id=close_job.work_item_id"
                " join runtime.execution_run run on run.realm_id=close_job.realm_id"
                " and run.id=close_job.run_id"
                " join runtime.job lifecycle_job on lifecycle_job.realm_id=close_job.realm_id"
                " and lifecycle_job.id=(close_job.payload->>'lifecycle_job_id')::uuid"
                " where close_job.realm_id=%s and close_job.state='ready'"
                " and close_job.required_capabilities=array[%s]::text[]"
                " and close_job.step_id=%s and close_job.max_attempts=1"
                " and close_job.payload->>'schema'='zekam-projection-close-job/v1'"
                " and item.state='verification'"
                " and not exists(select 1 from jsonb_array_elements(item.acceptance_criteria) c"
                "   where c->'verified' is distinct from 'true'::jsonb)"
                " and run.state='active' and lifecycle_job.state='completed'"
                " and lifecycle_job.run_id=close_job.run_id"
                " and lifecycle_job.plan_id=close_job.plan_id"
                " order by close_job.available_at,close_job.id limit 1",
                (self.realm_id, _CAPABILITY, _CLOSE_STEP_ID),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def assert_release_ready(self, job_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,run_id from runtime.job"
                " where realm_id=%s and id=%s and state='ready'",
                (self.realm_id, job_id),
            )
            row = cursor.fetchone()
            if row is None or row[1] is None or row[2] is None:
                raise PolicyViolation("Projection close ready job identity eksik")
            cursor.execute(
                "select session_id,client_id from runtime.execution_run"
                " where realm_id=%s and id=%s and state='active'",
                (self.realm_id, row[2]),
            )
            run = cursor.fetchone()
        if run is None:
            raise PolicyViolation("Projection close active run bulunamadi")
        release = MemoryContinuityRepository(
            self.connection, self.realm_id
        ).read_projection_release_snapshot(
            project_id=UUID(str(row[0])),
            work_item_id=UUID(str(row[1])),
            run_id=UUID(str(row[2])),
            session_id=str(run[0]),
            client_id=str(run[1]),
        )
        release.assert_release_ready(
            expected_source_digest=release.expected_projection_source_digest
        )

    def execute(self, work: Any, *, now: dt.datetime | None = None) -> str:
        moment = now or dt.datetime.now(dt.UTC)
        job = work.job
        payload = dict(job.payload)
        if (
            set(payload)
            != {
                "schema",
                "source_authorization_id",
                "lifecycle_job_id",
                "entry_digest",
            }
            or payload.get("schema") != "zekam-projection-close-job/v1"
            or job.required_capabilities != (_CAPABILITY,)
            or job.step_id != _CLOSE_STEP_ID
            or any(
                value is None
                for value in (job.work_item_id, job.plan_id, job.assignment_id, job.run_id)
            )
        ):
            raise PolicyViolation("Projection close immutable job contract drift")
        source_authorization_id = UUID(str(payload["source_authorization_id"]))
        actor_id = self._source_actor_id(
            source_authorization_id=source_authorization_id,
            work_item_id=job.work_item_id,
            plan_id=job.plan_id,
        )
        resource = f"work:{job.project_id}:{job.work_item_id}:projection-close:{job.run_id}"
        if tuple(str(request.resource) for request in job.resources) != (resource,):
            raise PolicyViolation("Projection close exact resource drift")

        checkpoint_id, checkpoint, envelope, completed_results = self._bind_checkpoint_envelope(
            work, resource=resource, now=moment
        )
        receipt = self._receipt(
            work,
            checkpoint_id=checkpoint_id,
            checkpoint=checkpoint,
            envelope=envelope,
            completed_results=completed_results,
            now=moment,
        )
        authorizations = AuthorizationRepository(self.connection, self.realm_id)
        service = ProjectionAwareClosureService(
            ProjectionClosureRepository(self.connection, self.realm_id), authorizations
        )
        idempotency_key = f"projection-close:{payload['entry_digest']}:job:{job.id}"
        plan = service.prepare(receipt, idempotency_key=idempotency_key, now=moment)
        # The local close authorization, effect claim and terminal receipt are
        # one transaction. A crash cannot strand newly minted high-risk
        # authority without its claim/receipt chain.
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("set transaction isolation level serializable")
            scope = AuthorizationScope(
                allowed_resources=(plan.resource,), allowed_effects=("database-write",)
            )
            authorization = Authorization.issue(
                realm_id=self.realm_id,
                actor_id=actor_id,
                work_item_id=job.work_item_id,
                plan_id=job.plan_id,
                plan_digest=plan.plan_digest,
                effect_digest=plan.effect_digest,
                scope=scope,
                risk="high",
                lifetime=dt.timedelta(minutes=10),
                now=moment,
            )
            authorizations.issue(authorization)
            host = ExecutionHost(
                self.connection, self.realm_id, worker_label=work.lease.worker_label
            )
            claim = host.claim_effect(
                work,
                operation=PROJECTION_CLOSURE_OPERATION,
                effect_digest=plan.effect_digest,
                authorization_digest=authorization.authorization_digest,
                resources=parse_requests(write=(plan.resource,)),
                adapter_digest=PROJECTION_CLOSURE_ADAPTER_DIGEST,
                authorization_id=authorization.id,
                idempotency_key=plan.claim_idempotency_key,
                now=moment,
            )
            applied = service.apply(
                plan,
                authorization_id=authorization.id,
                claim_id=claim.id,
                now=moment,
                transaction_bound=True,
            )
            return applied.result_digest

    def _source_actor_id(
        self, *, source_authorization_id: UUID, work_item_id: UUID, plan_id: UUID
    ) -> UUID:
        """Resolve the human authority whose completed bootstrap caused this close."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select auth.actor_id from security.authorization auth"
                " join core.actor actor on actor.realm_id=auth.realm_id"
                " and actor.id=auth.actor_id"
                " join runtime.effect_claim claim on claim.realm_id=auth.realm_id"
                " and claim.authorization_id=auth.id"
                " join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
                " and receipt.claim_id=claim.id"
                " where auth.realm_id=%s and auth.id=%s"
                " and auth.work_item_id=%s and auth.plan_id=%s"
                " and actor.kind='human' and actor.status='active'"
                " and claim.operation='client-lifecycle-bootstrap-materialize/v1'"
                " and receipt.status='completed' and receipt.result_digest is not null",
                (self.realm_id, source_authorization_id, work_item_id, plan_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Projection close completed human source authority ister")
        return UUID(str(rows[0][0]))

    def _bind_checkpoint_envelope(
        self, work: Any, *, resource: str, now: dt.datetime
    ) -> tuple[UUID, Checkpoint, ExecutionEnvelope, tuple[tuple[str, str], ...]]:
        job = work.job
        assert job.work_item_id is not None
        assert job.plan_id is not None
        assert job.assignment_id is not None
        assert job.run_id is not None
        template_repo = LifecycleRuntimeTemplateRepository(self.connection, self.realm_id)
        run = template_repo.run_bindings(job.run_id)
        parent_job_id, manifest_id, manifest_digest, packet_id, packet_digest = (
            template_repo.bootstrap_context(job.run_id)
        )
        source_revision, policy_digest = str(run[0]), str(run[1])
        template = template_repo.current(
            job.project_id, template_source_revision(source_revision), policy_digest
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select work.task_plan_execution_order(steps) from work.task_plan"
                " where realm_id=%s and id=%s",
                (self.realm_id, job.plan_id),
            )
            plan_row = cursor.fetchone()
            cursor.execute(
                "select prior.step_id,attempt.result_digest from runtime.job prior"
                " join lateral (select result_digest from runtime.job_attempt attempt"
                "   where attempt.realm_id=prior.realm_id and attempt.job_id=prior.id"
                "   and attempt.outcome='succeeded' order by attempt.attempt_number desc limit 1)"
                " attempt on true where prior.realm_id=%s and prior.run_id=%s"
                " and prior.id<>%s and prior.state='completed' order by prior.step_id",
                (self.realm_id, job.run_id, job.id),
            )
            results = tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
        if plan_row is None:
            raise PolicyViolation("Projection close TaskPlan bulunamadi")
        steps = tuple(str(value) for value in plan_row[0])
        result_map = dict(results)
        if (
            steps[-1:] != (_CLOSE_STEP_ID,)
            or set(result_map) != set(steps[:-1])
            or len(result_map) != len(results)
        ):
            raise PolicyViolation("Projection close prior step result zinciri eksik")
        ordered_results = tuple((step, result_map[step]) for step in steps[:-1])
        continuity = ContextContinuityRepository(
            self.connection, self.realm_id, job.project_id, job.work_item_id
        )
        journal = continuity.journal_head()
        if journal is None:
            raise PolicyViolation("Projection close WorkJournal head ister")
        checkpoint = Checkpoint(
            checkpoint_id=f"projection-close-{job.id}",
            project_id=str(job.project_id),
            work_item_id=str(job.work_item_id),
            plan_revision_id=str(job.plan_id),
            source_revision=source_revision,
            plan_steps=steps,
            completed_steps=steps[:-1],
            pending_steps=(_CLOSE_STEP_ID,),
            step_results=ordered_results,
            context_manifest_digest=manifest_digest,
            journal_head_digest=journal[1],
            next_safe_action="apply-atomic-projection-close",
            created_at=now,
        )
        checkpoint_id = continuity.store_checkpoint(
            checkpoint, task_plan_id=job.plan_id, job_id=job.id
        )
        execution = ExecutionRunRepository(self.connection, self.realm_id)
        execution.bind_assignment_environment(
            AssignmentEnvironmentBinding.create(
                realm_id=self.realm_id,
                assignment_id=job.assignment_id,
                execution_environment_snapshot_digest=(
                    template.execution_environment_snapshot_digest
                ),
                bound_at=now,
            )
        )
        turn = TurnExecutionSnapshot.create(
            realm_id=self.realm_id,
            assignment_id=job.assignment_id,
            run_id=job.run_id,
            attempt_id=work.attempt_id,
            client_session_id=str(run[6]),
            turn_id=f"projection-close-{job.id}",
            model_id=template.model_id,
            provider_id=template.provider_ref,
            route_decision_digest=template.route_decision_digest,
            reasoning_profile_digest=digest("projection-close-runtime-reasoning/v1"),
            execution_environment_snapshot_digest=(template.execution_environment_snapshot_digest),
            context_manifest_digest=manifest_digest,
            exposed_tool_set_digest=template.compiled_tool_set_digest,
            hook_set_digest=template.hook_set_digest,
            config_effective_digest=template.config_effective_digest,
            created_at=now,
        )
        execution.create_turn_snapshot(turn)
        scope = AuthorizationScope(
            allowed_resources=(resource,), allowed_effects=("database-write",)
        )
        envelope = ExecutionEnvelope.create(
            realm_id=self.realm_id,
            run_id=job.run_id,
            job_id=job.id,
            attempt_id=work.attempt_id,
            lease_id=work.lease.id,
            fencing_token=work.lease.fencing_token,
            request_ordinal=1,
            idempotency_key=f"projection-close-envelope:{job.id}",
            assignment_id=job.assignment_id,
            role="builder",
            route_decision_id=template.route_decision_id,
            route_decision_digest=template.route_decision_digest,
            route_expires_at=template.route_expires_at,
            model_id=template.model_id,
            provider_binding_id=template.provider_binding_id,
            provider_binding_digest=template.provider_binding_digest,
            provider_ref=template.provider_ref,
            context_manifest_id=manifest_id,
            context_manifest_digest=manifest_digest,
            context_packet_id=packet_id,
            context_packet_digest=packet_digest,
            turn_execution_snapshot_id=turn.id,
            turn_execution_snapshot_digest=turn.turn_snapshot_digest,
            checkpoint_id=checkpoint_id,
            checkpoint_digest=checkpoint.checkpoint_digest,
            checkpoint_disposition=CheckpointDisposition.BOUND,
            source_revision=source_revision,
            policy_digest=policy_digest,
            authorization_scope_digest=digest(scope.body()),
            output_schema_digest=digest("projection-aware-close-output/v1"),
            payload_digest=digest(
                {
                    "parent_job_id": str(parent_job_id),
                    "close_job_id": str(job.id),
                    "checkpoint_digest": checkpoint.checkpoint_digest,
                }
            ),
            max_input_tokens=int(run[2]),
            max_output_tokens=int(run[3]),
            max_cost_micros=int(run[4]),
            deadline=run[5],
            created_at=now,
        )
        execution.create_envelope(envelope)
        return checkpoint_id, checkpoint, envelope, ordered_results

    def _receipt(
        self,
        work: Any,
        *,
        checkpoint_id: UUID,
        checkpoint: Checkpoint,
        envelope: ExecutionEnvelope,
        completed_results: tuple[tuple[str, str], ...],
        now: dt.datetime,
    ) -> SessionCloseReceipt:
        job = work.job
        assert job.work_item_id is not None
        assert job.run_id is not None
        current = WorkItemRepository(self.connection, self.realm_id).get(job.work_item_id)
        if current.state is not WorkState.VERIFICATION or any(
            not criterion.verified for criterion in current.acceptance_criteria
        ):
            raise PolicyViolation("Projection close verified Work ister")
        session_id, client_id = self._run_identity(job.run_id)
        release = MemoryContinuityRepository(
            self.connection, self.realm_id
        ).read_projection_release_snapshot(
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            run_id=job.run_id,
            session_id=session_id,
            client_id=client_id,
        )
        release.assert_release_ready(
            expected_source_digest=release.expected_projection_source_digest
        )
        verifier_evidence, verified_outcomes = self._record_independent_verifier(
            work,
            current=current,
            checkpoint=checkpoint,
            envelope=envelope,
            completed_results=completed_results,
            release_digest=release.snapshot_digest,
            now=now,
        )
        evidence = EvidenceRef(
            kind="closure-checkpoint",
            reference=f"db:work.checkpoint/{checkpoint_id}",
            digest_value=checkpoint.checkpoint_digest,
        )
        completed = current.with_state(
            WorkState.COMPLETED,
            evidence=(evidence, verifier_evidence),
            now=now,
        )
        database_revision_digest = digest(
            {
                "project_id": str(completed.project_id),
                "work_item_id": str(completed.id),
                "work_revision": completed.revision,
                "work_state": completed.state.value,
                "work_record_digest": completed.record_digest,
            }
        )
        source_digest = canonical_projection_source_digest(
            source_head=release.source_head,
            source_tree_digest=release.source_tree_digest,
            migration_head=release.migration_head,
            database_revision_digest=database_revision_digest,
        )
        hydration = self._hydration_identity(
            job.project_id, job.work_item_id, job.run_id, session_id, client_id
        )
        return SessionCloseReceipt(
            receipt_id=uuid5(
                _CLOSE_NAMESPACE,
                f"{job.id}:{work.attempt_id}:{checkpoint.checkpoint_digest}",
            ),
            realm_id=self.realm_id,
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            run_id=job.run_id,
            session_id=session_id,
            client_id=client_id,
            job_id=job.id,
            attempt_id=work.attempt_id,
            envelope_digest=envelope.envelope_digest,
            fencing_token=work.lease.fencing_token,
            completed_steps=tuple(
                DigestReference(f"work-plan-step:{step}", result, TruthClass.REPO_FACT)
                for step, result in completed_results
            ),
            changed_artifacts=(),
            verified_outcomes=verified_outcomes,
            pending_steps=(),
            next_safe_action=None,
            human_decisions=(),
            discovered_constraints=(),
            failure_recovery_refs=(),
            candidate_lessons=(),
            candidate_skills=(),
            checkpoint_ref=DigestReference(
                f"db:work.checkpoint/{checkpoint_id}",
                checkpoint.checkpoint_digest,
                TruthClass.REPO_FACT,
            ),
            journal_head=DigestReference(
                "db:work.work_journal_entry/head",
                checkpoint.journal_head_digest,
                TruthClass.REPO_FACT,
            ),
            source_digest=source_digest,
            policy_digest=envelope.policy_digest,
            migration_digest=hydration[0],
            context_digest=hydration[1],
            status=CloseStatus.CLOSED,
            closed_at=now,
        )

    def _record_independent_verifier(
        self,
        work: Any,
        *,
        current: Any,
        checkpoint: Checkpoint,
        envelope: ExecutionEnvelope,
        completed_results: tuple[tuple[str, str], ...],
        release_digest: str,
        now: dt.datetime,
    ) -> tuple[EvidenceRef, tuple[DigestReference, ...]]:
        """Persist a provider-free sibling-verifier verdict before close effect."""

        job = work.job
        assert job.assignment_id is not None
        assert job.work_item_id is not None
        assert job.plan_id is not None
        assert job.run_id is not None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select verifier.id,builder.agent_ref,verifier.agent_ref,"
                " builder.parent_assignment_id,builder.context_manifest_digest"
                " from agents.assignment builder join agents.assignment verifier"
                " on verifier.realm_id=builder.realm_id"
                " and verifier.project_id=builder.project_id"
                " and verifier.work_item_id=builder.work_item_id"
                " and verifier.plan_id=builder.plan_id and verifier.step_id=builder.step_id"
                " and verifier.parent_assignment_id is not distinct from"
                " builder.parent_assignment_id"
                " where builder.realm_id=%s and builder.id=%s"
                " and builder.role='builder' and builder.status='active'"
                " and verifier.role='verifier' and verifier.status='active'"
                " and verifier.risk in ('low','medium')"
                " and verifier.agent_ref<>builder.agent_ref"
                " and verifier.context_manifest_digest=builder.context_manifest_digest"
                " order by verifier.id",
                (self.realm_id, job.assignment_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Projection close bir exact independent verifier ister")
        verifier_id = UUID(str(rows[0][0]))
        verifier_identity = f"projection-close-db-verifier:{verifier_id}:{job.id}"
        builder_identity = f"{work.lease.worker_label}:{work.lease.fencing_token}"
        if verifier_identity == builder_identity:
            raise PolicyViolation("Projection close verifier execution identity bagimsiz degil")
        invocation_id = uuid5(
            _CLOSE_VERIFIER_NAMESPACE,
            f"{self.realm_id}:{job.id}:{work.attempt_id}:{current.record_digest}:"
            f"{checkpoint.checkpoint_digest}:{release_digest}",
        )
        verdict_body = {
            "schema": "zekam-projection-close-db-verifier/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(job.project_id),
            "work_item_id": str(job.work_item_id),
            "plan_id": str(job.plan_id),
            "run_id": str(job.run_id),
            "job_id": str(job.id),
            "attempt_id": str(work.attempt_id),
            "builder_assignment_id": str(job.assignment_id),
            "builder_execution_identity": builder_identity,
            "verifier_assignment_id": str(verifier_id),
            "verifier_execution_identity": verifier_identity,
            "work_record_digest": current.record_digest,
            "acceptance_criteria": [item.as_dict() for item in current.acceptance_criteria],
            "checkpoint_digest": checkpoint.checkpoint_digest,
            "execution_envelope_digest": envelope.envelope_digest,
            "completed_results": [list(item) for item in completed_results],
            "projection_release_snapshot_digest": release_digest,
            "verdict": "passed",
            "verification_mode": "canonical-db-re-read",
            "provider_called": False,
            "grants_authority": False,
        }
        verdict_digest = digest(verdict_body)
        invocation_body = {
            "id": str(invocation_id),
            "realm_id": str(self.realm_id),
            "assignment_id": str(verifier_id),
            "client_id": "zekam-projection-close-db-verifier",
            "execution_identity": verifier_identity,
        }
        invocation = AgentInvocation(
            id=invocation_id,
            realm_id=self.realm_id,
            assignment_id=verifier_id,
            client_id=str(invocation_body["client_id"]),
            execution_identity=verifier_identity,
            invocation_digest=digest(invocation_body),
            created_at=now,
        )
        assignments = AgentAssignmentRepository(self.connection, self.realm_id)
        stored_id, _ = assignments.record_invocation(invocation)
        if stored_id != invocation_id:
            raise ConcurrencyConflict("Projection close verifier invocation replay drift")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select assignment_id,envelope_digest from agents.result_receipt"
                " where realm_id=%s and invocation_id=%s",
                (self.realm_id, invocation_id),
            )
            receipt_rows = cursor.fetchall()
        if not receipt_rows:
            assignments.store_result(
                assignment_id=verifier_id,
                invocation_id=invocation_id,
                envelope_digest=verdict_digest,
            )
        elif len(receipt_rows) != 1 or (
            UUID(str(receipt_rows[0][0])) != verifier_id
            or str(receipt_rows[0][1]) != verdict_digest
        ):
            raise ConcurrencyConflict("Projection close verifier result replay drift")
        evidence = EvidenceRef(
            kind="independent-verifier",
            reference=f"db:agents.result_receipt/{invocation_id}",
            digest_value=verdict_digest,
        )
        # One verifier receipt covers the complete acceptance-criteria vector in
        # ``verdict_body``.  Repeating the same reference once per criterion
        # violates SessionCloseReceipt's immutable uniqueness contract when a
        # Work has multiple criteria.
        outcomes = (
            DigestReference(
                f"db:agents.result_receipt/{invocation_id}",
                verdict_digest,
                TruthClass.REPO_FACT,
            ),
        )
        return evidence, outcomes

    def _run_identity(self, run_id: UUID) -> tuple[str, str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select session_id,client_id from runtime.execution_run"
                " where realm_id=%s and id=%s",
                (self.realm_id, run_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Projection close run identity bulunamadi")
        return str(row[0]), str(row[1])

    def _hydration_identity(
        self,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> tuple[str, str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select receipt_body->>'migration_digest',receipt_body->>'context_digest'"
                " from continuity.session_hydration_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s and fresh and complete"
                " order by created_at desc,id desc limit 1",
                (
                    self.realm_id,
                    project_id,
                    work_item_id,
                    run_id,
                    session_id,
                    client_id,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Projection close fresh hydration receipt ister")
        return str(row[0]), str(row[1])
