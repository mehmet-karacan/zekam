"""ZK-P2-005 golden resume corpus acceptance tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from zekam.application.golden_resume_corpus import (
    GOLDEN_RESUME_V1_DIGEST,
    default_golden_resume_corpus,
)
from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import (
    OpenEffect,
    OpenEffectState,
    Resumability,
    SandboxBindingV2,
    SandboxDisposition,
    StaleDigestBindings,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.golden_resume import GoldenResumeActual, evaluate_golden_resume
from zekam.domain.resume import ResumeObservation, RuntimeObservation


def _id(value: int) -> UUID:
    return UUID(int=value)


def _bindings(prefix: str = "same") -> StaleDigestBindings:
    return StaleDigestBindings(
        routing_context_snapshot_id=_id(90 if prefix == "same" else 91),
        source_revision="revision-1" if prefix == "same" else "revision-2",
        policy_digest=digest(f"{prefix}:policy"),
        capability_profile_digest=digest(f"{prefix}:capability"),
        dependency_snapshot_digest=digest(f"{prefix}:dependency"),
        migration_head_digest=digest(f"{prefix}:migration"),
        model_route_decision_digest=digest(f"{prefix}:route"),
        context_manifest_digest=digest(f"{prefix}:manifest"),
        context_packet_digest=digest(f"{prefix}:packet"),
        architecture_digest=digest(f"{prefix}:architecture"),
        rules_digest=digest(f"{prefix}:rules"),
        test_suite_digest=digest(f"{prefix}:suite"),
        model_inventory_digest=digest(f"{prefix}:inventory"),
        journal_head_digest=digest(f"{prefix}:journal"),
    )


def _observation(**changes: Any) -> ResumeObservation:
    value = ResumeObservation(
        realm_id=_id(1),
        project_id=_id(2),
        work_item_id=_id(3),
        work_state="active",
        checkpoint_id=_id(4),
        checkpoint_digest=digest("checkpoint"),
        checkpoint_revision=1,
        checkpoint_key="work-progress",
        plan_id=_id(5),
        plan_digest=digest("plan"),
        current_plan_id=_id(5),
        current_plan_digest=digest("plan"),
        checkpoint_bindings=_bindings(),
        current_bindings=_bindings(),
        pending_steps=("build",),
        next_step_id="build",
        open_effects=(),
        checkpoint_integrity=True,
        resumability=Resumability.SAFE_CONTINUE,
        logical_read_resources=("project:2:source",),
        logical_write_resources=("project:2:file:src/app.py",),
        runtime=RuntimeObservation(
            _id(60),
            _id(61),
            _id(62),
            _id(63),
            _id(64),
            digest("envelope"),
            _id(65),
            3,
            "running",
            dt.datetime(2026, 8, 23, 23, 59, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 25, tzinfo=dt.UTC),
        ),
        target_client_id="codex",
        required_route_role="implementer",
        context_recipe="resume:codex:implementer",
        observed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
        valid_until=dt.datetime(2026, 8, 24, 1, tzinfo=dt.UTC),
    )
    return replace(value, **changes)


def _plan(value: ResumeObservation, *, repository_realm: UUID | None = None):  # type: ignore[no-untyped-def]
    class Repository:
        realm_id = value.realm_id if repository_realm is None else repository_realm

        def read_snapshot(self, *_: object, **__: object) -> ResumeObservation:
            return value

    return ResumeCoordinator(Repository()).prepare(value.work_item_id, client_id="codex")


def _case(case_id: str):  # type: ignore[no-untyped-def]
    return next(item for item in default_golden_resume_corpus().cases if item.case_id == case_id)


def test_corpus_contains_exact_twelve_report_scenarios_and_is_stable() -> None:
    first = default_golden_resume_corpus()
    second = default_golden_resume_corpus()
    assert len(first.cases) == 12
    assert first.corpus_digest == second.corpus_digest
    assert first.corpus_digest == GOLDEN_RESUME_V1_DIGEST
    assert {item.category for item in first.cases}
    assert not first.grants_authority


def test_current_coordinator_matches_clean_cross_client_and_dirty_sandbox_goldens() -> None:
    corpus = default_golden_resume_corpus()
    clean = GoldenResumeActual.from_plan(_plan(_observation()))
    assert evaluate_golden_resume(corpus, _case("resume-01-clean-read-only"), clean).passed
    assert evaluate_golden_resume(corpus, _case("resume-09-cross-client"), clean).passed

    dirty = replace(
        _observation(),
        sandbox=SandboxBindingV2(
            SandboxDisposition.DIRTY,
            "workspace-1",
            "revision-1",
            digest("patch"),
            digest("dirty-state"),
        ),
    )
    actual = GoldenResumeActual.from_plan(_plan(dirty))
    assert evaluate_golden_resume(corpus, _case("resume-11-dirty-sandbox"), actual).passed


def test_current_coordinator_matches_source_drift_receiptless_and_legacy_goldens() -> None:
    corpus = default_golden_resume_corpus()
    drifted = replace(
        _bindings(),
        source_revision="revision-2",
    )
    source = GoldenResumeActual.from_plan(_plan(_observation(current_bindings=drifted)))
    assert evaluate_golden_resume(corpus, _case("resume-02-source-drift"), source).passed

    effect = OpenEffect(_id(70), digest("effect"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT)
    receiptless = GoldenResumeActual.from_plan(_plan(_observation(open_effects=(effect,))))
    assert evaluate_golden_resume(corpus, _case("resume-06-receiptless-effect"), receiptless).passed

    legacy = GoldenResumeActual.from_plan(_plan(_observation(legacy_limited=True)))
    assert evaluate_golden_resume(corpus, _case("resume-08-legacy-v1-handoff"), legacy).passed


@pytest.mark.parametrize(
    ("case_id", "binding_field"),
    (
        ("resume-03-policy-drift", "policy_digest"),
        ("resume-04-route-drift", "model_route_decision_digest"),
        ("resume-05-context-drift", "context_manifest_digest"),
    ),
)
def test_current_coordinator_matches_recompile_goldens(case_id: str, binding_field: str) -> None:
    current = replace(_bindings(), **{binding_field: digest(f"changed:{binding_field}")})
    actual = GoldenResumeActual.from_plan(_plan(_observation(current_bindings=current)))
    corpus = default_golden_resume_corpus()
    assert evaluate_golden_resume(corpus, _case(case_id), actual).passed


def test_current_coordinator_matches_completed_partition_and_budget_goldens() -> None:
    corpus = default_golden_resume_corpus()
    completed = GoldenResumeActual.from_plan(_plan(_observation(work_state="completed")))
    assert evaluate_golden_resume(
        corpus, _case("resume-07-completed-without-checkpoint"), completed
    ).passed

    runtime = replace(
        _observation().runtime,
        deadline=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    exhausted = GoldenResumeActual.from_plan(_plan(_observation(runtime=runtime)))
    assert evaluate_golden_resume(corpus, _case("resume-12-budget-exhausted"), exhausted).passed


def test_rejected_cross_realm_case_and_regression_findings_are_exact() -> None:
    corpus = default_golden_resume_corpus()
    with pytest.raises(PolicyViolation, match=r"resume\.cross-realm-binding") as error:
        _plan(_observation(), repository_realm=_id(999))
    rejected = GoldenResumeActual.rejected(str(error.value))
    result = evaluate_golden_resume(corpus, _case("resume-10-cross-realm-forgery"), rejected)
    assert result.passed

    wrong = replace(rejected, reason_codes=("wrong",), error_code="wrong")
    failed = evaluate_golden_resume(corpus, _case("resume-10-cross-realm-forgery"), wrong)
    assert not failed.passed
    assert [(item.field, item.expected, item.actual) for item in failed.findings] == [
        ("reason_codes", ("resume.cross-realm-binding",), ("wrong",))
    ]


def test_forged_actual_patch_and_nested_expectation_tamper_fail_closed() -> None:
    actual = GoldenResumeActual.from_plan(_plan(_observation()))
    with pytest.raises(ValidationFailed, match="body digest"):
        replace(actual, plan_digest=digest("no-resume-plan-exists"))
    with pytest.raises(ValidationFailed, match="ResumePlan"):
        replace(actual, plan={"schema": "zekam-resume-plan/v1"})  # type: ignore[arg-type]

    nested_plan = _plan(_observation())
    object.__setattr__(nested_plan.runtime, "execution_envelope_digest", "not-a-digest")
    with pytest.raises(ValidationFailed, match="Digest"):
        GoldenResumeActual.from_plan(nested_plan)

    dirty = replace(
        _observation(),
        sandbox=SandboxBindingV2(
            SandboxDisposition.DIRTY,
            "workspace-1",
            "revision-1",
            digest("patch"),
            digest("dirty-state"),
        ),
    )
    dirty_actual = GoldenResumeActual.from_plan(_plan(dirty))
    with pytest.raises(ValidationFailed, match="patch digest"):
        replace(dirty_actual, patch_digest="not-a-digest")

    corpus = default_golden_resume_corpus()
    expectation = corpus.cases[0].expectation
    object.__setattr__(expectation, "disposition", "attacker-value")
    with pytest.raises(ValidationFailed, match="disposition"):
        evaluate_golden_resume(corpus, corpus.cases[0], actual)
