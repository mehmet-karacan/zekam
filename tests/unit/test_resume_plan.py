from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import (
    OpenEffect,
    OpenEffectState,
    Resumability,
    StaleDigestBindings,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.resume import (
    ResumeDisposition,
    ResumeObservation,
    ResumePlan,
    RuntimeObservation,
)

pytestmark = pytest.mark.unit


def uid(value: int) -> UUID:
    return UUID(int=value)


def bindings(prefix: str = "same") -> StaleDigestBindings:
    return StaleDigestBindings(
        routing_context_snapshot_id=uid(90 if prefix == "same" else 91),
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


def observation(**changes: Any) -> ResumeObservation:
    value = ResumeObservation(
        realm_id=uid(1),
        project_id=uid(2),
        work_item_id=uid(3),
        work_state="active",
        checkpoint_id=uid(4),
        checkpoint_digest=digest("checkpoint"),
        checkpoint_revision=1,
        checkpoint_key="work-progress",
        plan_id=uid(5),
        plan_digest=digest("plan"),
        current_plan_id=uid(5),
        current_plan_digest=digest("plan"),
        checkpoint_bindings=bindings(),
        current_bindings=bindings(),
        pending_steps=("build",),
        next_step_id="build",
        open_effects=(),
        checkpoint_integrity=True,
        resumability=Resumability.SAFE_CONTINUE,
        logical_read_resources=("project:2:source",),
        logical_write_resources=("project:2:file:src/app.py",),
        runtime=RuntimeObservation(
            run_id=uid(60),
            job_id=uid(61),
            attempt_id=uid(62),
            assignment_id=uid(63),
            execution_envelope_id=uid(64),
            execution_envelope_digest=digest("envelope"),
            observed_lease_id=uid(65),
            observed_fencing_token=3,
            job_state="running",
            lease_expires_at=dt.datetime(2026, 8, 23, 23, 59, tzinfo=dt.UTC),
            deadline=dt.datetime(2026, 8, 25, tzinfo=dt.UTC),
        ),
        target_client_id="codex",
        required_route_role="implementer",
        context_recipe="resume:codex:implementer",
        observed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
        valid_until=dt.datetime(2026, 8, 24, 1, tzinfo=dt.UTC),
    )
    return replace(value, **changes)


class Repository:
    def __init__(self, value: ResumeObservation) -> None:
        self.value = value

    def read_snapshot(self, *_: object, **__: object) -> ResumeObservation:
        return self.value


def prepare(value: ResumeObservation) -> ResumePlan:
    return ResumeCoordinator(Repository(value)).prepare(value.work_item_id, client_id="codex")


def test_clean_snapshot_is_deterministic_authority_free_continue_plan() -> None:
    first = prepare(observation())
    second = prepare(observation())
    assert first == second
    assert first.plan_digest == second.plan_digest
    assert first.disposition is ResumeDisposition.SAFE_CONTINUE
    assert first.reacquire_resources == (
        "authorization",
        "lease",
        "resource-lock:project:2:file:src/app.py",
    )
    assert [item.kind for item in first.actions] == ["reacquire", "dispatch-next-step"]
    assert not first.grants_authority
    assert not first.carries_active_lease
    assert not first.approval_inherited


def test_live_target_lease_waits_instead_of_duplicate_dispatch() -> None:
    runtime = replace(
        observation().runtime,
        lease_expires_at=dt.datetime(2026, 8, 24, 0, 1, tzinfo=dt.UTC),
    )
    plan = prepare(observation(runtime=runtime))
    assert plan.disposition is ResumeDisposition.WAITING
    assert plan.blockers == ("resume.target-job-active-lease",)
    assert plan.actions == ()


def test_receiptless_effect_precedes_drift_and_never_dispatches() -> None:
    effect = OpenEffect(uid(30), digest("effect"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT)
    plan = prepare(observation(open_effects=(effect,), current_bindings=bindings("drift")))
    assert plan.disposition is ResumeDisposition.RECOVERY_REQUIRED
    assert len(plan.reconciliation_actions) == 1
    assert [item.kind for item in plan.actions] == ["reconcile-effect"]
    assert plan.reacquire_resources == ()


def test_invalid_checkpoint_never_derives_reconciliation_from_untrusted_head() -> None:
    effect = OpenEffect(uid(31), digest("effect"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT)
    plan = prepare(observation(checkpoint_integrity=False, open_effects=(effect,)))
    assert plan.disposition is ResumeDisposition.MANUAL_REVIEW
    assert plan.selected_checkpoint_reason == "ambiguous-or-invalid-v2-head"
    assert plan.reconciliation_actions == ()
    assert plan.actions == ()


def test_failed_receipt_is_explicit_reconciliation_not_retry() -> None:
    effect = OpenEffect(uid(32), digest("failed-effect"), OpenEffectState.FAILED_RECONCILIATION)
    plan = prepare(observation(open_effects=(effect,)))
    assert plan.disposition is ResumeDisposition.RECOVERY_REQUIRED
    assert plan.reconciliation_actions[0].reason_code == "resume.failed-effect-reconciliation"
    assert [item.kind for item in plan.actions] == ["reconcile-effect"]


def test_drift_precedence_is_fail_closed() -> None:
    replan = prepare(
        observation(
            current_bindings=replace(
                bindings(), dependency_snapshot_digest=digest("dependency-changed")
            )
        )
    )
    assert replan.disposition is ResumeDisposition.SAFE_REPLAN
    assert replan.stale_dimensions[0].reason_code == "resume.dependency-snapshot-digest-drift"

    manual = prepare(
        observation(
            current_bindings=replace(bindings(), migration_head_digest=digest("migration-changed"))
        )
    )
    assert manual.disposition is ResumeDisposition.MANUAL_REVIEW
    assert not any(action.kind == "dispatch-next-step" for action in manual.actions)


def test_terminal_cancelled_and_legacy_states_do_not_continue() -> None:
    assert prepare(observation(work_state="cancelled")).disposition is ResumeDisposition.DENIED
    assert prepare(observation(legacy_limited=True)).disposition is ResumeDisposition.MANUAL_REVIEW
    assert (
        prepare(
            observation(work_state="completed", pending_steps=(), next_step_id=None)
        ).disposition
        is ResumeDisposition.ALREADY_COMPLETED
    )


def test_denied_work_may_report_effect_but_never_exposes_executable_action() -> None:
    effect = OpenEffect(uid(33), digest("cancelled-effect"), OpenEffectState.FAILED_RECONCILIATION)
    plan = prepare(observation(work_state="cancelled", open_effects=(effect,)))
    assert plan.disposition is ResumeDisposition.DENIED
    assert len(plan.reconciliation_actions) == 1
    assert plan.actions == ()
    assert plan.reacquire_resources == ()


def test_resume_plan_rejects_inherited_authority() -> None:
    safe = prepare(observation())
    with pytest.raises(PolicyViolation):
        replace(safe, grants_authority=True)
    with pytest.raises(PolicyViolation):
        replace(safe, carries_active_lease=True)
    with pytest.raises(PolicyViolation):
        replace(safe, approval_inherited=True)


def test_every_semantic_drift_changes_plan_digest() -> None:
    baseline = prepare(observation())
    changed = prepare(observation(current_plan_id=uid(50), current_plan_digest=digest("new-plan")))
    assert baseline.plan_digest != changed.plan_digest
    assert changed.disposition is ResumeDisposition.SAFE_REPLAN


def test_observation_clock_and_validity_are_exact_plan_semantics() -> None:
    first = prepare(observation())
    later = prepare(observation(observed_at=dt.datetime(2026, 8, 24, 0, 1, tzinfo=dt.UTC)))
    assert first.observed_at != later.observed_at
    assert first.plan_digest != later.plan_digest


def test_expired_validity_window_is_waiting_without_actions() -> None:
    plan = prepare(observation(observed_at=dt.datetime(2026, 8, 24, 1, tzinfo=dt.UTC)))
    assert plan.disposition is ResumeDisposition.WAITING
    assert plan.blockers == ("resume.plan-validity-expired",)
    assert plan.actions == ()
