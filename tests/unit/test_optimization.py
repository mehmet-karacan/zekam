"""Generic optimization objective, evidence and directional vector tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
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

NOW = dt.datetime(2026, 8, 29, tzinfo=dt.UTC)


def _spec(
    metric_id: str,
    direction: MetricDirection,
    role: MetricRole = MetricRole.PRIMARY,
    **changes: object,
) -> MetricSpec:
    values: dict[str, object] = {
        "metric_id": metric_id,
        "name": metric_id,
        "unit": "point",
        "direction": direction,
        "role": role,
        "source_kind": "independent-test",
        "target_value": 1.0 if direction is not MetricDirection.RANGE else None,
        "min_value": 0.4 if direction is MetricDirection.RANGE else None,
        "max_value": 0.6 if direction is MetricDirection.RANGE else None,
        "minimum_meaningful_delta": 0.01,
        "regression_tolerance": 0.001,
    }
    values.update(changes)
    return MetricSpec(**values)  # type: ignore[arg-type]


def _evidence(metric_id: str, value: float, *, suffix: str, self_report: bool = False):  # type: ignore[no-untyped-def]
    return MeasurementEvidence(
        metric_id,
        value,
        f"test:{metric_id}:{suffix}",
        digest({"metric": metric_id, "suffix": suffix, "value": value}),
        "source-7",
        NOW,
        "external-measurer",
        "independent-verifier",
        self_report,
    )


def test_objective_is_stable_canonical_and_authority_free() -> None:
    specs = (
        _spec("guard", MetricDirection.MINIMIZE, MetricRole.HARD_GUARD),
        _spec("quality", MetricDirection.MAXIMIZE),
    )
    objective = OptimizationObjective(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "build",
        "source:artifact",
        digest("baseline"),
        digest("measurement-plan"),
        digest("validator-assets"),
        specs,
        3,
        1000,
        50_000,
        NOW + dt.timedelta(minutes=10),
        "loop-owned-inverse-patch",
        NOW,
    )
    assert objective.objective_digest == replace(objective).objective_digest
    assert objective.as_dict()["grants_authority"] is False
    with pytest.raises(PolicyViolation, match="authority"):
        replace(objective, grants_authority=True)
    with pytest.raises(ValidationFailed, match="kanonik"):
        replace(objective, metric_specs=tuple(reversed(specs)))


def test_maximize_minimize_target_and_range_have_directional_deltas() -> None:
    specs = (
        _spec("latency", MetricDirection.MINIMIZE, MetricRole.SECONDARY),
        _spec("quality", MetricDirection.MAXIMIZE),
        _spec("range", MetricDirection.RANGE, MetricRole.SECONDARY),
        _spec("target", MetricDirection.TARGET, MetricRole.SECONDARY),
    )
    previous = (
        _evidence("latency", 1.0, suffix="previous"),
        _evidence("quality", 0.0, suffix="previous"),
        _evidence("range", 0.8, suffix="previous"),
        _evidence("target", 0.5, suffix="previous"),
    )
    current = (
        _evidence("latency", 0.7, suffix="current"),
        _evidence("quality", 1.0, suffix="current"),
        _evidence("range", 0.5, suffix="current"),
        _evidence("target", 0.9, suffix="current"),
    )
    vector = evaluate_progress(specs, previous, previous, current, cost_micros=100)
    assert vector.progress_state is ProgressState.TARGET_REACHED
    assert dict(vector.deltas) == pytest.approx(
        {"latency": 0.3, "quality": 1.0, "range": 0.2, "target": 0.4}
    )
    assert vector.value_per_cost == pytest.approx(0.01)


def test_hard_guard_regression_rejects_even_when_primary_scalar_improves() -> None:
    specs = (
        _spec("guard", MetricDirection.MINIMIZE, MetricRole.HARD_GUARD),
        _spec("quality", MetricDirection.MAXIMIZE),
    )
    previous = (
        _evidence("guard", 0.1, suffix="previous"),
        _evidence("quality", 0.1, suffix="previous"),
    )
    current = (
        _evidence("guard", 0.9, suffix="current"),
        _evidence("quality", 1.0, suffix="current"),
    )
    vector = evaluate_progress(specs, previous, previous, current)
    assert vector.progress_state is ProgressState.REGRESSED
    assert any(
        item.regressed for item in vector.metric_results if item.role is MetricRole.HARD_GUARD
    )


def test_self_report_and_missing_measurement_are_invalid_not_progress() -> None:
    specs = (_spec("quality", MetricDirection.MAXIMIZE),)
    valid = (_evidence("quality", 0.0, suffix="baseline"),)
    self_report = (_evidence("quality", 1.0, suffix="current", self_report=True),)
    assert (
        evaluate_progress(specs, valid, valid, self_report).progress_state is ProgressState.INVALID
    )
    assert evaluate_progress(specs, valid, valid, ()).progress_state is ProgressState.INVALID


def test_nonfinite_and_malformed_metric_specs_fail_closed() -> None:
    with pytest.raises(ValidationFailed, match="sonlu"):
        _evidence("quality", float("nan"), suffix="bad")
    with pytest.raises(ValidationFailed, match="target_value"):
        _spec("quality", MetricDirection.MAXIMIZE, target_value=None)
    with pytest.raises(ValidationFailed, match="min_value/max_value"):
        _spec("range", MetricDirection.RANGE, min_value=2.0, max_value=1.0)


def test_validator_asset_manifest_is_immutable_and_outside_builder_write_scope() -> None:
    builder_id, verifier_id = uuid4(), uuid4()
    manifest = ValidatorAssetManifest(
        manifest_id=uuid4(),
        objective_id=uuid4(),
        validator_spec_digest=digest("validator"),
        source_revision="git:abc",
        builder_assignment_id=builder_id,
        verifier_assignment_id=verifier_id,
        assets=(
            ValidatorAsset(
                "fixture",
                "validator/fixtures/golden.json",
                digest("fixture"),
                ValidatorAssetRole.FIXTURE,
            ),
            ValidatorAsset(
                "threshold",
                "validator/thresholds/quality.json",
                digest("threshold"),
                ValidatorAssetRole.THRESHOLD,
            ),
        ),
        created_at=NOW,
    )

    assert manifest.manifest_digest.startswith("sha256:")
    manifest.assert_builder_write_scope(("src/feature.py",))
    with pytest.raises(PolicyViolation, match="write scope"):
        manifest.assert_builder_write_scope(("validator/thresholds/quality.json",))
    with pytest.raises(PolicyViolation, match="kendi validator"):
        replace(manifest, verifier_assignment_id=builder_id)
