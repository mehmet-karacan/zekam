"""W05: agentic unit-test dongusu.

REPLAY/MOCK: istemci adapter'i, Maven olcumu, ortam gozlemcisi. GERCEK: SQLite ledger,
yerel CAS, dosya sistemi patch calisma alani, canonical dispatch gateway.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_loop_support import (
    SRC,
    TEST_DIR,
    Harness,
    builder_files,
    default_builder,
    default_planner,
    default_verifier,
    make_test_file,
    scenario_doc,
)

from zekam.application.unit_test_agents import AgentSpecialty
from zekam.application.unit_test_measurement import RunStatus
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.domain.clients import DispatchOutcome
from zekam.domain.unit_test_engineering import (
    AttemptState,
    UnitTestBudget,
    UnitTestStopReason,
)

S1_PATH = f"{TEST_DIR}/S1Test.java"
S2_PATH = f"{TEST_DIR}/S2Test.java"


def evidence(h: Harness, digest_value: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(h.objects.get(digest_value).decode("utf-8"))
    return loaded


def attempt_evidence(h: Harness) -> list[dict[str, Any]]:
    result = []
    for attempt in h.ledger.list_attempts(h.request.request_digest):
        receipt = h.ledger.get_attempt_receipt(attempt.attempt_id)
        assert receipt is not None, "receipt'siz attempt kalmamali"
        result.append({**evidence(h, receipt.evidence_digest), "status": receipt.status})
    return result


def terminal_state(h: Harness, terminal_evidence: str | None) -> dict[str, Any]:
    assert terminal_evidence is not None
    state: dict[str, Any] = evidence(h, terminal_evidence)["state"]
    return state


def builder_with(per_call: Mapping[int, str]) -> Any:
    def handler(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        extra = per_call.get(call)
        if extra is not None:
            files = builder_files(context, extra=extra)
            owned = dict(context.get("owned_files", []))
            for item in files:
                item["preimage_digest"] = owned.get(item["path"])
            base["patch"] = {"files": files}
        return base

    return handler


# --------------------------------------------------------------------------- temel akis


def test_replay_happy_path_reaches_target_with_independent_verification(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail
    assert outcome.machine_status() == {
        "status": "passed",
        "reason": "target-reached",
        "final": True,
    }
    assert outcome.terminal.receipt_digest is not None
    assert h.calls(AgentSpecialty.ANALYZER_PLANNER) == 1
    assert h.calls(AgentSpecialty.FINAL_VERIFIER) == 1
    assert outcome.needs_specification == ("S3",)
    # needs-specification olan S3 digerlerini durdurmadi ve builder'a verilmedi
    for context in h.adapters[AgentSpecialty.BUILDER].contexts:
        assert "S3" not in {s["scenario_id"] for s in context["scenarios"]}
    kinds = [a["evidence"]["kind"] for a in attempt_evidence(h)]
    assert kinds == ["baseline", "build"], "kendi accepted patch'i drift/rebaseline uretmemeli"
    assert {S1_PATH, S2_PATH} <= h.tree()
    assert h.measurer.runs[1]["timeout"] <= h.request.budget.process_timeout_seconds


def test_replay_canonical_dispatch_is_assignment_first_and_builder_is_not_verifier(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.run()
    for index, event in enumerate(h.events):
        if event.startswith("adapter:"):
            assert h.events[index - 1] == "store:invocation"
            assert h.events[index - 2].startswith("store:assignment:")
    adapter_calls = sum(1 for e in h.events if e.startswith("adapter:"))
    assert len(h.store.results) == adapter_calls and adapter_calls >= 4
    roles = {a.agent_ref: a.role.value for a in h.store.assignments}
    assert roles["unit-test-analyzer-planner"] == "researcher"
    assert roles["unit-test-builder"] == "builder"
    assert roles["unit-test-verifier"] == "verifier"
    assert roles["unit-test-final-verifier"] == "verifier"
    assert all(
        a.is_child and a.parent_assignment_id == h.work.coordinator_assignment_id
        for a in h.store.assignments
    )
    builder_ids = {a.id for a in h.store.assignments if a.role.value == "builder"}
    verifier_ids = {a.id for a in h.store.assignments if a.role.value == "verifier"}
    assert builder_ids.isdisjoint(verifier_ids)
    builder_invocations = {i.id for i in h.store.invocations if i.assignment_id in builder_ids}
    verifier_invocations = {i.id for i in h.store.invocations if i.assignment_id in verifier_ids}
    assert builder_invocations.isdisjoint(verifier_invocations)


def test_replay_claim_precedes_effect_and_patch_is_applied_at_measurement(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    seen: dict[int, tuple[tuple[str, ...], set[str]]] = {}

    def hook(number: int) -> None:
        seen[number] = (h.ledger.unreceipted_attempts(h.request.request_digest), h.tree())

    h.measurer.on_measure = hook
    h.run()
    claimed, tree = seen[2]
    assert len(claimed) == 1 and claimed[0].endswith("-2")
    assert {S1_PATH, S2_PATH} <= tree


def test_replay_already_at_target_does_no_agent_work(tmp_path: Path) -> None:
    h = Harness(tmp_path, percent="30")
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.ALREADY_AT_TARGET
    assert outcome.machine_status()["status"] == "passed"
    assert all(a.calls == 0 for a in h.adapters.values())
    assert h.tree() == {f"{TEST_DIR}/ExistingTest.java"}
    assert evidence(h, outcome.terminal.evidence_digest or "")["detail"] == [
        "quality-not-independently-verified"
    ]


def test_replay_final_terminal_is_not_reexecuted_on_second_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.run()
    runs, calls = len(h.measurer.runs), sum(a.calls for a in h.adapters.values())
    second = h.run()
    assert second.terminal == first.terminal and second.executed_attempts == 0
    assert (len(h.measurer.runs), sum(a.calls for a in h.adapters.values())) == (runs, calls)


# --------------------------------------------------------------------------- izinler / butce


def test_replay_missing_permissions_block_before_any_effect(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    outcome = h.run(approvals=UnitTestApprovals())
    assert outcome.terminal.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING
    assert set(outcome.detail) == {"permission-missing:write_tests", "permission-missing:build"}
    assert h.measurer.runs == [] and all(a.calls == 0 for a in h.adapters.values())


def test_replay_remote_agents_need_their_own_permission(tmp_path: Path) -> None:
    h = Harness(tmp_path, remote=True)
    approvals = UnitTestApprovals(
        write_tests=True, build=True, approved_budget=h.request.budget, approved_limits=h.limits
    )
    outcome = h.run(approvals=approvals)
    assert outcome.detail == ("permission-missing:remote_model",)
    assert h.measurer.runs == []


def test_replay_budget_beyond_approved_range_needs_new_approval_inside_range_does_not(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    smaller = UnitTestApprovals(
        write_tests=True,
        build=True,
        remote_model=True,
        approved_budget=UnitTestBudget(2, 60, 3600),
        approved_limits=h.limits,
    )
    blocked = h.run(approvals=smaller)
    assert blocked.detail == ("budget-increase-needs-approval",) and h.measurer.runs == []
    h2 = Harness(tmp_path / "b")
    assert h2.run().terminal.stop_reason is UnitTestStopReason.TARGET_REACHED


def test_replay_attempt_budget_exhaustion_is_not_a_refactor_requirement(tmp_path: Path) -> None:
    def reject(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_verifier(context, call))
        base["verdict"] = "rejected"
        base["quality_findings"] = [{"code": "weak-assertion"}]
        return base

    h = Harness(tmp_path, max_attempts=3, handlers={AgentSpecialty.VERIFIER: reject})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.BUDGET_EXHAUSTED
    assert outcome.machine_status() == {
        "status": "budget-exhausted",
        "reason": "budget-exhausted",
        "final": True,
    }
    assert h.tree() == {f"{TEST_DIR}/ExistingTest.java"}, "reddedilen adaylar geri alindi"


# --------------------------------------------------------------------------- test-only korumalar


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("pom.xml", "build-or-coverage-config"),
        (SRC, "production-path"),
        (f"{TEST_DIR}/jacoco-rules.xml", "build-or-coverage-config"),
        (".mvn/jvm.config", "build-or-coverage-config"),
        ("src/test/java/p/verifier/Check.java", "protected-directory"),
        ("docs/NotATest.java", "outside-allowed-test-paths"),
    ],
)
def test_replay_test_only_mode_rejects_forbidden_targets_without_touching_them(
    tmp_path: Path, path: str, reason: str
) -> None:
    def evil(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        base["patch"] = {"files": [{"path": path, "content": "x", "preimage_digest": None}]}
        return base

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: evil})
    before = (h.root / SRC).read_bytes()
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.STAGNATION_REVIEW
    assert "builder-policy-violations" in outcome.detail and reason in outcome.detail
    assert (h.root / SRC).read_bytes() == before
    assert path == SRC or not (h.root / path).exists()
    assert len(h.ledger.list_attempts(h.request.request_digest)) == 1  # yalniz baseline
    assert len(h.measurer.runs) == 1


def test_replay_user_dirty_file_is_never_modified(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    existing = f"{TEST_DIR}/ExistingTest.java"
    h.environment.dirty = frozenset({existing})
    original = (h.root / existing).read_bytes()

    def edit_dirty(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        digest_value = __import__("zekam.domain.canonical", fromlist=["x"]).digest_of_bytes(
            original
        )
        base["patch"] = {
            "files": [
                {
                    "path": existing,
                    "content": "// hijack\n@Test assertEquals(1,1)",
                    "preimage_digest": digest_value,
                }
            ]
        }
        return base

    h2 = Harness(tmp_path / "b", handlers={AgentSpecialty.BUILDER: edit_dirty})
    h2.environment.dirty = frozenset({existing})
    outcome = h2.run()
    assert "user-dirty-path" in outcome.detail
    assert (h2.root / existing).read_bytes() == original


def test_replay_rejected_candidate_rolls_back_only_own_change_and_keeps_user_edit(
    tmp_path: Path,
) -> None:
    def user_edits_then_reject(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_verifier(context, call))
        if call == 1:
            target = h.root / S1_PATH
            target.write_text(
                target.read_text(encoding="utf-8") + "// user edit\n", encoding="utf-8"
            )
            base["verdict"] = "rejected"
        return base

    h = Harness(
        tmp_path, max_attempts=3, handlers={AgentSpecialty.VERIFIER: user_edits_then_reject}
    )
    h.run()
    assert "// user edit" in (h.root / S1_PATH).read_text(encoding="utf-8")
    assert not (h.root / S2_PATH).exists(), "gorevin kendi dosyasi geri alindi"
    second = attempt_evidence(h)[1]["evidence"]["revert"]
    assert second["foreign_modified"] == [S1_PATH] and second["restored"] == [S2_PATH]


# --------------------------------------------------------------------------- hata siniflari


def test_replay_compile_failure_uses_builder_only_repair_without_replanning(tmp_path: Path) -> None:
    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder_with({1: "// FAIL:compile\n"})})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED
    assert h.calls(AgentSpecialty.ANALYZER_PLANNER) == 1 and h.calls(AgentSpecialty.BUILDER) == 2
    first = attempt_evidence(h)[1]
    assert first["status"] is AttemptState.FAILED
    assert first["evidence"]["classification"]["kind"] == "compile"
    notes = h.adapters[AgentSpecialty.BUILDER].contexts[1]["failure_notes"]
    assert any(n.startswith("compile:") for values in notes.values() for n in values)


def test_replay_genuine_production_defect_is_preserved_and_never_called_success(
    tmp_path: Path,
) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        files = builder_files(context)
        for item in files:
            if item["path"] == S1_PATH and call == 1:
                item["content"] = "// FAIL:assertion\n" + item["content"]
        if call == 2:
            # "duzeltme": bug'li cikti expected yapilmis S1 yeniden yaratilmaya calisilir
            files = [make_test_file("S1", 0), *[f for f in files if f["path"] == S2_PATH]]
        base["patch"] = {"files": files}
        return base

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.PRODUCTION_DEFECT, outcome.detail
    assert outcome.machine_status()["reason"] == "production-defect"
    assert outcome.machine_status()["status"] != "passed"
    assert len(outcome.defect_proposals) == 1
    proposal = outcome.defect_proposals[0]
    assert proposal["scenario_ids"] == ["S1"] and proposal["grants_authority"] is False
    assert proposal["reproducer_paths"] and proposal["oracle_refs"] == ["spec#1"]
    assert (h.root / S1_PATH).exists() is False, (
        "kirmizi reproducer kullanici suite'inde birakilmadi"
    )
    # reproducer artifact'i korunur
    assert h.objects.exists(proposal["reproducer_candidate_digest"])
    state = terminal_state(h, outcome.terminal.evidence_digest)
    assert S1_PATH in state["frozen_paths"]
    notes = dict(state["failure_notes"])
    assert any("reproducer-frozen" in n for values in notes.values() for n in values)
    assert S2_PATH in h.tree()
    assert outcome.terminal.receipt_digest is None


def test_replay_triage_test_wrong_means_repair_not_defect(tmp_path: Path) -> None:
    def triage(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        del call
        return {
            "verdict": "rejected",
            "defect_review": {
                "scenario_id": context["scenarios"][0]["scenario_id"],
                "finding": "test-wrong",
                "evidence_refs": ["spec#1"],
            },
        }

    h = Harness(
        tmp_path,
        handlers={
            AgentSpecialty.BUILDER: builder_with({1: "// FAIL:assertion\n"}),
            AgentSpecialty.DEFECT_TRIAGE: triage,
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED
    assert outcome.defect_proposals == ()


def test_replay_flaky_failure_is_diagnosed_with_fixed_repeats_not_retried_until_green(
    tmp_path: Path,
) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        files = builder_files(context)
        for item in files:
            if item["path"] == S1_PATH:
                item["content"] = "// FLAKY\n" + item["content"]
        base["patch"] = {"files": files}
        return base

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder})
    outcome = h.run()
    assert outcome.terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED
    flaky_attempt = attempt_evidence(h)[1]
    repeats = flaky_attempt["evidence"]["repeats"]
    assert [r[1] for r in repeats["runs"]] == ["initial", "same-order", "same-order"]
    assert [r[2] for r in repeats["runs"]] == ["failed", "passed", "passed"] and repeats["mixed"]
    assert flaky_attempt["evidence"]["classification"]["kind"] == "flaky"
    assert flaky_attempt["status"] is AttemptState.FAILED
    # 1 baseline + 1 aday + flaky_repeats tani kosusu; yesile kadar retry yok
    assert [r["attempt_id"] for r in h.measurer.runs].count(
        "ut-" + h.request.request_digest[7:23] + "-2"
    ) == 3
    state = terminal_state(h, outcome.terminal.evidence_digest)
    rows = {r[0]: r[2] for r in state["board"]}
    assert rows["S1"] in {"flaky-quarantined", "unresolved"}
    assert S1_PATH not in h.tree()


def test_replay_environment_failure_stops_and_reverts_candidate(tmp_path: Path) -> None:
    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder_with({1: "// FAIL:env\n"})})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING
    assert h.tree() == {f"{TEST_DIR}/ExistingTest.java"}


def test_replay_unknown_failure_escalates_instead_of_guessing(tmp_path: Path) -> None:
    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder_with({1: "// FAIL:unknown\n"})})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.STAGNATION_REVIEW
    assert "unknown-failure-escalation" in outcome.detail


# --------------------------------------------------------------------------- plateau


def test_replay_plateau_changes_strategy_then_stops_without_impossibility_claim(
    tmp_path: Path,
) -> None:
    def reject(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_verifier(context, call))
        base["verdict"] = "rejected"
        base["quality_findings"] = [{"code": "weak-assertion"}]
        return base

    limits = LoopLimits(batch_size=3, max_repair_per_scenario=50, max_replans=3)
    h = Harness(
        tmp_path, max_attempts=60, limits=limits, handlers={AgentSpecialty.VERIFIER: reject}
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.STAGNATION_REVIEW
    assert "bu scope, yontem ve butceyle ek ilerleme bulunamadi" in outcome.detail
    required = [c["required_strategy"] for c in h.adapters[AgentSpecialty.BUILDER].contexts]
    assert any(r is not None for r in required), "farkli strateji denendi"
    assert not any("imkansiz" in d or "impossible" in d for d in outcome.detail)


# --------------------------------------------------------------------------- cancel/pause/resume


def test_replay_cancel_during_measurement_keeps_ownership_and_blocks_new_attempts(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.measurer.on_measure = lambda number: h.control.request_cancel() if number == 2 else None
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.USER_CANCELLED
    assert outcome.terminal.final and outcome.machine_status()["reason"] == "user-cancelled"
    last = attempt_evidence(h)[-1]
    assert last["status"] is AttemptState.INTERRUPTED
    readback = dict(tuple(item) for item in last["evidence"]["ownership_readback"])
    assert set(readback) == {S1_PATH, S2_PATH} and all(readback.values())
    assert {S1_PATH, S2_PATH} <= h.tree(), "cancel sonrasi patch sahipligi kanitla korunur"
    runs = len(h.measurer.runs)
    again = h.run()
    assert again.terminal == outcome.terminal and len(h.measurer.runs) == runs


def test_replay_process_timeout_is_incomplete_and_starts_no_new_attempt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.measurer.status_for[2] = RunStatus.TIMED_OUT
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.MEASUREMENT_INCOMPLETE
    assert len(h.ledger.list_attempts(h.request.request_digest)) == 2
    assert attempt_evidence(h)[-1]["status"] is AttemptState.INTERRUPTED


def test_replay_pause_then_resume_does_not_reexecute_completed_attempts(tmp_path: Path) -> None:
    limits = LoopLimits(batch_size=1)
    h = Harness(tmp_path, limits=limits)
    h.measurer.on_measure = lambda number: h.control.request_pause() if number == 2 else None
    paused = h.run()
    assert paused.terminal.stop_reason is UnitTestStopReason.USER_PAUSED
    assert not paused.terminal.final and paused.machine_status()["final"] is False
    assert h.calls(AgentSpecialty.ANALYZER_PLANNER) == 1
    done_runs = [r["attempt_id"] for r in h.measurer.runs]
    h.control.clear_pause()
    h.measurer.on_measure = None
    resumed = h.run()
    assert resumed.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, resumed.detail
    all_runs = [r["attempt_id"] for r in h.measurer.runs]
    assert all_runs[: len(done_runs)] == done_runs
    for attempt_id in set(done_runs):
        assert all_runs.count(attempt_id) == 1, "tamamlanan attempt yeniden calistirilmadi"
    assert h.calls(AgentSpecialty.ANALYZER_PLANNER) == 1, "plan resume'da kanittan okundu"
    kinds = [t.stop_reason for t in h.ledger.list_terminals(h.request.request_digest)]
    assert kinds == [UnitTestStopReason.USER_PAUSED, UnitTestStopReason.TARGET_REACHED]
    state = terminal_state(h, resumed.terminal.evidence_digest)
    assert dict(state["accepted_files"]).keys() == {S1_PATH, S2_PATH}


def test_replay_crash_after_claim_is_recovered_without_rerunning_the_claimed_attempt(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.measurer.raise_on = {2}
    with pytest.raises(RuntimeError):
        h.run()
    assert h.ledger.unreceipted_attempts(h.request.request_digest)
    assert {S1_PATH, S2_PATH} <= h.tree(), "coken attempt patch'i agacta"
    h.measurer.raise_on = set()
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail
    evidences = attempt_evidence(h)
    assert evidences[1]["status"] is AttemptState.INTERRUPTED
    assert sorted(evidences[1]["evidence"]["recovered"]["restored"]) == [S1_PATH, S2_PATH]
    ids = [r["attempt_id"] for r in h.measurer.runs]
    assert ids.count(evidences[1]["attempt_id"]) == 1, "claim edilmis attempt tekrar olculmedi"
    assert evidences[2]["evidence"]["kind"] == "build"


# --------------------------------------------------------------------------- drift (M12/M13)


def test_replay_external_production_edit_forces_rebaseline_and_drops_old_verification(
    tmp_path: Path,
) -> None:
    limits = LoopLimits(batch_size=1)

    def verifier(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        if call == 1:
            (h.root / SRC).write_text("class A { int changed; }\n", encoding="utf-8")
        return default_verifier(context, call)

    h = Harness(tmp_path, limits=limits, handlers={AgentSpecialty.VERIFIER: verifier})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail
    kinds = [a["evidence"]["kind"] for a in attempt_evidence(h)]
    assert "rebaseline" in kinds
    builds = [c["scenarios"][0]["scenario_id"] for c in h.adapters[AgentSpecialty.BUILDER].contexts]
    assert builds.count("S1") == 2, "dogrulanmis senaryo harici drift sonrasi yeniden planlandi"


def test_replay_source_revision_drift_stops_instead_of_carrying_old_success(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.measurer.on_measure = lambda number: (
        setattr(h.environment, "revision_override", "rev-2") if number == 1 else None
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.MEASUREMENT_INCOMPLETE
    assert outcome.detail == ("source-revision-drift",)


# --------------------------------------------------------------------------- plan / spec / agent


def test_replay_only_needs_specification_scenarios_stop_as_spec_ambiguous(tmp_path: Path) -> None:
    def planner(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        del context, call
        return {"plan": {"scenarios": [scenario_doc("S1", oracle="unresolved-assumption")]}}

    h = Harness(tmp_path, handlers={AgentSpecialty.ANALYZER_PLANNER: planner})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.SPEC_AMBIGUOUS
    assert outcome.needs_specification == ("S1",) and h.calls(AgentSpecialty.BUILDER) == 0


def test_replay_quota_style_plan_is_rejected_and_bounded(tmp_path: Path) -> None:
    def planner(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        del context, call
        return {"plan": {"test_count": 17, "scenarios": [scenario_doc("S1")]}}

    h = Harness(tmp_path, handlers={AgentSpecialty.ANALYZER_PLANNER: planner})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.STAGNATION_REVIEW
    assert "planner-invalid" in outcome.detail and h.calls(AgentSpecialty.BUILDER) == 0


@pytest.mark.parametrize("who", ["verifier", "final"])
def test_replay_verifier_with_builders_identity_is_refused(tmp_path: Path, who: str) -> None:
    spec = AgentSpecialty.VERIFIER if who == "verifier" else AgentSpecialty.FINAL_VERIFIER
    h = Harness(tmp_path, agent_ids={spec: "builder-1"})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING
    assert outcome.detail == ("verifier-not-independent",)
    assert outcome.terminal.receipt_digest is None


def test_replay_agent_dispatch_failures_are_bounded(tmp_path: Path) -> None:
    h = Harness(tmp_path, outcomes={AgentSpecialty.ANALYZER_PLANNER: DispatchOutcome.FAILED})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING
    assert outcome.detail[0] == "agent-unavailable"
    assert h.calls(AgentSpecialty.ANALYZER_PLANNER) == 3 and len(h.measurer.runs) == 1


def test_replay_sensitive_looking_inline_output_is_recoverable_not_final(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        artifact_bodies=False,
        handlers={
            AgentSpecialty.BUILDER: builder_with(
                {1: "// uses a token here\n", 2: "// token\n", 3: "// token\n"}
            )
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.RECOVERY_REQUIRED
    assert not outcome.terminal.final
    assert outcome.detail == ("agent-unavailable", "result-rejected-by-dispatch-policy")
    assert h.tree() == {f"{TEST_DIR}/ExistingTest.java"}


def test_replay_planner_default_matches_documented_handlers() -> None:
    assert default_planner({}, 1)["plan"]["scenarios"][0]["scenario_id"] == "S1"
