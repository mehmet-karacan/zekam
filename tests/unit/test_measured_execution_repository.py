from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.scaffolding_ablation import ScaffoldingAblationService
from zekam.application.topology_planner import TopologyPlanner, TopologySuitabilityRequest
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    GraphExecutionReceipt,
    GraphNodeMode,
    GraphNodeReceipt,
    GraphNodeTerminalState,
    GraphTerminalState,
    MeasurementSourceTier,
    TournamentBudget,
    TournamentCandidateAssignment,
    TournamentPlan,
)
from zekam.domain.loop_change_set import (
    LoopChangeBaseline,
    LoopOwnedChangeSet,
    LoopRollbackReceipt,
    LoopSourceEntry,
    SourceEntryKind,
)
from zekam.domain.model_context_experiment import ContextAblationProfile
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingArmEvidence,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingMetrics,
)
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType
from zekam.infrastructure.postgres.measured_execution_repository import (
    PostgresMeasuredExecutionRepository,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 29, 9, 0, tzinfo=dt.UTC)


@dataclass
class FakeCursor:
    results: list[bool]
    calls: list[tuple[str, tuple[Any, ...]]]

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.calls.append((statement, parameters))

    def fetchone(self) -> tuple[bool]:
        return (self.results.pop(0),)


@dataclass
class FakeConnection:
    results: list[bool] = field(default_factory=lambda: [True])
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.results, self.calls)


def _task_plan(realm_id: UUID | None = None) -> TaskPlan:
    item = WorkItem.create(
        realm_id=realm_id or uuid4(),
        project_id=uuid4(),
        type=WorkType.TASK,
        title="Measured execution",
        now=NOW,
    )
    return TaskPlan.create(
        work_item=item,
        revision=1,
        source_revision="git:abc",
        policy_digest=digest("policy"),
        steps=(PlanStep("build", "Build", EffectKind.FILE_WRITE),),
        now=NOW,
    )


def _topology(plan: TaskPlan):  # type: ignore[no-untyped-def]
    request = TopologySuitabilityRequest(
        plan=plan,
        objective_digest=digest("objective"),
        measurement_available=True,
        measurement_source_tier=MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
        measurement_estimated_cost_micros=1,
        action_estimated_cost_micros=10,
        reversible=True,
        idempotent_or_receipt_bound=True,
    )
    planner = TopologyPlanner()
    return planner.assess(request), planner.decide(request)


def test_topology_adapter_uses_canonical_payload_and_stable_id() -> None:
    plan = _task_plan()
    assessment, decision = _topology(plan)
    connection = FakeConnection([True, False])
    repository = PostgresMeasuredExecutionRepository(connection, plan.realm_id)

    first = repository.store_topology_decision(plan=plan, assessment=assessment, decision=decision)
    replay = repository.store_topology_decision(plan=plan, assessment=assessment, decision=decision)

    assert first.record_id == replay.record_id
    assert first.created is True and replay.created is False
    statement, parameters = connection.calls[0]
    assert "runtime.store_topology_decision" in statement
    assert json.loads(parameters[4])["assessment_digest"] == assessment.assessment_digest
    assert json.loads(parameters[7])["decision_digest"] == decision.decision_digest


def test_topology_adapter_rejects_cross_realm_and_suitability_drift() -> None:
    plan = _task_plan()
    assessment, decision = _topology(plan)
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        PostgresMeasuredExecutionRepository(FakeConnection(), uuid4()).store_topology_decision(
            plan=plan, assessment=assessment, decision=decision
        )
    other_plan = _task_plan(plan.realm_id)
    with pytest.raises(ValidationFailed, match="TaskPlan digest"):
        PostgresMeasuredExecutionRepository(
            FakeConnection(), plan.realm_id
        ).store_topology_decision(plan=other_plan, assessment=assessment, decision=decision)


def _graph_receipt() -> GraphExecutionReceipt:
    node = GraphNodeReceipt(
        "build",
        GraphNodeMode.DIRECT,
        NOW,
        NOW,
        NOW + dt.timedelta(seconds=1),
        0,
        0,
        0,
        0,
        0,
        0,
        digest("result"),
        GraphNodeTerminalState.COMPLETED,
    )
    return GraphExecutionReceipt.create(
        graph_root_id=uuid4(),
        plan_digest=digest("graph-plan"),
        node_receipts=(node,),
        critical_path=("build",),
        max_observed_concurrency=1,
        parallel_overlap_duration_millis=0,
        parallel_efficiency_ppm=0,
        coordination_input_tokens=0,
        coordination_output_tokens=0,
        coordination_cost_micros=0,
        coordination_message_count=0,
        fan_in_result_digest=digest("fan-in"),
        terminal_state=GraphTerminalState.COMPLETED,
        topology_feedback=("sequential-graph-observed",),
    )


def test_graph_adapter_marks_unsupported_parallel_claim_for_database_gate() -> None:
    connection = FakeConnection()
    receipt = _graph_receipt()
    stored = PostgresMeasuredExecutionRepository(connection, uuid4()).store_graph_execution_receipt(
        topology_decision_id=uuid4(), receipt=receipt, claimed_parallel=True
    )
    statement, parameters = connection.calls[0]
    assert "runtime.store_graph_execution_receipt" in statement
    assert parameters[-1] is True
    assert stored.record_digest == receipt.receipt_digest


def _tournament() -> TournamentPlan:
    candidates = (
        TournamentCandidateAssignment(uuid4(), "builder-a", "exec-a", 10, 10),
        TournamentCandidateAssignment(uuid4(), "builder-b", "exec-b", 10, 10),
    )
    return TournamentPlan.create(
        candidate_assignments=candidates,
        shared_objective_digest=digest("objective"),
        candidate_context_digest=digest("context"),
        selector_assignment_id=uuid4(),
        selector_model_id="verifier",
        selector_execution_identity="verify-exec",
        selector_spec_digest=digest("selector"),
        human_final_gate=False,
        budget=TournamentBudget(2, 20, 20, NOW + dt.timedelta(minutes=1)),
    )


def test_tournament_adapter_preserves_independent_selector_binding() -> None:
    connection = FakeConnection()
    plan = _tournament()
    stored = PostgresMeasuredExecutionRepository(connection, uuid4()).store_tournament_plan(
        topology_decision_id=uuid4(), plan=plan
    )
    statement, parameters = connection.calls[0]
    assert "runtime.store_tournament_plan" in statement
    assert parameters[2:5] == (
        plan.selector_assignment_id,
        plan.selector_model_id,
        plan.selector_execution_identity,
    )
    assert json.loads(parameters[5])["candidate_isolation"] is True
    assert stored.record_digest == plan.plan_digest


def _change_set() -> LoopOwnedChangeSet:
    attempt_id = uuid4()
    before = LoopSourceEntry("src/target.py", SourceEntryKind.FILE, digest("before"))
    after = LoopSourceEntry("src/target.py", SourceEntryKind.FILE, digest("after"))
    baseline = LoopChangeBaseline(
        attempt_id,
        "git:abc",
        digest("tree"),
        digest("dirty"),
        ("src/target.py",),
        (before,),
        (),
        NOW,
    )
    return LoopOwnedChangeSet.create(
        baseline=baseline,
        changed_resources=("src/target.py",),
        before_entries=(before,),
        after_entries=(after,),
        forward_patch_digest=digest("forward"),
        inverse_patch_digest=digest("inverse"),
        created_at=NOW + dt.timedelta(seconds=1),
    )


def _rollback_receipt(change_set: LoopOwnedChangeSet) -> LoopRollbackReceipt:
    return LoopRollbackReceipt(
        digest("rollback-plan"),
        change_set.change_set_digest,
        digest("apply-check"),
        change_set.inverse_patch_digest,
        change_set.changed_resources,
        digest("post-state"),
        NOW + dt.timedelta(seconds=2),
    )


def test_change_set_and_rollback_receipt_share_deterministic_foreign_key() -> None:
    connection = FakeConnection([True, True])
    repository = PostgresMeasuredExecutionRepository(connection, uuid4())
    change_set = _change_set()
    stored_change = repository.store_loop_change_set(loop_id=uuid4(), change_set=change_set)
    receipt = _rollback_receipt(change_set)
    stored_receipt = repository.store_loop_rollback_receipt(change_set=change_set, receipt=receipt)

    assert "runtime.store_loop_change_set" in connection.calls[0][0]
    assert "runtime.store_loop_rollback_receipt" in connection.calls[1][0]
    assert connection.calls[1][1][1] == stored_change.record_id
    assert stored_receipt.record_digest == receipt.receipt_digest


def test_rollback_adapter_rejects_receipt_for_another_change_set() -> None:
    change_set = _change_set()
    receipt = _rollback_receipt(change_set)
    other = _change_set()
    with pytest.raises(ValidationFailed, match="exact loop change set"):
        PostgresMeasuredExecutionRepository(FakeConnection(), uuid4()).store_loop_rollback_receipt(
            change_set=other, receipt=receipt
        )


def _arm(arm_id: str, *, candidate: bool) -> ScaffoldingArmEvidence:
    profile = (
        ContextAblationProfile(("core",), ("critic",))
        if candidate
        else ContextAblationProfile(("core", "critic"), ())
    )
    return ScaffoldingArmEvidence(
        arm_id,
        profile,
        "local-model",
        digest("execution"),
        digest("objective"),
        digest("metrics"),
        digest("validators"),
        digest("fixtures"),
        digest("paired-trials"),
        "git:abc",
        5,
        ScaffoldingMetrics(0.9, 0.9, 90 if candidate else 100, 90, 90),
        digest(("evidence", arm_id)),
    )


def test_scaffolding_adapter_persists_paired_evidence_as_review_only() -> None:
    plan = _task_plan()
    pair = ScaffoldingAblationPair(
        _arm("baseline", candidate=False), _arm("candidate", candidate=True), "critic"
    )
    policy = ScaffoldingAblationPolicy()
    rollback = ScaffoldingDeprecationRollbackPlan(
        "critic", digest("restore"), "git:abc", "review:critic"
    )
    decision = ScaffoldingAblationService.evaluate(pair=pair, policy=policy, rollback_plan=rollback)
    connection = FakeConnection()
    stored = PostgresMeasuredExecutionRepository(
        connection, plan.realm_id
    ).store_scaffolding_ablation(
        plan=plan,
        pair=pair,
        policy=policy,
        rollback_plan=rollback,
        decision=decision,
    )

    statement, parameters = connection.calls[0]
    body = json.loads(parameters[4])
    assert "runtime.store_scaffolding_ablation" in statement
    assert body["status"] == "review-required"
    assert body["auto_delete"] is False
    assert body["pair"]["baseline"]["evidence_manifest_digest"].startswith("sha256:")
    assert parameters[-1] == "deprecation-candidate"
    assert stored.record_digest == parameters[-2]
