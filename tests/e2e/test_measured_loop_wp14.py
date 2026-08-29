"""WP14 measured-loop acceptance journeys over the real domain/application plane."""

from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.graph_execution import GraphExecutionRecorder, GraphNodeObservation
from zekam.application.loop_orchestrator import DurableLoopOrchestrator
from zekam.application.loop_progress_compiler import LoopProgressCompiler
from zekam.application.loop_rollback import LoopRollbackService
from zekam.application.measured_loop_worker import (
    MeasuredLoopAttemptExecution,
    build_measured_loop_worker,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.scaffolding_ablation import ScaffoldingAblationService
from zekam.application.topology_planner import TopologyPlanner, TopologySuitabilityRequest
from zekam.application.tournament import (
    CandidateSubmission,
    IndependentTournamentSelector,
    SelectorScore,
    TournamentPlanner,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, Checkpoint, compile_context
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_topology import (
    ExecutionTopologyPattern,
    GraphNodeMode,
    GraphNodeTerminalState,
    MeasurementSourceTier,
    TournamentBudget,
    TournamentCandidateAssignment,
)
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
from zekam.domain.loop_progress import (
    AttemptNoveltyFingerprint,
    LoopAttemptProgress,
    LoopProgressCheckpoint,
    LoopStopReason,
    evaluate_attempt_gates,
    require_progress_packet,
)
from zekam.domain.model_context_experiment import ContextAblationProfile
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    ProgressState,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
    evaluate_progress,
)
from zekam.domain.runtime import AttemptOutcome, EffectClaim, EffectReceipt, JobKind
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingArmEvidence,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingDisposition,
    ScaffoldingMetrics,
)
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType
from zekam.infrastructure.git.loop_patch import GitLoopPatchAdapter
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.measured_loop_repository import (
    MeasuredLoopContractTuning,
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import EffectLedger, JobRepository

pytestmark = pytest.mark.e2e
NOW = dt.datetime(2026, 8, 29, 13, 0, tzinfo=dt.UTC)
TERMINALS = tuple(sorted(LoopTerminalState, key=str))


def _metric() -> MetricSpec:
    return MetricSpec(
        "quality",
        "Quality",
        "points",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "deterministic-external-validator",
        target_value=10.0,
        minimum_meaningful_delta=0.5,
    )


def _evidence(value: float, label: str) -> MeasurementEvidence:
    return MeasurementEvidence(
        "quality",
        value,
        f"evidence:{label}",
        digest((label, value)),
        "git:wp14",
        NOW,
        "measurement-worker",
        "independent-verifier",
    )


def _novelty(label: str, *, objective_digest: str | None = None) -> AttemptNoveltyFingerprint:
    return AttemptNoveltyFingerprint.build(
        objective_digest=objective_digest or digest("stable-objective"),
        artifact_digest=digest("artifact-baseline"),
        hypothesis_digest=digest(f"hypothesis:{label}"),
        patch_digest=digest(f"patch:{label}"),
        failure_signature=digest(f"failure:{label}"),
        action_semantics_digest=digest(f"action:{label}"),
    )


def _attempt(
    ordinal: int,
    label: str,
    before: str,
    after: str,
    vector: Any,
    *,
    diagnosis: str | None = None,
) -> LoopAttemptProgress:
    return LoopAttemptProgress(
        uuid4(),
        ordinal,
        digest(before),
        digest(after),
        _novelty(label),
        vector.progress_state,
        vector.progress_digest,
        None if diagnosis is None else digest(diagnosis),
    )


def _work_plan(*steps: PlanStep) -> TaskPlan:
    item = WorkItem.create(
        realm_id=uuid4(),
        project_id=uuid4(),
        type=WorkType.TASK,
        title="WP14",
        now=NOW,
    )
    return TaskPlan.create(
        work_item=item,
        revision=1,
        source_revision="git:wp14",
        policy_digest=digest("work-policy"),
        steps=tuple(steps),
        now=NOW,
    )


def test_fail_pass_duplicate_packet_and_restart_idempotency_form_one_bounded_journey() -> None:
    spec = _metric()
    baseline = (_evidence(5.0, "baseline"),)
    plateau_vector = evaluate_progress((spec,), baseline, baseline, (_evidence(5.1, "plateau"),))
    assert plateau_vector.progress_state is ProgressState.PLATEAU
    first = _attempt(1, "first", "a", "b", plateau_vector, diagnosis="new-diagnosis")
    first_gate = evaluate_attempt_gates(first, (), stall_limit=2, diagnostic_patience=1)
    assert first_gate.diagnostic_retry and not first_gate.progress_counted

    target_vector = evaluate_progress(
        (spec,), baseline, (_evidence(5.1, "previous"),), (_evidence(10.0, "pass"),)
    )
    second = _attempt(2, "second", "b", "c", target_vector)
    second_gate = evaluate_attempt_gates(second, (first,), stall_limit=2)
    assert second_gate.stop_reason is LoopStopReason.TARGET_REACHED
    assert second_gate.progress_counted and not second_gate.allow_next_attempt

    rephrased = replace(
        second,
        attempt_id=uuid4(),
        attempt_ordinal=3,
        artifact_before_digest=digest("c"),
        artifact_after_digest=digest("d"),
        novelty=AttemptNoveltyFingerprint.build(**second.novelty.semantic_body()),
    )
    duplicate_gate = evaluate_attempt_gates(rephrased, (first, second), stall_limit=3)
    assert duplicate_gate.stop_reason is LoopStopReason.REPEATED_PATCH
    assert not duplicate_gate.progress_counted

    checkpoint = LoopProgressCheckpoint(
        digest("objective"),
        "git:wp14",
        digest("plan"),
        digest("policy"),
        digest("validator-assets"),
        digest("b"),
        digest("c"),
        second.attempt_id,
        2,
        plateau_vector,
        target_vector,
        digest("accepted-hypothesis"),
        tuple(sorted(digest(f"rejected:{index}") for index in range(12))),
        digest("patch"),
        digest("failure"),
        "evidence:validator-diagnosis",
        digest("validator-diagnosis"),
        tuple(sorted((f"evidence:{index}", digest(index)) for index in range(12))),
        1,
        1_000,
        1_000,
        60,
        "Only adjust the measured artifact boundary",
        tuple(sorted(f"retry:{index}" for index in range(12))),
    )
    packet = LoopProgressCompiler().compile(checkpoint)
    assert packet.estimated_tokens <= packet.max_packet_tokens
    assert packet.packet_digest == LoopProgressCompiler().compile(checkpoint).packet_digest
    require_progress_packet(
        attempt_ordinal=2,
        packet=packet,
        objective_digest=checkpoint.objective_digest,
        source_revision=checkpoint.source_revision,
        plan_digest=checkpoint.plan_digest,
        policy_revision_digest=checkpoint.policy_revision_digest,
        validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
    )


def _assignment(
    *,
    realm_id: UUID,
    project_id: UUID,
    work_item_id: UUID,
    plan_id: UUID,
    context_digest: str,
    role: AssignmentRole,
    parent_id: UUID | None,
    now: dt.datetime,
    write_resources: tuple[str, ...] = (),
    read_resources: tuple[str, ...] = (),
) -> AgentAssignment:
    candidate = AgentAssignment(
        id=new_uuid7(now=now),
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_item_id,
        role=role,
        agent_ref=f"wp14-{role}",
        instruction_digest=digest(("instruction", str(role))),
        context_manifest_digest=context_digest,
        assignment_digest=digest("placeholder"),
        parent_assignment_id=parent_id,
        plan_id=None if role is AssignmentRole.COORDINATOR else plan_id,
        step_id=None if role is AssignmentRole.COORDINATOR else "build",
        read_resources=read_resources,
        write_resources=write_resources,
        created_at=now,
    )
    return replace(candidate, assignment_digest=digest(candidate.identity_body()))


def _invocation(
    *, realm_id: UUID, assignment_id: UUID, label: str, now: dt.datetime
) -> AgentInvocation:
    invocation_id = new_uuid7(now=now)
    execution_identity = f"wp14:{label}:{invocation_id}"
    candidate = AgentInvocation(
        id=invocation_id,
        realm_id=realm_id,
        assignment_id=assignment_id,
        client_id="local-wp14-harness",
        execution_identity=execution_identity,
        invocation_digest=digest("placeholder"),
        created_at=now,
    )
    return replace(
        candidate,
        invocation_digest=digest(
            {
                "id": str(candidate.id),
                "realm_id": str(candidate.realm_id),
                "assignment_id": str(candidate.assignment_id),
                "client_id": candidate.client_id,
                "execution_identity": candidate.execution_identity,
            }
        ),
    )


@pytest.mark.postgres
def test_real_postgres_restart_claim_receipt_completion_and_terminal_no_enqueue(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "wp14-real-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="WP14 real loop")
    plan = graph.create_plan(
        work.id,
        source_revision="git:wp14",
        policy_digest=digest("work-policy"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    policies = PostgresLoopPolicyRepository(connection, realm.id)
    now = policies.current_database_time()
    context = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    context_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(context)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.COORDINATOR,
        parent_id=None,
        now=now,
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.BUILDER,
        parent_id=coordinator.id,
        now=now,
        write_resources=("logical:artifact",),
    )
    verifier = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.VERIFIER,
        parent_id=coordinator.id,
        now=now,
        read_resources=("logical:test-suite", "logical:threshold"),
    )
    assignments.create(builder)
    assignments.create(verifier)

    spec = _metric()
    objective_id = new_uuid7(now=now)
    manifest = ValidatorAssetManifest(
        new_uuid7(now=now),
        objective_id,
        verifier.instruction_digest,
        "git:wp14",
        builder.id,
        verifier.id,
        (
            ValidatorAsset(
                "test-suite", "logical:test-suite", digest("tests"), ValidatorAssetRole.TEST
            ),
            ValidatorAsset(
                "threshold",
                "logical:threshold",
                digest("threshold"),
                ValidatorAssetRole.THRESHOLD,
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
        (spec,),
        2,
        10_000,
        10_000,
        now + dt.timedelta(hours=1),
        "inverse-patch",
        now,
    )
    tuning = MeasuredLoopContractTuning(2, 1, 2_048, 0.0)
    policy = LoopPolicy(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=builder.id,
        context_manifest_id=context_id,
        validator_assignment_id=verifier.id,
        max_attempts=2,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=objective.deadline,
        validator_spec_digest=verifier.instruction_digest,
        required_delta=(LoopDeltaKind.NEW_EVIDENCE,),
        forbidden_effects=(LoopEffectClass.DEPLOY,),
        terminal_states=TERMINALS,
        source_revision="git:wp14",
        context_manifest_digest=context.manifest_digest,
        plan_digest=plan.plan_digest,
        policy_revision_digest=digest("loop-policy"),
        canonical_effect_kind="none",
        created_at=now,
        objective_id=objective.objective_id,
        stable_objective_digest=objective.objective_digest,
        measurement_plan_digest=objective.measurement_plan_digest,
        validator_manifest_id=manifest.manifest_id,
        validator_asset_manifest_digest=manifest.manifest_digest,
        metric_specs_digest=digest([spec.as_dict()]),
        stall_limit=tuning.stall_limit,
        diagnostic_patience=tuning.diagnostic_patience,
        progress_token_budget=tuning.progress_token_budget,
        minimum_value_per_cost=tuning.minimum_value_per_cost,
    )
    policies.store_policy(policy)
    measured = PostgresMeasuredLoopRepository(connection, realm.id)
    assert measured.store_measured_loop_contract(
        objective=objective, policy=policy, validator_manifest=manifest, tuning=tuning
    )
    jobs = JobRepository(connection, realm.id)
    first_plan = DurableLoopOrchestrator(measured, jobs).plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        kind=JobKind.READ_ONLY,
        now=now,
    )
    first_enqueue = DurableLoopOrchestrator(measured, jobs).enqueue_attempt(first_plan)
    restarted_enqueue = DurableLoopOrchestrator(measured, jobs).enqueue_attempt(first_plan)
    assert first_enqueue.job_created and first_enqueue.binding_created
    assert not restarted_enqueue.job_created and not restarted_enqueue.binding_created
    assert first_enqueue.job.id == restarted_enqueue.job.id
    assert first_enqueue.job.max_attempts == 1

    novelty = _novelty("real-first", objective_digest=objective.objective_digest)
    admission = policies.admit(
        LoopAttemptRequest(
            policy.id,
            builder.instruction_digest,
            policy.context_manifest_digest,
            digest("action"),
            policy.source_revision,
            policy.plan_digest,
            policy.policy_revision_digest,
            policy.validator_spec_digest,
            100,
            100,
            100,
            attempt_ordinal=1,
            objective_digest=objective.objective_digest,
            validator_asset_manifest_digest=manifest.manifest_digest,
            novelty_digest=novelty.novelty_digest,
            novelty=novelty,
        )
    )
    assert admission.admitted and admission.attempt_id is not None

    with connection.cursor() as cursor:
        cursor.execute(
            "select state,available_at<=%s,required_capabilities,"
            " required_capabilities <@ array['loop.measured-attempt']::text[],"
            " attempt_count,max_attempts"
            " from runtime.job where id=%s",
            (now + dt.timedelta(seconds=1), first_enqueue.job.id),
        )
        claimable = cursor.fetchone()
    assert claimable == ("ready", True, ["loop.measured-attempt"], True, 0, 1)
    claimed = jobs.claim_next(
        worker_label="wp14-worker",
        capabilities=("loop.measured-attempt",),
        now=now + dt.timedelta(seconds=1),
    )
    assert claimed is not None and claimed.job.id == first_enqueue.job.id
    claim = EffectClaim.create(
        realm_id=realm.id,
        job_id=claimed.job.id,
        attempt_id=claimed.attempt_id,
        operation="local.verify",
        effect_digest=digest("local-effect"),
        authorization_digest=digest("authority-free-local-read"),
        idempotency_key=f"wp14:{claimed.job.id}",
        resources=(),
        execution_identity="wp14-local-worker",
        fencing_token=claimed.lease.fencing_token,
        adapter_digest=digest("local-adapter"),
        now=now,
    )
    ledger = EffectLedger(connection, realm.id)
    ledger.claim(claim)
    receipt = EffectReceipt.completed(
        realm_id=realm.id,
        claim=claim,
        result_digest=digest("local-result"),
        adapter_evidence_digest=digest("local-adapter-evidence"),
        now=now,
    )
    ledger.receipt(receipt)
    ContextContinuityRepository(connection, realm.id, project.id, work.id).store_checkpoint(
        Checkpoint(
            checkpoint_id=f"wp14-job-{claimed.job.id}",
            project_id=str(project.id),
            work_item_id=str(work.id),
            plan_revision_id=str(plan.id),
            source_revision=policy.source_revision,
            plan_steps=("build",),
            completed_steps=("build",),
            pending_steps=(),
            step_results=(("build", receipt.result_digest),),
            context_manifest_digest=context.manifest_digest,
            journal_head_digest=digest("wp14-journal"),
            next_safe_action="close-measured-loop",
            created_at=now,
        ),
        task_plan_id=plan.id,
        job_id=claimed.job.id,
    )
    assert jobs.complete(
        claimed.job.id,
        token=claimed.owner_token,
        fencing_token=claimed.lease.fencing_token,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=receipt.result_digest,
        now=now + dt.timedelta(seconds=1),
    )

    baseline = (_evidence(5, "real-baseline"),)
    previous = (_evidence(6, "real-previous"),)
    current = (_evidence(10, "real-target"),)
    previous_vector = evaluate_progress((spec,), baseline, baseline, previous)
    current_vector = evaluate_progress((spec,), baseline, previous, current)
    packet = LoopProgressCompiler().compile(
        LoopProgressCheckpoint(
            objective.objective_digest,
            policy.source_revision,
            policy.plan_digest,
            policy.policy_revision_digest,
            manifest.manifest_digest,
            digest("artifact-before"),
            digest("artifact-after"),
            admission.attempt_id,
            2,
            previous_vector,
            current_vector,
            digest("accepted-hypothesis"),
            (),
            digest("patch"),
            digest("failure"),
            "evidence:diagnosis",
            digest("diagnosis"),
            (),
            1,
            5_000,
            5_000,
            600,
            "Stop because target is reached",
            (),
        )
    )
    stored = measured.store_loop_progress(
        loop_id=policy.id,
        packet=packet,
        evidence=current,
        producer_assignment_id=builder.id,
        verifier_assignment_id=verifier.id,
        stop_reason=LoopStopReason.TARGET_REACHED,
    )
    builder_invocation = _invocation(
        realm_id=realm.id, assignment_id=builder.id, label="builder", now=now
    )
    verifier_invocation = _invocation(
        realm_id=realm.id, assignment_id=verifier.id, label="verifier", now=now
    )
    policies.bind_dispatch(admission.attempt_id, "agent", builder_invocation.id)
    assignments.record_invocation(builder_invocation)
    assignments.record_invocation(verifier_invocation)
    assignments.store_result(
        assignment_id=builder.id,
        invocation_id=builder_invocation.id,
        envelope_digest=digest("builder-result"),
    )
    assignments.store_result(
        assignment_id=verifier.id,
        invocation_id=verifier_invocation.id,
        envelope_digest=digest("verifier-result"),
    )
    terminal = policies.complete(
        admission.attempt_id,
        LoopValidation(
            LoopAttemptOutcome.PASSED,
            policy.validator_spec_digest,
            100,
            50,
            100,
            builder_invocation.id,
            verifier_invocation.id,
            effect_receipt_id=None,
            metric_evidence_refs=(current[0].evidence_ref,),
            metric_vector_digest=current_vector.progress_digest,
            progress_state=ProgressState.TARGET_REACHED,
            progress_decision_digest=stored.progress_decision_digest,
            progress_packet_digest=packet.packet_digest,
        ),
    )
    assert terminal == "passed"
    assert policies.terminal_state(policy.id) is LoopTerminalState.PASSED
    with pytest.raises(PolicyViolation, match="Terminal loop"):
        DurableLoopOrchestrator(measured, jobs).enqueue_attempt(first_plan)


def _topology_request(plan: TaskPlan, **changes: object) -> TopologySuitabilityRequest:
    values: dict[str, object] = {
        "plan": plan,
        "objective_digest": digest("topology-objective"),
        "measurement_available": True,
        "measurement_source_tier": MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
        "measurement_estimated_cost_micros": 10,
        "action_estimated_cost_micros": 100,
        "reversible": True,
        "idempotent_or_receipt_bound": True,
    }
    values.update(changes)
    return TopologySuitabilityRequest(**values)  # type: ignore[arg-type]


def test_creative_irreversible_and_graph_work_choose_and_execute_distinct_topologies() -> None:
    creative = _work_plan(PlanStep("draft", "Draft", EffectKind.FILE_WRITE))
    creative_decision = TopologyPlanner().decide(
        _topology_request(creative, creative_diversity_goal=True, parallelism_ceiling=2)
    )
    assert creative_decision.pattern is ExecutionTopologyPattern.TOURNAMENT

    candidates = (
        TournamentCandidateAssignment(uuid4(), "builder-a", "exec-a", 100, 100),
        TournamentCandidateAssignment(uuid4(), "builder-b", "exec-b", 100, 100),
    )
    tournament = TournamentPlanner().create_plan(
        candidates=candidates,
        shared_objective_digest=creative_decision.objective_digest,
        candidate_context_digest=digest("shared-context"),
        selector_assignment_id=uuid4(),
        selector_model_id="independent-selector",
        selector_execution_identity="selector-exec",
        selector_spec_digest=digest("selector-spec"),
        human_final_gate=True,
        budget=TournamentBudget(2, 200, 200, NOW + dt.timedelta(minutes=5)),
        now=NOW,
    )
    submissions = tuple(
        CandidateSubmission(
            item.assignment_id,
            digest(("result", index)),
            50,
            50,
            NOW + dt.timedelta(minutes=1),
        )
        for index, item in enumerate(candidates)
    )
    result = IndependentTournamentSelector().select(
        plan=tournament,
        submissions=submissions,
        scores=tuple(
            SelectorScore(item.assignment_id, float(index), digest(("score", index)))
            for index, item in enumerate(candidates)
        ),
        selector_assignment_id=tournament.selector_assignment_id,
        selector_model_id=tournament.selector_model_id,
        selector_execution_identity=tournament.selector_execution_identity,
        now=NOW + dt.timedelta(minutes=2),
    )
    assert result.status == "awaiting-human-review"
    assert result.grants_promotion is False

    irreversible = _work_plan(PlanStep("push", "Push", EffectKind.GIT_PUSH, risk="high"))
    human = TopologyPlanner().decide(_topology_request(irreversible, reversible=False))
    assert human.pattern is ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW
    assert human.required_human_gates == ("exact-human-review",)

    graph_plan = _work_plan(
        PlanStep("a", "A", EffectKind.FILE_WRITE, logical_resources=("path:x:a",)),
        PlanStep("b", "B", EffectKind.FILE_WRITE, logical_resources=("path:x:b",)),
        PlanStep("fan-in", "Fan in", EffectKind.NONE, depends_on=("a", "b")),
    )
    graph = TopologyPlanner().decide(
        _topology_request(
            graph_plan,
            distinct_deliverable_count=3,
            parallel_ready_count=2,
            fan_in_required=True,
            parallelism_ceiling=2,
        )
    )
    assert graph.pattern is ExecutionTopologyPattern.GRAPH

    def observation(step: str, start: float, end: float, cost: int = 0) -> GraphNodeObservation:
        return GraphNodeObservation(
            step,
            GraphNodeMode.DIRECT,
            NOW,
            NOW + dt.timedelta(seconds=start),
            NOW + dt.timedelta(seconds=end),
            digest(("result", step)),
            GraphNodeTerminalState.COMPLETED,
            coordination_cost_micros=cost,
        )

    receipt = GraphExecutionRecorder().build_receipt(
        graph_root_id=uuid4(),
        plan=graph_plan,
        observations=(
            observation("a", 0, 2, 4),
            observation("b", 0.5, 1.5, 4),
            observation("fan-in", 2.5, 3, 4),
        ),
        claimed_parallel=True,
        expected_coordination_cost_micros=10,
    )
    assert receipt.max_observed_concurrency == 2
    assert receipt.parallel_overlap_duration_millis == 1000
    assert receipt.critical_path == ("a", "fan-in")
    assert "simpler-topology-recommended" in receipt.topology_feedback


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def test_metric_regression_uses_exact_inverse_patch_and_preserves_user_dirty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "wp14@example.invalid")
    _git(root, "config", "user.name", "WP14")
    (root / "artifact.txt").write_text("baseline\n", encoding="utf-8")
    (root / "user.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "artifact.txt", "user.txt")
    _git(root, "commit", "-m", "baseline")

    (root / "user.txt").write_text("user-dirty\n", encoding="utf-8")
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(), allowed_paths=("artifact.txt",), captured_at=NOW
    )
    (root / "artifact.txt").write_text("regression\n", encoding="utf-8")
    captured = adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))
    service = LoopRollbackService(adapter)
    plan = service.prepare(
        baseline=baseline.baseline,
        change_set=captured.change_set,
        reason_code="metric-regression",
        prepared_at=NOW + dt.timedelta(seconds=2),
    )
    receipt = service.execute(
        baseline=baseline.baseline,
        captured=captured,
        change_set=captured.change_set,
        plan=plan,
        checked_at=NOW + dt.timedelta(seconds=3),
        applied_at=NOW + dt.timedelta(seconds=4),
    )
    assert (root / "artifact.txt").read_text(encoding="utf-8") == "baseline\n"
    assert (root / "user.txt").read_text(encoding="utf-8") == "user-dirty\n"
    assert receipt.changed_resources == ("artifact.txt",)


def _ablation_arm(arm_id: str, *, candidate: bool) -> ScaffoldingArmEvidence:
    return ScaffoldingArmEvidence(
        arm_id,
        (
            ContextAblationProfile(("core",), ("critic",))
            if candidate
            else ContextAblationProfile(("core", "critic"), ())
        ),
        "local-model",
        digest("execution-profile"),
        digest("objective"),
        digest("metrics"),
        digest("validator-assets"),
        digest("fixtures"),
        digest("paired-trials"),
        "git:wp14",
        5,
        ScaffoldingMetrics(0.9, 0.95, 80 if candidate else 100, 800, 800),
        digest(("arm", arm_id)),
    )


def test_scaffolding_ablation_requires_paired_evidence_and_never_auto_deletes() -> None:
    pair = ScaffoldingAblationPair(
        _ablation_arm("baseline", candidate=False),
        _ablation_arm("candidate", candidate=True),
        "critic",
    )
    decision = ScaffoldingAblationService.evaluate(
        pair=pair,
        policy=ScaffoldingAblationPolicy(),
        rollback_plan=ScaffoldingDeprecationRollbackPlan(
            "critic", digest("restore-critic"), "git:wp14", "review:wp14"
        ),
    )
    assert decision.disposition is ScaffoldingDisposition.DEPRECATION_CANDIDATE
    assert decision.review_status == "review-required"
    assert decision.auto_delete is False
    assert decision.grants_authority is False


@dataclass(slots=True)
class _WorkerLoopScope:
    realm: Any
    connection: Any
    project: Any
    work: Any
    plan: Any
    now: dt.datetime
    context: Any
    assignments: AgentAssignmentRepository
    builder: AgentAssignment
    verifier: AgentAssignment
    objective: OptimizationObjective
    policy: LoopPolicy
    manifest: ValidatorAssetManifest
    policies: PostgresLoopPolicyRepository
    measured: PostgresMeasuredLoopRepository
    jobs: JobRepository
    first_plan: Any


def _worker_loop_scope(
    realm_session: tuple[Any, Any], tmp_path: Path, *, label: str
) -> _WorkerLoopScope:
    realm, connection = realm_session
    source = tmp_path / f"worker-{label}"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title=f"Worker {label}")
    plan = graph.create_plan(
        work.id,
        source_revision="git:wp14-worker",
        policy_digest=digest(("worker-policy", label)),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    policies = PostgresLoopPolicyRepository(connection, realm.id)
    now = policies.current_database_time()
    context = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    context_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(context)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.COORDINATOR,
        parent_id=None,
        now=now,
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.BUILDER,
        parent_id=coordinator.id,
        now=now,
        write_resources=("logical:artifact",),
    )
    verifier = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        context_digest=context.manifest_digest,
        role=AssignmentRole.VERIFIER,
        parent_id=coordinator.id,
        now=now,
        read_resources=("logical:test-suite", "logical:threshold"),
    )
    assignments.create(builder)
    assignments.create(verifier)
    spec = _metric()
    objective_id = new_uuid7(now=now)
    manifest = ValidatorAssetManifest(
        new_uuid7(now=now),
        objective_id,
        verifier.instruction_digest,
        "git:wp14-worker",
        builder.id,
        verifier.id,
        (
            ValidatorAsset(
                "test-suite", "logical:test-suite", digest("tests"), ValidatorAssetRole.TEST
            ),
            ValidatorAsset(
                "threshold",
                "logical:threshold",
                digest("threshold"),
                ValidatorAssetRole.THRESHOLD,
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
        (spec,),
        2,
        10_000,
        10_000,
        now + dt.timedelta(hours=1),
        "inverse-patch",
        now,
    )
    tuning = MeasuredLoopContractTuning(2, 1, 2_048, 0.0)
    policy = LoopPolicy(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=builder.id,
        context_manifest_id=context_id,
        validator_assignment_id=verifier.id,
        max_attempts=2,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=objective.deadline,
        validator_spec_digest=verifier.instruction_digest,
        required_delta=(LoopDeltaKind.NEW_EVIDENCE,),
        forbidden_effects=(LoopEffectClass.DEPLOY,),
        terminal_states=TERMINALS,
        source_revision="git:wp14-worker",
        context_manifest_digest=context.manifest_digest,
        plan_digest=plan.plan_digest,
        policy_revision_digest=digest(("loop-policy", label)),
        canonical_effect_kind="none",
        created_at=now,
        objective_id=objective.objective_id,
        stable_objective_digest=objective.objective_digest,
        measurement_plan_digest=objective.measurement_plan_digest,
        validator_manifest_id=manifest.manifest_id,
        validator_asset_manifest_digest=manifest.manifest_digest,
        metric_specs_digest=digest([spec.as_dict()]),
        stall_limit=tuning.stall_limit,
        diagnostic_patience=tuning.diagnostic_patience,
        progress_token_budget=tuning.progress_token_budget,
        minimum_value_per_cost=tuning.minimum_value_per_cost,
    )
    policies.store_policy(policy)
    measured = PostgresMeasuredLoopRepository(connection, realm.id)
    measured.store_measured_loop_contract(
        objective=objective, policy=policy, validator_manifest=manifest, tuning=tuning
    )
    jobs = JobRepository(connection, realm.id)
    worker_novelty = _novelty(f"worker-{label}-1", objective_digest=objective.objective_digest)
    request = LoopAttemptRequest(
        policy.id,
        builder.instruction_digest,
        policy.context_manifest_digest,
        digest(("action", label, 1)),
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        policy.validator_spec_digest,
        100,
        100,
        100,
        attempt_ordinal=1,
        objective_digest=objective.objective_digest,
        validator_asset_manifest_digest=manifest.manifest_digest,
        novelty_digest=worker_novelty.novelty_digest,
        novelty=worker_novelty,
    )
    orchestrator = DurableLoopOrchestrator(measured, jobs)
    first_plan = orchestrator.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        admission_request=request,
        now=now,
    )
    orchestrator.enqueue_attempt(first_plan)
    return _WorkerLoopScope(
        realm,
        connection,
        project,
        work,
        plan,
        now,
        context,
        assignments,
        builder,
        verifier,
        objective,
        policy,
        manifest,
        policies,
        measured,
        jobs,
        first_plan,
    )


@dataclass(slots=True)
class _ContractLoader:
    scope: _WorkerLoopScope

    def load(self, loop_id: UUID) -> tuple[OptimizationObjective, LoopPolicy]:
        assert loop_id == self.scope.policy.id
        return self.scope.objective, self.scope.policy


@dataclass(slots=True)
class _TwoAttemptRunner:
    scope: _WorkerLoopScope
    calls: int = 0
    stage: str = "idle"

    def run(self, work: Any, admission: Any) -> MeasuredLoopAttemptExecution:
        self.calls += 1
        self.stage = "entered"
        assert admission.ordinal == self.calls
        moment = work.lease.heartbeat_at

        def measured(value: float, label: str) -> MeasurementEvidence:
            return MeasurementEvidence(
                "quality",
                value,
                f"evidence:{label}",
                digest((label, value)),
                self.scope.policy.source_revision,
                moment,
                "measurement-worker",
                "independent-verifier",
            )

        baseline = (measured(5, f"worker-{self.calls}-baseline"),)
        if self.calls == 1:
            previous_evidence = baseline
            current = (measured(6, "worker-first-improved"),)
            stop_reason = None
            outcome = LoopAttemptOutcome.RETRYABLE_FAILURE
        else:
            previous_evidence = (measured(6, "worker-second-previous"),)
            current = (measured(10, "worker-second-target"),)
            stop_reason = LoopStopReason.TARGET_REACHED
            outcome = LoopAttemptOutcome.PASSED
        previous_vector = evaluate_progress(
            self.scope.objective.metric_specs,
            baseline,
            baseline,
            previous_evidence,
        )
        current_vector = evaluate_progress(
            self.scope.objective.metric_specs,
            baseline,
            previous_evidence,
            current,
        )
        packet = LoopProgressCompiler().compile(
            LoopProgressCheckpoint(
                self.scope.objective.objective_digest,
                self.scope.policy.source_revision,
                self.scope.policy.plan_digest,
                self.scope.policy.policy_revision_digest,
                self.scope.manifest.manifest_digest,
                digest(("before", self.calls)),
                digest(("after", self.calls)),
                admission.attempt_id,
                self.calls + 1,
                previous_vector,
                current_vector,
                digest(("accepted", self.calls)),
                (),
                digest(("patch", self.calls)),
                digest(("failure", self.calls)),
                f"evidence:diagnosis:{self.calls}",
                digest(("diagnosis", self.calls)),
                (),
                2 - self.calls,
                5_000,
                5_000,
                600,
                "Continue only from measured evidence",
                (),
            )
        )
        self.stage = "packet"
        builder_invocation = _invocation(
            realm_id=self.scope.realm.id,
            assignment_id=self.scope.builder.id,
            label=f"worker-builder-{self.calls}",
            now=moment,
        )
        verifier_invocation = _invocation(
            realm_id=self.scope.realm.id,
            assignment_id=self.scope.verifier.id,
            label=f"worker-verifier-{self.calls}",
            now=moment,
        )
        self.scope.policies.bind_dispatch(admission.attempt_id, "agent", builder_invocation.id)
        self.scope.policies.bind_dispatch(admission.attempt_id, "agent", verifier_invocation.id)
        self.scope.assignments.record_invocation(builder_invocation)
        self.scope.assignments.record_invocation(verifier_invocation)
        self.scope.assignments.store_result(
            assignment_id=self.scope.builder.id,
            invocation_id=builder_invocation.id,
            envelope_digest=digest(("builder-result", self.calls)),
        )
        self.scope.assignments.store_result(
            assignment_id=self.scope.verifier.id,
            invocation_id=verifier_invocation.id,
            envelope_digest=digest(("verifier-result", self.calls)),
        )
        self.stage = "invocations"
        claim = EffectClaim.create(
            realm_id=self.scope.realm.id,
            job_id=work.job.id,
            attempt_id=work.attempt_id,
            operation="local.measured-loop",
            effect_digest=digest(("local-effect", self.calls)),
            authorization_digest=digest("authority-free-local"),
            idempotency_key=f"worker-effect:{work.job.id}",
            resources=(),
            execution_identity=f"worker-runner:{self.calls}",
            fencing_token=work.lease.fencing_token,
            adapter_digest=digest("local-worker-adapter"),
            now=moment,
        )
        ledger = EffectLedger(self.scope.connection, self.scope.realm.id)
        ledger.claim(claim)
        receipt = EffectReceipt.completed(
            realm_id=self.scope.realm.id,
            claim=claim,
            result_digest=digest(("runner-result", self.calls)),
            adapter_evidence_digest=digest(("adapter", self.calls)),
            now=moment,
        )
        ledger.receipt(receipt)
        self.stage = "receipt"
        ContextContinuityRepository(
            self.scope.connection,
            self.scope.realm.id,
            self.scope.project.id,
            self.scope.work.id,
        ).store_checkpoint(
            Checkpoint(
                checkpoint_id=f"worker-job-{work.job.id}",
                project_id=str(self.scope.project.id),
                work_item_id=str(self.scope.work.id),
                plan_revision_id=str(self.scope.plan.id),
                source_revision=self.scope.policy.source_revision,
                plan_steps=("build",),
                completed_steps=("build",),
                pending_steps=(),
                step_results=(("build", receipt.result_digest),),
                context_manifest_digest=self.scope.context.manifest_digest,
                journal_head_digest=digest(("worker-journal", self.calls)),
                next_safe_action="continue-measured-loop",
                created_at=moment,
            ),
            task_plan_id=self.scope.plan.id,
            job_id=work.job.id,
        )
        self.stage = "checkpoint"
        next_request = None
        if self.calls == 1:
            self.stage = "register-delta"
            delta_id = self.scope.policies.register_delta_evidence(
                self.scope.policy.id,
                str(LoopDeltaKind.NEW_EVIDENCE),
                verifier_invocation.id,
            )
            self.stage = "next-request"
            next_novelty = _novelty(
                "worker-next-2",
                objective_digest=self.scope.objective.objective_digest,
            )
            next_request = LoopAttemptRequest(
                self.scope.policy.id,
                self.scope.builder.instruction_digest,
                self.scope.policy.context_manifest_digest,
                digest(("action", 2)),
                self.scope.policy.source_revision,
                self.scope.policy.plan_digest,
                self.scope.policy.policy_revision_digest,
                self.scope.policy.validator_spec_digest,
                100,
                100,
                100,
                predecessor_attempt_id=admission.attempt_id,
                delta_evidence_ids=(delta_id,),
                attempt_ordinal=2,
                objective_digest=self.scope.objective.objective_digest,
                validator_asset_manifest_digest=self.scope.manifest.manifest_digest,
                progress_packet_digest=packet.packet_digest,
                metric_vector_digest=packet.current_metric_vector.progress_digest,
                novelty_digest=next_novelty.novelty_digest,
                novelty=next_novelty,
            )
        self.stage = "returned"
        return MeasuredLoopAttemptExecution(
            packet,
            current,
            outcome,
            builder_invocation.id,
            verifier_invocation.id,
            100,
            50,
            100,
            stop_reason=stop_reason,
            next_request=next_request,
        )


@pytest.mark.postgres
def test_production_measured_worker_atomically_enqueues_next_and_stops_terminal(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    scope = _worker_loop_scope(realm_session, tmp_path, label="success")
    runner = _TwoAttemptRunner(scope)
    worker = build_measured_loop_worker(
        scope.connection,
        scope.realm.id,
        contract_loader=_ContractLoader(scope),
        runner=runner,
    )
    first = worker.tick(now=scope.now + dt.timedelta(seconds=1))
    assert first.accepted_work and first.outcome is AttemptOutcome.SUCCEEDED, (
        runner.calls,
        runner.stage,
    )
    with scope.connection.cursor() as cursor:
        cursor.execute(
            "select count(*),count(*) filter(where job.state='ready'),"
            " bool_and(job.max_attempts=1)"
            " from runtime.loop_attempt_job binding"
            " join runtime.job job on job.realm_id=binding.realm_id and job.id=binding.job_id"
            " where binding.loop_id=%s",
            (scope.policy.id,),
        )
        assert cursor.fetchone() == (2, 1, True)
        cursor.execute(
            "select count(*) from runtime.loop_attempt_outcome where loop_id=%s",
            (scope.policy.id,),
        )
        assert cursor.fetchone() == (1,)

    second = worker.tick(now=scope.now + dt.timedelta(seconds=2))
    assert second.accepted_work and second.outcome is AttemptOutcome.SUCCEEDED
    assert scope.policies.terminal_state(scope.policy.id) is LoopTerminalState.PASSED
    third = worker.tick(now=scope.now + dt.timedelta(seconds=3))
    assert not third.accepted_work and third.skipped_reason == "kuyruk bos"
    with scope.connection.cursor() as cursor:
        cursor.execute(
            "select count(*),count(*) filter(where job.state='completed'),"
            " count(*) filter(where job.state='ready'),bool_and(job.max_attempts=1)"
            " from runtime.loop_attempt_job binding"
            " join runtime.job job on job.realm_id=binding.realm_id and job.id=binding.job_id"
            " where binding.loop_id=%s",
            (scope.policy.id,),
        )
        assert cursor.fetchone() == (2, 2, 0, True)
        cursor.execute(
            "select count(*) from runtime.effect_claim claim"
            " join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
            " and receipt.claim_id=claim.id where claim.job_id in"
            " (select job_id from runtime.loop_attempt_job where loop_id=%s)",
            (scope.policy.id,),
        )
        assert cursor.fetchone() == (2,)
    with pytest.raises(PolicyViolation, match="Terminal loop"):
        DurableLoopOrchestrator(scope.measured, scope.jobs).enqueue_attempt(scope.first_plan)


@dataclass(slots=True)
class _CrashRunner:
    calls: int = 0

    def run(self, work: Any, admission: Any) -> MeasuredLoopAttemptExecution:
        self.calls += 1
        raise RuntimeError("deterministic-runner-crash")


@pytest.mark.postgres
def test_production_measured_worker_crash_is_failed_once_without_silent_retry(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    scope = _worker_loop_scope(realm_session, tmp_path, label="crash")
    runner = _CrashRunner()
    worker = build_measured_loop_worker(
        scope.connection,
        scope.realm.id,
        contract_loader=_ContractLoader(scope),
        runner=runner,
    )
    first = worker.tick(now=scope.now + dt.timedelta(seconds=1))
    assert first.accepted_work and first.outcome is AttemptOutcome.FAILED
    second = worker.tick(now=scope.now + dt.timedelta(seconds=2))
    assert not second.accepted_work and second.skipped_reason == "kuyruk bos"
    assert runner.calls == 1
    with scope.connection.cursor() as cursor:
        cursor.execute(
            "select state,attempt_count,max_attempts from runtime.job where id=%s",
            (scope.first_plan.job.id,),
        )
        assert cursor.fetchone() == ("failed", 1, 1)
        cursor.execute(
            "select (select count(*) from runtime.loop_attempt_job where loop_id=%s),"
            " (select count(*) from runtime.loop_attempt where loop_id=%s),"
            " (select count(*) from runtime.loop_attempt_outcome where loop_id=%s)",
            (scope.policy.id, scope.policy.id, scope.policy.id),
        )
        assert cursor.fetchone() == (1, 1, 0)
