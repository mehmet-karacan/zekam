"""Model baglam admission sozlesmesi (RAG26-R08, T25)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.model_context_admission import (
    ModelLimits,
    admit_model_request,
    model_limits,
    models_missing_limits,
)
from zekam.domain.errors import ValidationFailed

_CONFIG = {
    "provider": {
        "litellm": {
            "models": {
                "openai/codepilot-qwen3": {
                    "name": "q3",
                    "limit": {"context": 32768, "output": 4096},
                },
                "no-limit-model": {"name": "x"},
                "half-limit": {"name": "y", "limit": {"context": 8000}},
            }
        }
    }
}


@pytest.mark.unit
def test_reported_overflow_case_is_blocked_before_provider_call() -> None:
    limits = ModelLimits(context=32768, output=32768)
    admission = admit_model_request(limits, input_tokens=769, output_reserve=32000)
    assert admission.allowed is False
    assert admission.reason == "context-overflow"


@pytest.mark.unit
def test_fitting_request_is_admitted_with_safety_margin() -> None:
    admission = admit_model_request(
        ModelLimits(context=32768, output=4096), input_tokens=12000, output_reserve=1800
    )
    assert admission.allowed is True
    assert admission.safety_margin >= 512


@pytest.mark.unit
def test_unknown_limits_fail_closed() -> None:
    admission = admit_model_request(None, input_tokens=10, output_reserve=10)
    assert admission.allowed is False
    assert admission.reason == "unknown-context-limit"


@pytest.mark.unit
def test_output_reserve_above_model_output_is_denied() -> None:
    admission = admit_model_request(
        ModelLimits(context=32768, output=1000), input_tokens=100, output_reserve=2000
    )
    assert admission.reason == "output-reserve-exceeds-model-output"


@pytest.mark.unit
def test_exact_boundary_is_denied_by_the_safety_margin() -> None:
    limits = ModelLimits(context=10000, output=10000)
    assert admit_model_request(limits, input_tokens=4000, output_reserve=5400).allowed is True
    assert admit_model_request(limits, input_tokens=4100, output_reserve=5400).allowed is False


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_inputs_are_rejected(bad: int) -> None:
    with pytest.raises(ValidationFailed):
        admit_model_request(ModelLimits(1000, 1000), input_tokens=bad, output_reserve=10)


@pytest.mark.unit
def test_limits_are_read_from_exact_config_never_guessed_from_the_name() -> None:
    assert model_limits(_CONFIG, "litellm", "openai/codepilot-qwen3") == ModelLimits(32768, 4096)
    assert model_limits(_CONFIG, "litellm", "no-limit-model") is None
    assert model_limits(_CONFIG, "litellm", "half-limit") is None
    assert model_limits(_CONFIG, "litellm", "codepilot-qwen3") is None
    assert model_limits(_CONFIG, "other", "openai/codepilot-qwen3") is None


@pytest.mark.unit
def test_models_missing_limits_reports_only_undeclared_models() -> None:
    assert models_missing_limits(_CONFIG) == (
        ("litellm", "half-limit"),
        ("litellm", "no-limit-model"),
    )


@pytest.mark.unit
def test_doctor_check_reports_missing_limits_without_network(tmp_path: Path) -> None:
    from zekam.application.diagnostics import CheckStatus
    from zekam.infrastructure.doctor.runtime_checks import OpenCodeModelLimitCheck

    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(_CONFIG), encoding="utf-8")
    result = OpenCodeModelLimitCheck(config_path=path).run()
    assert result.status is CheckStatus.DEGRADED
    assert result.evidence["provider_calls"] == 0
    assert result.evidence["network_calls"] == 0
    assert result.evidence["missing_limit_models"] == [
        "litellm/half-limit",
        "litellm/no-limit-model",
    ]

    complete = {"provider": {"p": {"models": {"m": {"limit": {"context": 8000, "output": 1000}}}}}}
    path.write_text(json.dumps(complete), encoding="utf-8")
    assert OpenCodeModelLimitCheck(config_path=path).run().status is CheckStatus.PASSED


@pytest.mark.unit
def test_doctor_check_is_skipped_when_config_is_unreadable(tmp_path: Path) -> None:
    from zekam.application.diagnostics import CheckStatus
    from zekam.infrastructure.doctor.runtime_checks import OpenCodeModelLimitCheck

    result = OpenCodeModelLimitCheck(config_path=tmp_path / "missing.json").run()
    assert result.status is CheckStatus.SKIPPED
