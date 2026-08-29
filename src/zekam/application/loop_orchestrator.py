"""One-job-per-attempt durable measured-loop orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid5

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import LoopAttemptRequest, LoopPolicy
from zekam.domain.loop_progress import LoopProgressPacket, require_progress_packet
from zekam.domain.optimization import OptimizationObjective
from zekam.domain.resources import ResourceRequest
from zekam.domain.runtime import Job, JobKind
from zekam.infrastructure.postgres.measured_loop_repository import (
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import JobRepository

_ATTEMPT_NAMESPACE = UUID("a8f7ebc3-c8bf-53f0-b167-21ce714a4cc5")
_JOB_NAMESPACE = UUID("51ea6e1d-6339-5fd2-a45f-0d927c82bef2")


def measured_loop_effect_scope_digest(
    *,
    job_id: UUID,
    loop_id: UUID,
    attempt_id: UUID,
    driver_digest: str,
    source_revision: str,
    topology_decision_id: UUID,
    topology_decision_digest: str,
    resources: tuple[str, ...],
) -> str:
    """Digest known after authority-free planning and before authorization issuance."""

    return digest(
        {
            "schema": "zekam-measured-loop-local-effect/v2",
            "job_id": str(job_id),
            "loop_id": str(loop_id),
            "attempt_id": str(attempt_id),
            "driver_digest": driver_digest,
            "source_revision": source_revision,
            "topology_decision_id": str(topology_decision_id),
            "topology_decision_digest": topology_decision_digest,
            "resources": sorted(resources),
        }
    )


@dataclass(frozen=True, slots=True)
class LoopAttemptJobPlan:
    loop_id: UUID
    attempt_id: UUID
    ordinal: int
    predecessor_attempt_id: UUID | None
    progress_packet: LoopProgressPacket | None
    idempotency_digest: str
    job: Job
    effect_scope_digest: str | None = None


@dataclass(frozen=True, slots=True)
class LoopAttemptJobResult:
    job: Job
    job_created: bool
    binding_created: bool
    idempotency_digest: str


@dataclass(frozen=True, slots=True)
class LoopAttemptAdmissionControl:
    paused: bool = False
    draining: bool = False
    cancelled: bool = False

    def assert_open(self) -> None:
        if self.cancelled:
            raise PolicyViolation("Cancelled loop yeni attempt acamaz")
        if self.draining:
            raise PolicyViolation("Draining loop yeni attempt acamaz")
        if self.paused:
            raise PolicyViolation("Paused loop yeni attempt acamaz")


@dataclass(frozen=True, slots=True)
class DurableLoopOrchestrator:
    measured_repository: PostgresMeasuredLoopRepository
    job_repository: JobRepository

    def plan_attempt(
        self,
        *,
        objective: OptimizationObjective,
        policy: LoopPolicy,
        attempt_ordinal: int,
        predecessor_attempt_id: UUID | None,
        progress_packet: LoopProgressPacket | None,
        kind: JobKind = JobKind.MUTATION,
        resources: tuple[ResourceRequest, ...] = (),
        required_capabilities: tuple[str, ...] = ("loop.measured-attempt",),
        priority: int = 100,
        run_id: UUID | None = None,
        now: dt.datetime | None = None,
        control: LoopAttemptAdmissionControl | None = None,
        admission_request: LoopAttemptRequest | None = None,
        effect_authorization_id: UUID | None = None,
        effect_authorization_digest: str | None = None,
        topology_decision_id: UUID | None = None,
        topology_decision_digest: str | None = None,
        topology_pattern: str | None = None,
        production_driver_digest: str | None = None,
    ) -> LoopAttemptJobPlan:
        """Build a provider-free durable job plan after exact packet freshness checks."""

        (control or LoopAttemptAdmissionControl()).assert_open()
        if (effect_authorization_id is None) != (effect_authorization_digest is None):
            raise ValidationFailed("Loop attempt effect authorization exact cift ister")
        if effect_authorization_digest is not None:
            parse_digest(effect_authorization_digest)
        topology_values = (
            topology_decision_id,
            topology_decision_digest,
            topology_pattern,
        )
        if any(item is not None for item in topology_values) and any(
            item is None for item in topology_values
        ):
            raise ValidationFailed("Loop attempt topology exact uclu binding ister")
        if topology_decision_digest is not None:
            parse_digest(topology_decision_digest)
        if topology_pattern is not None and topology_pattern != "bounded-loop":
            raise PolicyViolation("Measured loop job bounded-loop topology ister")
        if production_driver_digest is not None:
            parse_digest(production_driver_digest)
            if topology_decision_id is None:
                raise PolicyViolation("Production measured loop topology binding ister")
            if effect_authorization_id is not None:
                raise PolicyViolation("Production loop authorization plan sonrasinda attach edilir")
        self._assert_scope(objective, policy)
        require_progress_packet(
            attempt_ordinal=attempt_ordinal,
            packet=progress_packet,
            objective_digest=objective.objective_digest,
            source_revision=policy.source_revision,
            plan_digest=policy.plan_digest,
            policy_revision_digest=policy.policy_revision_digest,
            validator_asset_manifest_digest=objective.validator_asset_manifest_digest,
        )
        if attempt_ordinal == 1 and predecessor_attempt_id is not None:
            raise ValidationFailed("Ilk loop attempt predecessor tasiyamaz")
        if attempt_ordinal > 1:
            if predecessor_attempt_id is None or progress_packet is None:
                raise PolicyViolation("Loop attempt 2+ predecessor ve packet ister")
            if progress_packet.predecessor_attempt_id != predecessor_attempt_id:
                raise PolicyViolation("Loop attempt predecessor/progress packet drift")

        packet_digest = None if progress_packet is None else progress_packet.packet_digest
        identity_body = {
            "schema": "zekam-loop-attempt-job-identity/v1",
            "loop_id": str(policy.id),
            "ordinal": attempt_ordinal,
            "predecessor_attempt_id": (
                None if predecessor_attempt_id is None else str(predecessor_attempt_id)
            ),
            "progress_packet_digest": packet_digest,
        }
        idempotency_digest = digest(identity_body)
        attempt_id = uuid5(_ATTEMPT_NAMESPACE, idempotency_digest)
        job_id = uuid5(_JOB_NAMESPACE, idempotency_digest)
        if admission_request is not None:
            expected_request = (
                policy.id,
                attempt_ordinal,
                predecessor_attempt_id,
                objective.objective_digest,
                objective.validator_asset_manifest_digest,
                packet_digest,
            )
            observed_request = (
                admission_request.loop_id,
                admission_request.attempt_ordinal,
                admission_request.predecessor_attempt_id,
                admission_request.objective_digest,
                admission_request.validator_asset_manifest_digest,
                admission_request.progress_packet_digest,
            )
            if observed_request != expected_request:
                raise ValidationFailed("Loop attempt admission request/job plan binding drift")
        payload: dict[str, Any] = {
            "schema": "zekam-loop-attempt-job/v1",
            **identity_body,
            "attempt_id": str(attempt_id),
            "objective_digest": objective.objective_digest,
            "source_revision": policy.source_revision,
            "plan_digest": policy.plan_digest,
            "policy_revision_digest": policy.policy_revision_digest,
            "validator_asset_manifest_digest": objective.validator_asset_manifest_digest,
            "grants_authority": False,
        }
        if admission_request is not None:
            payload["admission"] = {
                "prompt_digest": admission_request.prompt_digest,
                "context_digest": admission_request.context_digest,
                "action_digest": admission_request.action_digest,
                "source_revision": admission_request.source_revision,
                "plan_digest": admission_request.plan_digest,
                "policy_revision_digest": admission_request.policy_revision_digest,
                "validator_spec_digest": admission_request.validator_spec_digest,
                "reserved_input_tokens": admission_request.reserved_input_tokens,
                "reserved_output_tokens": admission_request.reserved_output_tokens,
                "reserved_cost_micros": admission_request.reserved_cost_micros,
                "delta_evidence_ids": [str(item) for item in admission_request.delta_evidence_ids],
                "objective_digest": admission_request.objective_digest,
                "validator_asset_manifest_digest": (
                    admission_request.validator_asset_manifest_digest
                ),
                "progress_packet_digest": admission_request.progress_packet_digest,
                "metric_vector_digest": admission_request.metric_vector_digest,
                "novelty_digest": admission_request.novelty_digest,
                "novelty_body": (
                    None
                    if admission_request.novelty is None
                    else admission_request.novelty.semantic_body()
                ),
            }
        if effect_authorization_id is not None:
            payload["effect_authorization"] = {
                "authorization_id": str(effect_authorization_id),
                "authorization_digest": effect_authorization_digest,
            }
        if topology_decision_id is not None:
            payload["topology"] = {
                "decision_id": str(topology_decision_id),
                "decision_digest": topology_decision_digest,
                "pattern": topology_pattern,
            }
        effect_scope_digest = None
        if production_driver_digest is not None:
            assert topology_decision_id is not None
            assert topology_decision_digest is not None
            effect_scope_digest = measured_loop_effect_scope_digest(
                job_id=job_id,
                loop_id=policy.id,
                attempt_id=attempt_id,
                driver_digest=production_driver_digest,
                source_revision=policy.source_revision,
                topology_decision_id=topology_decision_id,
                topology_decision_digest=topology_decision_digest,
                resources=tuple(item.resource.text for item in resources),
            )
            payload["effect_scope_digest"] = effect_scope_digest
        job = replace(
            Job.create(
                realm_id=policy.realm_id,
                project_id=policy.project_id,
                kind=kind,
                idempotency_key=f"loop-attempt:{policy.id}:{attempt_ordinal}:{idempotency_digest}",
                resources=resources,
                required_capabilities=required_capabilities,
                priority=priority,
                max_attempts=1,
                work_item_id=policy.work_item_id,
                plan_id=policy.plan_id,
                step_id=policy.step_id,
                assignment_id=policy.assignment_id,
                run_id=run_id,
                payload=payload,
                now=now,
            ),
            id=job_id,
        )
        return LoopAttemptJobPlan(
            policy.id,
            attempt_id,
            attempt_ordinal,
            predecessor_attempt_id,
            progress_packet,
            idempotency_digest,
            job,
            effect_scope_digest,
        )

    @staticmethod
    def attach_effect_authorization(
        plan: LoopAttemptJobPlan,
        authorization_id: UUID,
        effect_scope_digest: str,
    ) -> LoopAttemptJobPlan:
        """Attach one exact authorization after the deterministic job plan exists."""

        parse_digest(effect_scope_digest)
        if plan.effect_scope_digest is None or effect_scope_digest != plan.effect_scope_digest:
            raise PolicyViolation("Loop effect authorization scope digest drift")
        if "effect_authorization" in plan.job.payload:
            raise PolicyViolation("Loop effect authorization zaten bagli")
        payload = dict(plan.job.payload)
        payload["effect_authorization"] = {
            "authorization_id": str(authorization_id),
            "effect_digest": effect_scope_digest,
        }
        return replace(plan, job=replace(plan.job, payload=payload))

    def enqueue_attempt(self, plan: LoopAttemptJobPlan) -> LoopAttemptJobResult:
        """Enqueue and bind within the caller-visible PostgreSQL transaction boundary."""

        if plan.loop_id != UUID(str(plan.job.payload.get("loop_id"))):
            raise ValidationFailed("Loop attempt plan/job binding drift")
        if plan.effect_scope_digest is not None:
            authorization = plan.job.payload.get("effect_authorization")
            if (
                not isinstance(authorization, dict)
                or authorization.get("effect_digest") != plan.effect_scope_digest
            ):
                raise PolicyViolation("Production loop enqueue attached exact authorization ister")
        with self.measured_repository.connection.transaction():
            self.measured_repository.assert_loop_open(plan.loop_id)
            job, job_created = self.job_repository.enqueue(plan.job)
            binding_created = self.measured_repository.bind_loop_attempt_job(
                loop_id=plan.loop_id,
                ordinal=plan.ordinal,
                predecessor_attempt_id=plan.predecessor_attempt_id,
                progress_packet=plan.progress_packet,
                job=job,
                idempotency_digest=plan.idempotency_digest,
            )
        return LoopAttemptJobResult(
            job,
            job_created,
            binding_created,
            plan.idempotency_digest,
        )

    def _assert_scope(self, objective: OptimizationObjective, policy: LoopPolicy) -> None:
        if self.measured_repository.realm_id != self.job_repository.realm_id:
            raise PolicyViolation("Measured loop repository realm binding drift")
        if objective.realm_id != self.measured_repository.realm_id:
            raise PolicyViolation("Measured loop objective realm binding drift")
        if (
            objective.project_id,
            objective.work_item_id,
            objective.plan_id,
            objective.step_id,
        ) != (policy.project_id, policy.work_item_id, policy.plan_id, policy.step_id):
            raise ValidationFailed("Measured loop objective/policy scope drift")
