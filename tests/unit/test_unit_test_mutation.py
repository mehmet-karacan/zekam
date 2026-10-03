"""W06 / D10: PIT mutations.xml parser, skor tanimlari ve mutation kabul karari (replay fixture).

Fixture'lar elle yazilmis ``mutations.xml`` parcalaridir; gercek bir PIT kosusundan
uretilmemistir. GERCEK PIT KOSUSU BU MAKINEDE YOKTUR (Maven/PIT kurulu degil); ayni line
coverage'da guclu assertion'in yeni mutanti yakaladigini gosteren GERCEK calisma W08'dedir.
Buradaki cift yalniz parser/karsilastirma mantiginin 'replay' kanitidir.
"""

from __future__ import annotations

import json

import pytest
from tests.unit.unit_test_runner_support import GOOD_JACOCO

from zekam.application.unit_test_mutation import (
    MAX_REVIEW_ITEMS,
    NOT_RUN_OUTCOME,
    MutationAcceptancePolicy,
    MutationDecision,
    MutationOutcome,
    MutationRunState,
    MutationScoreKind,
    MutationVerdict,
    PitCounters,
    PitStatus,
    compare_reports,
    compute_score,
    decide_mutation_acceptance,
    mutation_payload,
)
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.jacoco_report import (
    build_observations,
    parse_jacoco_report,
)
from zekam.infrastructure.unit_test_runner.pit_report import parse_pit_report

_DETECTED = {"KILLED": "true", "SURVIVED": "false", "NO_COVERAGE": "false", "NON_VIABLE": "false"}


def mutation(
    status: str,
    *,
    line: int = 3,
    mutator: str = "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
    index: str = "5",
    method: str = "f",
    killing: str = "",
) -> str:
    detected = f" detected='{_DETECTED[status]}'" if status in _DETECTED else ""
    kill = f"<killingTest>{killing}</killingTest>" if killing else "<killingTest/>"
    return (
        f"<mutation{detected} status='{status}' numberOfTestsRun='1'>"
        f"<sourceFile>A.java</sourceFile><mutatedClass>p.A</mutatedClass>"
        f"<mutatedMethod>{method}</mutatedMethod><methodDescription>()I</methodDescription>"
        f"<lineNumber>{line}</lineNumber><mutator>{mutator}</mutator>"
        f"<indexes><index>{index}</index></indexes><blocks><block>0</block></blocks>"
        f"{kill}<description>replaced</description></mutation>"
    )


def mutations(*items: str) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?><mutations>" + "".join(items) + "</mutations>"
    ).encode()


def counters_of(**values: int) -> PitCounters:
    return PitCounters(**values)


# ------------------------------------------------------------------ parser: sonuc siniflari


def test_replay_every_result_class_is_a_separate_raw_counter() -> None:
    data = mutations(
        mutation("KILLED", index="1"),
        mutation("KILLED", index="2"),
        mutation("SURVIVED", index="3"),
        mutation("NO_COVERAGE", index="4"),
        mutation("NON_VIABLE", index="5"),
        mutation("TIMED_OUT", index="6"),
        mutation("MEMORY_ERROR", index="7"),
        mutation("RUN_ERROR", index="8"),
        mutation("STARTED", index="9"),
        mutation("SOMETHING_NEW", index="10"),
    )
    c = parse_pit_report(data).counters
    assert c == PitCounters(
        killed=2, survived=1, no_coverage=1, non_viable=1, timed_out=1, memory_error=1,
        run_error=1, unfinished=1, unknown=1,
    )  # fmt: skip
    assert c.total_listed == 10 and c.detected == 4
    assert not c.complete  # RUN_ERROR + bitmemis + taninmayan: sessizce yutulmaz


def test_replay_empty_report_has_zero_counters_and_no_fake_score() -> None:
    report = parse_pit_report(mutations())
    assert report.counters == PitCounters() and report.counters.complete
    assert compute_score(report.counters, MutationScoreKind.MUTATION_SCORE) is None
    assert compute_score(report.counters, MutationScoreKind.TEST_STRENGTH) is None


def test_replay_score_definitions_state_denominator_effects() -> None:
    c = counters_of(
        killed=3, timed_out=1, memory_error=1, survived=2, no_coverage=1, non_viable=2, run_error=1
    )
    score = compute_score(c, MutationScoreKind.MUTATION_SCORE)
    strength = compute_score(c, MutationScoreKind.TEST_STRENGTH)
    assert score is not None and strength is not None
    # detected = killed + timed_out + memory_error = 5
    assert (score.numerator, score.denominator) == (5, 8)  # + survived 2 + no_coverage 1
    assert (strength.numerator, strength.denominator) == (5, 7)  # NO_COVERAGE payda disi
    # NON_VIABLE ve RUN_ERROR hicbir paydada yok: degistirmek skoru etkilemez.
    more = counters_of(
        killed=3, timed_out=1, memory_error=1, survived=2, no_coverage=1, non_viable=50, run_error=9
    )
    assert compute_score(more, MutationScoreKind.MUTATION_SCORE) == score
    assert "NO_COVERAGE" in score.definition and "NON_VIABLE" in score.definition
    assert score.meets(5, 8) and not score.meets(2, 3)  # 5/8 < 2/3, yuvarlama yok


def test_replay_survivors_and_no_coverage_are_review_required_not_test_gaps() -> None:
    report = parse_pit_report(
        mutations(
            mutation("KILLED", index="1"),
            mutation("SURVIVED", index="2"),
            mutation("NO_COVERAGE", index="3"),
            mutation("TIMED_OUT", index="4"),
        )
    )
    assert report.review_required_count == 2
    assert {i.status for i in report.review_items} == {PitStatus.SURVIVED, PitStatus.NO_COVERAGE}
    assert all(i.review_required and "equivalent" in i.reason for i in report.review_items)


def test_replay_review_items_are_bounded_but_counters_stay_exact() -> None:
    items = [mutation("SURVIVED", index=str(i)) for i in range(MAX_REVIEW_ITEMS + 5)]
    report = parse_pit_report(mutations(*items))
    assert report.counters.survived == MAX_REVIEW_ITEMS + 5
    assert len(report.review_items) == MAX_REVIEW_ITEMS and report.review_items_truncated


def test_replay_identical_mutants_get_distinct_keys() -> None:
    report = parse_pit_report(mutations(mutation("SURVIVED"), mutation("SURVIVED")))
    assert len({k for k, _ in report.statuses}) == 2


# ------------------------------------------------------------------ parser: red


@pytest.mark.parametrize(
    "payload",
    [
        b"<mutations><mutation status='KILLED'>",  # malformed
        b"",
        b"<!DOCTYPE mutations SYSTEM 'x.dtd'><mutations/>",
        b'<!DOCTYPE mutations [<!ENTITY x "boom">]><mutations><mutation status="&x;"/></mutations>',
        b'<!DOCTYPE m [<!ENTITY e SYSTEM "file:///etc/passwd">]><mutations>&e;</mutations>',
        b"<other/>",
        b"<mutations/><mutations/>",
        mutations(mutation("SURVIVED").replace("detected='false'", "detected='true'")),
        mutations(mutation("KILLED").replace("detected='true'", "detected='false'")),
    ],
)
def test_replay_pit_parser_rejects_malformed_entity_and_inconsistent_reports(
    payload: bytes,
) -> None:
    with pytest.raises(ValidationFailed):
        parse_pit_report(payload)


def test_replay_pit_parser_enforces_size_limit_without_reading_content() -> None:
    big = mutations(*[mutation("KILLED", index=str(i)) for i in range(50)])
    with pytest.raises(ValidationFailed, match="boyut"):
        parse_pit_report(big, max_bytes=len(big) - 1)


# ------------------------------------------------------------ ayni coverage, guclu assertion


def test_replay_same_line_coverage_stronger_assertion_kills_new_mutant() -> None:
    """Replay cifti: ayni JaCoCo LINE sayaci; zayif assertion mutanti kacirir, guclusu yakalar."""

    jacoco = {"": parse_jacoco_report(GOOD_JACOCO.encode())}
    weak_cov = build_observations(["src/main/java/p/A.java"], modules=[""], reports=jacoco)
    strong_cov = build_observations(["src/main/java/p/A.java"], modules=[""], reports=jacoco)
    assert weak_cov == strong_cov  # ayni line coverage (ayni rapor, ayni sayac)

    weak = parse_pit_report(
        mutations(
            mutation("SURVIVED", index="5"), mutation("KILLED", index="6", killing="p.ATest.a")
        )
    )
    strong = parse_pit_report(
        mutations(
            mutation("KILLED", index="5", killing="p.ATest.exactValue"),
            mutation("KILLED", index="6", killing="p.ATest.a"),
        )
    )
    comparison = compare_reports(weak, strong)
    assert comparison.comparable
    assert [k.index for k in comparison.newly_killed] == ["5"] and not comparison.regressed
    assert weak.counters.survived == 1 and strong.counters.survived == 0
    reverse = compare_reports(strong, weak)
    assert [k.index for k in reverse.regressed] == ["5"] and not reverse.newly_killed


def test_replay_compare_flags_changed_mutant_set_as_not_comparable() -> None:
    before = parse_pit_report(mutations(mutation("SURVIVED", index="1")))
    after = parse_pit_report(
        mutations(mutation("KILLED", index="1"), mutation("KILLED", index="2"))
    )
    comparison = compare_reports(before, after)
    assert not comparison.comparable  # payda degisti: dogrudan skor kiyasi yok
    assert [k.index for k in comparison.only_in_candidate] == ["2"]


# ------------------------------------------------------------------ kabul karari


def completed(**values: int) -> MutationOutcome:
    report = parse_pit_report(
        mutations(
            *[mutation(s.upper(), index=f"{s}{i}") for s, n in values.items() for i in range(n)]
        )
    )
    return MutationOutcome(
        MutationRunState.COMPLETED, plan_digest=PLAN_D, report=report,
        report_digest="sha256:" + "b" * 64, candidate_digest=CAND_OLD,
    )  # fmt: skip


PLAN_D = "sha256:" + "a" * 64
CAND_OLD = "sha256:" + "1" * 64
CAND_NEW = "sha256:" + "2" * 64


def decide(policy: MutationAcceptancePolicy, outcome: MutationOutcome) -> MutationDecision:
    return decide_mutation_acceptance(
        policy, outcome, expected_plan_digest=PLAN_D, expected_candidate_digest=CAND_OLD
    )


REQUIRED_80 = MutationAcceptancePolicy(
    required=True, threshold_numerator=4, threshold_denominator=5
)


def test_replay_mutation_not_run_by_default_and_base_flow_unaffected() -> None:
    decision = decide(MutationAcceptancePolicy(), NOT_RUN_OUTCOME)
    assert decision.verdict is MutationVerdict.NOT_REQUIRED and decision.satisfied
    assert decision.mutation_status == "mutation_not_run"
    payload = mutation_payload(NOT_RUN_OUTCOME, decision)
    assert payload["mutation_status"] == "mutation_not_run" and payload["counters"] is None
    assert payload["score"] is None and payload["review_items"] == []


@pytest.mark.parametrize(
    "state",
    [
        MutationRunState.NOT_RUN,
        MutationRunState.TOOL_MISSING,
        MutationRunState.SETUP_REQUIRED,
        MutationRunState.NOT_SUPPORTED,
        MutationRunState.RUN_FAILED,
        MutationRunState.REPORT_INVALID,
    ],
)
def test_replay_required_mutation_never_passes_when_not_run_or_failed(
    state: MutationRunState,
) -> None:
    decision = decide(REQUIRED_80, MutationOutcome(state, reasons=("x",)))
    assert decision.verdict is MutationVerdict.NOT_MET and not decision.satisfied
    assert decision.mutation_status in {"mutation_not_run", "mutation_run_failed"}


def test_replay_required_mutation_threshold_uses_cross_multiplication() -> None:
    met = decide(REQUIRED_80, completed(killed=4, survived=1))
    assert met.verdict is MutationVerdict.MET and met.satisfied
    assert met.review_required_count == 1  # esik gecse de survivor review konusu
    below = decide(REQUIRED_80, completed(killed=79, survived=21))
    assert below.verdict is MutationVerdict.NOT_MET and not below.satisfied  # %79 -> %80 degil


def test_replay_required_mutation_no_coverage_denominator_effect_by_kind() -> None:
    outcome = completed(killed=4, no_coverage=1)
    assert decide(REQUIRED_80, outcome).verdict is MutationVerdict.MET
    stricter = MutationAcceptancePolicy(
        required=True, threshold_numerator=9, threshold_denominator=10
    )
    assert decide(stricter, outcome).verdict is MutationVerdict.NOT_MET
    strength = MutationAcceptancePolicy(
        True, MutationScoreKind.TEST_STRENGTH, threshold_numerator=9, threshold_denominator=10
    )
    assert decide(strength, outcome).verdict is MutationVerdict.MET


def test_replay_required_mutation_incomplete_or_empty_is_inconclusive_not_passed() -> None:
    with_error = decide(REQUIRED_80, completed(killed=10, run_error=1))
    assert with_error.verdict is MutationVerdict.INCONCLUSIVE and not with_error.satisfied
    empty = decide(REQUIRED_80, completed())
    assert empty.verdict is MutationVerdict.INCONCLUSIVE and not empty.satisfied
    only_non_viable = decide(REQUIRED_80, completed(non_viable=3))
    assert only_non_viable.verdict is MutationVerdict.INCONCLUSIVE


def test_replay_required_mutation_rejects_result_bound_to_other_plan() -> None:
    decision = decide_mutation_acceptance(
        REQUIRED_80,
        completed(killed=5),
        expected_plan_digest="sha256:" + "c" * 64,
        expected_candidate_digest=CAND_OLD,
    )
    assert decision.verdict is MutationVerdict.NOT_MET and not decision.satisfied


def test_replay_policy_has_no_universal_default_threshold() -> None:
    with pytest.raises(ValidationFailed):
        MutationAcceptancePolicy(required=True)  # esik kullanicidan gelmeli
    with pytest.raises(ValidationFailed):
        MutationAcceptancePolicy(threshold_numerator=1, threshold_denominator=2)
    with pytest.raises(ValidationFailed):
        MutationAcceptancePolicy(required=True, threshold_numerator=3, threshold_denominator=2)
    with pytest.raises(ValidationFailed):
        MutationAcceptancePolicy(required=True, threshold_numerator=1)


def test_replay_outcome_report_only_when_completed_and_tool_percent_is_separate() -> None:
    with pytest.raises(ValidationFailed):
        MutationOutcome(MutationRunState.COMPLETED)
    base = completed(killed=1, survived=1)
    tool = MutationOutcome(
        base.state,
        base.plan_digest,
        base.report,
        base.report_digest,
        tool_reported_percent="73",
        candidate_digest=base.candidate_digest,
    )
    decision = decide(REQUIRED_80, tool)
    payload = mutation_payload(tool, decision)
    assert payload["tool_reported_percent"] == "73"
    assert payload["score"]["numerator"] == 1 and payload["score"]["denominator"] == 2
    assert decision.verdict is MutationVerdict.NOT_MET  # karar arac yuzdesinden degil sayaclardan
    json.dumps(payload)  # kanonik payload serilestirilebilir


# --------------------------------------------------------- verifier bulgulari (regresyon)


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [(0.8, 1), (4, 5.0), (True, True), (1, True), ("4", "5"), (False, 1)],
)
def test_replay_policy_threshold_accepts_only_plain_int(
    numerator: object, denominator: object
) -> None:
    with pytest.raises(ValidationFailed):
        MutationAcceptancePolicy(
            required=True,
            threshold_numerator=numerator,  # type: ignore[arg-type]
            threshold_denominator=denominator,  # type: ignore[arg-type]
        )


def completed_for(candidate: str | None) -> MutationOutcome:
    base = completed(killed=5)
    return MutationOutcome(
        base.state, base.plan_digest, base.report, base.report_digest, candidate_digest=candidate
    )


def test_replay_required_decision_demands_expected_plan_and_candidate_digest() -> None:
    outcome = completed_for(CAND_OLD)
    ok = decide_mutation_acceptance(
        REQUIRED_80, outcome, expected_plan_digest=PLAN_D, expected_candidate_digest=CAND_OLD
    )
    assert ok.verdict is MutationVerdict.MET
    for plan, cand in (
        (None, CAND_OLD),
        (PLAN_D, None),
        ("", CAND_OLD),
        (PLAN_D, ""),
        (None, None),
    ):
        decision = decide_mutation_acceptance(
            REQUIRED_80, outcome, expected_plan_digest=plan, expected_candidate_digest=cand
        )
        assert decision.verdict is MutationVerdict.NOT_MET and not decision.satisfied
    with pytest.raises(TypeError):  # opsiyonel degil: cagiran vermek zorunda
        decide_mutation_acceptance(REQUIRED_80, outcome)  # type: ignore[call-arg]


def test_replay_old_candidate_report_does_not_pass_new_candidate() -> None:
    old_report = completed_for(CAND_OLD)
    stale = decide_mutation_acceptance(
        REQUIRED_80, old_report, expected_plan_digest=PLAN_D, expected_candidate_digest=CAND_NEW
    )
    assert stale.verdict is MutationVerdict.NOT_MET and not stale.satisfied
    unbound = decide_mutation_acceptance(
        REQUIRED_80,
        old_report,
        expected_plan_digest="sha256:" + "f" * 64,
        expected_candidate_digest=CAND_OLD,
    )
    assert unbound.verdict is MutationVerdict.NOT_MET


def test_replay_completed_outcome_must_carry_candidate_digest() -> None:
    with pytest.raises(ValidationFailed):
        completed_for(None)
