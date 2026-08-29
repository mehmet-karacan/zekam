from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from zekam.application.loop_control import LoopControlService, LoopControlState
from zekam.application.loop_observatory import LoopObservatory
from zekam.application.loop_orchestrator import DurableLoopOrchestrator
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import AuthorityLevel, compile_context
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
    LoopValidation,
)
from zekam.domain.loop_progress import AttemptNoveltyFingerprint, LoopProgressPacket
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
    evaluate_progress,
)
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.markdown_projection_repository import (
    PostgresMarkdownProjectionRepository,
)
from zekam.infrastructure.postgres.measured_loop_repository import (
    MeasuredLoopContractTuning,
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import JobRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
TERMINALS = tuple(sorted(LoopTerminalState, key=str))


def _novelty(objective_digest: str, label: str) -> AttemptNoveltyFingerprint:
    return AttemptNoveltyFingerprint.build(
        objective_digest=objective_digest,
        artifact_digest=digest(f"artifact:{label}"),
        hypothesis_digest=digest(f"hypothesis:{label}"),
        patch_digest=digest(f"patch:{label}"),
        failure_signature=digest(f"failure:{label}"),
        action_semantics_digest=digest(f"action:{label}"),
    )


def _assignment(
    *,
    realm: Any,
    project: Any,
    work: Any,
    plan: Any,
    manifest_digest: str,
    role: AssignmentRole,
    parent_id: Any,
    now: dt.datetime,
    read_resources: tuple[str, ...] = (),
    write_resources: tuple[str, ...] = (),
) -> AgentAssignment:
    candidate = AgentAssignment(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        role=role,
        agent_ref=str(role),
        instruction_digest=digest(("instruction", str(role))),
        context_manifest_digest=manifest_digest,
        assignment_digest=digest("placeholder"),
        parent_assignment_id=parent_id,
        plan_id=None if role is AssignmentRole.COORDINATOR else plan.id,
        step_id=None if role is AssignmentRole.COORDINATOR else "build",
        read_resources=read_resources,
        write_resources=write_resources,
        created_at=now,
    )
    return replace(candidate, assignment_digest=digest(candidate.identity_body()))


def _measurement(value: float, revision: str, now: dt.datetime) -> MeasurementEvidence:
    return MeasurementEvidence(
        "quality",
        value,
        f"evidence:{revision}",
        digest((revision, value)),
        revision,
        now,
        "builder-measurement-worker",
        "independent-verifier-worker",
    )


@pytest.mark.security
def test_measured_contract_progress_and_one_job_per_attempt_are_durable(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "measured-loop-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Measured loop")
    plan = graph.create_plan(
        work.id,
        source_revision="git:measured-loop",
        policy_digest=digest("work-policy"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    now = PostgresLoopPolicyRepository(connection, realm.id).current_database_time()
    context_manifest = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    context_manifest_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(context_manifest)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=context_manifest.manifest_digest,
        role=AssignmentRole.COORDINATOR,
        parent_id=None,
        now=now,
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=context_manifest.manifest_digest,
        role=AssignmentRole.BUILDER,
        parent_id=coordinator.id,
        now=now,
        write_resources=("logical:artifact",),
    )
    verifier = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=context_manifest.manifest_digest,
        role=AssignmentRole.VERIFIER,
        parent_id=coordinator.id,
        now=now,
        read_resources=("logical:quality", "logical:test-suite"),
    )
    assignments.create(builder)
    assignments.create(verifier)

    specs = (
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
    )
    objective_id = new_uuid7(now=now)
    validator_manifest = ValidatorAssetManifest(
        manifest_id=new_uuid7(now=now),
        objective_id=objective_id,
        validator_spec_digest=verifier.instruction_digest,
        source_revision="git:measured-loop",
        builder_assignment_id=builder.id,
        verifier_assignment_id=verifier.id,
        assets=(
            ValidatorAsset(
                "test-suite",
                "logical:test-suite",
                digest("tests"),
                ValidatorAssetRole.TEST,
            ),
            ValidatorAsset(
                "threshold",
                "logical:quality",
                digest("threshold"),
                ValidatorAssetRole.THRESHOLD,
            ),
        ),
        created_at=now,
    )
    manifest_digest = validator_manifest.manifest_digest
    objective = OptimizationObjective(
        objective_id,
        realm.id,
        project.id,
        work.id,
        plan.id,
        "build",
        "logical:artifact",
        digest("artifact-baseline"),
        digest("measurement-plan"),
        manifest_digest,
        specs,
        3,
        10_000,
        10_000,
        now + dt.timedelta(hours=1),
        "inverse-patch",
        now,
    )
    tuning = MeasuredLoopContractTuning(2, 1, 8_192, 0.001)
    policy = LoopPolicy(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=builder.id,
        context_manifest_id=context_manifest_id,
        validator_assignment_id=verifier.id,
        max_attempts=3,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=objective.deadline,
        validator_spec_digest=verifier.instruction_digest,
        required_delta=(LoopDeltaKind.NEW_EVIDENCE,),
        forbidden_effects=(LoopEffectClass.DEPLOY,),
        terminal_states=TERMINALS,
        source_revision="git:measured-loop",
        context_manifest_digest=context_manifest.manifest_digest,
        plan_digest=plan.plan_digest,
        policy_revision_digest=digest("loop-policy-revision"),
        canonical_effect_kind="none",
        created_at=now,
        objective_id=objective.objective_id,
        stable_objective_digest=objective.objective_digest,
        measurement_plan_digest=objective.measurement_plan_digest,
        validator_manifest_id=validator_manifest.manifest_id,
        validator_asset_manifest_digest=manifest_digest,
        metric_specs_digest=digest([item.as_dict() for item in specs]),
        stall_limit=tuning.stall_limit,
        diagnostic_patience=tuning.diagnostic_patience,
        progress_token_budget=tuning.progress_token_budget,
        minimum_value_per_cost=tuning.minimum_value_per_cost,
    )
    policies = PostgresLoopPolicyRepository(connection, realm.id)
    policies.store_policy(policy)
    measured = PostgresMeasuredLoopRepository(connection, realm.id)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("savepoint measured_payload_security")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(
                "select runtime.assert_measured_payload_safe(%s::jsonb)",
                (canonical_json({"note": "person@example.invalid"}),),
            )
        cursor.execute("rollback to savepoint measured_payload_security")

        cursor.execute("savepoint measured_secret_value_security")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(
                "select runtime.assert_measured_payload_safe(%s::jsonb)",
                (canonical_json({"note": "pass" + "word='synthetic-sensitive-value'"}),),
            )
        cursor.execute("rollback to savepoint measured_secret_value_security")

        cursor.execute("savepoint validator_write_scope")
        cursor.execute(
            "insert into agents.assignment_resource"
            " (realm_id,assignment_id,resource,mode) values (%s,%s,%s,'write')",
            (realm.id, builder.id, "logical:test-suite"),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(objective.as_dict()),
                    objective.objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(policy.body()),
                    policy.policy_digest,
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint validator_write_scope")

        cursor.execute("savepoint forged_validator_manifest")
        forged_manifest_body = validator_manifest.as_dict()
        forged_manifest_body["assets"] = forged_manifest_body["assets"][:-1]
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="canonical body ile uyusmuyor",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(objective.as_dict()),
                    objective.objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(forged_manifest_body),
                    validator_manifest.manifest_digest,
                    canonical_json(policy.body()),
                    policy.policy_digest,
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint forged_validator_manifest")

        cursor.execute("savepoint null_validator_asset")
        null_manifest_body = validator_manifest.as_dict()
        null_manifest_body["assets"][0]["content_digest"] = None
        null_manifest_digest = digest(null_manifest_body)
        null_objective_body = objective.as_dict()
        null_objective_body["validator_asset_manifest_digest"] = null_manifest_digest
        null_objective_digest = digest(null_objective_body)
        null_policy_body = policy.body()
        null_policy_body["measured_v2"]["stable_objective_digest"] = null_objective_digest
        null_policy_body["measured_v2"]["validator_asset_manifest_digest"] = null_manifest_digest
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="canonical asset ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(null_objective_body),
                    null_objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(null_manifest_body),
                    null_manifest_digest,
                    canonical_json(null_policy_body),
                    digest(null_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint null_validator_asset")

        cursor.execute("savepoint authority_grant_objective")
        authority_objective_body = objective.as_dict()
        authority_objective_body["grants_authority"] = True
        authority_objective_digest = digest(authority_objective_body)
        authority_policy_body = policy.body()
        authority_policy_body["measured_v2"]["stable_objective_digest"] = authority_objective_digest
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="canonical exact body ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(authority_objective_body),
                    authority_objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(authority_policy_body),
                    digest(authority_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint authority_grant_objective")

        cursor.execute("savepoint authority_grant_policy")
        authority_policy_body = policy.body()
        authority_policy_body["grants_authority"] = True
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="canonical exact body ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(objective.as_dict()),
                    objective.objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(authority_policy_body),
                    digest(authority_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint authority_grant_policy")

        cursor.execute(
            "select canonical_body from runtime.loop_policy where realm_id=%s and id=%s",
            (realm.id, policy.id),
        )
        canonical_policy_body = dict(cursor.fetchone()[0])
        canonical_policy_body.pop("schema", None)
        canonical_policy_body["measured_v2"] = policy.body()["measured_v2"]

        cursor.execute("savepoint objective_scope_drift")
        drift_objective_body = objective.as_dict()
        drift_objective_body["step_id"] = "unreviewed-step"
        drift_objective_digest = digest(drift_objective_body)
        drift_policy_body = dict(canonical_policy_body)
        drift_policy_body["measured_v2"] = dict(canonical_policy_body["measured_v2"])
        drift_policy_body["measured_v2"]["stable_objective_digest"] = drift_objective_digest
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="policy canonical exact body ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(drift_objective_body),
                    drift_objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(drift_policy_body),
                    digest(drift_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint objective_scope_drift")

        cursor.execute("savepoint infinite_objective_deadline")
        infinite_objective_body = objective.as_dict()
        infinite_objective_body["deadline"] = "infinity"
        infinite_objective_digest = digest(infinite_objective_body)
        infinite_policy_body = dict(canonical_policy_body)
        infinite_policy_body["measured_v2"] = dict(canonical_policy_body["measured_v2"])
        infinite_policy_body["measured_v2"]["stable_objective_digest"] = infinite_objective_digest
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="canonical exact body ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(infinite_objective_body),
                    infinite_objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(infinite_policy_body),
                    digest(infinite_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint infinite_objective_deadline")

        for savepoint, field, value, message in (
            ("fractional_objective_budget", "max_attempts", 3.4, "budget/time/metric"),
            ("decimal_objective_budget", "max_attempts", 3.0, "budget/time/metric"),
            ("string_false_authority", "grants_authority", "false", "canonical exact body"),
            (
                "offset_objective_deadline",
                "deadline",
                objective.deadline.isoformat(),
                "canonical exact body",
            ),
        ):
            cursor.execute(f"savepoint {savepoint}")
            malformed_objective_body = objective.as_dict()
            malformed_objective_body[field] = value
            malformed_objective_digest = digest(malformed_objective_body)
            malformed_policy_body = dict(canonical_policy_body)
            malformed_policy_body["measured_v2"] = dict(canonical_policy_body["measured_v2"])
            malformed_policy_body["measured_v2"]["stable_objective_digest"] = (
                malformed_objective_digest
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match=message):
                cursor.execute(
                    "select runtime.store_measured_loop_contract("
                    " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                    " %s,%s,%s,%s)",
                    (
                        objective.objective_id,
                        policy.id,
                        validator_manifest.manifest_id,
                        canonical_json(malformed_objective_body),
                        malformed_objective_digest,
                        policy.source_revision,
                        policy.assignment_id,
                        policy.validator_assignment_id,
                        canonical_json(validator_manifest.as_dict()),
                        validator_manifest.manifest_digest,
                        canonical_json(malformed_policy_body),
                        digest(malformed_policy_body),
                        tuning.stall_limit,
                        tuning.diagnostic_patience,
                        tuning.progress_token_budget,
                        tuning.minimum_value_per_cost,
                    ),
                )
            cursor.execute(f"rollback to savepoint {savepoint}")

        cursor.execute("savepoint string_minimum_value_per_cost")
        string_minimum_policy_body = dict(canonical_policy_body)
        string_minimum_policy_body["measured_v2"] = dict(canonical_policy_body["measured_v2"])
        string_minimum_policy_body["measured_v2"]["minimum_value_per_cost"] = "0.001"
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="policy canonical exact body ister",
        ):
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(objective.as_dict()),
                    objective.objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(validator_manifest.as_dict()),
                    validator_manifest.manifest_digest,
                    canonical_json(string_minimum_policy_body),
                    digest(string_minimum_policy_body),
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
        cursor.execute("rollback to savepoint string_minimum_value_per_cost")

    assert measured.store_measured_loop_contract(
        objective=objective,
        policy=policy,
        validator_manifest=validator_manifest,
        tuning=tuning,
    )
    assert not measured.store_measured_loop_contract(
        objective=objective,
        policy=policy,
        validator_manifest=validator_manifest,
        tuning=tuning,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="loop-observatory-control", now=now)
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    control_service = LoopControlService(measured, authorizations)
    pause = control_service.prepare(
        policy.id,
        target_state=LoopControlState.PAUSED,
        reason_digest=digest("observatory-reviewed-pause"),
    )
    pause_authorization = authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=plan.id,
            plan_digest=plan.plan_digest,
            effect_digest=pause.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(pause.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=now,
        )
    )
    pause_receipt = control_service.apply(
        pause,
        authorization_id=pause_authorization.id,
        now=now,
    )
    status = LoopObservatory(connection, realm.id).status(policy.id)
    assert status["loop_control"] == {
        "state": "paused",
        "event_id": str(pause_receipt.event_id),
        "reason_digest": pause.reason_digest,
        "created_at": pause_receipt.created_at,
    }
    resume = control_service.prepare(
        policy.id,
        target_state=LoopControlState.ACTIVE,
        reason_digest=digest("observatory-reviewed-resume"),
    )
    resume_authorization = authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=plan.id,
            plan_digest=plan.plan_digest,
            effect_digest=resume.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(resume.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=now,
        )
    )
    control_service.apply(resume, authorization_id=resume_authorization.id, now=now)
    with (
        pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="builder validator asset write scope disinda olmali",
        ),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into agents.assignment_resource"
            " (realm_id,assignment_id,resource,mode) values (%s,%s,%s,'write')",
            (realm.id, builder.id, "logical:test-suite"),
        )
    orchestrator = DurableLoopOrchestrator(measured, JobRepository(connection, realm.id))
    first_plan = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        now=now,
    )
    first_attempt_job = orchestrator.enqueue_attempt(first_plan)
    assert first_attempt_job.job_created is True

    first_novelty = _novelty(objective.objective_digest, "first")
    request = LoopAttemptRequest(
        policy.id,
        builder.instruction_digest,
        policy.context_manifest_digest,
        digest("action"),
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        policy.validator_spec_digest,
        10,
        10,
        10,
        attempt_ordinal=1,
        objective_digest=objective.objective_digest,
        validator_asset_manifest_digest=manifest_digest,
        novelty_digest=first_novelty.novelty_digest,
        novelty=first_novelty,
    )
    forged_body = {**first_novelty.semantic_body(), "patch_digest": digest("forged-patch")}
    legacy_semantic = digest(
        {
            "prompt_digest": request.prompt_digest,
            "context_digest": request.context_digest,
            "action_digest": request.action_digest,
        }
    )
    legacy_binding = digest(
        {
            "source_revision": request.source_revision,
            "plan_digest": request.plan_digest,
            "policy_revision_digest": request.policy_revision_digest,
            "validator_spec_digest": request.validator_spec_digest,
            "predecessor_attempt_id": None,
        }
    )
    with (
        pytest.raises(psycopg.Error, match="supplied digest canonical body ile uyusmuyor"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "select * from runtime.admit_loop_attempt_current_v3("
            " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            " %s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                first_plan.attempt_id,
                policy.id,
                None,
                legacy_semantic,
                request.prompt_digest,
                request.context_digest,
                request.action_digest,
                legacy_binding,
                request.source_revision,
                request.plan_digest,
                request.policy_revision_digest,
                request.validator_spec_digest,
                request.reserved_input_tokens,
                request.reserved_output_tokens,
                request.reserved_cost_micros,
                [],
                request.delta_digest,
                request.attempt_ordinal,
                request.objective_digest,
                request.validator_asset_manifest_digest,
                request.progress_packet_digest,
                request.metric_vector_digest,
                request.novelty_digest,
                canonical_json(forged_body),
            ),
        )
    first_admission = policies.admit(request, attempt_id=first_plan.attempt_id)
    assert first_admission.attempt_id is not None
    repeated_hypothesis = AttemptNoveltyFingerprint.build(
        objective_digest=objective.objective_digest,
        artifact_digest=digest("artifact:repeated-hypothesis"),
        hypothesis_digest=first_novelty.hypothesis_digest,
        patch_digest=digest("patch:repeated-hypothesis"),
        failure_signature=digest("failure:repeated-hypothesis"),
        action_semantics_digest=digest("action:repeated-hypothesis"),
    )
    duplicate_request = replace(
        request,
        predecessor_attempt_id=first_admission.attempt_id,
        attempt_ordinal=2,
        progress_packet_digest=digest("not-yet-created-packet"),
        metric_vector_digest=digest("not-yet-created-vector"),
        novelty_digest=repeated_hypothesis.novelty_digest,
        novelty=repeated_hypothesis,
    )
    with (
        pytest.raises(psycopg.Error, match="novelty component duplicate: hypothesis"),
        connection.transaction(),
    ):
        policies.admit(duplicate_request)

    baseline = (_measurement(1.0, "git:base", now),)
    previous = (_measurement(2.0, "git:before", now),)
    current = (_measurement(4.0, "git:after", now),)
    previous_vector = evaluate_progress(specs, baseline, baseline, previous)
    current_vector = evaluate_progress(specs, baseline, previous, current)
    packet = LoopProgressPacket(
        objective.objective_digest,
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        manifest_digest,
        digest("artifact-before"),
        digest("artifact-after"),
        first_admission.attempt_id,
        2,
        previous_vector,
        current_vector,
        current_vector.deltas,
        digest("accepted-hypothesis"),
        (),
        digest("patch"),
        digest("failure-signature"),
        "evidence:diagnosis",
        digest("diagnosis"),
        (("evidence:quality", digest("quality-evidence")),),
        1,
        5_000,
        5_000,
        600,
        "Improve quality",
        (),
        8_192,
    )
    stored = measured.store_loop_progress(
        loop_id=policy.id,
        packet=packet,
        evidence=current,
        producer_assignment_id=builder.id,
        verifier_assignment_id=verifier.id,
        stop_reason=None,
    )
    replay = measured.store_loop_progress(
        loop_id=policy.id,
        packet=packet,
        evidence=current,
        producer_assignment_id=builder.id,
        verifier_assignment_id=verifier.id,
        stop_reason=None,
    )
    assert stored.created is True
    assert replay.created is False
    assert stored.evidence_id == replay.evidence_id
    assert stored.packet_id == replay.packet_id

    def invocation_receipt(
        assignment: AgentAssignment, execution_identity: str, envelope: str
    ) -> AgentInvocation:
        invocation_id = new_uuid7(now=now)
        body = {
            "id": str(invocation_id),
            "realm_id": str(realm.id),
            "assignment_id": str(assignment.id),
            "client_id": "local-harness",
            "execution_identity": execution_identity,
        }
        invocation = AgentInvocation(
            invocation_id,
            realm.id,
            assignment.id,
            "local-harness",
            execution_identity,
            digest(body),
            now,
        )
        policies.bind_dispatch(first_admission.attempt_id, "agent", invocation.id)
        assignments.record_invocation(invocation)
        assignments.store_result(
            assignment_id=assignment.id,
            invocation_id=invocation.id,
            envelope_digest=digest(envelope),
        )
        return invocation

    result_invocation = invocation_receipt(builder, "builder-local", "result")
    verifier_invocation = invocation_receipt(verifier, "verifier-local", "verified")
    validation = LoopValidation(
        outcome=LoopAttemptOutcome.RETRYABLE_FAILURE,
        validator_spec_digest=policy.validator_spec_digest,
        actual_input_tokens=10,
        actual_output_tokens=10,
        actual_cost_micros=10,
        result_invocation_id=result_invocation.id,
        verifier_invocation_id=verifier_invocation.id,
        metric_evidence_refs=(current[0].evidence_ref,),
        metric_vector_digest=current_vector.progress_digest,
        progress_state=current_vector.progress_state,
        progress_decision_digest=stored.progress_decision_digest,
        progress_packet_digest=stored.packet_digest,
    )
    assert policies.complete(first_admission.attempt_id, validation) == "active"

    next_plan = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=2,
        predecessor_attempt_id=first_admission.attempt_id,
        progress_packet=packet,
        now=now,
    )
    first_job = orchestrator.enqueue_attempt(next_plan)
    replay_job = orchestrator.enqueue_attempt(next_plan)
    assert first_job.job_created is True and first_job.binding_created is True
    assert replay_job.job_created is False and replay_job.binding_created is False
    assert first_job.job.id == replay_job.job.id

    second_novelty = _novelty(objective.objective_digest, "second")
    second_request = LoopAttemptRequest(
        policy.id,
        digest("second prompt"),
        policy.context_manifest_digest,
        digest("second action"),
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        policy.validator_spec_digest,
        10,
        10,
        10,
        predecessor_attempt_id=first_admission.attempt_id,
        attempt_ordinal=2,
        objective_digest=objective.objective_digest,
        validator_asset_manifest_digest=manifest_digest,
        progress_packet_digest=packet.packet_digest,
        metric_vector_digest=current_vector.progress_digest,
        novelty_digest=second_novelty.novelty_digest,
        novelty=second_novelty,
    )
    second_admission = policies.admit(second_request, attempt_id=next_plan.attempt_id)
    assert second_admission.admitted is True
    assert second_admission.ordinal == 2
    assert second_admission.attempt_id is not None
    assert policies.interrupt(second_admission.attempt_id, digest("stop-after-proof")) is (
        LoopTerminalState.MANUAL_REVIEW
    )
    with pytest.raises(PolicyViolation, match="Terminal"):
        orchestrator.enqueue_attempt(next_plan)

    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from runtime.optimization_objective where id=%s),"
            " (select count(*) from runtime.loop_progress_packet where loop_id=%s),"
            " (select count(*) from runtime.loop_attempt_job where loop_id=%s)",
            (objective.objective_id, policy.id, policy.id),
        )
        assert cursor.fetchone() == (1, 1, 2)

    projection = PostgresMarkdownProjectionRepository(connection, realm.id).load_obsidian_records(
        project.id, realm_slug=realm.slug
    )
    measured_notes = [item for item in projection if item.memory_class == "measured-loop"]
    assert len(measured_notes) == 1
    assert measured_notes[0].record.entity_id == str(policy.id)
    assert stored.packet_digest in measured_notes[0].record.summary
    assert "Improve quality" not in measured_notes[0].record.summary
