from __future__ import annotations

from fractions import Fraction

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import LoopTerminalState
from zekam.domain.loop_progress import LoopStopReason
from zekam.domain.unit_test_engineering import (
    STOP_REASON_LOOP_REASON,
    STOP_REASON_TERMINAL_STATE,
    CoverageMetric,
    CoverageObservation,
    CoveragePolicy,
    CoverageState,
    EvaluationVerdict,
    RatioThreshold,
    UnitTestAttempt,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
    evaluate_scope,
    normalize_relative_source,
)

BUDGET = UnitTestBudget(3, 60, 600)
A = "mod-a/src/main/java/p/Service.java"
B = "mod-b/src/main/java/q/Service.java"
D1 = digest({"n": 1})
D2 = digest({"n": 2})


def request(
    *, policy: CoveragePolicy = CoveragePolicy.PER_FILE, percent: str = "90"
) -> UnitTestRequest:
    return UnitTestRequest(
        project_id="proj-1",
        source_binding_id="bind-1",
        source_revision="rev1",
        source_files=(A, B),
        metric=CoverageMetric.LINE,
        threshold=RatioThreshold.from_percent(percent),
        policy=policy,
        budget=BUDGET,
    )


def measured(
    path: str, covered: int, missed: int, metric: CoverageMetric = CoverageMetric.LINE
) -> CoverageObservation:
    return CoverageObservation.measured(path, metric, covered=covered, missed=missed)


def test_defaults_are_line_per_file_and_visible() -> None:
    built = UnitTestRequest.with_defaults(
        project_id="p",
        source_binding_id="b",
        source_revision="r",
        source_files=[A],
        percent=90,
        budget=BUDGET,
    )
    assert built.metric is CoverageMetric.LINE
    assert built.policy is CoveragePolicy.PER_FILE
    assert built.defaults_applied == ("metric=line", "policy=per-file")
    explicit = UnitTestRequest.with_defaults(
        project_id="p",
        source_binding_id="b",
        source_revision="r",
        source_files=[A],
        percent=90,
        budget=BUDGET,
        metric=CoverageMetric.BRANCH,
        policy=CoveragePolicy.AGGREGATE,
    )
    assert explicit.defaults_applied == ()
    assert explicit.request_digest != built.request_digest


@pytest.mark.parametrize(
    "path", ["../x.java", "/abs.java", "C:/x.java", "a\\b.java", "a//b", "./a", "", " a", "a/../b"]
)
def test_source_path_rejects_traversal_and_foreign_roots(path: str) -> None:
    with pytest.raises(ValidationFailed):
        normalize_relative_source(path)


def test_request_rejects_duplicates_and_overlap() -> None:
    def build(**changes: object) -> UnitTestRequest:
        values: dict[str, object] = {
            "project_id": "p",
            "source_binding_id": "b",
            "source_revision": "r",
            "source_files": (A,),
            "metric": CoverageMetric.LINE,
            "threshold": RatioThreshold(9, 10),
            "policy": CoveragePolicy.PER_FILE,
            "budget": BUDGET,
        }
        values.update(changes)
        return UnitTestRequest(**values)  # type: ignore[arg-type]

    with pytest.raises(ValidationFailed):
        build(source_files=(A, A))
    with pytest.raises(ValidationFailed):
        build(source_files=())
    with pytest.raises(ValidationFailed):
        build(allowed_test_paths=(A,))
    with pytest.raises(ValidationFailed):
        build(allowed_test_paths=("t/X.java",), forbidden_paths=("t/X.java",))
    with pytest.raises(ValidationFailed):
        build(source_files=("../escape.java",))


def test_same_basename_in_two_modules_stay_distinct() -> None:
    result = evaluate_scope(request(), [measured(A, 9, 1), measured(B, 1, 9)])
    by_file = {item.source_file: item for item in result.files}
    assert by_file[A].verdict is EvaluationVerdict.MET
    assert by_file[B].verdict is EvaluationVerdict.NOT_MET
    assert result.verdict is EvaluationVerdict.NOT_MET


def test_threshold_is_exact_not_rounded() -> None:
    threshold = RatioThreshold.from_percent("90")
    assert not threshold.met_by(8999, 10000)
    assert threshold.met_by(9000, 10000)
    assert RatioThreshold.from_percent("89.5").as_fraction() == Fraction(179, 200)
    assert threshold.met_by(9, 10) and not threshold.met_by(89, 100)
    with pytest.raises(ValidationFailed):
        RatioThreshold.from_percent(90.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        RatioThreshold.from_percent("101")
    with pytest.raises(ValidationFailed):
        threshold.met_by(1, 0)


def test_rounded_ninety_blocks_success() -> None:
    req = request(policy=CoveragePolicy.AGGREGATE)
    result = evaluate_scope(req, [measured(A, 4499, 501), measured(B, 4500, 500)])
    assert result.aggregate_covered == 8999 and result.aggregate_total == 10000
    assert result.verdict is EvaluationVerdict.NOT_MET


def test_aggregate_sums_counters_not_average_of_ratios() -> None:
    req = request(policy=CoveragePolicy.AGGREGATE)
    result = evaluate_scope(req, [measured(A, 1000, 0), measured(B, 1, 99)])
    assert result.aggregate_ratio == Fraction(1001, 1100)
    assert result.aggregate_met is True
    assert result.verdict is EvaluationVerdict.MET
    per_file = evaluate_scope(request(), [measured(A, 1000, 0), measured(B, 1, 99)])
    assert per_file.verdict is EvaluationVerdict.NOT_MET
    assert per_file.aggregate_met is True


def test_states_are_distinct_and_never_fake_zero() -> None:
    req = request()
    zero = evaluate_scope(req, [measured(A, 0, 10), measured(B, 10, 0)])
    assert zero.verdict is EvaluationVerdict.NOT_MET
    assert zero.files[0].state is CoverageState.MEASURED and zero.files[0].covered == 0
    for state in (
        CoverageState.NOT_MEASURED,
        CoverageState.MISSING_REPORT,
        CoverageState.MAPPING_ERROR,
    ):
        obs = CoverageObservation(A, CoverageMetric.LINE, state, reason="neden")
        result = evaluate_scope(req, [obs, measured(B, 10, 0)])
        assert result.verdict is EvaluationVerdict.INCONCLUSIVE
        assert result.files[0].state is state
        assert result.files[0].covered is None
    missing = evaluate_scope(req, [measured(B, 10, 0)])
    assert missing.verdict is EvaluationVerdict.INCONCLUSIVE
    assert missing.files[0].state is CoverageState.MISSING_REPORT


def test_not_applicable_is_visible_and_excluded_but_not_universal() -> None:
    req = request()
    na = CoverageObservation(
        A, CoverageMetric.LINE, CoverageState.NOT_APPLICABLE, reason="satir yok"
    )
    mixed = evaluate_scope(req, [na, measured(B, 10, 0)])
    assert mixed.verdict is EvaluationVerdict.MET
    assert mixed.files[0].state is CoverageState.NOT_APPLICABLE
    both = evaluate_scope(
        req,
        [
            na,
            CoverageObservation(
                B, CoverageMetric.LINE, CoverageState.NOT_APPLICABLE, reason="satir yok"
            ),
        ],
    )
    assert both.verdict is EvaluationVerdict.INCONCLUSIVE


def test_request_binding_fields_are_digest_bound_and_optional_for_legacy_rows() -> None:
    base = request()
    bound = UnitTestRequest.with_defaults(
        project_id=base.project_id,
        source_binding_id=base.source_binding_id,
        source_revision=base.source_revision,
        source_files=base.source_files,
        percent="80",
        budget=base.budget,
        work_item_id="work-1",
        plan_id="plan-1",
        run_id="run-1",
        source_snapshot_id="snapshot-1",
        graph_generation_digest=digest("graph"),
    )
    assert bound.request_digest != base.request_digest
    assert bound.to_payload()["work_item_id"] == "work-1"
    assert "work_item_id" not in base.to_payload()


def test_observation_invariants() -> None:
    with pytest.raises(ValidationFailed):
        CoverageObservation(A, CoverageMetric.LINE, CoverageState.MEASURED, 1, 1, 3)
    with pytest.raises(ValidationFailed):
        CoverageObservation(A, CoverageMetric.LINE, CoverageState.MEASURED, 0, 0, 0)
    with pytest.raises(ValidationFailed):
        CoverageObservation(A, CoverageMetric.LINE, CoverageState.MEASURED, True, 0, 1)
    with pytest.raises(ValidationFailed):
        CoverageObservation(A, CoverageMetric.LINE, CoverageState.NOT_MEASURED, 0, 0, 0, "x")
    with pytest.raises(ValidationFailed):
        CoverageObservation(A, CoverageMetric.LINE, CoverageState.MAPPING_ERROR)


def test_branch_observations_do_not_leak_into_line_target() -> None:
    req = request()
    result = evaluate_scope(
        req,
        [measured(A, 10, 0), measured(B, 10, 0), measured(A, 0, 10, CoverageMetric.BRANCH)],
    )
    assert result.verdict is EvaluationVerdict.MET
    with pytest.raises(ValidationFailed):
        evaluate_scope(req, [measured("other/X.java", 1, 0)])
    with pytest.raises(ValidationFailed):
        evaluate_scope(req, [measured(A, 1, 0), measured(A, 1, 0)])


def test_attempt_lineage_rules() -> None:
    first = UnitTestAttempt("att-1", D1, 1, None, D1, D2, "key-1", ((A, D1),))
    same = UnitTestAttempt("att-1", D1, 1, None, D1, D2, "key-1", ((A, D1),))
    assert first.effect_digest == same.effect_digest
    with pytest.raises(ValidationFailed):
        UnitTestAttempt("att-2", D1, 2, None, D1, D2, "key-2")
    with pytest.raises(ValidationFailed):
        UnitTestAttempt("att-2", D1, 1, "att-1", D1, D2, "key-2")
    with pytest.raises(ValidationFailed):
        UnitTestAttempt("att-1", D1, 1, None, D1, D2, "key-1", ((A, D1), (A, D2)))


def test_every_reason_maps_to_existing_loop_enums_without_success_boolean() -> None:
    assert set(STOP_REASON_TERMINAL_STATE) == set(UnitTestStopReason)
    assert set(STOP_REASON_LOOP_REASON) == set(UnitTestStopReason)
    assert len({r.value for r in UnitTestStopReason}) == len(UnitTestStopReason)
    reached = UnitTestTerminal(D1, UnitTestStopReason.TARGET_REACHED, D2)
    assert reached.status is LoopTerminalState.PASSED
    assert reached.loop_stop_reason is LoopStopReason.TARGET_REACHED
    assert "success" not in reached.machine_status()
    budget = UnitTestTerminal(D1, UnitTestStopReason.BUDGET_EXHAUSTED, None)
    assert budget.status is LoopTerminalState.BUDGET_EXHAUSTED
    for reason in (
        UnitTestStopReason.PRODUCTION_DEFECT,
        UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED,
        UnitTestStopReason.STAGNATION_REVIEW,
    ):
        assert UnitTestTerminal(D1, reason, None).status is LoopTerminalState.MANUAL_REVIEW
    for reason in (
        UnitTestStopReason.ENVIRONMENT_MISSING,
        UnitTestStopReason.TECHNOLOGY_UNSUPPORTED,
        UnitTestStopReason.SPEC_AMBIGUOUS,
    ):
        assert UnitTestTerminal(D1, reason, None).status is LoopTerminalState.BLOCKED
    assert not UnitTestTerminal(D1, UnitTestStopReason.USER_PAUSED, None).final
    assert UnitTestTerminal(D1, UnitTestStopReason.USER_CANCELLED, None).final


def test_success_terminal_requires_receipt() -> None:
    for reason in (UnitTestStopReason.TARGET_REACHED, UnitTestStopReason.ALREADY_AT_TARGET):
        with pytest.raises(PolicyViolation):
            UnitTestTerminal(D1, reason, None)
