"""Generic, evidence-bound optimization contracts shared by measured loops."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    TARGET = "target"
    RANGE = "range"


class MetricRole(StrEnum):
    PRIMARY = "primary"
    HARD_GUARD = "hard-guard"
    SECONDARY = "secondary"
    COST = "cost"


class MetricAggregation(StrEnum):
    LATEST = "latest"
    MEAN = "mean"
    MEDIAN = "median"
    P95 = "p95"
    SUM = "sum"


class ProgressState(StrEnum):
    IMPROVED = "improved"
    TARGET_REACHED = "target-reached"
    PLATEAU = "plateau"
    REGRESSED = "regressed"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    metric_id: str
    name: str
    unit: str
    direction: MetricDirection
    role: MetricRole
    source_kind: str
    target_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    minimum_meaningful_delta: float = 0.0
    regression_tolerance: float = 0.0
    aggregation: MetricAggregation = MetricAggregation.LATEST

    def __post_init__(self) -> None:
        for label, value in (
            ("metric_id", self.metric_id),
            ("name", self.name),
            ("unit", self.unit),
            ("source_kind", self.source_kind),
        ):
            if not value.strip():
                raise ValidationFailed(f"Metric {label} bos olamaz")
        values = (
            self.target_value,
            self.min_value,
            self.max_value,
            self.minimum_meaningful_delta,
            self.regression_tolerance,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValidationFailed("Metric esik ve toleranslari sonlu olmali")
        if self.minimum_meaningful_delta < 0 or self.regression_tolerance < 0:
            raise ValidationFailed("Metric delta ve regression toleransi negatif olamaz")
        if self.direction in {MetricDirection.MAXIMIZE, MetricDirection.MINIMIZE}:
            if (
                self.target_value is None
                or self.min_value is not None
                or self.max_value is not None
            ):
                raise ValidationFailed("Maximize/minimize metric exact target_value ister")
        elif self.direction is MetricDirection.TARGET:
            if (
                self.target_value is None
                or self.min_value is not None
                or self.max_value is not None
            ):
                raise ValidationFailed("Target metric exact target_value ister")
        elif (
            self.target_value is not None
            or self.min_value is None
            or self.max_value is None
            or self.min_value > self.max_value
        ):
            raise ValidationFailed("Range metric gecerli min_value/max_value ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "unit": self.unit,
            "direction": str(self.direction),
            "role": str(self.role),
            "source_kind": self.source_kind,
            "target_value": self.target_value,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "minimum_meaningful_delta": self.minimum_meaningful_delta,
            "regression_tolerance": self.regression_tolerance,
            "aggregation": str(self.aggregation),
        }

    @property
    def spec_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class OptimizationObjective:
    objective_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    step_id: str
    artifact_ref: str
    artifact_baseline_digest: str
    measurement_plan_digest: str
    validator_asset_manifest_digest: str
    metric_specs: tuple[MetricSpec, ...]
    max_attempts: int
    max_tokens: int
    max_cost_micros: int
    deadline: dt.datetime
    reversibility_class: str
    created_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Optimization objective authority veremez")
        for label, value in (
            ("step_id", self.step_id),
            ("artifact_ref", self.artifact_ref),
            ("reversibility_class", self.reversibility_class),
        ):
            if not value.strip():
                raise ValidationFailed(f"Objective {label} bos olamaz")
        for value in (
            self.artifact_baseline_digest,
            self.measurement_plan_digest,
            self.validator_asset_manifest_digest,
        ):
            parse_digest(value)
        if not 1 <= self.max_attempts <= 100:
            raise ValidationFailed("Objective max attempts 1..100 araliginda olmali")
        if self.max_tokens < 1 or self.max_cost_micros < 1:
            raise ValidationFailed("Objective token ve cost butcesi pozitif olmali")
        if self.created_at.tzinfo is None or self.deadline.tzinfo is None:
            raise ValidationFailed("Objective zamanlari timezone-aware olmali")
        if self.deadline <= self.created_at:
            raise ValidationFailed("Objective deadline created_at sonrasinda olmali")
        metric_ids = tuple(item.metric_id for item in self.metric_specs)
        if not metric_ids or metric_ids != tuple(sorted(set(metric_ids))):
            raise ValidationFailed("Objective metric listesi dolu, tekil ve kanonik olmali")
        if not any(item.role is MetricRole.PRIMARY for item in self.metric_specs):
            raise ValidationFailed("Objective en az bir primary metric ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective_id": str(self.objective_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id),
            "step_id": self.step_id,
            "artifact_ref": self.artifact_ref,
            "artifact_baseline_digest": self.artifact_baseline_digest,
            "measurement_plan_digest": self.measurement_plan_digest,
            "validator_asset_manifest_digest": self.validator_asset_manifest_digest,
            "metric_specs": [item.as_dict() for item in self.metric_specs],
            "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
            "reversibility_class": self.reversibility_class,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def objective_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class MeasurementEvidence:
    metric_id: str
    value: float
    evidence_ref: str
    evidence_digest: str
    source_revision: str
    measured_at: dt.datetime
    measurement_identity: str
    verifier_identity: str
    producer_self_report: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("metric_id", self.metric_id),
            ("evidence_ref", self.evidence_ref),
            ("source_revision", self.source_revision),
            ("measurement_identity", self.measurement_identity),
            ("verifier_identity", self.verifier_identity),
        ):
            if not value.strip():
                raise ValidationFailed(f"Measurement {label} bos olamaz")
        if not math.isfinite(self.value):
            raise ValidationFailed("Measurement value sonlu olmali")
        parse_digest(self.evidence_digest)
        if self.measured_at.tzinfo is None:
            raise ValidationFailed("Measurement zamani timezone-aware olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "value": self.value,
            "evidence_ref": self.evidence_ref,
            "evidence_digest": self.evidence_digest,
            "source_revision": self.source_revision,
            "measured_at": self.measured_at,
            "measurement_identity": self.measurement_identity,
            "verifier_identity": self.verifier_identity,
            "producer_self_report": self.producer_self_report,
        }


@dataclass(frozen=True, slots=True)
class MetricProgressResult:
    metric_id: str
    role: MetricRole
    favorable_delta: float
    meaningful_progress: bool
    regressed: bool
    target_reached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "role": str(self.role),
            "favorable_delta": self.favorable_delta,
            "meaningful_progress": self.meaningful_progress,
            "regressed": self.regressed,
            "target_reached": self.target_reached,
        }


@dataclass(frozen=True, slots=True)
class ProgressVector:
    baseline_values: tuple[tuple[str, float], ...]
    previous_values: tuple[tuple[str, float], ...]
    current_values: tuple[tuple[str, float], ...]
    deltas: tuple[tuple[str, float], ...]
    metric_results: tuple[MetricProgressResult, ...]
    evidence_digests: tuple[str, ...]
    value_per_cost: float | None
    progress_state: ProgressState
    invalid_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        value_sets = (
            self.baseline_values,
            self.previous_values,
            self.current_values,
            self.deltas,
        )
        for values in value_sets:
            keys = tuple(key for key, _value in values)
            if keys != tuple(sorted(set(keys))):
                raise ValidationFailed("Progress metric values kanonik ve tekil olmali")
            if any(not math.isfinite(value) for _key, value in values):
                raise ValidationFailed("Progress metric values sonlu olmali")
        key_sets = tuple(tuple(key for key, _value in values) for values in value_sets)
        result_keys = tuple(item.metric_id for item in self.metric_results)
        if self.progress_state is not ProgressState.INVALID and (
            len(set(key_sets)) != 1 or result_keys != key_sets[-1]
        ):
            raise ValidationFailed("Progress vector metric cardinality/binding drift")
        if any(
            result.favorable_delta != delta
            for result, (_metric_id, delta) in zip(
                self.metric_results, self.deltas, strict=False
            )
        ):
            raise ValidationFailed("Progress vector result/delta drift")
        if self.value_per_cost is not None and not math.isfinite(self.value_per_cost):
            raise ValidationFailed("Progress value-per-cost sonlu olmali")
        for value in self.evidence_digests:
            parse_digest(value)
        if self.evidence_digests != tuple(sorted(set(self.evidence_digests))):
            raise ValidationFailed("Progress evidence digest listesi kanonik olmali")
        if self.progress_state is ProgressState.INVALID and not self.invalid_reasons:
            raise ValidationFailed("Invalid progress gerekce ister")
        if self.progress_state is not ProgressState.INVALID and self.invalid_reasons:
            raise ValidationFailed("Gecerli progress invalid reason tasiyamaz")
        if self.progress_state is not ProgressState.INVALID and not self.evidence_digests:
            raise ValidationFailed("Gecerli progress external evidence ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_values": dict(self.baseline_values),
            "previous_values": dict(self.previous_values),
            "current_values": dict(self.current_values),
            "deltas": dict(self.deltas),
            "hard_guard_results": [
                item.as_dict() for item in self.metric_results if item.role is MetricRole.HARD_GUARD
            ],
            "primary_progress_results": [
                item.as_dict() for item in self.metric_results if item.role is MetricRole.PRIMARY
            ],
            "target_results": [item.as_dict() for item in self.metric_results],
            "evidence_digests": list(self.evidence_digests),
            "value_per_cost": self.value_per_cost,
            "progress_state": str(self.progress_state),
            "invalid_reasons": list(self.invalid_reasons),
        }

    @property
    def progress_digest(self) -> str:
        return digest(self.as_dict())


def _distance_to_target(spec: MetricSpec, value: float) -> float:
    if spec.direction is MetricDirection.TARGET:
        assert spec.target_value is not None
        return abs(value - spec.target_value)
    if spec.direction is MetricDirection.RANGE:
        assert spec.min_value is not None and spec.max_value is not None
        if value < spec.min_value:
            return spec.min_value - value
        if value > spec.max_value:
            return value - spec.max_value
        return 0.0
    raise AssertionError("distance only applies to target/range")


def _favorable_delta(spec: MetricSpec, previous: float, current: float) -> float:
    if spec.direction is MetricDirection.MAXIMIZE:
        return current - previous
    if spec.direction is MetricDirection.MINIMIZE:
        return previous - current
    return _distance_to_target(spec, previous) - _distance_to_target(spec, current)


def _target_reached(spec: MetricSpec, value: float) -> bool:
    if spec.direction is MetricDirection.MAXIMIZE:
        assert spec.target_value is not None
        return value >= spec.target_value
    if spec.direction is MetricDirection.MINIMIZE:
        assert spec.target_value is not None
        return value <= spec.target_value
    return _distance_to_target(spec, value) <= spec.regression_tolerance


def _evidence_values(
    specs: tuple[MetricSpec, ...], evidence: tuple[MeasurementEvidence, ...], label: str
) -> tuple[dict[str, float], tuple[str, ...]]:
    expected = {item.metric_id for item in specs}
    observed = [item.metric_id for item in evidence]
    if len(observed) != len(set(observed)):
        return {}, (f"{label}:duplicate-metric",)
    missing = expected - set(observed)
    unexpected = set(observed) - expected
    reasons: list[str] = []
    if missing:
        reasons.append(f"{label}:missing-metric")
    if unexpected:
        reasons.append(f"{label}:unexpected-metric")
    if any(item.producer_self_report for item in evidence):
        reasons.append(f"{label}:producer-self-report")
    revisions = {item.source_revision for item in evidence}
    if len(revisions) != 1:
        reasons.append(f"{label}:source-revision-drift")
    return {item.metric_id: item.value for item in evidence}, tuple(sorted(reasons))


def evaluate_progress(
    specs: tuple[MetricSpec, ...],
    baseline: tuple[MeasurementEvidence, ...],
    previous: tuple[MeasurementEvidence, ...],
    current: tuple[MeasurementEvidence, ...],
    *,
    cost_micros: int = 0,
) -> ProgressVector:
    """Evaluate an external directional metric vector; self-report never counts."""

    metric_ids = tuple(item.metric_id for item in specs)
    if not metric_ids or metric_ids != tuple(sorted(set(metric_ids))):
        raise ValidationFailed("Progress metric specs dolu, tekil ve kanonik olmali")
    if cost_micros < 0:
        raise ValidationFailed("Progress cost negatif olamaz")
    baseline_values, baseline_errors = _evidence_values(specs, baseline, "baseline")
    previous_values, previous_errors = _evidence_values(specs, previous, "previous")
    current_values, current_errors = _evidence_values(specs, current, "current")
    errors = tuple(sorted((*baseline_errors, *previous_errors, *current_errors)))
    evidence_digests = tuple(
        sorted({item.evidence_digest for rows in (baseline, previous, current) for item in rows})
    )
    if errors:
        return ProgressVector(
            (), (), (), (), (), evidence_digests, None, ProgressState.INVALID, errors
        )

    results: list[MetricProgressResult] = []
    deltas: list[tuple[str, float]] = []
    for spec in specs:
        favorable = _favorable_delta(
            spec, previous_values[spec.metric_id], current_values[spec.metric_id]
        )
        deltas.append((spec.metric_id, favorable))
        results.append(
            MetricProgressResult(
                spec.metric_id,
                spec.role,
                favorable,
                favorable >= spec.minimum_meaningful_delta
                and favorable > spec.regression_tolerance,
                favorable < -spec.regression_tolerance,
                _target_reached(spec, current_values[spec.metric_id]),
            )
        )

    hard_guard_regression = any(
        item.regressed for item in results if item.role is MetricRole.HARD_GUARD
    )
    primary = tuple(item for item in results if item.role is MetricRole.PRIMARY)
    primary_regression = any(item.regressed for item in primary)
    if hard_guard_regression or primary_regression:
        state = ProgressState.REGRESSED
    elif primary and all(item.target_reached for item in primary):
        state = ProgressState.TARGET_REACHED
    elif any(item.meaningful_progress for item in primary):
        state = ProgressState.IMPROVED
    else:
        state = ProgressState.PLATEAU
    positive_primary = sum(max(item.favorable_delta, 0.0) for item in primary)
    value_per_cost = positive_primary / cost_micros if cost_micros else None
    return ProgressVector(
        tuple(sorted(baseline_values.items())),
        tuple(sorted(previous_values.items())),
        tuple(sorted(current_values.items())),
        tuple(deltas),
        tuple(results),
        evidence_digests,
        value_per_cost,
        state,
    )
