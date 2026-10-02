"""W04: taze rapor/kaynak baglama ve kabul karari (FAKE mvn + replay rapor fixture'lari).

Gercek Maven/JaCoCo kosusu yoktur; raporlari FakeMaven yazar. Bu testler kabul/red
mantigini ve baglama kontratini dogrular, gercek arac davranisini degil.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_runner_support import (
    GOOD_JACOCO,
    LINES_3_1,
    FakeMaven,
    counter,
    good_scenario,
    jacoco_xml,
    make_project,
    surefire_xml,
    write,
)

from zekam.application.unit_test_measurement import (
    CurrentState,
    FreshnessVerdict,
    MeasurementRecord,
    MeasurementScope,
    MeasurementStatus,
    RunStatus,
    TestRunKind,
    assemble_measurement,
    check_freshness,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoverageState,
    EvaluationVerdict,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
)
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    PlanResult,
    PlanStatus,
    build_unit_test_plan,
)
from zekam.infrastructure.unit_test_runner.measurement import (
    PlanBlocked,
    digest_files,
    measure_unit_tests,
)
from zekam.infrastructure.unit_test_runner.pom_inspect import discover_project

TARGET = "src/main/java/p/A.java"
CANDIDATE_1 = digest_of_bytes(b"candidate-1")
CANDIDATE_2 = digest_of_bytes(b"candidate-2")


def request(percent: str = "70") -> UnitTestRequest:
    return UnitTestRequest.with_defaults(
        project_id="proj1",
        source_binding_id="bind1",
        source_revision="rev-1",
        source_files=[TARGET],
        percent=percent,
        budget=UnitTestBudget(3, 60, 600),
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fake = FakeMaven(tmp_path / "fake")
        self.root = make_project(tmp_path / "proj")
        plan = build_unit_test_plan(
            self.root, target_modules=("",), search_path=self.fake.search_path
        )
        assert plan.plan is not None
        self.plan_result = plan
        self.auth = ExecutionAuthorization(
            plan.plan.plan_digest, plan.plan.execution_class, allow_network=True
        )

    def measure(
        self,
        req: UnitTestRequest | None = None,
        *,
        run_id: str = "run-1",
        candidate: str = CANDIDATE_1,
        timeout: float = 30,
        cancel: threading.Event | None = None,
        plan_result: PlanResult | None = None,
    ) -> MeasurementRecord | PlanBlocked:
        return measure_unit_tests(
            req or request(),
            plan_result or self.plan_result,
            self.auth,
            discover_project(self.root),
            lock_dir=self.tmp / "locks",
            run_id=run_id,
            attempt_id="attempt-1",
            test_candidate_digest=candidate,
            timeout_seconds=timeout,
            cancel=cancel,
            maven_version="3.9.9",
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def accepted(env: Env, **scenario: Any) -> MeasurementRecord:
    env.fake.set(**good_scenario(**scenario))
    record = env.measure()
    assert isinstance(record, MeasurementRecord)
    return record


def test_replay_accepted_measurement_is_fully_bound(env: Env) -> None:
    record = accepted(env)
    assert record.status is MeasurementStatus.ACCEPTED and record.stop_reason is None
    assert record.evaluation is not None and record.evaluation.verdict is EvaluationVerdict.MET
    assert (record.evaluation.aggregate_covered, record.evaluation.aggregate_total) == (3, 4)
    binding = record.binding
    assert binding is not None and binding.scope is MeasurementScope.UNIT
    assert binding.source_revision == "rev-1" and binding.run_id == "run-1"
    assert binding.attempt_id == "attempt-1" and binding.test_candidate_digest == CANDIDATE_1
    assert dict(binding.production_digests) == digest_files(env.root, (TARGET,))
    assert set(dict(binding.class_digests)) == {":p/A.class", ":p/A$Inner.class"}
    kinds = {key.split(":")[0] for key, _ in binding.report_digests}
    assert kinds == {"process-output", "surefire", "jacoco-xml", "jacoco-exec"}
    toolchain = binding.toolchain
    assert toolchain.test_jvm_version == "21.0.1" and toolchain.compile_release == "1.8"
    assert toolchain.maven_version == "3.9.9"
    assert dict(toolchain.plugin_versions) == {"surefire": "3.2.5", "jacoco": "0.8.12"}
    assert dict(record.evidence.test_counts)["executed"] == 2
    assert record.record_digest.startswith("sha256:")


def test_replay_threshold_not_met_is_a_verdict_not_a_pass(env: Env) -> None:
    env.fake.set(**good_scenario())
    record = env.measure(request("90"))
    assert isinstance(record, MeasurementRecord)
    assert record.status is MeasurementStatus.ACCEPTED
    assert record.evaluation is not None and record.evaluation.verdict is EvaluationVerdict.NOT_MET


def test_replay_old_exec_and_reports_cannot_leak_into_new_run_m07(env: Env) -> None:
    # Onceki kosudan %100 gorunen eski rapor/exec; yeni kosu hicbir rapor yazmaz.
    stale_lines = '<line nr="1" mi="0" ci="5" mb="0" cb="0"/>'
    write(
        env.root / "target/site/jacoco/jacoco.xml",
        jacoco_xml(lines=stale_lines, line_counter=counter("LINE", 1, 0)),
    )
    write(
        env.root / "target/surefire-reports/TEST-p.ATest.xml", surefire_xml([("p.ATest", "a", "")])
    )
    (env.root / "target/jacoco.exec").write_bytes(b"old-exec")
    env.fake.set(print=[])
    record = env.measure()
    assert isinstance(record, MeasurementRecord)
    assert record.status is MeasurementStatus.NO_TESTS_BASELINE  # PASS degil, eski veri okunmadi
    assert record.evaluation is None
    assert {o.state for o in record.evidence.observations} == {CoverageState.MISSING_REPORT}
    assert list((env.root / "target").glob("jacoco.exec.stale-*"))  # silinmedi, karantinada


def test_replay_fresh_xml_without_fresh_exec_is_unbound_m07(env: Env) -> None:
    scenario = good_scenario()
    scenario["write_bytes"] = {k: v for k, v in scenario["write_bytes"].items() if "exec" not in k}
    env.fake.set(**scenario)
    record = env.measure()
    assert isinstance(record, MeasurementRecord)
    assert record.status is MeasurementStatus.UNBOUND_OR_STALE
    assert any("exec yok" in b for b in record.blockers)


def test_replay_integration_artifacts_or_tests_are_rejected_m08(env: Env) -> None:
    record = accepted(
        env, write={**good_scenario()["write"], "target/failsafe-reports/TEST-IT.xml": "<x/>"}
    )
    assert record.status is MeasurementStatus.INTEGRATION_MIX and record.evaluation is None
    it_cases = surefire_xml([("p.OrderIT", "a", ""), ("p.ATest", "b", "")])
    env2_write = {**good_scenario()["write"], "target/surefire-reports/TEST-p.ATest.xml": it_cases}
    record = accepted(env, write=env2_write)
    assert record.status is MeasurementStatus.INTEGRATION_MIX
    assert record.evidence.test_kind is TestRunKind.INTEGRATION_TESTS_PRESENT


def test_replay_unexpected_or_missing_plugin_executions_block_acceptance(env: Env) -> None:
    lines = [
        *good_scenario()["print"],
        "[INFO] --- maven-failsafe-plugin:3.2.5:integration-test (x) @ app ---",
    ]
    record = accepted(env, print=lines)
    assert record.status is MeasurementStatus.MEASUREMENT_INCOMPLETE
    assert any("failsafe:integration-test" in b for b in record.blockers)
    record = accepted(env, print=good_scenario()["print"][:1])  # jacoco:report hic kosmadi
    assert record.status is MeasurementStatus.MEASUREMENT_INCOMPLETE
    assert any("eksik plugin" in b for b in record.blockers)


@pytest.mark.parametrize(
    ("cases", "status", "kind"),
    [
        ([], MeasurementStatus.NO_TESTS_BASELINE, TestRunKind.NO_TESTS),
        (
            [("p.ATest", "a", "<skipped/>")],
            MeasurementStatus.TESTS_NOT_ACCEPTABLE,
            TestRunKind.ALL_SKIPPED,
        ),
        ([("p.ATest", "a", "<failure/>")], MeasurementStatus.TESTS_FAILED, TestRunKind.FAILED),
    ],
)
def test_replay_zero_skipped_or_failed_tests_are_not_acceptance_u06(
    env: Env, cases: list[tuple[str, str, str]], status: MeasurementStatus, kind: TestRunKind
) -> None:
    write_map = {
        **good_scenario()["write"],
        "target/surefire-reports/TEST-p.ATest.xml": surefire_xml(cases),
    }
    record = accepted(env, write=write_map, exit=1 if kind is TestRunKind.FAILED else 0)
    assert record.status is status and record.evidence.test_kind is kind
    assert record.evaluation is None  # coverage kabul edilmez/uydurulmaz
    if status is MeasurementStatus.NO_TESTS_BASELINE:
        assert record.stop_reason is None  # baseline, PASS degil


def test_replay_exit_zero_with_no_reports_is_not_success(env: Env) -> None:
    env.fake.set(exit=0)
    record = env.measure()
    assert isinstance(record, MeasurementRecord)
    assert record.status is MeasurementStatus.NO_TESTS_BASELINE and record.evaluation is None


def test_replay_nonzero_exit_with_good_reports_is_incomplete(env: Env) -> None:
    record = accepted(env, exit=1)
    assert record.status is MeasurementStatus.MEASUREMENT_INCOMPLETE
    assert any("exit code 1" in b for b in record.blockers)


def test_replay_missing_or_broken_or_unmappable_jacoco_is_incomplete_u07(env: Env) -> None:
    only_tests = good_scenario()
    only_tests["write"].pop("target/site/jacoco/jacoco.xml")
    record = accepted(env, **only_tests)
    assert record.status is MeasurementStatus.MEASUREMENT_INCOMPLETE
    assert {o.state for o in record.evidence.observations} == {CoverageState.MISSING_REPORT}
    broken = good_scenario()
    broken["write"]["target/site/jacoco/jacoco.xml"] = "<report><package"
    record = accepted(env, **broken)
    states = {o.state for o in record.evidence.observations}
    assert states == {CoverageState.MAPPING_ERROR} and record.evaluation is not None
    assert record.evaluation.verdict is EvaluationVerdict.INCONCLUSIVE
    nodebug = good_scenario()
    nodebug["write"]["target/site/jacoco/jacoco.xml"] = jacoco_xml(lines="", line_counter="")
    record = accepted(env, **nodebug)
    assert {o.state for o in record.evidence.observations} == {CoverageState.MAPPING_ERROR}
    assert record.status is MeasurementStatus.MEASUREMENT_INCOMPLETE
    assert record.stop_reason is UnitTestStopReason.MEASUREMENT_INCOMPLETE


def test_replay_branch_metric_is_separate_and_not_applicable_without_branches(env: Env) -> None:
    record = accepted(env)
    branch = [o for o in record.evidence.observations if o.metric is CoverageMetric.BRANCH]
    assert [o.state for o in branch] == [CoverageState.NOT_APPLICABLE]


def test_replay_timeout_cancel_and_tool_missing_do_not_invent_measurements(
    env: Env, tmp_path: Path
) -> None:
    env.fake.set(**good_scenario(sleep=30))
    timed = env.measure(timeout=1.5)
    assert isinstance(timed, MeasurementRecord)
    assert timed.status is MeasurementStatus.RUN_INCOMPLETE
    assert (
        timed.stop_reason is UnitTestStopReason.MEASUREMENT_INCOMPLETE and timed.evaluation is None
    )
    cancel = threading.Event()
    threading.Timer(0.8, cancel.set).start()
    cancelled = env.measure(timeout=30, cancel=cancel)
    assert isinstance(cancelled, MeasurementRecord)
    assert cancelled.stop_reason is UnitTestStopReason.USER_CANCELLED
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = build_unit_test_plan(env.root, target_modules=("",), search_path=str(empty))
    assert missing.status is PlanStatus.TOOL_MISSING
    blocked = env.measure(plan_result=missing)
    assert isinstance(blocked, PlanBlocked)
    assert blocked.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING and blocked.setup_plan
    no_pom = tmp_path / "gradle"
    write(no_pom / "build.gradle", "")
    unsupported = build_unit_test_plan(no_pom, target_modules=("",))
    blocked = env.measure(plan_result=unsupported)
    assert isinstance(blocked, PlanBlocked)
    assert blocked.stop_reason is UnitTestStopReason.TECHNOLOGY_UNSUPPORTED


def test_replay_assemble_tool_missing_and_foreign_binding() -> None:
    from zekam.application.unit_test_measurement import MeasurementEvidence

    evidence = MeasurementEvidence(RunStatus.TOOL_MISSING, None, TestRunKind.UNAVAILABLE, (), ())
    record = assemble_measurement(request(), None, evidence)
    assert record.status is MeasurementStatus.TOOL_MISSING
    assert record.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING


def test_replay_binding_digest_changes_with_candidate_and_run(env: Env) -> None:
    first = accepted(env).binding
    env.fake.set(**good_scenario())
    second_record = env.measure(run_id="run-2", candidate=CANDIDATE_2)
    assert isinstance(second_record, MeasurementRecord) and second_record.binding is not None
    assert first is not None
    assert first.binding_digest != second_record.binding.binding_digest


# ------------------------------------------------------------------ freshness (M12, M13)


def current_for(env: Env, binding_candidate: str, **overrides: Any) -> CurrentState:
    record = accepted(env)
    assert record.binding is not None
    state: dict[str, Any] = {
        "request_digest": request().request_digest,
        "source_revision": "rev-1",
        "production_digests": digest_files(env.root, (TARGET,)),
        "build_config_digest": record.binding.build_config_digest,
        "toolchain_digest": record.binding.toolchain.toolchain_digest,
        "test_candidate_digest": binding_candidate,
    }
    state.update(overrides)
    return CurrentState(**state)


def test_replay_external_production_edit_after_success_makes_it_stale_m12(env: Env) -> None:
    record = accepted(env)
    assert record.binding is not None
    current = current_for(env, CANDIDATE_1)
    assert check_freshness(record.binding, current) is FreshnessVerdict.FRESH
    (env.root / TARGET).write_text("package p; class A { int f(){return 2;} }\n", encoding="utf-8")
    verdict = check_freshness(
        record.binding,
        replace(current, production_digests=digest_files(env.root, (TARGET,))),
    )
    assert verdict is FreshnessVerdict.PRODUCTION_DRIFT and verdict.is_external_drift


@pytest.mark.parametrize(
    ("field", "value", "verdict"),
    [
        ("source_revision", "rev-2", FreshnessVerdict.SOURCE_REVISION_DRIFT),
        ("build_config_digest", digest_of_bytes(b"pom-changed"), FreshnessVerdict.CONFIG_DRIFT),
        ("toolchain_digest", digest_of_bytes(b"jdk-changed"), FreshnessVerdict.TOOLCHAIN_DRIFT),
        ("request_digest", digest_of_bytes(b"other"), FreshnessVerdict.REQUEST_MISMATCH),
    ],
)
def test_replay_other_drift_kinds_are_external_drift(
    env: Env, field: str, value: str, verdict: FreshnessVerdict
) -> None:
    record = accepted(env)
    assert record.binding is not None
    current = current_for(env, CANDIDATE_1, **{field: value})
    result = check_freshness(record.binding, current)
    assert result is verdict
    assert result.is_external_drift is (verdict is not FreshnessVerdict.REQUEST_MISMATCH)


def test_replay_own_accepted_test_patch_is_not_drift_and_chains_m13(env: Env) -> None:
    first = accepted(env)
    assert first.binding is not None
    # Kabul edilen kendi test patch'i: aday digest'i degisir, production ayni.
    state_after_patch = current_for(env, CANDIDATE_2)
    verdict = check_freshness(first.binding, state_after_patch)
    assert verdict is FreshnessVerdict.CANDIDATE_MISMATCH
    assert not verdict.is_external_drift  # sonsuz source-drift/replan dongusu yok
    env.fake.set(**good_scenario())
    second = env.measure(run_id="run-2", candidate=CANDIDATE_2)
    assert isinstance(second, MeasurementRecord) and second.binding is not None
    assert check_freshness(second.binding, state_after_patch) is FreshnessVerdict.FRESH
    assert second.binding.test_candidate_digest != first.binding.test_candidate_digest


def test_replay_freshness_blocks_acceptance_in_assemble(env: Env) -> None:
    record = accepted(env)
    assert record.binding is not None
    again = assemble_measurement(
        request(), record.binding, record.evidence, freshness=FreshnessVerdict.PRODUCTION_DRIFT
    )
    assert again.status is MeasurementStatus.UNBOUND_OR_STALE and again.evaluation is None
    foreign = assemble_measurement(request("95"), record.binding, record.evidence)
    assert foreign.status is MeasurementStatus.UNBOUND_OR_STALE  # baska isteğe ait baglama


def test_replay_lines_fixture_constants_are_consistent() -> None:
    assert GOOD_JACOCO.count("<line ") == LINES_3_1.count("<line ")
