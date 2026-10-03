"""W07: testability refactor kapisi saf sozlesmeleri (proposal, onay, baseline karsilastirma).

Saf birim testleri; ledger/model/Maven yok.
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from typing import Any

import pytest

from zekam.application.unit_test_refactor import (
    ApprovalMismatch,
    ApprovalRefused,
    CoverageGain,
    ImpactDeclaration,
    IncomparableBaselines,
    OutOfScopeKind,
    ProblemKind,
    ProposalInvalid,
    ProposalTarget,
    RefactorOrigin,
    RefactorProposal,
    ReviewArea,
    RiskLevel,
    ScopeViolation,
    SourceBaseline,
    TestabilityProblem,
    UserApprovalDecision,
    VerificationPlan,
    check_production_target,
    compare_coverage,
    derive_followup_request,
    derive_verification_request,
    grant_approval,
    incomparability_reasons,
    lineage_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
)

SRC = "src/main/java/p/A.java"
D_PRE = digest("pre")
D_POST = digest("post")
D_PLAN = digest("plan")
D_EVID = digest("evidence")
D_FIND = digest("finding")


def make_request(revision: str = "rev-1") -> UnitTestRequest:
    return UnitTestRequest.with_defaults(
        project_id="p1",
        source_binding_id="b1",
        source_revision=revision,
        source_files=[SRC],
        percent="80",
        budget=UnitTestBudget(10, 60, 3600),
        allowed_test_paths=["src/test/java"],
        regression_scope=["src/test/java/p/AllTest.java"],
    )


def make_problem(**over: Any) -> TestabilityProblem:
    base: dict[str, Any] = {
        "observable": "A#total sonucu",
        "kind": ProblemKind.AMBIENT_CLOCK_OR_RANDOM,
        "dependency_or_state": "A, Instant.now() cagrisini dogrudan yapar",
        "tried_test_only": ("attempt:ut-1-3 fixture", "attempt:ut-1-5 setup"),
        "finding_digest": D_FIND,
        "source_refs": (f"{SRC}:12",),
    }
    return TestabilityProblem(**{**base, **over})


def make_proposal(**over: Any) -> RefactorProposal:
    base: dict[str, Any] = {
        "origin": RefactorOrigin(
            make_request().request_digest,
            UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED,
            D_EVID,
            D_PLAN,
        ),
        "source_revision": "rev-1",
        "problems": (make_problem(),),
        "technique": "clock-injection",
        "minimal_change": (
            "A'ya Clock parametreli ikinci kurucu ekle; varsayilan kurucu ayni kalir."
        ),
        "impact": (ImpactDeclaration(ReviewArea.API, "A'ya public Clock kurucusu eklenir"),),
        "risk": RiskLevel.LOW,
        "risk_notes": "Yalniz kurucu eklenir",
        "rollback_plan": "Preimage digest'li dosya geri yazilir",
        "verification": VerificationPlan(
            True,
            ("characterization:A#total",),
            ("src/test/java/p/AllTest.java",),
            tuple(ReviewArea),
        ),
        "targets": (ProposalTarget(SRC, D_PRE, D_POST),),
    }
    return RefactorProposal(**{**base, **over})


# --------------------------------------------------------------------------- proposal


def test_proposal_is_exact_scope_and_digest_bound() -> None:
    proposal = make_proposal()
    assert proposal.proposal_digest == make_proposal().proposal_digest
    assert proposal.target_paths == (SRC,)
    changed = make_proposal(technique="dependency-seam")
    assert changed.proposal_digest != proposal.proposal_digest
    other_target = make_proposal(targets=(ProposalTarget(SRC, D_PRE, digest("post2")),))
    assert other_target.proposal_digest != proposal.proposal_digest


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"problems": ()}, "problems-required"),
        ({"targets": ()}, "targets-required"),
        ({"rollback_plan": "  "}, "text-empty"),
        ({"minimal_change": ""}, "text-empty"),
    ],
)
def test_proposal_requires_all_mandatory_parts(override: dict[str, Any], reason: str) -> None:
    with pytest.raises(ProposalInvalid) as info:
        make_proposal(**override)
    assert info.value.reason == reason


def test_problem_needs_tried_test_only_paths_and_source_evidence() -> None:
    with pytest.raises(ProposalInvalid):
        make_problem(tried_test_only=())
    with pytest.raises(ProposalInvalid):
        make_problem(source_refs=())
    with pytest.raises(ValidationFailed):
        make_problem(finding_digest="not-a-digest")


def test_there_is_no_budget_reason_kind_and_free_text_cannot_replace_finding() -> None:
    assert not any("budget" in kind.value for kind in ProblemKind)
    assert not any("budget" in kind.value for kind in OutOfScopeKind)


def test_verification_plan_must_review_all_areas_and_rerun_existing_tests() -> None:
    with pytest.raises(ProposalInvalid):
        VerificationPlan(True, ("c",), (), (ReviewArea.API, ReviewArea.CONFIG))
    with pytest.raises(ProposalInvalid):
        VerificationPlan(False, ("c",), (), tuple(ReviewArea))
    with pytest.raises(ProposalInvalid):
        VerificationPlan(True, (), (), tuple(ReviewArea))


def test_origin_must_be_refactor_budget_or_stagnation_terminal() -> None:
    with pytest.raises(ProposalInvalid):
        RefactorOrigin(
            make_request().request_digest, UnitTestStopReason.TARGET_REACHED, D_EVID, D_PLAN
        )


def test_duplicate_and_case_colliding_targets_are_rejected() -> None:
    upper = "src/main/java/p/a.java"
    with pytest.raises(ProposalInvalid):
        make_proposal(
            targets=(ProposalTarget(SRC, D_PRE, D_POST), ProposalTarget(upper, D_PRE, D_POST))
        )


def test_secret_like_value_in_proposal_is_rejected() -> None:
    with pytest.raises(ProposalInvalid):
        make_proposal(risk_notes='password = "hunter2hunter2"')


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/test/java/p/ATest.java", "test-tree-path"),
        ("pom.xml", "build-or-coverage-config"),
        ("module/pom.xml", "build-or-coverage-config"),
        ("src/main/resources/jacoco-config.xml", "build-or-coverage-config"),
        (".mvn/maven.config", "protected-directory"),
        ("src/main/SETTIN~1.XML", "windows-short-name"),
        ("../outside/A.java", "invalid-path"),
    ],
)
def test_target_guard_rejects_tests_build_config_and_traversal(path: str, reason: str) -> None:
    with pytest.raises(ScopeViolation) as info:
        check_production_target(path)
    assert info.value.reason == reason


def test_target_noop_is_rejected() -> None:
    with pytest.raises(ProposalInvalid):
        ProposalTarget(SRC, D_PRE, D_PRE)


# --------------------------------------------------------------------------- onay


def decision(proposal: RefactorProposal) -> UserApprovalDecision:
    return UserApprovalDecision(proposal.proposal_digest, "chat:turn-17")


def test_approval_binds_proposal_revision_plan_and_targets() -> None:
    proposal = make_proposal()
    approval = grant_approval(proposal, decision(proposal))
    approval.verify_for(
        proposal, current_source_revision="rev-1", current_target_digests={SRC: D_PRE}
    )
    payload = approval.to_payload()
    assert payload["proposal_digest"] == proposal.proposal_digest
    assert payload["plan_digest"] == D_PLAN and payload["source_revision"] == "rev-1"


def test_decision_for_other_digest_is_refused() -> None:
    proposal = make_proposal()
    other = make_proposal(technique="dependency-seam")
    with pytest.raises(ApprovalMismatch) as info:
        grant_approval(proposal, decision(other))
    assert info.value.reason == "decision-for-other-proposal"
    with pytest.raises(ApprovalRefused):
        UserApprovalDecision(proposal.proposal_digest, " ")


def test_approval_does_not_transfer_to_other_proposal_revision_plan_or_target() -> None:
    proposal = make_proposal()
    approval = grant_approval(proposal, decision(proposal))
    ok = {SRC: D_PRE}

    other_proposal = make_proposal(technique="dependency-seam")
    with pytest.raises(ApprovalMismatch) as info:
        approval.verify_for(
            other_proposal, current_source_revision="rev-1", current_target_digests=ok
        )
    assert info.value.reason == "proposal-digest-mismatch"

    with pytest.raises(ApprovalMismatch) as info:
        approval.verify_for(proposal, current_source_revision="rev-2", current_target_digests=ok)
    assert info.value.reason == "source-revision-changed"

    with pytest.raises(ApprovalMismatch) as info:
        approval.verify_for(
            proposal, current_source_revision="rev-1", current_target_digests={SRC: digest("user")}
        )
    assert info.value.reason == "production-digest-changed"

    forged_plan = dataclasses.replace(approval, plan_digest=digest("other-plan"))
    with pytest.raises(ApprovalMismatch) as info:
        forged_plan.verify_for(proposal, current_source_revision="rev-1", current_target_digests=ok)
    assert info.value.reason == "plan-digest-mismatch"

    forged_targets = dataclasses.replace(approval, targets=((SRC, D_PRE, digest("x")),))
    with pytest.raises(ApprovalMismatch) as info:
        forged_targets.verify_for(
            proposal, current_source_revision="rev-1", current_target_digests=ok
        )
    assert info.value.reason == "target-digests-mismatch"

    forged_revision = dataclasses.replace(approval, source_revision="rev-0")
    with pytest.raises(ApprovalMismatch) as info:
        forged_revision.verify_for(
            proposal, current_source_revision="rev-1", current_target_digests=ok
        )
    assert info.value.reason == "approval-source-revision-differs"


@pytest.mark.parametrize("kind", list(OutOfScopeKind))
def test_out_of_scope_proposal_cannot_be_approved(kind: OutOfScopeKind) -> None:
    proposal = make_proposal(out_of_scope=(kind,))
    assert not proposal.approvable
    with pytest.raises(ApprovalRefused) as info:
        grant_approval(proposal, decision(proposal))
    assert info.value.reason == "scope-decision-required"
    # onay nesnesi baska yoldan uretilse bile dogrulama reddeder
    clean = make_proposal()
    forged = dataclasses.replace(
        grant_approval(clean, decision(clean)), proposal_digest=proposal.proposal_digest
    )
    with pytest.raises(ApprovalMismatch) as mismatch:
        forged.verify_for(
            proposal, current_source_revision="rev-1", current_target_digests={SRC: D_PRE}
        )
    assert mismatch.value.reason == "scope-decision-required"


# --------------------------------------------------------------------------- baseline


def baseline(
    counters: tuple[tuple[str, int, int], ...] = ((SRC, 3, 10),),
    *,
    production: str = "p",
    lineage: str | None = None,
    metric: CoverageMetric = CoverageMetric.LINE,
    policy: CoveragePolicy = CoveragePolicy.PER_FILE,
) -> SourceBaseline:
    return SourceBaseline(
        request_digest=make_request().request_digest,
        source_revision="rev-1",
        metric=metric,
        policy=policy,
        production_digests=((SRC, digest(production)),),
        counters=counters,
        measurement_digest=digest("m"),
        lineage_digest=lineage,
    )


def test_gain_is_integer_exact_within_one_baseline() -> None:
    before = baseline(((SRC, 3, 10),))
    after = baseline(((SRC, 7, 10),))
    gain = compare_coverage(before, after)
    assert isinstance(gain, CoverageGain)
    assert gain.covered_delta == 4 and gain.per_file == ((SRC, 4),)
    assert gain.ratio_delta == Fraction(2, 5)


def test_comparison_across_different_denominators_is_refused() -> None:
    old = baseline(((SRC, 8, 10),))  # %80
    refactored = baseline(((SRC, 12, 15),), lineage=digest("lineage"))  # %80, farkli payda
    with pytest.raises(IncomparableBaselines) as info:
        compare_coverage(old, refactored)
    assert "denominator" in str(info.value) and "refactor-lineage" in str(info.value)
    # ayni paydali olsa bile farkli refactor kusagi dogrudan karsilastirilmaz
    same_total = baseline(((SRC, 9, 10),), lineage=digest("lineage"), production="p2")
    reasons = incomparability_reasons(old, same_total)
    assert {"production-digests", "refactor-lineage"} <= set(reasons)
    with pytest.raises(IncomparableBaselines):
        compare_coverage(old, same_total)


@pytest.mark.parametrize(
    "other",
    [
        baseline(metric=CoverageMetric.BRANCH),
        baseline(policy=CoveragePolicy.AGGREGATE),
        baseline(production="changed"),
        baseline(lineage=digest("l")),
    ],
)
def test_any_baseline_identity_difference_blocks_gain(other: SourceBaseline) -> None:
    with pytest.raises(IncomparableBaselines):
        compare_coverage(baseline(), other)


def test_baseline_counters_are_validated_integers() -> None:
    with pytest.raises(ValidationFailed):
        baseline(((SRC, 11, 10),))
    with pytest.raises(ValidationFailed):
        baseline(((SRC, 0, 0),))
    with pytest.raises(ValidationFailed):
        baseline(())


# --------------------------------------------------------------------------- istek turetme


def test_followup_keeps_target_policy_but_is_a_new_request_bound_to_lineage() -> None:
    origin = make_request()
    lineage = digest("lineage")
    followup = derive_followup_request(origin, source_revision="rev-2", lineage=lineage)
    assert followup.request_digest != origin.request_digest
    assert (followup.metric, followup.threshold, followup.policy, followup.budget) == (
        origin.metric,
        origin.threshold,
        origin.policy,
        origin.budget,
    )
    assert followup.source_files == origin.source_files
    assert followup.allowed_test_paths == origin.allowed_test_paths
    assert followup.regression_scope == origin.regression_scope
    assert followup.source_revision == "rev-2"
    assert any(item.startswith("refactor-lineage:") for item in followup.defaults_applied)
    same_revision = derive_followup_request(origin, source_revision="rev-1", lineage=lineage)
    assert same_revision.request_digest != origin.request_digest


def test_verification_request_is_distinct_per_proposal_and_approval() -> None:
    origin = make_request()
    first = derive_verification_request(
        origin, proposal_digest=digest("p1"), approval_digest=digest("a1")
    )
    second = derive_verification_request(
        origin, proposal_digest=digest("p1"), approval_digest=digest("a2")
    )
    assert len({origin.request_digest, first.request_digest, second.request_digest}) == 3


def test_lineage_digest_binds_all_inputs() -> None:
    args: dict[str, Any] = {
        "origin_request_digest": digest("o"),
        "proposal_digest": digest("p"),
        "approval_digest": digest("a"),
        "verification_request_digest": digest("v"),
        "post_target_digests": [(SRC, D_POST)],
        "old_baseline_id": digest("b"),
    }
    base = lineage_digest(**args)
    assert lineage_digest(**args) == base
    assert lineage_digest(**{**args, "post_target_digests": [(SRC, digest("z"))]}) != base
    assert lineage_digest(**{**args, "old_baseline_id": None}) != base


def test_payloads_never_claim_behavioral_equivalence() -> None:
    forbidden = ("equival", "mathemat", "identical", "same_behavior", "behavior_preserved")
    proposal = make_proposal()
    approval = grant_approval(proposal, decision(proposal))
    text = json.dumps(
        [proposal.to_payload(), approval.to_payload(), baseline().to_payload()]
    ).lower()
    assert not any(word in text for word in forbidden)
