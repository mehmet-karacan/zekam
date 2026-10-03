"""W06 / D10: PIT Maven plani, POM tanisi, yetki ve calistirici (fake-tool; GERCEK PIT YOK).

Launcher tmp dizinindeki ``FakeMaven``'dir ve ``mutations.xml`` elle yazilmis replay
fixture'idir. Bu makinede gercek Maven/pitest yoktur; gercek PIT kosusu W08'dedir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.test_unit_test_mutation import mutation, mutations
from tests.unit.unit_test_runner_support import (
    JUNIT4_DEP,
    JUPITER_DEP,
    PLUGIN_LINES,
    FakeMaven,
    make_project,
    pom,
)

from zekam.application.unit_test_mutation import (
    MutationAcceptancePolicy,
    MutationRunState,
    MutationVerdict,
    decide_mutation_acceptance,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.unit_test_runner.maven_plan import ExecutionClass, PlanStatus
from zekam.infrastructure.unit_test_runner.pit_plan import (
    EFFECT_BYTECODE,
    EFFECT_PLUGIN,
    PitApproval,
    PitAuthorization,
    PitPlan,
    build_pit_plan,
    check_pit_authorization,
)
from zekam.infrastructure.unit_test_runner.pit_runner import run_pit

PIT_LINE = "[INFO] --- pitest-maven:1.15.8:mutationCoverage (default-cli) @ app ---"
PLAN_X = "sha256:" + "9" * 64
REQUIRED = MutationAcceptancePolicy(required=True, threshold_numerator=1, threshold_denominator=2)


def pit_plugin(*, version: str | None = "1.15.8", junit5: bool = False, config: str = "") -> str:
    ver = f"<version>{version}</version>" if version else ""
    dep = (
        "<dependencies><dependency><groupId>org.pitest</groupId>"
        "<artifactId>pitest-junit5-plugin</artifactId><version>1.2.1</version></dependency>"
        "</dependencies>"
        if junit5
        else ""
    )
    cfg = f"<configuration>{config}</configuration>" if config else ""
    return (
        f"<plugin><groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId>{ver}"
        f"{dep}{cfg}</plugin>"
    )


def approval(**overrides: object) -> PitApproval:
    values: dict[str, object] = {
        "module": "",
        "target_classes": ("p.A*",),
        "target_tests": ("p.ATest",),
        "mutators": ("CONDITIONALS_BOUNDARY", "MATH"),
        "timeout_constant_ms": 4000,
        "timeout_factor": "1.25",
        "process_timeout_seconds": 600,
    }
    values.update(overrides)
    return PitApproval(**values)  # type: ignore[arg-type]


def project(tmp_path: Path, **pom_kwargs: object) -> Path:
    kwargs: dict[str, object] = {"extra_plugins": pit_plugin()}
    kwargs.update(pom_kwargs)
    return make_project(tmp_path / "proj", pom_text=pom(**kwargs))  # type: ignore[arg-type]


def ready(tmp_path: Path) -> tuple[FakeMaven, Path, PitPlan]:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path)
    result = build_pit_plan(root, approval(), search_path=fake.search_path)
    assert result.status is PlanStatus.READY and result.plan is not None
    return fake, root, result.plan


def full_auth(plan: PitPlan) -> PitAuthorization:
    return PitAuthorization(plan.pit_plan_digest, (EFFECT_PLUGIN, EFFECT_BYTECODE), True)


# ------------------------------------------------------------------ plan


def test_replay_ready_plan_has_explicit_argv_and_separate_bytecode_effect(tmp_path: Path) -> None:
    _, _, plan = ready(tmp_path)
    assert plan.maven_plan.args == (
        "-B",
        "-DtargetClasses=p.A*",
        "-DtargetTests=p.ATest",
        "-Dmutators=CONDITIONALS_BOUNDARY,MATH",
        "-DtimeoutConstant=4000",
        "-DtimeoutFactor=1.25",
        "-Dthreads=1",
        "-DoutputFormats=XML",
        "-DtimestampedReports=false",
        "test-compile",
        "org.pitest:pitest-maven:1.15.8:mutationCoverage",
    )
    assert plan.maven_plan.execution_class is ExecutionClass.PLUGIN_EXECUTION
    assert plan.effects == (EFFECT_PLUGIN, EFFECT_BYTECODE) and plan.maven_plan.network_possible
    assert plan.maven_plan.network_isolation == "not-provided"
    assert plan.report_path == "target/pit-reports/mutations.xml"
    assert plan.pit_plan_digest.startswith("sha256:")


def test_replay_plan_digest_changes_with_any_approved_scope_value(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path)
    digests = set()
    for override in (
        {},
        {"mutators": ("MATH",)},
        {"target_tests": ("p.BTest",)},
        {"timeout_constant_ms": 5000},
        {"max_mutations_per_class": 10},
    ):
        plan = build_pit_plan(root, approval(**override), search_path=fake.search_path).plan
        assert plan is not None
        digests.add(plan.pit_plan_digest)
    assert len(digests) == 5


def test_replay_authorization_requires_exact_digest_effect_and_network(tmp_path: Path) -> None:
    _, _, plan = ready(tmp_path)
    check_pit_authorization(plan, full_auth(plan))
    with pytest.raises(PolicyViolation):
        check_pit_authorization(plan, None)
    with pytest.raises(PolicyViolation, match="digest"):
        check_pit_authorization(plan, PitAuthorization("sha256:" + "0" * 64, plan.effects, True))
    with pytest.raises(PolicyViolation, match="bytecode"):
        check_pit_authorization(
            plan, PitAuthorization(plan.pit_plan_digest, (EFFECT_PLUGIN,), True)
        )
    with pytest.raises(PolicyViolation, match="ag"):
        check_pit_authorization(plan, PitAuthorization(plan.pit_plan_digest, plan.effects, False))


def test_replay_junit5_module_needs_junit5_plugin_diagnosed_not_written(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path, deps=JUPITER_DEP)
    before = (root / "pom.xml").read_bytes()
    result = build_pit_plan(root, approval(), search_path=fake.search_path)
    assert result.status is PlanStatus.SETUP_REQUIRED and result.plan is None
    assert any(f.code == "pit-junit5-plugin-missing" for f in result.findings)
    assert any("pitest-junit5-plugin" in step for step in result.setup_plan)
    assert (root / "pom.xml").read_bytes() == before  # POM'a yazilmaz
    root2 = make_project(
        tmp_path / "ok", pom_text=pom(deps=JUPITER_DEP, extra_plugins=pit_plugin(junit5=True))
    )
    ok = build_pit_plan(root2, approval(), search_path=fake.search_path)
    assert ok.status is PlanStatus.READY and ok.plan is not None
    assert ok.plan.junit5_plugin_version == "1.2.1"
    assert any(f.code == "pit-plugin-pair-unverified" for f in ok.findings)


@pytest.mark.parametrize(
    ("pom_kwargs", "code"),
    [
        ({"extra_plugins": ""}, "pit-plugin-missing"),
        ({"extra_plugins": pit_plugin(version=None)}, "pit-plugin-version-unresolved"),
        ({"extra_plugins": pit_plugin(version="${pit.v}")}, "pit-plugin-version-unresolved"),
        ({"extra_plugins": pit_plugin(version="LATEST")}, "pit-plugin-version-unresolved"),
        ({"deps": ""}, "engine-unknown"),
        (
            {
                "extra_plugins": pit_plugin(
                    config="<targetClasses><param>x.*</param></targetClasses>"
                )
            },
            "pit-pom-config-overrides-plan",
        ),
        (
            {"extra_plugins": pit_plugin(config="<skip>true</skip>")},
            "pit-pom-config-overrides-plan",
        ),
        ({"props": "<skipTests>true</skipTests>"}, "tests-skipped"),
    ],
)
def test_replay_setup_required_for_pom_problems_without_silent_fix(
    tmp_path: Path, pom_kwargs: dict[str, object], code: str
) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path, **pom_kwargs)
    result = build_pit_plan(root, approval(), search_path=fake.search_path)
    assert result.status is PlanStatus.SETUP_REQUIRED and result.plan is None
    assert code in {f.code for f in result.findings}


def test_replay_property_version_resolves_statically_and_history_is_warning(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(
        tmp_path,
        props="<pit.v>1.16.0</pit.v>",
        extra_plugins=pit_plugin(version="${pit.v}", config="<withHistory>true</withHistory>"),
    )
    result = build_pit_plan(root, approval(), search_path=fake.search_path)
    assert result.status is PlanStatus.READY and result.plan is not None
    assert result.plan.plugin_version == "1.16.0"
    assert "pit-history-configured" in {f.code for f in result.findings}


def test_replay_tool_missing_and_not_supported_states(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    root = project(tmp_path)
    missing = build_pit_plan(root, approval(), search_path=str(empty))
    assert missing.status is PlanStatus.TOOL_MISSING and missing.plan is None
    assert any("kurulum" in step for step in missing.setup_plan)
    bare = tmp_path / "bare"
    bare.mkdir()
    assert build_pit_plan(bare, approval()).status is PlanStatus.NOT_SUPPORTED
    assert (
        build_pit_plan(root, approval(module="nope"), search_path=str(empty)).status
        is PlanStatus.NOT_SUPPORTED
    )


@pytest.mark.parametrize(
    "override",
    [
        {"target_classes": ()},
        {"target_classes": ("p.A$Inner",)},
        {"target_classes": ("p.A; rm -rf /",)},
        {"target_tests": ("p.T", "p.T")},
        {"mutators": ("math",)},
        {"mutators": ()},
        {"timeout_constant_ms": 5},
        {"timeout_factor": "0.5"},
        {"timeout_factor": "1.5; x"},
        {"process_timeout_seconds": 0},
        {"max_mutations_per_class": 0},
        {"module": "../escape"},
    ],
)
def test_replay_approval_rejects_unsafe_or_unbounded_values(override: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        approval(**override)


def test_replay_junit4_needs_no_junit5_plugin(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path, deps=JUNIT4_DEP)
    result = build_pit_plan(root, approval(), search_path=fake.search_path)
    assert result.status is PlanStatus.READY and result.plan is not None
    assert result.plan.junit5_plugin_version is None


# ------------------------------------------------------------------ calistirici (fake-tool)


def test_replay_run_pit_collects_fresh_report_and_never_touches_sources(tmp_path: Path) -> None:
    fake, root, plan = ready(tmp_path)
    source = root / "src/main/java/p/A.java"
    before = source.read_bytes()
    xml = mutations(
        mutation("KILLED", index="1"),
        mutation("SURVIVED", index="2"),
        mutation("NON_VIABLE", index="3"),
    ).decode()
    fake.set(write={"target/pit-reports/mutations.xml": xml}, print=[*PLUGIN_LINES, PIT_LINE])
    stale = root / "target/pit-reports"
    stale.mkdir(parents=True)
    (stale / "mutations.xml").write_text("<old/>", encoding="utf-8")
    result = run_pit(plan, full_auth(plan), lock_dir=tmp_path / "locks", run_id="r1")
    assert fake.invocation()["argv"] == list(plan.maven_plan.args)
    assert source.read_bytes() == before  # kaynak Java degismez
    assert list((root / "target").glob("pit-reports.stale-*"))  # eski rapor silinmeden karantinada
    outcome = result.outcome
    assert outcome.state is MutationRunState.COMPLETED and outcome.report is not None
    assert (outcome.report.counters.killed, outcome.report.counters.survived) == (1, 1)
    assert outcome.report.counters.non_viable == 1
    assert outcome.plan_digest == plan.pit_plan_digest
    assert outcome.report_digest == digest_of_bytes(xml.encode())
    decision = decide_mutation_acceptance(
        REQUIRED,
        outcome,
        expected_plan_digest=plan.pit_plan_digest,
        expected_candidate_digest=plan.test_candidate_digest,
    )
    assert decision.verdict is MutationVerdict.MET and decision.review_required_count == 1


def test_replay_run_pit_requires_pit_authorization(tmp_path: Path) -> None:
    _, _, plan = ready(tmp_path)
    with pytest.raises(PolicyViolation):
        run_pit(plan, None, lock_dir=tmp_path / "locks", run_id="r")
    with pytest.raises(PolicyViolation):
        run_pit(
            plan,
            PitAuthorization(plan.pit_plan_digest, (EFFECT_PLUGIN,), True),
            lock_dir=tmp_path / "locks",
            run_id="r",
        )


@pytest.mark.parametrize(
    ("scenario", "state"),
    [
        ({"exit": 1, "print": [PIT_LINE]}, MutationRunState.RUN_FAILED),
        ({"print": [*PLUGIN_LINES]}, MutationRunState.RUN_FAILED),  # pitest plugin gorulmedi
        ({"print": [PIT_LINE]}, MutationRunState.REPORT_INVALID),  # rapor yok
        (
            {"print": [PIT_LINE], "write": {"target/pit-reports/mutations.xml": "<mutations"}},
            MutationRunState.REPORT_INVALID,
        ),
        (
            {
                "print": [PIT_LINE],
                "write": {
                    "target/pit-reports/mutations.xml": '<!DOCTYPE m [<!ENTITY e "x">]><mutations/>'
                },
            },
            MutationRunState.REPORT_INVALID,
        ),
    ],
)
def test_replay_failed_run_never_becomes_completed_and_fails_required_condition(
    tmp_path: Path, scenario: dict[str, object], state: MutationRunState
) -> None:
    fake, _, plan = ready(tmp_path)
    fake.set(**scenario)
    result = run_pit(plan, full_auth(plan), lock_dir=tmp_path / "locks", run_id="r2")
    assert result.outcome.state is state and result.outcome.report is None
    decision = decide_mutation_acceptance(
        REQUIRED,
        result.outcome,
        expected_plan_digest=plan.pit_plan_digest,
        expected_candidate_digest=plan.test_candidate_digest,
    )
    assert decision.verdict is MutationVerdict.NOT_MET and not decision.satisfied
    assert decision.mutation_status == "mutation_run_failed"


def test_replay_required_mutation_with_unavailable_pit_is_not_satisfied(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    root = project(tmp_path)
    plan_result = build_pit_plan(root, approval(), search_path=str(empty))
    from zekam.application.unit_test_mutation import MutationOutcome

    outcome = MutationOutcome(MutationRunState.TOOL_MISSING, reasons=plan_result.setup_plan)
    decision = decide_mutation_acceptance(
        REQUIRED, outcome, expected_plan_digest=PLAN_X, expected_candidate_digest=PLAN_X
    )
    assert not decision.satisfied and decision.mutation_status == "mutation_not_run"
    optional = decide_mutation_acceptance(
        MutationAcceptancePolicy(),
        outcome,
        expected_plan_digest=PLAN_X,
        expected_candidate_digest=PLAN_X,
    )
    assert optional.satisfied and optional.verdict is MutationVerdict.NOT_REQUIRED


# --------------------------------------------------------- verifier bulgulari (regresyon)


def _write_test(root: Path, body: str) -> None:
    target = root / "src/test/java/p/ATest.java"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_replay_plan_and_outcome_bind_test_candidate_digest(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path)
    _write_test(root, "class ATest { void a() { assertTrue(new A().f() > 0); } }")
    old = build_pit_plan(root, approval(), search_path=fake.search_path).plan
    assert old is not None and old.test_candidate_digest.startswith("sha256:")
    _write_test(root, "class ATest { void a() { assertEquals(1, new A().f()); } }")
    new = build_pit_plan(root, approval(), search_path=fake.search_path).plan
    assert new is not None
    assert new.test_candidate_digest != old.test_candidate_digest
    assert new.pit_plan_digest != old.pit_plan_digest  # aday degisince plan digest'i degisir


def test_replay_old_candidate_pit_report_does_not_pass_new_candidate(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = project(tmp_path)
    _write_test(root, "class ATest { void a() { assertTrue(new A().f() > 0); } }")
    old_plan = build_pit_plan(root, approval(), search_path=fake.search_path).plan
    assert old_plan is not None
    xml = mutations(mutation("KILLED", index="1")).decode()
    fake.set(write={"target/pit-reports/mutations.xml": xml}, print=[*PLUGIN_LINES, PIT_LINE])
    old_outcome = run_pit(
        old_plan, full_auth(old_plan), lock_dir=tmp_path / "l", run_id="a"
    ).outcome
    assert old_outcome.candidate_digest == old_plan.test_candidate_digest
    same = decide_mutation_acceptance(
        REQUIRED,
        old_outcome,
        expected_plan_digest=old_plan.pit_plan_digest,
        expected_candidate_digest=old_plan.test_candidate_digest,
    )
    assert same.verdict is MutationVerdict.MET
    # Test agaci degisti: yeni plan/aday; eski raporu yeni adayi GECIRMEZ.
    _write_test(root, "class ATest { void a() { assertEquals(1, new A().f()); } }")
    new_plan = build_pit_plan(root, approval(), search_path=fake.search_path).plan
    assert new_plan is not None
    stale = decide_mutation_acceptance(
        REQUIRED,
        old_outcome,
        expected_plan_digest=new_plan.pit_plan_digest,
        expected_candidate_digest=new_plan.test_candidate_digest,
    )
    assert stale.verdict is MutationVerdict.NOT_MET and not stale.satisfied
    # Eski plani degisen test agaciyla calistirmak da reddedilir (drift).
    with pytest.raises(PolicyViolation, match="aday"):
        run_pit(old_plan, full_auth(old_plan), lock_dir=tmp_path / "l", run_id="b")
