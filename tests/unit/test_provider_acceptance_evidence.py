from __future__ import annotations

from dataclasses import dataclass

import pytest

from zekam.application.provider_acceptance_evidence import (
    _validated_executed_call_evidence,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


@dataclass(frozen=True)
class _Kind:
    value: str


@dataclass(frozen=True)
class _Call:
    kind: _Kind
    canonical_model_id: str


def test_health_gated_missing_benchmark_reconstructs_outcome_skip_evidence() -> None:
    executed = {"health-model-a": "sha256:" + "1" * 64}
    result = _validated_executed_call_evidence(
        expected_calls={
            "health-model-a": _Call(_Kind("health"), "model-a"),
            "benchmark-model-a-1": _Call(_Kind("benchmark"), "model-a"),
        },
        executed_evidence=executed,
        health_status_by_model={"model-a": "failed"},
    )

    assert executed == {"health-model-a": "sha256:" + "1" * 64}
    assert result == {
        "health-model-a": "sha256:" + "1" * 64,
        "benchmark-model-a-1": digest(
            {
                "status": "not-run-health-failed",
                "model_id": "model-a",
                "call_id": "benchmark-model-a-1",
            }
        ),
    }


def test_missing_call_without_failed_health_is_rejected() -> None:
    with pytest.raises(PolicyViolation, match="expected executed call evidence missing"):
        _validated_executed_call_evidence(
            expected_calls={
                "health-model-a": _Call(_Kind("health"), "model-a"),
                "benchmark-model-a-1": _Call(_Kind("benchmark"), "model-a"),
            },
            executed_evidence={"health-model-a": "sha256:" + "1" * 64},
            health_status_by_model={"model-a": "passed"},
        )
