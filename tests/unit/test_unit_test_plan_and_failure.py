"""W05: davranis plani (kota degil), oracle, needs-specification ve hata siniflandirma."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.unit_test_loop_support import SRC, default_plan, scenario_doc

from zekam.application.unit_test_failure import (
    Confidence,
    DefectProposal,
    FailureKind,
    FailureSignals,
    FlakyDiagnosis,
    OutputMarker,
    RepeatRun,
    classify_failure,
    extract_output_markers,
)
from zekam.application.unit_test_measurement import MeasurementStatus, RunStatus, TestRunKind
from zekam.application.unit_test_plan import (
    BehaviorPlan,
    OracleKind,
    RiskStatus,
    ScenarioBoard,
    ScenarioState,
    actionable_scenarios,
    parse_behavior_plan,
    parse_plan_delta,
    parse_scenario,
    risk_status,
    unresolved_material,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest


def request() -> UnitTestRequest:
    return UnitTestRequest.with_defaults(
        project_id="p1",
        source_binding_id="b1",
        source_revision="r1",
        source_files=[SRC],
        percent="80",
        budget=UnitTestBudget(5, 60, 600),
    )


def plan(doc: dict[str, Any] | None = None) -> BehaviorPlan:
    return parse_behavior_plan(doc or default_plan(), request(), version=1)


def test_plan_is_not_a_test_quota() -> None:
    for key in ("test_count", "required_tests", "exact_tests"):
        with pytest.raises(PolicyViolation, match="kota"):
            plan({**default_plan(), key: 17})
    with pytest.raises(PolicyViolation):
        parse_scenario(scenario_doc("S1") | {"test_count": 17})
    with pytest.raises(ValidationFailed):
        plan({**default_plan(), "surprise": 1})
    estimate = scenario_doc("S1") | {"estimate": {"low_lines": 1, "high_lines": 4}}
    assert parse_scenario(estimate).to_payload()["estimate"]["forecast"] is True
    with pytest.raises(ValidationFailed):
        parse_scenario(scenario_doc("S1") | {"estimate": {"low_lines": 5, "high_lines": 1}})


def test_scenario_carries_risk_oracle_priority_and_rejects_scope_escape() -> None:
    scenario = plan().scenario("S1")
    assert (scenario.risk_id, scenario.oracle_kind.value, scenario.priority.value) == (
        "R-S1",
        "explicit-contract",
        "high",
    )
    assert scenario.is_material and not plan().scenario("S2").is_material
    with pytest.raises(PolicyViolation):
        parse_scenario(
            scenario_doc("S1") | {"source_files": ["src/main/java/other/B.java"]},
            allowed_sources=frozenset({SRC}),
        )
    with pytest.raises(ValidationFailed):
        parse_scenario(scenario_doc("S1", ref=""))
    with pytest.raises(ValidationFailed):
        parse_scenario(scenario_doc("S1") | {"oracle_kind": "vibes"})


def test_unresolved_oracle_blocks_only_its_own_scenario() -> None:
    p = plan()
    board = ScenarioBoard.initial(p)
    assert board.state_of("S3") is ScenarioState.NEEDS_SPECIFICATION
    assert [s.scenario_id for s in actionable_scenarios(p, board)] == ["S1", "S2"]
    assert OracleKind.UNRESOLVED_ASSUMPTION.value == "unresolved-assumption"


def test_actionable_order_is_priority_then_id() -> None:
    doc = {
        "scenarios": [
            scenario_doc("Z1", priority="low"),
            scenario_doc("A1", priority="normal"),
            scenario_doc("M1", priority="critical"),
        ]
    }
    p = plan(doc)
    ordered = [s.scenario_id for s in actionable_scenarios(p, ScenarioBoard.initial(p))]
    assert ordered == ["M1", "A1", "Z1"]


def test_material_risk_status_and_model_cannot_grant_itself_an_exception() -> None:
    p = plan()
    board = ScenarioBoard.initial(p)
    assert [s.scenario_id for s in unresolved_material(p, board)] == ["S1"]
    board = board.with_state("S1", ScenarioState.VERIFIED)
    assert unresolved_material(p, board) == ()
    assert risk_status(ScenarioState.PRODUCTION_DEFECT) is RiskStatus.UNRESOLVED
    assert risk_status(ScenarioState.VERIFIED) is RiskStatus.VERIFIED
    with pytest.raises(PolicyViolation, match="istisna"):
        board.with_state("S1", ScenarioState.ACCEPTED_EXCEPTION)


def test_old_verification_is_not_carried_to_changed_scenario_or_source_drift_m13() -> None:
    p = plan()
    board = ScenarioBoard.initial(p).with_state("S1", ScenarioState.VERIFIED)
    changed = plan({"scenarios": [scenario_doc("S1", ref="spec#2"), scenario_doc("S2")]})
    assert board.carry_over(changed, keep_verified=True).state_of("S1") is ScenarioState.PLANNED
    assert board.carry_over(p, keep_verified=True).state_of("S1") is ScenarioState.VERIFIED
    assert board.reset_verified(p).state_of("S1") is ScenarioState.PLANNED
    assert BehaviorPlan.from_payload(p.to_payload(), request()).plan_digest == p.plan_digest
    assert ScenarioBoard.from_payload(board.to_payload()) == board


def test_plan_delta_cannot_smuggle_authority_or_scope() -> None:
    delta = parse_plan_delta(
        {
            "rationale": "yeni risk",
            "new_scenarios": [scenario_doc("N1")],
            "requires_authority": ["production-change"],
        },
        request(),
    )
    assert delta.requires_authority == ("production-change",)
    with pytest.raises(ValidationFailed):
        parse_plan_delta({"rationale": "x", "surprise": 1}, request())
    extended = plan().with_scenarios(delta.new_scenarios)
    assert extended.version == 2 and extended.scenario("N1")


# ---------------------------------------------------------------------- hata siniflandirma


def signals(**kw: Any) -> FailureSignals:
    base: dict[str, Any] = {
        "run_status": RunStatus.COMPLETED,
        "measurement_status": MeasurementStatus.TESTS_FAILED,
        "test_kind": TestRunKind.FAILED,
    }
    return FailureSignals(**(base | kw))


M = OutputMarker
ASSERT = frozenset({M.ASSERTION_FAILURE})


@pytest.mark.parametrize(
    ("kw", "oracles", "kind", "confidence"),
    [
        (
            {"run_status": RunStatus.TOOL_MISSING, "test_kind": None},
            {},
            FailureKind.ENVIRONMENT,
            Confidence.PROVEN,
        ),
        ({"plan_blocked": True}, {}, FailureKind.ENVIRONMENT, Confidence.PROVEN),
        (
            {"markers": frozenset({M.DEPENDENCY_RESOLUTION, M.COMPILATION_ERROR})},
            {},
            FailureKind.ENVIRONMENT,
            Confidence.PROBABLE,
        ),
        ({"markers": frozenset({M.TOOLCHAIN})}, {}, FailureKind.ENVIRONMENT, Confidence.PROBABLE),
        (
            {"markers": frozenset({M.COMPILATION_ERROR}), "test_kind": TestRunKind.UNAVAILABLE},
            {},
            FailureKind.COMPILE,
            Confidence.PROBABLE,
        ),
        (
            {"markers": frozenset({M.MISSING_IMPORT, M.COMPILATION_ERROR})},
            {},
            FailureKind.IMPORT,
            Confidence.PROBABLE,
        ),
        (
            {"failed_cases": ("p.T#t",), "markers": frozenset({M.SETUP_ERROR})},
            {"S1": OracleKind.INVARIANT},
            FailureKind.FIXTURE,
            Confidence.PROBABLE,
        ),
        (
            {"failed_cases": ("p.T#t",), "markers": ASSERT},
            {"S1": OracleKind.CHARACTERIZATION},
            FailureKind.EXPECTATION,
            Confidence.PROBABLE,
        ),
        (
            {"failed_cases": ("p.T#t",), "markers": ASSERT},
            {"S1": OracleKind.UNRESOLVED_ASSUMPTION},
            FailureKind.EXPECTATION,
            Confidence.PROBABLE,
        ),
        ({"test_kind": TestRunKind.NO_TESTS}, {}, FailureKind.DISCOVERY, Confidence.PROBABLE),
        ({"test_kind": TestRunKind.ALL_SKIPPED}, {}, FailureKind.DISCOVERY, Confidence.PROBABLE),
        (
            {
                "test_kind": TestRunKind.INCONSISTENT_REPORT,
                "measurement_status": MeasurementStatus.TESTS_NOT_ACCEPTABLE,
            },
            {},
            FailureKind.MEASUREMENT_PARSER,
            Confidence.PROBABLE,
        ),
        (
            {
                "measurement_status": MeasurementStatus.UNBOUND_OR_STALE,
                "test_kind": TestRunKind.PASSED,
            },
            {},
            FailureKind.MEASUREMENT_PARSER,
            Confidence.PROBABLE,
        ),
        ({"test_kind": TestRunKind.FLAKY_SUSPECTED}, {}, FailureKind.FLAKY, Confidence.PROBABLE),
        ({}, {}, FailureKind.UNKNOWN, Confidence.UNKNOWN),
        ({"run_status": RunStatus.TIMED_OUT}, {}, FailureKind.UNKNOWN, Confidence.UNKNOWN),
    ],
)
def test_failure_classes_are_kept_apart_with_calibrated_confidence(
    kw: dict[str, Any], oracles: dict[str, OracleKind], kind: FailureKind, confidence: Confidence
) -> None:
    result = classify_failure(signals(**kw), scenario_oracles=oracles)
    assert (result.kind, result.confidence) == (kind, confidence)
    assert result.evidence


def test_independent_oracle_assertion_failure_is_a_defect_candidate_not_a_test_fix() -> None:
    result = classify_failure(
        signals(failed_cases=("p.T#t",), markers=ASSERT),
        scenario_oracles={"S1": OracleKind.EXPLICIT_CONTRACT, "S2": OracleKind.CHARACTERIZATION},
    )
    assert result.kind is FailureKind.PRODUCTION_DEFECT and result.needs_defect_triage
    assert result.scenario_ids == ("S1",) and not result.repairable
    assert result.confidence is Confidence.PROBABLE, "harness tek basina PROVEN demez"


def test_flaky_requires_recorded_ordered_repeats_and_first_failure_is_never_forgiven() -> None:
    runs = (
        RepeatRun(1, "initial", TestRunKind.FAILED, ("p.T#t",)),
        RepeatRun(2, "same-order", TestRunKind.PASSED),
        RepeatRun(3, "same-order", TestRunKind.PASSED),
    )
    diagnosis = FlakyDiagnosis(runs)
    assert diagnosis.mixed and not diagnosis.deterministic_failure
    result = classify_failure(
        signals(failed_cases=("p.T#t",), markers=ASSERT),
        scenario_oracles={"S1": OracleKind.INVARIANT},
        repeats=diagnosis,
    )
    assert result.kind is FailureKind.FLAKY and result.confidence is Confidence.PROVEN
    deterministic = FlakyDiagnosis((runs[0], RepeatRun(2, "same-order", TestRunKind.FAILED)))
    assert deterministic.deterministic_failure and not deterministic.mixed
    with pytest.raises(ValidationFailed):
        FlakyDiagnosis((runs[0],))
    with pytest.raises(ValidationFailed):
        FlakyDiagnosis((runs[0], RepeatRun(5, "x", TestRunKind.PASSED)))


def test_output_markers_are_bounded_cause_hints_only() -> None:
    assert M.MISSING_IMPORT in extract_output_markers("x" * 100_000 + "package a.b does not exist")
    assert extract_output_markers("COMPILATION ERROR") == {M.COMPILATION_ERROR}
    assert extract_output_markers("A" * 40_000 + "AssertionFailedError" + "B" * 20_000) == set()
    assert extract_output_markers("") == frozenset()


def test_defect_proposal_keeps_reproducer_and_oracle_and_grants_no_authority() -> None:
    proposal = DefectProposal(
        ("S1",),
        ("spec#1",),
        digest("cand"),
        ("a/T.java",),
        digest("sig"),
        ("p.T#t",),
        digest("triage"),
    )
    payload = proposal.to_payload()
    assert payload["grants_authority"] is False and payload["oracle_refs"] == ["spec#1"]
    assert proposal.proposal_digest.startswith("sha256:")
