from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.measured_loop_runtime import PostgresMeasuredLoopContractLoader
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, compile_context
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
)
from zekam.domain.optimization import (
    MetricDirection,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.measured_loop_repository import (
    MeasuredLoopContractTuning,
    PostgresMeasuredLoopRepository,
)

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]


def _assignment(
    realm: Any,
    project: Any,
    work: Any,
    plan: Any,
    context_digest: str,
    role: AssignmentRole,
    parent_id: Any,
    now: dt.datetime,
) -> AgentAssignment:
    candidate = AgentAssignment(
        new_uuid7(now=now),
        realm.id,
        project.id,
        work.id,
        role,
        f"local-{role}",
        digest(("instruction", str(role))),
        context_digest,
        digest("placeholder"),
        parent_assignment_id=parent_id,
        plan_id=None if role is AssignmentRole.COORDINATOR else plan.id,
        step_id=None if role is AssignmentRole.COORDINATOR else "build",
        read_resources=("logical:test-suite",) if role is AssignmentRole.VERIFIER else (),
        write_resources=("logical:artifact",) if role is AssignmentRole.BUILDER else (),
        created_at=now,
    )
    return replace(candidate, assignment_digest=digest(candidate.identity_body()))


def test_production_contract_loader_reads_exact_postgres_authority(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "runtime-composition-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Runtime loop")
    plan = graph.create_plan(
        work.id,
        source_revision="git:runtime-composition",
        policy_digest=digest("runtime-work-policy"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    now = PostgresLoopPolicyRepository(connection, realm.id).current_database_time()
    context = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    context_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(context)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm, project, work, plan, context.manifest_digest, AssignmentRole.COORDINATOR, None, now
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm,
        project,
        work,
        plan,
        context.manifest_digest,
        AssignmentRole.BUILDER,
        coordinator.id,
        now,
    )
    verifier = _assignment(
        realm,
        project,
        work,
        plan,
        context.manifest_digest,
        AssignmentRole.VERIFIER,
        coordinator.id,
        now,
    )
    assignments.create(builder)
    assignments.create(verifier)
    metric = MetricSpec(
        "quality",
        "Quality",
        "points",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "external-validator",
        target_value=10.0,
        minimum_meaningful_delta=0.5,
    )
    objective_id = new_uuid7(now=now)
    manifest = ValidatorAssetManifest(
        new_uuid7(now=now),
        objective_id,
        verifier.instruction_digest,
        "git:runtime-composition",
        builder.id,
        verifier.id,
        (
            ValidatorAsset(
                "test-suite", "logical:test-suite", digest("tests"), ValidatorAssetRole.TEST
            ),
        ),
        now,
    )
    objective = OptimizationObjective(
        objective_id,
        realm.id,
        project.id,
        work.id,
        plan.id,
        "build",
        "logical:artifact",
        digest("baseline"),
        digest("measurement-plan"),
        manifest.manifest_digest,
        (metric,),
        2,
        10_000,
        10_000,
        now + dt.timedelta(hours=1),
        "inverse-patch",
        now,
    )
    tuning = MeasuredLoopContractTuning(2, 1, 2_048, 0.0)
    policy = LoopPolicy(
        new_uuid7(now=now),
        realm.id,
        project.id,
        work.id,
        plan.id,
        "build",
        builder.id,
        context_id,
        verifier.id,
        2,
        10_000,
        10_000,
        objective.deadline,
        verifier.instruction_digest,
        (LoopDeltaKind.NEW_EVIDENCE,),
        (LoopEffectClass.DEPLOY,),
        tuple(sorted(LoopTerminalState, key=str)),
        "git:runtime-composition",
        context.manifest_digest,
        plan.plan_digest,
        digest("runtime-loop-policy"),
        "none",
        now,
        objective_id=objective.objective_id,
        stable_objective_digest=objective.objective_digest,
        measurement_plan_digest=objective.measurement_plan_digest,
        validator_manifest_id=manifest.manifest_id,
        validator_asset_manifest_digest=manifest.manifest_digest,
        metric_specs_digest=digest([metric.as_dict()]),
        stall_limit=tuning.stall_limit,
        diagnostic_patience=tuning.diagnostic_patience,
        progress_token_budget=tuning.progress_token_budget,
        minimum_value_per_cost=tuning.minimum_value_per_cost,
    )
    PostgresLoopPolicyRepository(connection, realm.id).store_policy(policy)
    assert PostgresMeasuredLoopRepository(connection, realm.id).store_measured_loop_contract(
        objective=objective,
        policy=policy,
        validator_manifest=manifest,
        tuning=tuning,
    )

    loaded_objective, loaded_policy = PostgresMeasuredLoopContractLoader(connection, realm.id).load(
        policy.id
    )
    assert loaded_objective.objective_digest == objective.objective_digest
    assert loaded_policy.id == policy.id
    assert loaded_policy.created_at >= policy.created_at
    assert loaded_policy.assignment_id == builder.id
    assert loaded_policy.validator_assignment_id == verifier.id
