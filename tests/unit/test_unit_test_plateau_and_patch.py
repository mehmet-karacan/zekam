"""W05: plateau, butce/onay zarfi, test-only patch korumalari ve atomik calisma alani."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_loop_support import SRC, default_plan

from zekam.application.unit_test_patch import (
    CandidateManifest,
    FileChange,
    GuardViolation,
    PreimageMismatch,
    SmellCode,
    TestOnlyPolicy,
    TestPatch,
    accepted_state_digest,
    find_smell_candidates,
    has_executable_test,
    parse_patch,
)
from zekam.application.unit_test_plan import ScenarioBoard, parse_behavior_plan
from zekam.application.unit_test_plateau import (
    STAGNATION_MESSAGE,
    AttemptOutcomeKind,
    AttemptRecord,
    BudgetMeter,
    LoopLimits,
    PlateauActionKind,
    PlateauPolicy,
    ProgressGain,
    ProgressKind,
    StrategyKind,
    UnitTestApprovals,
    approach_digest,
    detect_plateau,
    plan_plateau_response,
    state_gap_digest,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
from zekam.infrastructure.unit_test_runner.patch_workspace import FileSystemTestPatchWorkspace

GAP_A, GAP_B = digest("gap-a"), digest("gap-b")


def rec(
    n: int,
    approach: str,
    *,
    gap: str = GAP_A,
    outcome: AttemptOutcomeKind = AttemptOutcomeKind.REJECTED,
    gains: tuple[ProgressGain, ...] = (),
    **flags: bool,
) -> AttemptRecord:
    return AttemptRecord(n, outcome, digest(approach), gap, gains, **flags)


POLICY = PlateauPolicy(window=3)


def respond(history: tuple[AttemptRecord, ...], **kw: Any) -> Any:
    signal = detect_plateau(history, POLICY)
    assert signal is not None
    args: dict[str, Any] = {
        "applied_remedies": frozenset(),
        "tried_strategies": frozenset(),
        "attempts_remaining": 5,
        "discovery_used": 0,
        "policy": POLICY,
    } | kw
    return plan_plateau_response(signal, history, **args)


# ------------------------------------------------------------------------------ plateau


def test_plateau_needs_same_gap_no_verified_gain_and_repeated_failed_approach() -> None:
    repeated = tuple(rec(i, "same") for i in range(1, 4))
    assert detect_plateau(repeated, POLICY) is not None
    # rounded yuzde degil: gap degisti -> plateau yok
    moved = (*repeated[:2], rec(3, "same", gap=GAP_B))
    assert detect_plateau(moved, POLICY) is None
    # dogrulanmis kazanim -> plateau yok
    gain = (ProgressGain(ProgressKind.VERIFIED_SCENARIO, digest("e")),)
    assert detect_plateau((*repeated[:2], rec(3, "same", gains=gain)), POLICY) is None
    # her seferinde farkli yaklasim (tekrar yok) -> plateau degil
    assert detect_plateau(tuple(rec(i, f"a{i}") for i in range(1, 4)), POLICY) is None
    # pencere dolmadan karar yok
    assert detect_plateau(repeated[:2], POLICY) is None


def test_plateau_checks_stale_scope_and_context_before_changing_strategy_once_per_gap() -> None:
    history = (rec(1, "x", stale_measurement=True), rec(2, "x"), rec(3, "x"))
    first = respond(history)
    assert first.kind is PlateauActionKind.REMEASURE_FRESH and first.remedy_key
    again = respond(history, applied_remedies=frozenset({first.remedy_key}))
    assert again.kind is PlateauActionKind.CHANGE_STRATEGY
    scope = respond((rec(1, "x"), rec(2, "x", scope_suspect=True), rec(3, "x")))
    assert scope.kind is PlateauActionKind.RECHECK_SCOPE
    ctx = respond((rec(1, "x"), rec(2, "x"), rec(3, "x", context_incomplete=True)))
    assert ctx.kind is PlateauActionKind.REFRESH_CONTEXT


def test_plateau_tries_different_strategies_then_stops_honestly() -> None:
    history = tuple(rec(i, "x") for i in range(1, 4))
    first = respond(history, tried_strategies=frozenset({StrategyKind.ALTERNATE_FIXTURE}))
    assert first.kind is PlateauActionKind.CHANGE_STRATEGY
    assert first.strategy is StrategyKind.INPUT_PARTITION
    all_tried = frozenset(StrategyKind)
    stop = respond(history, tried_strategies=all_tried)
    assert stop.kind is PlateauActionKind.STOP_STAGNATION and stop.message == STAGNATION_MESSAGE
    assert stop.production_impossibility_claim is False
    no_budget = respond(history, attempts_remaining=0)
    assert no_budget.kind is PlateauActionKind.STOP_STAGNATION


def test_setup_discovery_has_its_own_bounded_budget() -> None:
    history = tuple(rec(i, "x") for i in range(1, 4))
    tried = frozenset(StrategyKind) - {StrategyKind.SETUP_DISCOVERY}
    allowed = respond(history, tried_strategies=tried, discovery_used=0)
    assert allowed.strategy is StrategyKind.SETUP_DISCOVERY
    spent = respond(history, tried_strategies=tried, discovery_used=1)
    assert spent.kind is PlateauActionKind.STOP_STAGNATION


def test_progress_gain_requires_evidence_and_approach_identity_ignores_content() -> None:
    with pytest.raises(ValidationFailed):
        ProgressGain(ProgressKind.NEW_COVERED_COUNT, "novel sentence")
    one = approach_digest(StrategyKind.INPUT_PARTITION, ["S2", "S1"], ["b", "a"])
    two = approach_digest(StrategyKind.INPUT_PARTITION, ["S1", "S2"], ["a", "b"])
    assert one == two != approach_digest(StrategyKind.MOCK_BOUNDARY, ["S1", "S2"], ["a", "b"])


def test_gap_digest_changes_with_counters_or_unresolved_risks_only() -> None:
    request = UnitTestRequest.with_defaults(
        project_id="p",
        source_binding_id="b",
        source_revision="r",
        source_files=[SRC],
        percent="80",
        budget=UnitTestBudget(3, 10, 100),
    )
    plan = parse_behavior_plan(default_plan(), request, version=1)
    board = ScenarioBoard.initial(plan)
    base = state_gap_digest({SRC: (3, 10)}, plan, board)
    assert base == state_gap_digest({SRC: (3, 10)}, plan, board)
    assert base != state_gap_digest({SRC: (4, 10)}, plan, board)
    assert AttemptRecord.from_payload(rec(1, "x").to_payload()) == rec(1, "x")


# ------------------------------------------------------------------------------ butce / onay


def test_budget_dimensions_are_checked_independently() -> None:
    budget = UnitTestBudget(3, 30, 100)
    limits = LoopLimits(max_output_bytes=100, max_remote_calls=2)
    meter = BudgetMeter()
    assert meter.exceeded(budget, limits, attempts_used=1) == ()
    assert meter.exceeded(budget, limits, attempts_used=3) == ("attempts",)
    meter.elapsed_seconds = 100
    meter.output_bytes = 100
    meter.remote_calls = 2
    assert set(meter.exceeded(budget, limits, attempts_used=0)) == {
        "elapsed",
        "output",
        "remote-calls",
    }
    assert BudgetMeter.from_payload(meter.to_payload()).output_bytes == 100


def test_approval_envelope_separates_permissions_and_avoids_reprompt_inside_range() -> None:
    budget = UnitTestBudget(5, 30, 600)
    limits = LoopLimits()
    approvals = UnitTestApprovals(
        write_tests=True, build=True, approved_budget=budget, approved_limits=limits
    )
    assert approvals.missing_for_run(remote_agents=False) == ()
    assert approvals.missing_for_run(remote_agents=True) == ("remote_model",)
    assert UnitTestApprovals().missing_for_run(remote_agents=False) == ("write_tests", "build")
    assert not approvals.budget_increase_needs_approval(UnitTestBudget(4, 30, 500), limits)
    assert approvals.budget_increase_needs_approval(UnitTestBudget(6, 30, 600), limits)
    assert approvals.budget_increase_needs_approval(budget, LoopLimits(max_output_bytes=10**9))
    assert UnitTestApprovals(write_tests=True, build=True).budget_increase_needs_approval(
        budget, limits
    )
    assert approvals.production_change is False and approvals.dependency_change is False
    with pytest.raises(ValidationFailed):
        LoopLimits(batch_size=0)


# ------------------------------------------------------------------------------ patch guard


def policy(**kw: Any) -> TestOnlyPolicy:
    request = UnitTestRequest.with_defaults(
        project_id="p",
        source_binding_id="b",
        source_revision="r",
        source_files=[SRC],
        percent="80",
        budget=UnitTestBudget(3, 10, 100),
        allowed_test_paths=["src/test/java"],
        forbidden_paths=["src/test/java/p/Locked.java"],
    )
    return TestOnlyPolicy.for_request(request, **kw)


T = "src/test/java/p/FooTest.java"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (SRC, "production-path"),
        ("mod/src/main/java/q/B.java", "production-path"),
        ("pom.xml", "build-or-coverage-config"),
        ("mod/pom.xml", "build-or-coverage-config"),
        ("src/test/java/p/jacoco.exec", "build-or-coverage-config"),
        ("src/test/java/p/SurefireConfig.java", "build-or-coverage-config"),
        (".mvn/maven.config", "protected-directory"),
        (".github/workflows/x.java", "protected-directory"),
        ("src/test/java/p/verifier/V.java", "protected-directory"),
        ("src/test/java/p/Locked.java", "forbidden-or-verifier-asset"),
        ("src/test/java/p/Frozen.java", "reproducer-frozen"),
        ("scripts/Run.java", "outside-allowed-test-paths"),
    ],
)
def test_test_only_policy_blocks_production_build_coverage_verifier_and_frozen(
    path: str, reason: str
) -> None:
    p = policy(frozen_paths=["src/test/java/p/Frozen.java"])
    with pytest.raises(GuardViolation) as raised:
        p.check_path(path, modifies_existing=False)
    assert raised.value.reason == reason


def test_test_only_policy_allows_clean_new_test_and_protects_dirty_user_files() -> None:
    p = policy(protected_dirty_paths=[T])
    p.check_path("src/test/java/p/BarTest.java", modifies_existing=False)
    p.check_path(T, modifies_existing=False)  # yeni dosya olusturmak dirty degil
    with pytest.raises(GuardViolation) as raised:
        p.check_path(T, modifies_existing=True)
    assert raised.value.reason == "user-dirty-path"
    default_layout = TestOnlyPolicy(frozenset({SRC}))
    default_layout.check_path("m/src/test/java/p/X.java", modifies_existing=False)
    with pytest.raises(GuardViolation):
        default_layout.check_path("m/src/test/resources/x.txt", modifies_existing=False)


def test_patch_bounds_and_strict_parse() -> None:
    with pytest.raises(PolicyViolation):
        TestPatch(tuple(FileChange(f"src/test/java/p/T{i}.java", "x", None) for i in range(21)))
    with pytest.raises(ValidationFailed):
        TestPatch(
            (
                FileChange("src/test/java/p/A.java", "x", None),
                FileChange("src/test/java/p/a.java", "y", None),
            )
        )
    with pytest.raises(PolicyViolation):
        FileChange("src/test/java/p/Big.java", "x" * (300 * 1024), None)
    with pytest.raises(ValidationFailed):
        FileChange("../escape.java", "x", None)
    with pytest.raises(ValidationFailed):
        parse_patch({"files": [], "extra": 1})
    with pytest.raises(ValidationFailed):
        parse_patch(
            {"files": [{"path": "src/test/java/A.java", "content": 5, "preimage_digest": None}]}
        )
    ok = parse_patch(
        {"files": [{"path": "src/test/java/A.java", "content": "x", "preimage_digest": None}]}
    )
    assert ok.paths == ("src/test/java/A.java",)


def test_static_smells_are_candidates_and_print_only_is_not_an_executable_test() -> None:
    def patch(text: str) -> TestPatch:
        return TestPatch((FileChange("src/test/java/p/S.java", text, None),))

    good = patch("@Test void t() { assertEquals(1, f()); }")
    assert has_executable_test(good) and find_smell_candidates(good) == ()
    codes = {
        c.code
        for c in find_smell_candidates(
            patch("@Test void t() { Thread.sleep(5); assertTrue(true); m.setAccessible(true); }")
        )
    }
    assert codes == {SmellCode.SLEEP, SmellCode.TAUTOLOGY, SmellCode.REFLECTION}
    printer = patch('@Test void t() { System.out.println("x"); }')
    assert not has_executable_test(printer)
    assert [c.code for c in find_smell_candidates(printer)] == [SmellCode.PRINT_ONLY]
    assert [
        c.code for c in find_smell_candidates(patch("@Disabled @Test void t(){assertEquals(1,2);}"))
    ] == [SmellCode.DISABLED]


def test_candidate_manifest_binds_parent_state_and_post_images() -> None:
    patch = TestPatch((FileChange(T, "a", None),))
    parent = accepted_state_digest({})
    manifest = CandidateManifest.for_patch(parent, patch)
    assert manifest.changed_files == ((T, digest_of_bytes(b"a")),)
    assert CandidateManifest.from_payload(manifest.to_payload()) == manifest
    other = CandidateManifest.for_patch(accepted_state_digest({T: digest("x")}), patch)
    assert other.candidate_digest != manifest.candidate_digest


# ------------------------------------------------------------------------------ workspace


def test_workspace_apply_is_atomic_and_preimage_checked(tmp_path: Path) -> None:
    ws = FileSystemTestPatchWorkspace(tmp_path)
    existing = tmp_path / "src/test/java/p/Old.java"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    old = digest_of_bytes(b"old")
    patch = TestPatch(
        (
            FileChange("src/test/java/p/Old.java", "new", old),
            FileChange("src/test/java/p/New.java", "n", None),
        )
    )
    applied = ws.apply(patch)
    assert existing.read_bytes() == b"new" and (tmp_path / "src/test/java/p/New.java").exists()
    assert {f.path for f in applied.files} == set(patch.paths)
    # preimage uyusmazligi: hicbir sey yazilmaz
    stale = TestPatch(
        (
            FileChange("src/test/java/p/Other.java", "o", None),
            FileChange("src/test/java/p/Old.java", "again", old),
        )
    )
    with pytest.raises(PreimageMismatch):
        ws.apply(stale)
    assert not (tmp_path / "src/test/java/p/Other.java").exists()
    assert existing.read_bytes() == b"new"
    # var olan dosyayi 'yeni' diye ezmek de reddedilir
    with pytest.raises(PreimageMismatch):
        ws.apply(TestPatch((FileChange("src/test/java/p/Old.java", "z", None),)))


def test_workspace_mid_write_failure_rolls_back_already_applied_files(tmp_path: Path) -> None:
    ws = FileSystemTestPatchWorkspace(tmp_path)
    (tmp_path / "src/test/java/p").mkdir(parents=True)
    blocker = tmp_path / "src/test/java/p/z"
    blocker.write_text("i am a file", encoding="utf-8")
    patch = TestPatch(
        (
            FileChange("src/test/java/p/A.java", "a", None),
            FileChange("src/test/java/p/z/B.java", "b", None),
        )
    )
    with pytest.raises(OSError):
        ws.apply(patch)
    assert not (tmp_path / "src/test/java/p/A.java").exists()
    assert blocker.read_text(encoding="utf-8") == "i am a file"


def test_workspace_revert_restores_only_own_changes_and_leaves_foreign_edits(
    tmp_path: Path,
) -> None:
    ws = FileSystemTestPatchWorkspace(tmp_path)
    base = tmp_path / "src/test/java/p"
    base.mkdir(parents=True)
    (base / "Old.java").write_bytes(b"old")
    (base / "Mine.java").write_bytes(b"mine-before")
    patch = TestPatch(
        (
            FileChange("src/test/java/p/Old.java", "new", digest_of_bytes(b"old")),
            FileChange("src/test/java/p/Mine.java", "mine-after", digest_of_bytes(b"mine-before")),
            FileChange("src/test/java/p/New.java", "n", None),
        )
    )
    applied = ws.apply(patch)
    preimages = ws.read_bytes([])
    backup = {"src/test/java/p/Old.java": b"old", "src/test/java/p/Mine.java": b"mine-before"}
    assert preimages == {}
    (base / "Mine.java").write_bytes(b"USER EDIT AFTER APPLY")
    receipt = ws.revert(applied, preimages=backup)
    assert (base / "Old.java").read_bytes() == b"old" and not (base / "New.java").exists()
    assert (base / "Mine.java").read_bytes() == b"USER EDIT AFTER APPLY"
    assert receipt.foreign_modified == ("src/test/java/p/Mine.java",) and not receipt.clean
    assert sorted(receipt.restored) == ["src/test/java/p/New.java", "src/test/java/p/Old.java"]
    again = ws.revert(applied, preimages=backup)
    assert again.restored == () and "src/test/java/p/Old.java" in again.already_original
    wrong = ws.apply(
        TestPatch((FileChange("src/test/java/p/Old.java", "x", digest_of_bytes(b"old")),))
    )
    broken = ws.revert(wrong, preimages={"src/test/java/p/Old.java": b"tampered"})
    assert broken.failed == ("src/test/java/p/Old.java",)


def test_workspace_rejects_traversal_and_reports_digests(tmp_path: Path) -> None:
    ws = FileSystemTestPatchWorkspace(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"x")
    assert ws.digest_paths(["a.txt", "missing.txt"]) == {
        "a.txt": digest_of_bytes(b"x"),
        "missing.txt": None,
    }
    with pytest.raises(ValidationFailed):
        ws.digest_paths(["../outside"])
