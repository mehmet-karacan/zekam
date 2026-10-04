from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from zekam.application.unit_test_measurement import RunStatus
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionClass,
    PlanStatus,
    build_unit_test_plan,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "unit_test_engineering_java"


def test_real_maven_fixture_is_ready_or_explicitly_not_supported() -> None:
    result = build_unit_test_plan(FIXTURE, target_modules=())
    if shutil.which("mvn") is None:
        assert result.status is PlanStatus.TOOL_MISSING
        assert any("Maven" in reason or "mvn" in reason for reason in result.setup_plan)
        return
    assert result.status is PlanStatus.READY, result.reasons
    assert result.plan is not None
    assert result.discovery is not None
    assert any(module.engine.value == "junit-platform" for module in result.discovery.modules)
    assert result.plan.goals == ("test", "jacoco:report")
    assert result.plan.execution_class is ExecutionClass.PLUGIN_EXECUTION


@pytest.mark.skipif(shutil.which("mvn") is None, reason="Maven bu cihazda kurulu degil")
def test_real_maven_fixture_executes_unit_and_jacoco_path(tmp_path: Path) -> None:
    """Gercek Maven varsa process/rapor yolunu kosar; yoksa yukaridaki test acik blocker verir."""

    from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
    from zekam.infrastructure.unit_test_runner.maven_plan import (
        ExecutionAuthorization,
        discover_project,
    )
    from zekam.infrastructure.unit_test_runner.measurement import measure_unit_tests

    planned = build_unit_test_plan(FIXTURE, target_modules=("",))
    assert planned.plan is not None
    request = UnitTestRequest.with_defaults(
        project_id="fixture-project",
        source_binding_id="fixture-binding",
        source_revision="fixture-revision",
        source_files=["src/main/java/com/zekam/fixture/Calculator.java"],
        percent="50",
        budget=UnitTestBudget(1, 120, 300),
    )
    measured = measure_unit_tests(
        request,
        planned,
        ExecutionAuthorization(
            planned.plan.plan_digest,
            planned.plan.execution_class,
            allow_network=True,
        ),
        discover_project(FIXTURE),
        lock_dir=tmp_path / ".zekam-locks",
        run_id="fixture-run",
        attempt_id="fixture-attempt",
        test_candidate_digest=request.request_digest,
        timeout_seconds=120,
    )
    assert measured.status.value in {"accepted", "below-target"}, measured
    assert measured.binding is not None
    assert measured.evidence.run_status is RunStatus.COMPLETED
    assert measured.evidence.exit_code == 0
    assert measured.evidence.test_counts
    assert measured.evidence.observations
    assert all(
        observation.covered is not None and observation.total is not None
        for observation in measured.evidence.observations
    )
    assert measured.record_digest.startswith("sha256:")
