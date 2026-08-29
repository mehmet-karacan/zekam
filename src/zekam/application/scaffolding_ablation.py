"""Paired scaffolding ablation evaluator; it never deletes or promotes code."""

from __future__ import annotations

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationDecision,
    ScaffoldingAblationGate,
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingDisposition,
)


class ScaffoldingAblationService:
    """Classify only paired evidence as a review-required deprecation candidate."""

    @staticmethod
    def evaluate(
        *,
        pair: ScaffoldingAblationPair,
        policy: ScaffoldingAblationPolicy,
        rollback_plan: ScaffoldingDeprecationRollbackPlan,
    ) -> ScaffoldingAblationDecision:
        pair.__post_init__()
        policy.__post_init__()
        rollback_plan.__post_init__()
        if (
            rollback_plan.feature != pair.removed_feature
            or rollback_plan.source_revision != pair.baseline.source_revision
        ):
            raise PolicyViolation("Scaffolding deprecation exact rollback plan ister")
        baseline = pair.baseline.metrics
        candidate = pair.candidate.metrics
        gates = (
            ScaffoldingAblationGate(
                "scaffolding.quality-no-regression",
                candidate.quality >= baseline.quality - policy.max_quality_drop,
                baseline.quality,
                candidate.quality,
                baseline.quality - policy.max_quality_drop,
            ),
            ScaffoldingAblationGate(
                "scaffolding.reliability-no-regression",
                candidate.reliability >= baseline.reliability - policy.max_reliability_drop,
                baseline.reliability,
                candidate.reliability,
                baseline.reliability - policy.max_reliability_drop,
            ),
            ScaffoldingAblationGate(
                "scaffolding.latency-budget",
                candidate.latency_ms
                <= baseline.latency_ms * (1 + policy.max_latency_increase_ratio),
                baseline.latency_ms,
                candidate.latency_ms,
                baseline.latency_ms * (1 + policy.max_latency_increase_ratio),
            ),
            ScaffoldingAblationGate(
                "scaffolding.token-budget",
                candidate.token_count
                <= baseline.token_count * (1 + policy.max_token_increase_ratio),
                baseline.token_count,
                candidate.token_count,
                baseline.token_count * (1 + policy.max_token_increase_ratio),
            ),
            ScaffoldingAblationGate(
                "scaffolding.cost-budget",
                candidate.cost_micros
                <= baseline.cost_micros * (1 + policy.max_cost_increase_ratio),
                baseline.cost_micros,
                candidate.cost_micros,
                baseline.cost_micros * (1 + policy.max_cost_increase_ratio),
            ),
        )
        disposition = (
            ScaffoldingDisposition.DEPRECATION_CANDIDATE
            if all(item.passed for item in gates)
            else ScaffoldingDisposition.KEEP_SCAFFOLDING
        )
        semantic = {
            "schema": "zekam-scaffolding-ablation-decision/v1",
            "pair_digest": pair.pair_digest,
            "policy_digest": policy.policy_digest,
            "removed_feature": pair.removed_feature,
            "disposition": str(disposition),
            "gates": [item.as_dict() for item in gates],
            "rollback_plan_digest": rollback_plan.plan_digest,
            "review_status": "review-required",
            "auto_delete": False,
            "grants_authority": False,
        }
        return ScaffoldingAblationDecision(
            pair_digest=pair.pair_digest,
            policy_digest=policy.policy_digest,
            removed_feature=pair.removed_feature,
            disposition=disposition,
            gates=gates,
            rollback_plan_digest=rollback_plan.plan_digest,
            decision_digest=digest(semantic),
        )
