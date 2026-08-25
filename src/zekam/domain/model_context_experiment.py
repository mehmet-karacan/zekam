"""Reproducible, authority-free model/context experiment contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BenchmarkAggregate,
    MetricAggregate,
    TrialResult,
    VerifierIdentity,
    VerifierVerdict,
    aggregate_trials,
)


class ExperimentDecision(StrEnum):
    PROPOSE_CANDIDATE = "propose-candidate"
    KEEP_BASELINE = "keep-baseline"


@dataclass(frozen=True, slots=True)
class ModelContextExperimentPolicy:
    max_quality_drop: float = 0.0
    max_reliability_drop: float = 0.0
    max_latency_increase_ratio: float = 0.10
    max_token_increase_ratio: float = 0.10
    max_cost_increase_ratio: float = 0.05
    minimum_value_ratio: float = 1.0
    policy_version: str = "1"

    def __post_init__(self) -> None:
        tolerances = (
            self.max_quality_drop,
            self.max_reliability_drop,
            self.max_latency_increase_ratio,
            self.max_token_increase_ratio,
            self.max_cost_increase_ratio,
        )
        if any(not math.isfinite(value) or value < 0 for value in tolerances):
            raise ValidationFailed("experiment toleranslari gecersiz")
        if not math.isfinite(self.minimum_value_ratio) or self.minimum_value_ratio < 1:
            raise ValidationFailed("experiment minimum value ratio en az 1 olmali")
        if not self.policy_version.strip():
            raise ValidationFailed("experiment policy version ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-context-experiment-policy/v1",
            "policy_version": self.policy_version,
            "max_quality_drop": self.max_quality_drop,
            "max_reliability_drop": self.max_reliability_drop,
            "max_latency_increase_ratio": self.max_latency_increase_ratio,
            "max_token_increase_ratio": self.max_token_increase_ratio,
            "max_cost_increase_ratio": self.max_cost_increase_ratio,
            "minimum_value_ratio": self.minimum_value_ratio,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ContextAblationProfile:
    included_features: tuple[str, ...]
    excluded_features: tuple[str, ...]
    profile_version: str = "1"

    def __post_init__(self) -> None:
        if not self.profile_version.strip():
            raise ValidationFailed("ablation profile version ister")
        for values in (self.included_features, self.excluded_features):
            if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
                raise ValidationFailed("ablation features tekil, sirali ve dolu olmali")
        if set(self.included_features) & set(self.excluded_features):
            raise ValidationFailed("ablation feature included ve excluded olamaz")
        if not self.included_features:
            raise ValidationFailed("ablation en az bir included feature ister")

    @property
    def feature_universe(self) -> tuple[str, ...]:
        return tuple(sorted((*self.included_features, *self.excluded_features)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-ablation-profile/v1",
            "profile_version": self.profile_version,
            "included_features": list(self.included_features),
            "excluded_features": list(self.excluded_features),
        }

    @property
    def profile_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ExperimentEvidenceManifest:
    tested_model_id: str
    context_packet_digest: str
    context_manifest_digest: str
    route_decision_digest: str
    ablation_profile_digest: str
    source_revision: str
    suite_digest: str
    workload: str
    paired_trial_set_digest: str
    aggregate_evidence_digest: str
    verifier_model_id: str
    verifier_execution_identity: str
    verifier_provenance_digest: str
    repetitions: int
    trial_evidence_digests: tuple[str, ...]
    verifier_verdict_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.tested_model_id,
                self.source_revision,
                self.workload,
                self.verifier_model_id,
                self.verifier_execution_identity,
            )
        ):
            raise ValidationFailed("experiment evidence identity/scope ister")
        for value in (
            self.context_packet_digest,
            self.context_manifest_digest,
            self.route_decision_digest,
            self.ablation_profile_digest,
            self.suite_digest,
            self.paired_trial_set_digest,
            self.aggregate_evidence_digest,
            self.verifier_provenance_digest,
        ):
            parse_digest(value)
        if self.repetitions < 5:
            raise PolicyViolation("experiment evidence en az bes repetition ister")
        if self.tested_model_id == self.verifier_model_id:
            raise PolicyViolation("experiment tested model kendi verifier'i olamaz")
        for values, label in (
            (self.trial_evidence_digests, "trial evidence"),
            (self.verifier_verdict_digests, "verifier verdict"),
        ):
            if len(values) != self.repetitions:
                raise ValidationFailed(f"experiment {label} repetition sayisi uyusmuyor")
            for value in values:
                parse_digest(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-context-experiment-evidence/v1",
            "tested_model_id": self.tested_model_id,
            "context_packet_digest": self.context_packet_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "route_decision_digest": self.route_decision_digest,
            "ablation_profile_digest": self.ablation_profile_digest,
            "source_revision": self.source_revision,
            "suite_digest": self.suite_digest,
            "workload": self.workload,
            "paired_trial_set_digest": self.paired_trial_set_digest,
            "aggregate_evidence_digest": self.aggregate_evidence_digest,
            "verifier_model_id": self.verifier_model_id,
            "verifier_execution_identity": self.verifier_execution_identity,
            "verifier_provenance_digest": self.verifier_provenance_digest,
            "repetitions": self.repetitions,
            "trial_evidence_digests": list(self.trial_evidence_digests),
            "verifier_verdict_digests": list(self.verifier_verdict_digests),
        }

    @property
    def evidence_manifest_digest(self) -> str:
        return digest(self.as_dict())


def _validate_metric(name: str, metric: MetricAggregate) -> None:
    values = (metric.mean, metric.median, metric.p95, metric.variance)
    if any(not math.isfinite(value) or value < 0 for value in values) or metric.p95 < metric.median:
        raise ValidationFailed(f"experiment {name} metrikleri gecersiz")
    if name in {"quality", "reliability"} and any(value > 1 for value in values[:3]):
        raise ValidationFailed(f"experiment {name} metrikleri 0..1 olmali")


@dataclass(frozen=True, slots=True)
class ExperimentArm:
    arm_id: str
    model_id: str
    context_packet_digest: str
    context_manifest_digest: str
    route_decision_digest: str
    source_revision: str
    suite_digest: str
    workload: str
    ablation_profile: ContextAblationProfile
    aggregate: BenchmarkAggregate
    evidence_manifest: ExperimentEvidenceManifest
    trials: tuple[TrialResult, ...]
    verifier: VerifierIdentity
    verifier_verdicts: tuple[VerifierVerdict, ...]

    def __post_init__(self) -> None:
        self.ablation_profile.__post_init__()
        self.evidence_manifest.__post_init__()
        self.verifier.__post_init__()
        for trial in self.trials:
            trial.__post_init__()
        for verifier_verdict in self.verifier_verdicts:
            verifier_verdict.__post_init__()
        if not all(
            value.strip()
            for value in (self.arm_id, self.model_id, self.source_revision, self.workload)
        ):
            raise ValidationFailed("experiment arm kimlik ve scope ister")
        for value in (
            self.context_packet_digest,
            self.context_manifest_digest,
            self.route_decision_digest,
            self.suite_digest,
            self.aggregate.evidence_digest,
            self.aggregate.verifier_provenance_digest,
        ):
            parse_digest(value)
        expected = (
            self.model_id,
            self.context_packet_digest,
            self.context_manifest_digest,
            self.route_decision_digest,
            self.ablation_profile.profile_digest,
            self.source_revision,
            self.suite_digest,
            self.workload,
            self.aggregate.evidence_digest,
            self.aggregate.verifier_model_id,
            self.aggregate.verifier_execution_identity,
            self.aggregate.verifier_provenance_digest,
        )
        observed = (
            self.evidence_manifest.tested_model_id,
            self.evidence_manifest.context_packet_digest,
            self.evidence_manifest.context_manifest_digest,
            self.evidence_manifest.route_decision_digest,
            self.evidence_manifest.ablation_profile_digest,
            self.evidence_manifest.source_revision,
            self.evidence_manifest.suite_digest,
            self.evidence_manifest.workload,
            self.evidence_manifest.aggregate_evidence_digest,
            self.evidence_manifest.verifier_model_id,
            self.evidence_manifest.verifier_execution_identity,
            self.evidence_manifest.verifier_provenance_digest,
        )
        if expected != observed or self.aggregate.tested_model_id != self.model_id:
            raise ValidationFailed("experiment arm evidence manifest binding drift")
        recomputed = aggregate_trials(
            self.trials, tested_model_id=self.model_id, verifier=self.verifier
        )
        if recomputed != self.aggregate:
            raise ValidationFailed("experiment aggregate canonical trial recompute drift")
        ordered_trials = tuple(
            sorted(
                self.trials,
                key=lambda row: (
                    row.fixture_digest,
                    row.repetition,
                    row.response_digest,
                    row.evidence_digest,
                ),
            )
        )
        verdict_by_response = {item.tested_response_digest: item for item in self.verifier_verdicts}
        if len(verdict_by_response) != len(ordered_trials):
            raise ValidationFailed("experiment verifier verdict trial cardinality drift")
        for trial in ordered_trials:
            verdict = verdict_by_response.get(trial.response_digest)
            if verdict is None or (
                verdict.tested_model_id,
                verdict.verifier_model_id,
                verdict.execution_identity,
                verdict.approved,
            ) != (
                self.model_id,
                self.verifier.model_id,
                self.verifier.execution_identity,
                trial.verifier_approved,
            ):
                raise ValidationFailed("experiment verifier verdict trial binding drift")
        expected_pair_set = digest(
            [
                {"fixture_digest": row.fixture_digest, "repetition": row.repetition}
                for row in ordered_trials
            ]
        )
        expected_trial_evidence = tuple(row.evidence_digest for row in ordered_trials)
        expected_verdicts = tuple(
            digest(
                {
                    "tested_model_id": verdict_by_response[row.response_digest].tested_model_id,
                    "verifier_model_id": verdict_by_response[row.response_digest].verifier_model_id,
                    "execution_identity": verdict_by_response[
                        row.response_digest
                    ].execution_identity,
                    "tested_response_digest": verdict_by_response[
                        row.response_digest
                    ].tested_response_digest,
                    "approved": verdict_by_response[row.response_digest].approved,
                    "evidence_digest": verdict_by_response[row.response_digest].evidence_digest,
                }
            )
            for row in ordered_trials
        )
        if (
            self.evidence_manifest.repetitions != len(ordered_trials)
            or self.evidence_manifest.paired_trial_set_digest != expected_pair_set
            or self.evidence_manifest.trial_evidence_digests != expected_trial_evidence
            or self.evidence_manifest.verifier_verdict_digests != expected_verdicts
        ):
            raise ValidationFailed("experiment evidence manifest trial provenance drift")
        if self.aggregate.approved and self.aggregate.unsafe:
            raise ValidationFailed("unsafe aggregate approved olamaz")
        for name, metric in (
            ("quality", self.aggregate.quality),
            ("reliability", self.aggregate.reliability),
            ("latency", self.aggregate.latency_ms),
            ("token", self.aggregate.token_count),
            ("cost", self.aggregate.cost),
        ):
            _validate_metric(name, metric)

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "model_id": self.model_id,
            "context_packet_digest": self.context_packet_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "route_decision_digest": self.route_decision_digest,
            "source_revision": self.source_revision,
            "suite_digest": self.suite_digest,
            "workload": self.workload,
            "ablation_profile": self.ablation_profile.as_dict(),
            "aggregate": self.aggregate.as_dict(),
            "evidence_manifest": self.evidence_manifest.as_dict(),
            "evidence_manifest_digest": self.evidence_manifest.evidence_manifest_digest,
            "trial_evidence_digests": [item.evidence_digest for item in self.trials],
            "verifier_verdict_evidence_digests": [
                item.evidence_digest for item in self.verifier_verdicts
            ],
        }

    @property
    def arm_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ExperimentGate:
    code: str
    passed: bool
    baseline: float | str | bool
    candidate: float | str | bool
    limit: float | str | bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "passed": self.passed,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "limit": self.limit,
        }


def _value(aggregate: BenchmarkAggregate) -> float:
    return aggregate.quality.mean * aggregate.reliability.mean / (1 + aggregate.cost.mean)


def _comparison(
    b: ExperimentArm, c: ExperimentArm, p: ModelContextExperimentPolicy
) -> tuple[tuple[ExperimentGate, ...], float, float, float]:
    bv, cv = _value(b.aggregate), _value(c.aggregate)
    ratio = cv / bv if bv > 0 else 0.0
    gates = (
        ExperimentGate(
            "experiment.baseline-approved", b.aggregate.approved, True, b.aggregate.approved, True
        ),
        ExperimentGate(
            "experiment.candidate-approved", c.aggregate.approved, True, c.aggregate.approved, True
        ),
        ExperimentGate(
            "experiment.safety-no-regression",
            not c.aggregate.unsafe,
            b.aggregate.unsafe,
            c.aggregate.unsafe,
            False,
        ),
        ExperimentGate(
            "experiment.quality-no-regression",
            c.aggregate.quality.mean >= b.aggregate.quality.mean - p.max_quality_drop,
            b.aggregate.quality.mean,
            c.aggregate.quality.mean,
            b.aggregate.quality.mean - p.max_quality_drop,
        ),
        ExperimentGate(
            "experiment.reliability-no-regression",
            c.aggregate.reliability.mean >= b.aggregate.reliability.mean - p.max_reliability_drop,
            b.aggregate.reliability.mean,
            c.aggregate.reliability.mean,
            b.aggregate.reliability.mean - p.max_reliability_drop,
        ),
        ExperimentGate(
            "experiment.latency-budget",
            c.aggregate.latency_ms.p95
            <= b.aggregate.latency_ms.p95 * (1 + p.max_latency_increase_ratio),
            b.aggregate.latency_ms.p95,
            c.aggregate.latency_ms.p95,
            b.aggregate.latency_ms.p95 * (1 + p.max_latency_increase_ratio),
        ),
        ExperimentGate(
            "experiment.token-budget",
            c.aggregate.token_count.mean
            <= b.aggregate.token_count.mean * (1 + p.max_token_increase_ratio),
            b.aggregate.token_count.mean,
            c.aggregate.token_count.mean,
            b.aggregate.token_count.mean * (1 + p.max_token_increase_ratio),
        ),
        ExperimentGate(
            "experiment.cost-budget",
            c.aggregate.cost.mean <= b.aggregate.cost.mean * (1 + p.max_cost_increase_ratio),
            b.aggregate.cost.mean,
            c.aggregate.cost.mean,
            b.aggregate.cost.mean * (1 + p.max_cost_increase_ratio),
        ),
        ExperimentGate(
            "experiment.cost-value", ratio >= p.minimum_value_ratio, bv, cv, p.minimum_value_ratio
        ),
    )
    return gates, bv, cv, ratio


def _validate_pair(b: ExperimentArm, c: ExperimentArm) -> None:
    b.__post_init__()
    c.__post_init__()
    if b.arm_id == c.arm_id:
        raise ValidationFailed("experiment arms ayri olmali")
    for field in ("source_revision", "suite_digest", "workload"):
        if getattr(b, field) != getattr(c, field):
            raise ValidationFailed(f"experiment paired scope drift: {field}")
    if b.evidence_manifest.paired_trial_set_digest != c.evidence_manifest.paired_trial_set_digest:
        raise ValidationFailed("experiment paired trial set drift")
    if (
        b.aggregate.evidence_digest == c.aggregate.evidence_digest
        or b.evidence_manifest.evidence_manifest_digest
        == c.evidence_manifest.evidence_manifest_digest
    ):
        raise ValidationFailed("experiment candidate distinct benchmark evidence ister")
    if (
        b.model_id == c.model_id
        and b.ablation_profile.profile_digest == c.ablation_profile.profile_digest
    ):
        raise ValidationFailed("experiment candidate model veya ablation degistirmeli")
    if b.ablation_profile.feature_universe != c.ablation_profile.feature_universe:
        raise ValidationFailed("experiment ablation feature universe drift")
    universe = set(b.ablation_profile.feature_universe)
    if (
        set(b.ablation_profile.excluded_features)
        != universe - set(b.ablation_profile.included_features)
        or set(c.ablation_profile.excluded_features)
        != universe - set(c.ablation_profile.included_features)
        or not set(c.ablation_profile.included_features).issubset(
            b.ablation_profile.included_features
        )
    ):
        raise ValidationFailed("experiment ablation kontrollu feature delta ister")


@dataclass(frozen=True, slots=True)
class ModelContextExperimentResult:
    baseline: ExperimentArm
    candidate: ExperimentArm
    policy: ModelContextExperimentPolicy
    decision: ExperimentDecision
    gates: tuple[ExperimentGate, ...]
    baseline_value: float
    candidate_value: float
    value_ratio: float
    experiment_digest: str
    review_status: str = "review-required"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _validate_pair(self.baseline, self.candidate)
        self.policy.__post_init__()
        expected_gates, bv, cv, ratio = _comparison(self.baseline, self.candidate, self.policy)
        expected_decision = (
            ExperimentDecision.PROPOSE_CANDIDATE
            if all(item.passed for item in expected_gates)
            else ExperimentDecision.KEEP_BASELINE
        )
        if (
            self.gates != expected_gates
            or self.decision is not expected_decision
            or (self.baseline_value, self.candidate_value, self.value_ratio) != (bv, cv, ratio)
        ):
            raise ValidationFailed("experiment result comparison semantic drift")
        if any(
            not math.isfinite(value) or value < 0
            for value in (self.baseline_value, self.candidate_value, self.value_ratio)
        ):
            raise ValidationFailed("experiment result value metrikleri gecersiz")
        if self.review_status != "review-required" or self.grants_authority:
            raise PolicyViolation("experiment sonucu review olmadan authority veremez")
        if self.experiment_digest != digest(self.semantic_body()):
            raise ValidationFailed("experiment result digest drift")

    def semantic_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-context-experiment/v1",
            "baseline_arm_digest": self.baseline.arm_digest,
            "candidate_arm_digest": self.candidate.arm_digest,
            "policy_digest": self.policy.policy_digest,
            "decision": str(self.decision),
            "gates": [item.as_dict() for item in self.gates],
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "value_ratio": self.value_ratio,
            "review_status": "review-required",
            "grants_authority": False,
        }

    def to_bytes(self) -> bytes:
        return canonical_bytes(
            {
                **self.semantic_body(),
                "experiment_digest": self.experiment_digest,
                "baseline": self.baseline.as_dict(),
                "candidate": self.candidate.as_dict(),
                "policy": self.policy.as_dict(),
            }
        )


def compare_model_context_arms(
    baseline: ExperimentArm, candidate: ExperimentArm, policy: ModelContextExperimentPolicy
) -> ModelContextExperimentResult:
    _validate_pair(baseline, candidate)
    policy.__post_init__()
    gates, bv, cv, ratio = _comparison(baseline, candidate, policy)
    decision = (
        ExperimentDecision.PROPOSE_CANDIDATE
        if all(item.passed for item in gates)
        else ExperimentDecision.KEEP_BASELINE
    )
    provisional = {
        "schema": "zekam-model-context-experiment/v1",
        "baseline_arm_digest": baseline.arm_digest,
        "candidate_arm_digest": candidate.arm_digest,
        "policy_digest": policy.policy_digest,
        "decision": str(decision),
        "gates": [item.as_dict() for item in gates],
        "baseline_value": bv,
        "candidate_value": cv,
        "value_ratio": ratio,
        "review_status": "review-required",
        "grants_authority": False,
    }
    return ModelContextExperimentResult(
        baseline, candidate, policy, decision, gates, bv, cv, ratio, digest(provisional)
    )
