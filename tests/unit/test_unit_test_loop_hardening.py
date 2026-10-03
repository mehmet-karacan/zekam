"""W05 bagimsiz verifier bulgulari (B1-B4, sanitizer, characterization kapisi) regresyonlari.

REPLAY/MOCK: istemci, olcum, ortam. GERCEK: ledger, CAS, dosya sistemi, gateway.
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
    default_final,
    default_verifier,
    scenario_doc,
)

from zekam.application.unit_test_agents import (
    AgentDispatchFailure,
    AgentSpecialty,
    AgentTask,
    CanonicalUnitTestAgentGateway,
)
from zekam.application.unit_test_patch import GuardViolation, TestOnlyPolicy
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.unit_test_engineering import UnitTestStopReason

S1_PATH = f"{TEST_DIR}/S1Test.java"
EXISTING = f"{TEST_DIR}/ExistingTest.java"


def evidence(h: Harness, digest_value: str | None) -> dict[str, Any]:
    assert digest_value is not None
    loaded: dict[str, Any] = json.loads(h.objects.get(digest_value).decode("utf-8"))
    return loaded


def disk_digest(h: Harness, path: str) -> str:
    return digest_of_bytes((h.root / path).read_bytes())


def assert_success_matches_disk(h: Harness, outcome: Any) -> None:
    """Basari terminali yalniz kayitli digest'ler diskle birebir ise yazilabilir."""

    if outcome.terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED:
        return
    state = evidence(h, outcome.terminal.evidence_digest)["state"]
    for path, recorded in state["accepted_files"]:
        assert recorded == disk_digest(h, path), f"{path}: kayitli digest diskle uyusmuyor"
    production = dict(state["baseline_obs"]["production_digests"])
    assert production[SRC] == disk_digest(h, SRC), "production digest diskle uyusmuyor"


# ======================================================== B1: success'ten hemen once taze mi


def test_b1_accepted_test_edited_during_final_verifier_never_yields_stale_success(
    tmp_path: Path,
) -> None:
    def final(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        if call == 1:
            target = h.root / S1_PATH
            target.write_text(
                target.read_text(encoding="utf-8") + "// external\n", encoding="utf-8"
            )
        return default_final(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.FINAL_VERIFIER: final})
    outcome = h.run()
    assert_success_matches_disk(h, outcome)
    assert h.calls(AgentSpecialty.FINAL_VERIFIER) >= 2 or (
        outcome.terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED
    ), "ilk (bayat) final dogrulamadan sonra basari yazilmamali"


def test_b1_production_edited_during_final_verifier_never_yields_stale_success(
    tmp_path: Path,
) -> None:
    def final(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        if call == 1:
            (h.root / SRC).write_text("class A { int changed; }\n", encoding="utf-8")
        return default_final(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.FINAL_VERIFIER: final})
    outcome = h.run()
    assert_success_matches_disk(h, outcome)
    kinds = []
    for attempt in h.ledger.list_attempts(h.request.request_digest):
        receipt = h.ledger.get_attempt_receipt(attempt.attempt_id)
        assert receipt is not None
        kinds.append(evidence(h, receipt.evidence_digest)["evidence"]["kind"])
    assert "rebaseline" in kinds, "production degisimi yeniden olcum gerektirdi"


def test_b1_accepted_test_edited_during_attempt_verifier_is_not_accepted_as_is(
    tmp_path: Path,
) -> None:
    def verifier(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        if call == 1:
            target = h.root / S1_PATH
            target.write_text(
                target.read_text(encoding="utf-8") + "// external\n", encoding="utf-8"
            )
        return default_verifier(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.VERIFIER: verifier})
    outcome = h.run()
    assert_success_matches_disk(h, outcome)
    state = evidence(h, outcome.terminal.evidence_digest)["state"]
    for path, recorded in state["accepted_files"]:
        assert recorded == disk_digest(h, path)


def test_m12_external_edit_of_a_test_file_the_task_does_not_own_is_drift(tmp_path: Path) -> None:
    limits = LoopLimits(batch_size=1, max_remote_calls=1000)

    def verifier(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        if call == 1:
            target = h.root / EXISTING
            target.write_text(
                target.read_text(encoding="utf-8") + "// user edit\n", encoding="utf-8"
            )
        return default_verifier(context, call)

    h = Harness(tmp_path, limits=limits, handlers={AgentSpecialty.VERIFIER: verifier})
    outcome = h.run()
    kinds = []
    for attempt in h.ledger.list_attempts(h.request.request_digest):
        receipt = h.ledger.get_attempt_receipt(attempt.attempt_id)
        assert receipt is not None
        kinds.append(evidence(h, receipt.evidence_digest)["evidence"]["kind"])
    assert "rebaseline" in kinds, "harici test-yolu degisimi (M12) gorulmedi"
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail


def test_m13_own_accepted_patch_is_not_drift_even_with_test_tree_tracking(tmp_path: Path) -> None:
    limits = LoopLimits(batch_size=1, max_remote_calls=1000)
    h = Harness(tmp_path, limits=limits)
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED
    kinds = []
    for attempt in h.ledger.list_attempts(h.request.request_digest):
        receipt = h.ledger.get_attempt_receipt(attempt.attempt_id)
        assert receipt is not None
        kinds.append(evidence(h, receipt.evidence_digest)["evidence"]["kind"])
    assert kinds == ["baseline", "build", "build"]


def test_b1_already_at_target_is_revalidated_before_terminal(tmp_path: Path) -> None:
    h = Harness(tmp_path, percent="30")

    def edit(number: int) -> None:
        if number == 1:
            (h.root / SRC).write_text("class A { int changed; }\n", encoding="utf-8")

    h.measurer.on_measure = edit
    outcome = h.run()
    if outcome.terminal.stop_reason is UnitTestStopReason.ALREADY_AT_TARGET:
        pytest.fail("baseline olcumu sirasinda production degisti; basari yazilmamali")


# ================================================================ B2: cancel/pause claim'den once


def test_b2_cancel_during_builder_call_claims_nothing_and_writes_no_patch(tmp_path: Path) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        h.control.request_cancel()
        return default_builder(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.USER_CANCELLED
    assert len(h.ledger.list_attempts(h.request.request_digest)) == 1, "yalniz baseline claim'i"
    assert h.tree() == {EXISTING}, "cancel sonrasi patch yazilmadi"
    assert len(h.measurer.runs) == 1


def test_b2_pause_during_builder_call_claims_nothing_and_is_resumable(tmp_path: Path) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        h.control.request_pause()
        return default_builder(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: builder})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.USER_PAUSED
    assert not outcome.terminal.final
    assert len(h.ledger.list_attempts(h.request.request_digest)) == 1
    assert h.tree() == {EXISTING}


def test_b2_cancel_during_final_verifier_wins_over_target_reached(tmp_path: Path) -> None:
    def final(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        h.control.request_cancel()
        return default_final(context, call)

    h = Harness(tmp_path, handlers={AgentSpecialty.FINAL_VERIFIER: final})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.USER_CANCELLED
    assert outcome.terminal.receipt_digest is None
    reasons = [t.stop_reason for t in h.ledger.list_terminals(h.request.request_digest)]
    assert UnitTestStopReason.TARGET_REACHED not in reasons


# ================================================================ B3: remote cagri butcesi


def remote_harness(tmp_path: Path, max_calls: int) -> Harness:
    limits = LoopLimits(batch_size=3, max_remote_calls=max_calls)
    h = Harness(tmp_path, remote=True, limits=limits)
    return h


def test_b3_remote_agents_require_an_explicit_positive_call_budget(tmp_path: Path) -> None:
    h = remote_harness(tmp_path, 0)
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.ENVIRONMENT_MISSING
    assert outcome.detail == ("remote-call-budget-missing",)
    assert h.measurer.runs == [] and all(a.calls == 0 for a in h.adapters.values())


def test_b3_remote_call_counter_is_enforced_before_each_dispatch(tmp_path: Path) -> None:
    h = remote_harness(tmp_path, 2)
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.BUDGET_EXHAUSTED
    assert "remote-calls" in outcome.detail
    assert sum(a.calls for a in h.adapters.values()) == 2, "limit asilmadi"
    state = evidence(h, outcome.terminal.evidence_digest)["state"]
    assert state["meter"]["remote_calls"] == 2


def test_b3_zero_means_no_remote_calls_for_local_agents_and_is_not_unlimited(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, limits=LoopLimits(batch_size=3, max_remote_calls=0))
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED
    state = evidence(h, outcome.terminal.evidence_digest)["state"]
    assert state["meter"]["remote_calls"] == 0
    assert sum(a.calls for a in h.adapters.values()) >= 4


def test_b3_remote_model_approval_also_requires_positive_budget(tmp_path: Path) -> None:
    h = Harness(tmp_path, limits=LoopLimits(batch_size=3, max_remote_calls=0))
    approvals = UnitTestApprovals(
        write_tests=True,
        build=True,
        remote_model=True,
        approved_budget=h.request.budget,
        approved_limits=h.limits,
    )
    outcome = h.run(approvals=approvals)
    assert outcome.detail == ("remote-call-budget-missing",)


# ================================================================ B4: buyuk/kucuk harf + Windows


def broad_policy(**kw: Any) -> TestOnlyPolicy:
    return TestOnlyPolicy(
        frozenset({SRC}),
        allowed_test_paths=("src", "mod"),
        forbidden_paths=frozenset({"src/test/java/p/Locked.java"}),
        **kw,
    )


@pytest.mark.parametrize(
    "path",
    [
        "POM.XML",
        "Pom.xml",
        "pom.xml.",
        "pom.xml ",
        "mod/POM.XML",
        "src/Main/java/p/Evil.java",
        "SRC/MAIN/java/p/Evil.java",
        "src/main./java/p/Evil.java",
        "MVNW.CMD",
        "Mvnw",
        ".MVN/x",
        ".mvn./x",
        ".GIT/x",
        ".Github/workflows/x.yml",
        "src/test/java/p/JaCoCo.xml",
        "src/test/java/p/Verifier/V.java",
        "src/test/java/p/SUREFIRE.java",
        "src/test/java/p/LOCKED.java",
        "src/test/java/p/Locked.java.",
        "SRC/MAIN/JAVA/P/A.JAVA",
    ],
)
def test_b4_path_guards_are_case_and_trailing_dot_insensitive(path: str) -> None:
    with pytest.raises(GuardViolation) as raised:
        broad_policy().check_path(path, modifies_existing=False)
    assert raised.value.reason != "outside-allowed-test-paths", (
        "koruma kuralindan once reddedilmeli"
    )


def test_b4_frozen_dirty_and_allowed_comparisons_are_case_insensitive() -> None:
    frozen = broad_policy(frozen_paths=frozenset({"src/test/java/p/Frozen.java"}))
    with pytest.raises(GuardViolation) as raised:
        frozen.check_path("SRC/Test/Java/P/FROZEN.JAVA", modifies_existing=False)
    assert raised.value.reason == "reproducer-frozen"
    dirty = broad_policy(protected_dirty_paths=frozenset({"src/test/java/p/MyTest.java"}))
    with pytest.raises(GuardViolation) as raised2:
        dirty.check_path("src/test/java/p/MYTEST.JAVA", modifies_existing=True)
    assert raised2.value.reason == "user-dirty-path"
    narrow = TestOnlyPolicy(frozenset({SRC}), allowed_test_paths=("src/test/java",))
    narrow.check_path("SRC/Test/Java/p/X.java", modifies_existing=False)
    with pytest.raises(GuardViolation):
        narrow.check_path("src/test/javax/p/X.java", modifies_existing=False)
    with pytest.raises(GuardViolation):
        narrow.check_path("src/test/java/p/pom.xml::$DATA", modifies_existing=False)


# ================================================================ sanitizer / artifact


def planner_with_words(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del context, call
    doc = scenario_doc("S1")
    doc["behavior_contract"] = "PasswordValidator rejects empty password and expired token"
    return {"plan": {"scenarios": [doc]}}


def password_builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    base = dict(default_builder(context, call))
    files = builder_files(context)
    for item in files:
        item["content"] = (
            "// secret token password credential\n"
            + item["content"]
            + 'class PasswordValidatorTest { String passwordRule = "min-8-chars"; }\n'
        )
    base["patch"] = {"files": files}
    return base


def test_sanitizer_artifact_path_lets_password_validator_style_code_through(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        percent="60",
        artifact_bodies=True,
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: planner_with_words,
            AgentSpecialty.BUILDER: password_builder,
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail
    for adapter in h.adapters.values():
        if adapter.calls == 0:
            continue
        assert set(adapter.last_payload) == {"artifact_digest"}, "zarfta yalniz digest"
    assert "PasswordValidatorTest" in (h.root / S1_PATH).read_text(encoding="utf-8")


def test_sanitizer_inline_policy_is_unchanged_and_refusal_is_recoverable_not_final(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path,
        percent="60",
        artifact_bodies=False,
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: planner_with_words,
            AgentSpecialty.BUILDER: password_builder,
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.RECOVERY_REQUIRED
    assert not outcome.terminal.final and outcome.machine_status()["final"] is False
    assert "result-rejected-by-dispatch-policy" in outcome.detail
    assert h.tree() == {EXISTING}
    for adapter in h.adapters.values():  # kurtarma: ayni istek artifact yoluyla surdurulur
        adapter.artifact = True
    resumed = h.run()
    assert resumed.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, resumed.detail


def _gateway(h: Harness) -> CanonicalUnitTestAgentGateway:
    return h.gateway


def _artifact_task() -> AgentTask:
    return AgentTask(AgentSpecialty.BUILDER, digest("r"), 2, 0, {"scenarios": []})


@pytest.mark.parametrize(
    "secret_text",
    [
        'String apiKey = "' + "sk_" + "live_" + "A" * 28 + '";',
        'String k = "' + "AK" + "IA" + "ABCDEFGHIJKLMNOP" + '";',
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        'String t = "' + "ghp_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ" + '";',
        'client_secret = "' + "AbCdEfGhIjKlMnOpQrStUvWxYz" + "012345" + '"',
    ],
)
def test_sanitizer_secret_values_are_still_rejected_in_artifacts(
    tmp_path: Path, secret_text: str
) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        base["note_for_review"] = secret_text
        return base

    h = Harness(tmp_path, artifact_bodies=True, handlers={AgentSpecialty.BUILDER: builder})
    with pytest.raises(AgentDispatchFailure) as raised:
        _gateway(h).invoke(_artifact_task())
    assert raised.value.category == "result-contains-secret-value"


def test_sanitizer_artifact_integrity_and_size_are_enforced(tmp_path: Path) -> None:
    h = Harness(tmp_path, artifact_bodies=True)
    adapter = h.adapters[AgentSpecialty.BUILDER]
    original = adapter.handler

    def big(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(original(context, call))
        base["padding"] = "x" * 2_000_000
        return base

    adapter.handler = big
    with pytest.raises(AgentDispatchFailure) as raised:
        _gateway(h).invoke(_artifact_task())
    assert raised.value.category == "artifact-too-large"
    adapter.handler = original
    h2 = Harness(tmp_path / "b", artifact_bodies=True)
    gateway = h2.gateway
    h2.adapters[AgentSpecialty.BUILDER].artifact = True
    missing = digest_of_bytes(canonical_bytes({"never": "stored"}))
    h2.adapters[AgentSpecialty.BUILDER].extra_result = {}
    original2 = h2.adapters[AgentSpecialty.BUILDER].dispatch

    def dangling(request: Any, *, cwd: Path, permit: Any) -> Any:
        result = original2(request, cwd=cwd, permit=permit)
        return type(result)(
            result.assignment_id,
            result.invocation_id,
            result.client_id,
            result.role,
            result.outcome,
            0,
            {"artifact_digest": missing},
        )

    h2.adapters[AgentSpecialty.BUILDER].dispatch = dangling  # type: ignore[method-assign]
    with pytest.raises(AgentDispatchFailure) as raised2:
        gateway.invoke(_artifact_task())
    assert raised2.value.category == "artifact-unavailable"


def test_sanitizer_context_allows_plain_words_but_not_secret_values(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ok = AgentTask(
        AgentSpecialty.BUILDER,
        digest("r"),
        2,
        0,
        {
            "scenarios": [
                {"scenario_id": "S1", "behavior_contract": "PasswordValidator checks token expiry"}
            ]
        },
    )
    assert h.gateway.invoke(ok).envelope.status.value == "completed"
    bad = AgentTask(
        AgentSpecialty.BUILDER,
        digest("r"),
        3,
        0,
        {"note": 'api_key = "' + "sk_" + "live_" + "A" * 28 + '"'},
    )
    from zekam.domain.errors import PolicyViolation

    with pytest.raises(PolicyViolation, match="secret"):
        h.gateway.invoke(bad)


# ================================================================ characterization kapisi


def char_planner(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del context, call
    return {
        "plan": {
            "scenarios": [
                scenario_doc("S1", oracle="characterization", ref="current-behavior#1"),
            ]
        }
    }


def char_builder(updates: bool, fail_first: bool = True) -> Any:
    def handler(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        files = builder_files(context)
        if call == 1 and fail_first:
            for item in files:
                item["content"] = "// FAIL:assertion\n" + item["content"]
        base["patch"] = {"files": files}
        if call >= 2 and updates:
            base["characterization_update"] = [
                {"scenario_id": "S1", "evidence_ref": "ticket#4711 documents new behavior"}
            ]
        return base

    return handler


def test_characterization_failure_cannot_be_repaired_by_just_changing_expected(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path,
        percent="60",
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: char_planner,
            AgentSpecialty.BUILDER: char_builder(updates=False),
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED
    assert "characterization-update-evidence-missing" in json.dumps(
        evidence(h, outcome.terminal.evidence_digest)["state"]["failure_notes"]
    )
    assert S1_PATH not in h.tree()


def test_characterization_update_needs_evidence_and_independent_confirmation(
    tmp_path: Path,
) -> None:
    def confirming(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_verifier(context, call))
        base["characterization_updates"] = ["S1"]
        return base

    h = Harness(
        tmp_path,
        percent="60",
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: char_planner,
            AgentSpecialty.BUILDER: char_builder(updates=True),
            AgentSpecialty.VERIFIER: confirming,
        },
    )
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED, outcome.detail
    # dogrulayici onaylamazsa ayni kanit yetmez
    h2 = Harness(
        tmp_path / "b",
        percent="60",
        max_attempts=4,
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: char_planner,
            AgentSpecialty.BUILDER: char_builder(updates=True),
        },
    )
    outcome2 = h2.run()
    assert outcome2.terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED


def test_characterization_gate_is_not_triggered_for_clean_runs(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        percent="60",
        handlers={
            AgentSpecialty.ANALYZER_PLANNER: char_planner,
            AgentSpecialty.BUILDER: char_builder(updates=False, fail_first=False),
        },
    )
    assert h.run().terminal.stop_reason is UnitTestStopReason.TARGET_REACHED


def test_accepted_characterization_test_cannot_be_edited_without_update_evidence(
    tmp_path: Path,
) -> None:
    def planner(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        del context, call
        return {
            "plan": {
                "scenarios": [
                    scenario_doc("S1", oracle="characterization", ref="current#1"),
                    scenario_doc("S2", oracle="invariant", priority="normal"),
                ]
            }
        }

    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        files = builder_files(context)
        if call == 2:  # kabul edilmis karakterizasyon dosyasini sessizce "duzelt"
            owned = dict(context["owned_files"])
            files.append(
                {
                    "path": S1_PATH,
                    "content": (
                        "// COVER:3\nclass S1Test { @Test void t() { assertEquals(2, f(3)); } }\n"
                    ),
                    "preimage_digest": owned[S1_PATH],
                }
            )
        base["patch"] = {"files": files}
        return base

    h = Harness(
        tmp_path,
        percent="80",
        limits=LoopLimits(batch_size=1, max_remote_calls=1000),
        handlers={AgentSpecialty.ANALYZER_PLANNER: planner, AgentSpecialty.BUILDER: builder},
    )
    outcome = h.run()
    notes = json.dumps(evidence(h, outcome.terminal.evidence_digest)["state"]["failure_notes"])
    assert "characterization-update-evidence-missing" in notes
    assert "assertEquals(2, f(3))" not in (h.root / S1_PATH).read_text(encoding="utf-8")
