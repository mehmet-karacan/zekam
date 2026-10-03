"""W05: canonical agent gateway (REPLAY istemci), body dogrulama ve Maven loop adaptorleri.

Gateway/CanonicalAgentDispatchService GERCEKTIR; istemci ``ReplayClientAdapter`` taklittir.
Maven adaptoru FAKE ``mvn`` (tmp betik) ile calisir: gercek Maven kosusu degildir.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_loop_support import (
    Harness,
    ReplayClientAdapter,
    default_builder,
    default_planner,
    default_triage,
    default_verifier,
    scenario_doc,
)
from tests.unit.unit_test_runner_support import (
    FakeMaven,
    good_scenario,
    make_project,
    surefire_xml,
)

from zekam.application.unit_test_agents import (
    BLOCKING_QUALITY_CODES,
    AgentDispatchFailure,
    AgentSpecialty,
    AgentTask,
    CanonicalUnitTestAgentGateway,
    Verdict,
    parse_build_body,
    parse_plan_body,
    parse_verify_body,
)
from zekam.application.unit_test_loop_state import MeasuredRun
from zekam.application.unit_test_measurement import MeasurementStatus
from zekam.domain.canonical import digest
from zekam.domain.clients import DispatchOutcome
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import (
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
)
from zekam.infrastructure.unit_test_runner.loop_adapters import (
    MavenEnvironmentObserver,
    MavenUnitTestMeasurer,
)

RD = digest("request")


def task(spec: AgentSpecialty = AgentSpecialty.BUILDER, **context: Any) -> AgentTask:
    return AgentTask(
        spec, RD, 2, 0, context or {"scenarios": []}, write_resources=("src/test/java",)
    )


def test_gateway_persists_assignment_and_instruction_by_digest_before_dispatch(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    response = h.gateway.invoke(task(scenarios=[{"scenario_id": "S1"}]))
    assert h.events[:3] == [
        "store:assignment:unit-test-builder",
        "store:invocation",
        "adapter:builder",
    ]
    assignment = h.store.assignments[0]
    assert assignment.role.value == "builder" and assignment.risk == "medium"
    assert assignment.write_resources == ("src/test/java",)
    instruction = h.objects.get(assignment.instruction_digest).decode("utf-8")
    assert "production" in instruction and "no raw reasoning" in instruction
    assert b"S1" in h.objects.get(assignment.context_manifest_digest)
    assert response.envelope.role.value == "builder", "rol harness'tan gelir"
    assert response.envelope.status.value == "completed" and response.envelope.evidence
    assert h.store.results == [h.store.results[0]]


def test_gateway_ids_are_deterministic_so_store_replays_are_idempotent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.gateway.invoke(task())
    h.gateway.invoke(task())
    assert h.store.assignments[0].id == h.store.assignments[1].id
    assert h.store.invocations[0].id == h.store.invocations[1].id
    h.gateway.invoke(replace(task(), sequence=1))
    assert h.store.assignments[2].id != h.store.assignments[0].id


@pytest.mark.parametrize(
    ("spec", "role"),
    [
        (AgentSpecialty.ANALYZER_PLANNER, "researcher"),
        (AgentSpecialty.BUILDER, "builder"),
        (AgentSpecialty.VERIFIER, "verifier"),
        (AgentSpecialty.DEFECT_TRIAGE, "verifier"),
        (AgentSpecialty.FINAL_VERIFIER, "verifier"),
    ],
)
def test_gateway_reuses_existing_roles_without_new_global_enum(
    tmp_path: Path, spec: AgentSpecialty, role: str
) -> None:
    h = Harness(tmp_path)
    h.gateway.invoke(task(spec, scenarios=[{"scenario_id": "S1"}]))
    assert h.store.assignments[0].role.value == role
    assert h.store.assignments[0].agent_ref == f"unit-test-{spec.value}"


def test_gateway_refuses_oversized_or_secret_like_context_without_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    small = CanonicalUnitTestAgentGateway(
        h.work, h.store, h.objects, h.adapters, h.root, max_context_bytes=200
    )
    with pytest.raises(PolicyViolation, match="siniri"):
        small.invoke(task(blob="x" * 1000))
    with pytest.raises(PolicyViolation, match="secret"):
        test_key = "sk_" + "live_" + "A" * 28
        h.gateway.invoke(task(note=f'api_key = "{test_key}"'))
    assert h.events == [] and all(a.calls == 0 for a in h.adapters.values())


def test_gateway_unusable_results_become_bounded_dispatch_failures(tmp_path: Path) -> None:
    h = Harness(tmp_path, outcomes={AgentSpecialty.BUILDER: DispatchOutcome.TIMED_OUT})
    with pytest.raises(AgentDispatchFailure) as raised:
        h.gateway.invoke(task())
    assert raised.value.category == "replay-failure"
    h2 = Harness(tmp_path / "b", extra_result={"status": "blocked", "blockers": ["no access"]})
    with pytest.raises(AgentDispatchFailure) as blocked:
        h2.gateway.invoke(task())
    assert blocked.value.category == "envelope-blocked" and blocked.value.blockers == ("no access",)
    h3 = Harness(tmp_path / "c")
    missing = dict(h3.gateway.adapters)
    del missing[AgentSpecialty.BUILDER]
    broken = CanonicalUnitTestAgentGateway(h3.work, h3.store, h3.objects, missing, h3.root)
    with pytest.raises(AgentDispatchFailure, match="no-adapter"):
        broken.invoke(task())


def test_gateway_secret_looking_agent_output_is_refused_by_dispatch_policy(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        artifact_bodies=False,
        handlers={AgentSpecialty.BUILDER: lambda c, n: {"note": "my token"}},
    )
    with pytest.raises(AgentDispatchFailure) as raised:
        h.gateway.invoke(task())
    assert raised.value.category == "result-rejected-by-dispatch-policy"


def test_replay_adapter_is_clearly_labelled_mock() -> None:
    assert ReplayClientAdapter.__doc__ is not None and "REPLAY/MOCK" in ReplayClientAdapter.__doc__


# ---------------------------------------------------------------------------- body dogrulama


def request() -> UnitTestRequest:
    return UnitTestRequest.with_defaults(
        project_id="p1",
        source_binding_id="b1",
        source_revision="r1",
        source_files=["src/main/java/p/A.java"],
        percent="80",
        budget=UnitTestBudget(5, 60, 600),
    )


def test_planner_body_is_strict_and_testability_findings_need_evidence() -> None:
    body = parse_plan_body(default_planner({}, 1))
    assert body.plan_document["scenarios"]
    with pytest.raises(ValidationFailed):
        parse_plan_body({"plan": {}, "surprise": 1})
    with pytest.raises(ValidationFailed):
        parse_plan_body(
            {
                "plan": {},
                "testability_findings": [
                    {"observable": "x", "tried_test_only": [], "evidence_refs": []}
                ],
            }
        )
    ok = parse_plan_body(
        {
            "plan": {},
            "testability_findings": [
                {"observable": "clock", "tried_test_only": ["fake"], "evidence_refs": ["A.java:10"]}
            ],
        }
    )
    assert ok.testability_findings


def test_builder_body_requires_links_known_strategy_and_test_only_patch_shape() -> None:
    ctx = {"scenarios": [{"scenario_id": "S1"}]}
    good = parse_build_body(default_builder(ctx, 1), request())
    assert good.links[0].scenario_id == "S1" and good.patch.paths
    for broken in (
        {**default_builder(ctx, 1), "links": []},
        {**default_builder(ctx, 1), "approach": {"strategy": "magic", "note": "x"}},
        {**default_builder(ctx, 1), "surprise": 1},
        {k: v for k, v in default_builder(ctx, 1).items() if k != "patch"},
    ):
        with pytest.raises(ValidationFailed):
            parse_build_body(broken, request())


def test_verifier_cannot_accept_with_blocking_quality_finding_or_grant_exceptions() -> None:
    ctx = {"scenarios": [{"scenario_id": "S1"}]}
    ok = parse_verify_body(default_verifier(ctx, 1), request())
    assert ok.effective_verdict is Verdict.ACCEPTED and ok.verified == ("S1",)
    for code in sorted(BLOCKING_QUALITY_CODES):
        body = {**default_verifier(ctx, 1), "quality_findings": [{"code": code}]}
        assert parse_verify_body(body, request()).effective_verdict is Verdict.REJECTED
    soft = {**default_verifier(ctx, 1), "quality_findings": [{"code": "naming-nit"}]}
    assert parse_verify_body(soft, request()).effective_verdict is Verdict.ACCEPTED
    exception = {
        **default_verifier(ctx, 1),
        "scenario_results": [{"scenario_id": "S1", "status": "accepted-exception"}],
    }
    with pytest.raises(PolicyViolation):
        parse_verify_body(exception, request())
    risky = {**default_verifier(ctx, 1), "new_material_risks": [scenario_doc("N1")]}
    assert parse_verify_body(risky, request()).new_material_risks[0].scenario_id == "N1"
    triage = parse_verify_body(default_triage(ctx, 1), request())
    assert triage.defect_review is not None and triage.defect_review.finding == "defect-confirmed"
    with pytest.raises(ValidationFailed):
        parse_verify_body(
            {
                "verdict": "accepted",
                "defect_review": {"scenario_id": "S1", "finding": "meh", "evidence_refs": []},
            },
            request(),
        )
    with pytest.raises(ValidationFailed):
        parse_verify_body({"verdict": "maybe"}, request())


# ---------------------------------------------------------------------------- Maven adaptoru


TARGET = "src/main/java/p/A.java"


def maven_request(percent: str = "70") -> UnitTestRequest:
    return UnitTestRequest.with_defaults(
        project_id="proj1",
        source_binding_id="bind1",
        source_revision="rev-1",
        source_files=[TARGET],
        percent=percent,
        budget=UnitTestBudget(3, 60, 600),
    )


def test_fake_tool_measurer_returns_real_record_and_failure_diagnostics(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "proj")
    measurer = MavenUnitTestMeasurer(
        root,
        ("",),
        tmp_path / "locks",
        allow_network=True,
        search_path=fake.search_path,
        maven_version="3.9.9",
    )
    fake.set(**good_scenario())
    run = measurer.measure(
        maven_request(),
        attempt_id="a1",
        run_id="r1",
        candidate_digest=digest("c1"),
        timeout_seconds=30,
        cancel=threading.Event(),
    )
    assert run.record is not None and run.record.status is MeasurementStatus.ACCEPTED
    assert run.failed_cases == () and run.record.binding is not None
    assert run.record.binding.test_candidate_digest == digest("c1")
    failing = good_scenario(
        write={
            **good_scenario()["write"],
            "target/surefire-reports/TEST-p.ATest.xml": surefire_xml(
                [("p.ATest", "a", ""), ("p.ATest", "boom", "<failure/>")]
            ),
        },
        print=["AssertionFailedError: expected: <1> but was: <2>"],
        exit=1,
    )
    fake.set(**failing)
    bad = measurer.measure(
        maven_request(),
        attempt_id="a2",
        run_id="r2",
        candidate_digest=digest("c2"),
        timeout_seconds=30,
        cancel=threading.Event(),
    )
    assert bad.record is not None and bad.record.status is MeasurementStatus.TESTS_FAILED
    assert bad.failed_cases == ("p.ATest#boom",) and "AssertionFailedError" in bad.output_tail


def test_fake_tool_measurer_blocks_when_plan_is_not_ready_or_drifted(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "proj")
    pinned = MavenUnitTestMeasurer(
        root,
        ("",),
        tmp_path / "locks",
        search_path=fake.search_path,
        approved_plan_digest=digest("some-other-plan"),
    )
    blocked = pinned.measure(
        maven_request(),
        attempt_id="a",
        run_id="r",
        candidate_digest=digest("c"),
        timeout_seconds=30,
        cancel=threading.Event(),
    )
    assert blocked.record is None and "plan-digest-drift" in blocked.blocked_details[0]
    (root / "pom.xml").unlink()
    unsupported = MavenUnitTestMeasurer(root, ("",), tmp_path / "locks").measure(
        maven_request(),
        attempt_id="a",
        run_id="r",
        candidate_digest=digest("c"),
        timeout_seconds=30,
        cancel=threading.Event(),
    )
    assert unsupported.record is None
    assert unsupported.blocked_reason is UnitTestStopReason.TECHNOLOGY_UNSUPPORTED
    assert isinstance(unsupported, MeasuredRun)


def test_environment_observer_reads_digests_and_sees_external_production_edit(
    tmp_path: Path,
) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "proj")
    observer = MavenEnvironmentObserver(
        root,
        ("",),
        (TARGET,),
        lambda: "rev-1",
        lambda: frozenset({"x/DirtyTest.java"}),
        search_path=fake.search_path,
    )
    before = observer.observe()
    assert before.protected_dirty_paths == frozenset({"x/DirtyTest.java"})
    assert before.source_revision == "rev-1" and set(before.production_digests) == {TARGET}
    (root / TARGET).write_text("package p; class A { int f(){return 2;} }\n", encoding="utf-8")
    after = observer.observe()
    assert after.drifted_from(before) == ("production",)
    assert before.drifted_from(before) == ()
