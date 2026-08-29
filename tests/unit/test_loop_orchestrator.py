from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application.loop_orchestrator import (
    DurableLoopOrchestrator,
    LoopAttemptAdmissionControl,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.loop_policy import (
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
)
from zekam.domain.loop_progress import LoopProgressPacket
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    evaluate_progress,
)
from zekam.domain.runtime import Job
from zekam.infrastructure.postgres.measured_loop_repository import (
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import JobRepository

NOW = dt.datetime(2026, 8, 29, 8, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 20))
TERMINALS = tuple(sorted(LoopTerminalState, key=str))


@dataclass(slots=True)
class _Connection:
    transactions: int = 0

    def transaction(self) -> AbstractContextManager[None]:
        self.transactions += 1
        return nullcontext()


@dataclass(slots=True)
class _MeasuredRepository:
    connection: _Connection
    realm_id: UUID
    bindings: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = False

    def assert_loop_open(self, _loop_id: UUID) -> None:
        if self.terminal:
            raise PolicyViolation("Terminal loop yeni attempt job uretemez")

    def bind_loop_attempt_job(self, **values: Any) -> bool:
        self.bindings.append(values)
        return len(self.bindings) == 1


@dataclass(slots=True)
class _JobRepository:
    realm_id: UUID
    jobs: dict[str, Job] = field(default_factory=dict)

    def enqueue(self, job: Job) -> tuple[Job, bool]:
        existing = self.jobs.get(job.idempotency_key)
        if existing is not None:
            return existing, False
        self.jobs[job.idempotency_key] = job
        return job, True


def _policy() -> LoopPolicy:
    return LoopPolicy(
        id=IDS[5],
        realm_id=IDS[0],
        project_id=IDS[1],
        work_item_id=IDS[2],
        plan_id=IDS[3],
        step_id="build",
        assignment_id=IDS[6],
        context_manifest_id=IDS[7],
        validator_assignment_id=IDS[8],
        max_attempts=3,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=NOW + dt.timedelta(hours=1),
        validator_spec_digest=digest("validator"),
        required_delta=(LoopDeltaKind.NEW_EVIDENCE,),
        forbidden_effects=(LoopEffectClass.DEPLOY,),
        terminal_states=TERMINALS,
        source_revision="git:abc",
        context_manifest_digest=digest("context"),
        plan_digest=digest("plan"),
        policy_revision_digest=digest("policy-revision"),
        canonical_effect_kind="file-write",
        created_at=NOW,
    )


def _objective(policy: LoopPolicy) -> OptimizationObjective:
    return OptimizationObjective(
        objective_id=IDS[4],
        realm_id=policy.realm_id,
        project_id=policy.project_id,
        work_item_id=policy.work_item_id,
        plan_id=policy.plan_id,
        step_id=policy.step_id,
        artifact_ref="logical:artifact",
        artifact_baseline_digest=digest("artifact-baseline"),
        measurement_plan_digest=digest("measurement-plan"),
        validator_asset_manifest_digest=digest("validator-assets"),
        metric_specs=(
            MetricSpec(
                "quality",
                "Quality",
                "points",
                MetricDirection.MAXIMIZE,
                MetricRole.PRIMARY,
                "external-validator",
                target_value=10.0,
                minimum_meaningful_delta=0.5,
            ),
        ),
        max_attempts=policy.max_attempts,
        max_tokens=policy.max_tokens,
        max_cost_micros=policy.max_cost_micros,
        deadline=policy.deadline,
        reversibility_class="inverse-patch",
        created_at=policy.created_at,
    )


def _measurement(value: float, revision: str) -> MeasurementEvidence:
    return MeasurementEvidence(
        "quality",
        value,
        f"evidence:{revision}",
        digest((revision, value)),
        revision,
        NOW,
        "measurement-worker",
        "independent-verifier",
    )


def _packet(policy: LoopPolicy, objective: OptimizationObjective) -> LoopProgressPacket:
    baseline = (_measurement(1.0, "git:base"),)
    previous = (_measurement(2.0, "git:before"),)
    current = (_measurement(4.0, "git:after"),)
    previous_vector = evaluate_progress(objective.metric_specs, baseline, baseline, previous)
    current_vector = evaluate_progress(objective.metric_specs, baseline, previous, current)
    return LoopProgressPacket(
        objective.objective_digest,
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        objective.validator_asset_manifest_digest,
        digest("artifact-before"),
        digest("artifact-after"),
        IDS[9],
        2,
        previous_vector,
        current_vector,
        current_vector.deltas,
        digest("accepted-hypothesis"),
        (digest("rejected-hypothesis"),),
        digest("patch"),
        digest("failure-signature"),
        "evidence:diagnosis",
        digest("diagnosis"),
        (("evidence:new", digest("new-evidence")),),
        1,
        5_000,
        5_000,
        600,
        "Improve quality metric",
        (digest("forbidden-retry"),),
        8_192,
    )


def _orchestrator() -> tuple[DurableLoopOrchestrator, _MeasuredRepository, _JobRepository]:
    measured = _MeasuredRepository(_Connection(), IDS[0])
    jobs = _JobRepository(IDS[0])
    return (
        DurableLoopOrchestrator(
            cast(PostgresMeasuredLoopRepository, measured), cast(JobRepository, jobs)
        ),
        measured,
        jobs,
    )


def test_first_attempt_is_one_job_and_enqueue_binding_is_atomic_and_idempotent() -> None:
    orchestrator, measured, _jobs = _orchestrator()
    policy = _policy()
    objective = _objective(policy)

    plan = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        now=NOW,
    )
    assert plan.job.max_attempts == 1
    assert plan.job.payload["ordinal"] == 1
    assert plan.job.payload["progress_packet_digest"] is None

    first = orchestrator.enqueue_attempt(plan)
    second = orchestrator.enqueue_attempt(plan)

    assert first.job_created is True
    assert second.job_created is False
    assert first.job.id == second.job.id
    assert measured.connection.transactions == 2
    assert len(measured.bindings) == 2
    assert measured.bindings[0]["job"].id == first.job.id


def test_attempt_two_requires_exact_fresh_progress_packet() -> None:
    orchestrator, _measured, _jobs = _orchestrator()
    policy = _policy()
    objective = _objective(policy)

    with pytest.raises(PolicyViolation, match="packet"):
        orchestrator.plan_attempt(
            objective=objective,
            policy=policy,
            attempt_ordinal=2,
            predecessor_attempt_id=IDS[9],
            progress_packet=None,
        )

    packet = _packet(policy, objective)
    with pytest.raises(PolicyViolation, match="predecessor"):
        orchestrator.plan_attempt(
            objective=objective,
            policy=policy,
            attempt_ordinal=2,
            predecessor_attempt_id=IDS[10],
            progress_packet=packet,
        )

    plan = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=2,
        predecessor_attempt_id=IDS[9],
        progress_packet=packet,
        now=NOW,
    )
    assert plan.job.max_attempts == 1
    assert plan.job.payload["progress_packet_digest"] == packet.packet_digest
    assert plan.idempotency_digest in plan.job.idempotency_key


def test_attempt_two_idempotency_changes_only_with_bound_packet() -> None:
    orchestrator, _measured, _jobs = _orchestrator()
    policy = _policy()
    objective = _objective(policy)
    packet = _packet(policy, objective)

    first = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=2,
        predecessor_attempt_id=packet.predecessor_attempt_id,
        progress_packet=packet,
        now=NOW,
    )
    replay = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=2,
        predecessor_attempt_id=packet.predecessor_attempt_id,
        progress_packet=packet,
        now=NOW,
    )
    assert first.idempotency_digest == replay.idempotency_digest
    assert first.job.idempotency_key == replay.job.idempotency_key


@pytest.mark.parametrize("state", ("paused", "draining", "cancelled"))
def test_paused_draining_or_cancelled_loop_cannot_plan_next_attempt(state: str) -> None:
    orchestrator, _measured, _jobs = _orchestrator()
    control = LoopAttemptAdmissionControl(**{state: True})
    with pytest.raises(PolicyViolation, match=state.capitalize()):
        orchestrator.plan_attempt(
            objective=_objective(_policy()),
            policy=_policy(),
            attempt_ordinal=1,
            predecessor_attempt_id=None,
            progress_packet=None,
            control=control,
        )


def test_terminal_loop_cannot_enqueue_new_attempt() -> None:
    orchestrator, measured, _jobs = _orchestrator()
    policy = _policy()
    plan = orchestrator.plan_attempt(
        objective=_objective(policy),
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
    )
    measured.terminal = True
    with pytest.raises(PolicyViolation, match="Terminal"):
        orchestrator.enqueue_attempt(plan)


def test_production_plan_is_deterministic_and_requires_post_plan_exact_auth_attach() -> None:
    orchestrator, _measured, _jobs = _orchestrator()
    policy = _policy()
    arguments = {
        "objective": _objective(policy),
        "policy": policy,
        "attempt_ordinal": 1,
        "predecessor_attempt_id": None,
        "progress_packet": None,
        "resources": (),
        "topology_decision_id": IDS[11],
        "topology_decision_digest": digest("topology"),
        "topology_pattern": "bounded-loop",
        "production_driver_digest": digest("drivers"),
        "now": NOW,
    }
    first = orchestrator.plan_attempt(**arguments)
    replay = orchestrator.plan_attempt(**arguments)
    assert first.job.id == replay.job.id
    assert first.effect_scope_digest == replay.effect_scope_digest
    assert "effect_authorization" not in first.job.payload

    with pytest.raises(PolicyViolation, match="attached exact authorization"):
        orchestrator.enqueue_attempt(first)
    with pytest.raises(PolicyViolation, match="scope digest drift"):
        orchestrator.attach_effect_authorization(first, IDS[12], digest("wrong"))

    attached = orchestrator.attach_effect_authorization(
        first, IDS[12], first.effect_scope_digest or ""
    )
    assert attached.job.payload["effect_authorization"] == {
        "authorization_id": str(IDS[12]),
        "effect_digest": first.effect_scope_digest,
    }
    assert orchestrator.enqueue_attempt(attached).job_created is True
