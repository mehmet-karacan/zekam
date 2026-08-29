from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

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
from zekam.domain.loop_progress import LoopProgressPacket
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
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.markdown_projection_repository import (
    PostgresMarkdownProjectionRepository,
)
from zekam.infrastructure.postgres.measured_loop_repository import (
    MeasuredLoopContractTuning,
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
TERMINALS = tuple(sorted(LoopTerminalState, key=str))


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
                (canonical_json({"note": "password='supersecretvalue'"}),),
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
        novelty_digest=digest("first-novelty"),
    )
    first_admission = policies.admit(request, attempt_id=first_plan.attempt_id)
    assert first_admission.attempt_id is not None

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
        novelty_digest=digest("second-novelty"),
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
