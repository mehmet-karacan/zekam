"""Measured topology, rollback ve ablation kanitlari icin PostgreSQL adapter'i.

Adapter ikinci bir execution authority olusturmaz. Kimlikleri kanonik domain
digest'lerinden deterministik uretir ve butun yazmalari migration 0076'nin
security-definer fonksiyonlarina yonlendirir.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    ExecutionTopologyDecision,
    GraphExecutionReceipt,
    LoopSuitabilityAssessment,
    TournamentPlan,
)
from zekam.domain.loop_change_set import LoopOwnedChangeSet, LoopRollbackReceipt
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationDecision,
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingDisposition,
)
from zekam.domain.work import TaskPlan

_TOPOLOGY_NAMESPACE = UUID("bb4df60f-5123-5f3f-a802-f53b1693c321")
_GRAPH_RECEIPT_NAMESPACE = UUID("f93ad9d3-1cd4-5d8a-9a73-92a9c95db3b3")
_TOURNAMENT_NAMESPACE = UUID("f9f69ac7-3d0b-59a6-a78f-76aeed143f1d")
_CHANGE_SET_NAMESPACE = UUID("0816f72f-341d-50b0-b4a6-339c1e91c9d9")
_ROLLBACK_RECEIPT_NAMESPACE = UUID("a205463d-7592-5ed6-a071-6ac8344b41c9")
_SCAFFOLDING_NAMESPACE = UUID("f9775de9-8947-5233-98ab-42f6f2ce640f")


def _stable_id(namespace: UUID, content_digest: str) -> UUID:
    return uuid5(namespace, content_digest)


@dataclass(frozen=True, slots=True)
class StoredMeasuredExecution:
    record_id: UUID
    record_digest: str
    created: bool


@dataclass(frozen=True, slots=True)
class PostgresMeasuredExecutionRepository:
    connection: Any
    realm_id: UUID

    def store_topology_decision(
        self,
        *,
        plan: TaskPlan,
        assessment: LoopSuitabilityAssessment,
        decision: ExecutionTopologyDecision,
    ) -> StoredMeasuredExecution:
        self._assert_plan(plan)
        if decision.plan_digest != plan.plan_digest:
            raise ValidationFailed("Topology decision current TaskPlan digest'ine bagli degil")
        if decision.pattern is not assessment.recommended_pattern:
            raise PolicyViolation("Topology decision suitability sonucundan sapamaz")
        record_id = _stable_id(_TOPOLOGY_NAMESPACE, decision.decision_digest)
        created = self._store_boolean(
            "select runtime.store_topology_decision( %s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s)",
            (
                record_id,
                plan.project_id,
                plan.work_item_id,
                plan.id,
                canonical_json(assessment.as_dict()),
                assessment.assessment_digest,
                decision.pattern.value,
                canonical_json(decision.as_dict()),
                decision.decision_digest,
            ),
        )
        return StoredMeasuredExecution(record_id, decision.decision_digest, created)

    def store_graph_execution_receipt(
        self,
        *,
        topology_decision_id: UUID,
        receipt: GraphExecutionReceipt,
        claimed_parallel: bool,
    ) -> StoredMeasuredExecution:
        fake_parallelism = claimed_parallel and (
            receipt.max_observed_concurrency < 2 or receipt.parallel_overlap_duration_millis <= 0
        )
        record_id = _stable_id(_GRAPH_RECEIPT_NAMESPACE, receipt.receipt_digest)
        created = self._store_boolean(
            "select runtime.store_graph_execution_receipt( %s,%s,%s,%s::jsonb,%s,%s)",
            (
                record_id,
                topology_decision_id,
                receipt.graph_root_id,
                canonical_json(receipt.as_dict()),
                receipt.receipt_digest,
                fake_parallelism,
            ),
        )
        return StoredMeasuredExecution(record_id, receipt.receipt_digest, created)

    def store_tournament_plan(
        self,
        *,
        topology_decision_id: UUID,
        plan: TournamentPlan,
    ) -> StoredMeasuredExecution:
        record_id = _stable_id(_TOURNAMENT_NAMESPACE, plan.plan_digest)
        created = self._store_boolean(
            "select runtime.store_tournament_plan( %s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                record_id,
                topology_decision_id,
                plan.selector_assignment_id,
                plan.selector_model_id,
                plan.selector_execution_identity,
                canonical_json(plan.as_dict()),
                plan.plan_digest,
            ),
        )
        return StoredMeasuredExecution(record_id, plan.plan_digest, created)

    def store_loop_change_set(
        self,
        *,
        loop_id: UUID,
        change_set: LoopOwnedChangeSet,
    ) -> StoredMeasuredExecution:
        change_digest = change_set.change_set_digest
        record_id = _stable_id(_CHANGE_SET_NAMESPACE, change_digest)
        created = self._store_boolean(
            "select runtime.store_loop_change_set( %s,%s,%s,%s::jsonb,%s,%s)",
            (
                record_id,
                loop_id,
                change_set.attempt_id,
                canonical_json(change_set.semantic_body()),
                change_digest,
                change_set.inverse_patch_digest,
            ),
        )
        return StoredMeasuredExecution(record_id, change_digest, created)

    def store_loop_rollback_receipt(
        self,
        *,
        change_set: LoopOwnedChangeSet,
        receipt: LoopRollbackReceipt,
    ) -> StoredMeasuredExecution:
        change_digest = change_set.change_set_digest
        if receipt.change_set_digest != change_digest:
            raise ValidationFailed("Rollback receipt exact loop change set'e bagli degil")
        if (
            receipt.inverse_patch_digest != change_set.inverse_patch_digest
            or receipt.changed_resources != change_set.changed_resources
        ):
            raise PolicyViolation("Rollback receipt inverse patch/resource binding drift")
        change_set_id = _stable_id(_CHANGE_SET_NAMESPACE, change_digest)
        receipt_digest = receipt.receipt_digest
        record_id = _stable_id(_ROLLBACK_RECEIPT_NAMESPACE, receipt_digest)
        body = {
            "schema": "zekam-loop-rollback-receipt/v1",
            "plan_digest": receipt.plan_digest,
            "change_set_digest": receipt.change_set_digest,
            "apply_check_digest": receipt.apply_check_digest,
            "inverse_patch_digest": receipt.inverse_patch_digest,
            "changed_resources": list(receipt.changed_resources),
            "post_state_digest": receipt.post_state_digest,
            "applied_at": receipt.applied_at,
            "status": receipt.status,
            "grants_authority": False,
        }
        created = self._store_boolean(
            "select runtime.store_loop_rollback_receipt(%s,%s,%s::jsonb,%s)",
            (record_id, change_set_id, canonical_json(body), receipt_digest),
        )
        return StoredMeasuredExecution(record_id, receipt_digest, created)

    def store_scaffolding_ablation(
        self,
        *,
        plan: TaskPlan,
        pair: ScaffoldingAblationPair,
        policy: ScaffoldingAblationPolicy,
        rollback_plan: ScaffoldingDeprecationRollbackPlan,
        decision: ScaffoldingAblationDecision,
    ) -> StoredMeasuredExecution:
        self._assert_plan(plan)
        if decision.pair_digest != pair.pair_digest:
            raise ValidationFailed("Scaffolding decision exact paired evidence'a bagli degil")
        if decision.policy_digest != policy.policy_digest:
            raise ValidationFailed("Scaffolding decision exact policy'ye bagli degil")
        if decision.rollback_plan_digest != rollback_plan.plan_digest:
            raise ValidationFailed("Scaffolding decision exact rollback planina bagli degil")
        body = {
            "schema": "zekam-scaffolding-ablation-record/v1",
            "pair": {
                "baseline": pair.baseline.as_dict(),
                "candidate": pair.candidate.as_dict(),
                "removed_feature": pair.removed_feature,
                "pair_digest": pair.pair_digest,
            },
            "policy": {
                "max_quality_drop": policy.max_quality_drop,
                "max_reliability_drop": policy.max_reliability_drop,
                "max_latency_increase_ratio": policy.max_latency_increase_ratio,
                "max_token_increase_ratio": policy.max_token_increase_ratio,
                "max_cost_increase_ratio": policy.max_cost_increase_ratio,
                "policy_digest": policy.policy_digest,
            },
            "rollback_plan": {
                "schema": "zekam-scaffolding-deprecation-rollback/v1",
                "feature": rollback_plan.feature,
                "restore_action_digest": rollback_plan.restore_action_digest,
                "source_revision": rollback_plan.source_revision,
                "review_ref": rollback_plan.review_ref,
                "grants_authority": False,
            },
            "rollback_plan_digest": rollback_plan.plan_digest,
            "decision": decision.semantic_body() | {"decision_digest": decision.decision_digest},
            "status": decision.review_status,
            "auto_delete": decision.auto_delete,
            "grants_authority": False,
        }
        ablation_digest = digest(body)
        record_id = _stable_id(_SCAFFOLDING_NAMESPACE, ablation_digest)
        disposition = (
            "deprecation-candidate"
            if decision.disposition is ScaffoldingDisposition.DEPRECATION_CANDIDATE
            else "keep-baseline"
        )
        created = self._store_boolean(
            "select runtime.store_scaffolding_ablation( %s,%s,%s,%s,%s::jsonb,%s,%s)",
            (
                record_id,
                plan.project_id,
                plan.work_item_id,
                plan.id,
                canonical_json(body),
                ablation_digest,
                disposition,
            ),
        )
        return StoredMeasuredExecution(record_id, ablation_digest, created)

    def _assert_plan(self, plan: TaskPlan) -> None:
        if plan.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm measured execution kaydi reddedildi")

    def _store_boolean(self, statement: str, parameters: tuple[Any, ...]) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return bool(cursor.fetchone()[0])
