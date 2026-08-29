"""Paired, execution-controlled scaffolding ablation evidence contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_context_experiment import ContextAblationProfile


@dataclass(frozen=True, slots=True)
class ScaffoldingMetrics:
    quality: float
    reliability: float
    latency_ms: float
    token_count: float
    cost_micros: float

    def __post_init__(self) -> None:
        values = (
            self.quality,
            self.reliability,
            self.latency_ms,
            self.token_count,
            self.cost_micros,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValidationFailed(
                "Scaffolding ablation metrikleri sonlu ve negatif olmayan olmali"
            )
        if self.quality > 1 or self.reliability > 1:
            raise ValidationFailed("Scaffolding quality/reliability 0..1 olmali")

    def as_dict(self) -> dict[str, float]:
        return {
            "quality": self.quality,
            "reliability": self.reliability,
            "latency_ms": self.latency_ms,
            "token_count": self.token_count,
            "cost_micros": self.cost_micros,
        }


@dataclass(frozen=True, slots=True)
class ScaffoldingArmEvidence:
    arm_id: str
    profile: ContextAblationProfile
    tested_model_id: str
    execution_profile_digest: str
    objective_digest: str
    metric_vector_digest: str
    validator_asset_manifest_digest: str
    fixture_set_digest: str
    paired_trial_set_digest: str
    source_revision: str
    repetitions: int
    metrics: ScaffoldingMetrics
    evidence_manifest_digest: str

    def __post_init__(self) -> None:
        self.profile.__post_init__()
        self.metrics.__post_init__()
        if not all(
            value.strip() for value in (self.arm_id, self.tested_model_id, self.source_revision)
        ):
            raise ValidationFailed("Scaffolding arm identity/model/source ister")
        for value in (
            self.execution_profile_digest,
            self.objective_digest,
            self.metric_vector_digest,
            self.validator_asset_manifest_digest,
            self.fixture_set_digest,
            self.paired_trial_set_digest,
            self.evidence_manifest_digest,
        ):
            parse_digest(value)
        if self.repetitions < 5:
            raise PolicyViolation("Scaffolding ablation en az bes paired repetition ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "profile": self.profile.as_dict(),
            "tested_model_id": self.tested_model_id,
            "execution_profile_digest": self.execution_profile_digest,
            "objective_digest": self.objective_digest,
            "metric_vector_digest": self.metric_vector_digest,
            "validator_asset_manifest_digest": self.validator_asset_manifest_digest,
            "fixture_set_digest": self.fixture_set_digest,
            "paired_trial_set_digest": self.paired_trial_set_digest,
            "source_revision": self.source_revision,
            "repetitions": self.repetitions,
            "metrics": self.metrics.as_dict(),
            "evidence_manifest_digest": self.evidence_manifest_digest,
        }

    @property
    def arm_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ScaffoldingAblationPair:
    baseline: ScaffoldingArmEvidence
    candidate: ScaffoldingArmEvidence
    removed_feature: str

    def __post_init__(self) -> None:
        self.baseline.__post_init__()
        self.candidate.__post_init__()
        if not self.removed_feature.strip():
            raise ValidationFailed("Scaffolding ablation removed feature ister")
        if self.baseline.arm_id == self.candidate.arm_id:
            raise ValidationFailed("Scaffolding ablation ayri arm ister")
        equal_fields = (
            "tested_model_id",
            "execution_profile_digest",
            "objective_digest",
            "metric_vector_digest",
            "validator_asset_manifest_digest",
            "fixture_set_digest",
            "paired_trial_set_digest",
            "source_revision",
            "repetitions",
        )
        drift = [
            field
            for field in equal_fields
            if getattr(self.baseline, field) != getattr(self.candidate, field)
        ]
        if drift:
            raise PolicyViolation("Scaffolding paired control drift: " + ",".join(sorted(drift)))
        if self.baseline.evidence_manifest_digest == self.candidate.evidence_manifest_digest:
            raise ValidationFailed("Scaffolding arms distinct evidence ister")
        if self.baseline.profile.feature_universe != self.candidate.profile.feature_universe:
            raise ValidationFailed("Scaffolding feature universe drift")
        baseline_features = set(self.baseline.profile.included_features)
        candidate_features = set(self.candidate.profile.included_features)
        if baseline_features - candidate_features != {self.removed_feature}:
            raise ValidationFailed("Scaffolding ablation exact tek feature removal ister")
        if not candidate_features.issubset(baseline_features):
            raise ValidationFailed("Scaffolding candidate controlled removal olmali")

    @property
    def pair_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-scaffolding-ablation-pair/v1",
                "baseline_arm_digest": self.baseline.arm_digest,
                "candidate_arm_digest": self.candidate.arm_digest,
                "removed_feature": self.removed_feature,
            }
        )


@dataclass(frozen=True, slots=True)
class ScaffoldingAblationPolicy:
    max_quality_drop: float = 0.0
    max_reliability_drop: float = 0.0
    max_latency_increase_ratio: float = 0.0
    max_token_increase_ratio: float = 0.0
    max_cost_increase_ratio: float = 0.0

    def __post_init__(self) -> None:
        values = (
            self.max_quality_drop,
            self.max_reliability_drop,
            self.max_latency_increase_ratio,
            self.max_token_increase_ratio,
            self.max_cost_increase_ratio,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValidationFailed("Scaffolding ablation policy toleranslari gecersiz")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-scaffolding-ablation-policy/v1",
                "max_quality_drop": self.max_quality_drop,
                "max_reliability_drop": self.max_reliability_drop,
                "max_latency_increase_ratio": self.max_latency_increase_ratio,
                "max_token_increase_ratio": self.max_token_increase_ratio,
                "max_cost_increase_ratio": self.max_cost_increase_ratio,
            }
        )


@dataclass(frozen=True, slots=True)
class ScaffoldingAblationGate:
    code: str
    passed: bool
    baseline: float
    candidate: float
    limit: float

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValidationFailed("Scaffolding gate code ister")
        if any(not math.isfinite(value) for value in (self.baseline, self.candidate, self.limit)):
            raise ValidationFailed("Scaffolding gate sonlu metric ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "passed": self.passed,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "limit": self.limit,
        }


class ScaffoldingDisposition(StrEnum):
    DEPRECATION_CANDIDATE = "deprecation-candidate"
    KEEP_SCAFFOLDING = "keep-scaffolding"


@dataclass(frozen=True, slots=True)
class ScaffoldingDeprecationRollbackPlan:
    feature: str
    restore_action_digest: str
    source_revision: str
    review_ref: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Scaffolding rollback plan authority veremez")
        required = (self.feature, self.source_revision, self.review_ref)
        if not all(value.strip() for value in required):
            raise ValidationFailed("Scaffolding rollback exact feature/source/review ister")
        parse_digest(self.restore_action_digest)

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-scaffolding-deprecation-rollback/v1",
                "feature": self.feature,
                "restore_action_digest": self.restore_action_digest,
                "source_revision": self.source_revision,
                "review_ref": self.review_ref,
                "grants_authority": False,
            }
        )


@dataclass(frozen=True, slots=True)
class ScaffoldingAblationDecision:
    pair_digest: str
    policy_digest: str
    removed_feature: str
    disposition: ScaffoldingDisposition
    gates: tuple[ScaffoldingAblationGate, ...]
    rollback_plan_digest: str
    decision_digest: str
    review_status: str = "review-required"
    auto_delete: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (self.pair_digest, self.policy_digest, self.rollback_plan_digest):
            parse_digest(value)
        if not self.removed_feature.strip() or not self.gates:
            raise ValidationFailed("Scaffolding decision feature ve gate ister")
        if self.review_status != "review-required" or self.auto_delete or self.grants_authority:
            raise PolicyViolation("Scaffolding sonucu yalniz review-required candidate olabilir")
        if self.decision_digest != digest(self.semantic_body()):
            raise ValidationFailed("Scaffolding ablation decision digest drift")

    def semantic_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-scaffolding-ablation-decision/v1",
            "pair_digest": self.pair_digest,
            "policy_digest": self.policy_digest,
            "removed_feature": self.removed_feature,
            "disposition": str(self.disposition),
            "gates": [item.as_dict() for item in self.gates],
            "rollback_plan_digest": self.rollback_plan_digest,
            "review_status": "review-required",
            "auto_delete": False,
            "grants_authority": False,
        }
