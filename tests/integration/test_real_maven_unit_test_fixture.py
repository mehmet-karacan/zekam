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


def _write_multi_module_fixture(root: Path) -> None:
    """Create a small parent/util/service reactor entirely inside tmp_path."""

    (root / "util/src/main/java/com/zekam/fixture/util").mkdir(parents=True)
    (root / "util/src/test/java/com/zekam/fixture/util").mkdir(parents=True)
    (root / "service/src/main/java/com/zekam/fixture/service").mkdir(parents=True)
    (root / "service/src/test/java/com/zekam/fixture/service").mkdir(parents=True)
    (root / "pom.xml").write_text(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<project xmlns=\"http://maven.apache.org/POM/4.0.0\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
         xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.zekam.fixture.multi</groupId>
  <artifactId>unit-test-engineering-reactor</artifactId>
  <version>1.0-SNAPSHOT</version>
  <packaging>pom</packaging>
  <modules><module>util</module><module>service</module></modules>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <junit.version>5.10.2</junit.version>
    <surefire.version>3.2.5</surefire.version>
    <jacoco.version>0.8.12</jacoco.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
      <version>${junit.version}</version><scope>test</scope>
    </dependency>
  </dependencies>
  <build><plugins>
    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>${surefire.version}</version></plugin>
    <plugin><groupId>org.jacoco</groupId><artifactId>jacoco-maven-plugin</artifactId><version>${jacoco.version}</version>
      <executions>
        <execution><id>prepare-agent</id><goals><goal>prepare-agent</goal></goals></execution>
        <execution><id>report</id><phase>verify</phase><goals><goal>report</goal></goals></execution>
      </executions>
    </plugin>
  </plugins></build>
</project>
""",
        encoding="utf-8",
    )
    for module, artifact in (("util", "fixture-util"), ("service", "fixture-service")):
        dependency = (
            "<dependencies><dependency><groupId>com.zekam.fixture.multi</groupId>"
            "<artifactId>fixture-util</artifactId><version>1.0-SNAPSHOT</version></dependency>"
            "</dependencies>"
            if module == "service"
            else ""
        )
        (root / module / "pom.xml").write_text(
            f"""<project xmlns=\"http://maven.apache.org/POM/4.0.0\">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.zekam.fixture.multi</groupId><artifactId>unit-test-engineering-reactor</artifactId>
    <version>1.0-SNAPSHOT</version><relativePath>../pom.xml</relativePath></parent>
  <artifactId>{artifact}</artifactId>
  {dependency}
</project>
""",
            encoding="utf-8",
        )
    (root / "util/src/main/java/com/zekam/fixture/util/Numbers.java").write_text(
        "package com.zekam.fixture.util;\npublic final class Numbers {\n  private Numbers() {}\n  public static int add(int a, int b) { return a + b; }\n}\n",
        encoding="utf-8",
    )
    (root / "util/src/test/java/com/zekam/fixture/util/NumbersTest.java").write_text(
        "package com.zekam.fixture.util;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\nimport org.junit.jupiter.api.Test;\nclass NumbersTest { @Test void adds() { assertEquals(5, Numbers.add(2, 3)); } }\n",
        encoding="utf-8",
    )
    (root / "service/src/main/java/com/zekam/fixture/service/Calculator.java").write_text(
        "package com.zekam.fixture.service;\nimport com.zekam.fixture.util.Numbers;\npublic final class Calculator { private Calculator() {} public static int total(int a, int b) { return Numbers.add(a, b); } }\n",
        encoding="utf-8",
    )
    (root / "service/src/test/java/com/zekam/fixture/service/CalculatorTest.java").write_text(
        "package com.zekam.fixture.service;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\nimport org.junit.jupiter.api.Test;\nclass CalculatorTest { @Test void totals() { assertEquals(9, Calculator.total(4, 5)); } }\n",
        encoding="utf-8",
    )


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

    fixture = tmp_path / "java-fixture"
    shutil.copytree(FIXTURE, fixture, ignore=shutil.ignore_patterns("target"))
    planned = build_unit_test_plan(fixture, target_modules=("",))
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
        discover_project(fixture),
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


@pytest.mark.skipif(shutil.which("mvn") is None, reason="Maven bu cihazda kurulu degil")
def test_real_maven_multimodule_reactor_binds_each_module_report(tmp_path: Path) -> None:
    """A real reactor run must select the service with ``-pl ... -am`` and bind both reports."""

    from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
    from zekam.infrastructure.unit_test_runner.maven_plan import (
        ExecutionAuthorization,
        discover_project,
    )
    from zekam.infrastructure.unit_test_runner.measurement import measure_unit_tests

    root = tmp_path / "reactor"
    root.mkdir()
    _write_multi_module_fixture(root)
    planned = build_unit_test_plan(root, target_modules=("service",))
    assert planned.status is PlanStatus.READY, planned.reasons
    assert planned.plan is not None
    assert planned.plan.target_modules == ("service",)
    assert "-pl" in planned.plan.args and "service" in planned.plan.args
    assert "-am" in planned.plan.args
    assert planned.discovery is not None
    assert planned.discovery.module_dirs == ("", "util", "service")

    request = UnitTestRequest.with_defaults(
        project_id="fixture-reactor",
        source_binding_id="fixture-binding",
        source_revision="fixture-reactor-revision",
        source_files=[
            "util/src/main/java/com/zekam/fixture/util/Numbers.java",
            "service/src/main/java/com/zekam/fixture/service/Calculator.java",
        ],
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
        discover_project(root),
        lock_dir=tmp_path / ".zekam-locks",
        run_id="fixture-reactor-run",
        attempt_id="fixture-reactor-attempt",
        test_candidate_digest=request.request_digest,
        timeout_seconds=120,
    )
    assert measured.status.value in {"accepted", "below-target"}, measured
    assert measured.binding is not None
    report_labels = {label for label, _digest in measured.binding.report_digests}
    assert {"jacoco-xml:util", "jacoco-xml:service"} <= report_labels
    assert {"jacoco-exec:util", "jacoco-exec:service"} <= report_labels
    assert measured.evidence.run_status is RunStatus.COMPLETED
    assert measured.evidence.exit_code == 0
    assert measured.evidence.test_counts
    line_observations = tuple(
        observation
        for observation in measured.evidence.observations
        if observation.metric.value == "line"
    )
    assert line_observations
    assert all(
        observation.covered is not None and observation.total is not None
        for observation in line_observations
    )
    assert any(
        observation.metric.value == "branch" and observation.state.value == "not-applicable"
        for observation in measured.evidence.observations
    )
