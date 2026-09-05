from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    CapabilityExecutionProfile,
    CapabilityModelResult,
    CapabilityTaskRegistry,
    CapabilityTaskSpec,
    aggregate_capability_episodes,
)
from zekam.domain.model_routing import AgentRole, RouteCapabilityDimension

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
D = digest("bound")
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 12))


def _profile(**changes: Any) -> CapabilityExecutionProfile:
    values: dict[str, Any] = {
        "profile_id": "local-bounded",
        "version": 1,
        "wall_budget_seconds": 60,
        "cancellation_grace_seconds": 5,
        "max_model_turns": 4,
        "max_input_tokens_total": 4096,
        "max_output_tokens_total": 2048,
        "max_tool_calls": 8,
        "max_retries": 0,
        "allowed_tools": ("read", "test"),
        "sandbox_digest": D,
        "network_policy_digest": D,
        "evaluator_provenance_digest": D,
    }
    values.update(changes)
    return CapabilityExecutionProfile(**values)


def _task(role: AgentRole, index: int = 1, **changes: Any) -> CapabilityTaskSpec:
    values: dict[str, Any] = {
        "task_id": f"task-{role.value}-{index}",
        "version": 1,
        "role": role,
        "workload": "python",
        "fixture_source": f"fixtures/task-{index}.json",
        "content_digest": digest(("content", role.value, index)),
        "expected_schema_digest": D,
        "hidden_evaluator_digest": D,
        "max_duration_seconds": 45,
        "max_output_tokens": 1024,
        "required_checkpoints": ("implement", "verify"),
        "expected_markers": ("PASS",),
        "forbidden_markers": ("secret",),
        "minimum_self_corrections": 0,
        "max_tool_calls": 4,
        "route_dimensions": (RouteCapabilityDimension.TOOL,),
    }
    values.update(changes)
    return CapabilityTaskSpec(**values)


def _registry() -> CapabilityTaskRegistry:
    return CapabilityTaskRegistry(
        1,
        (
            _task(AgentRole.IMPLEMENTER, 1),
            _task(AgentRole.REVIEWER, 2),
            _task(AgentRole.RESEARCHER, 3),
        ),
    )


def _plan(**changes: Any) -> CapabilityCohortPlan:
    values: dict[str, Any] = {
        "source_campaign_id": IDS[0],
        "source_revision": "git:abc",
        "inventory_digest": D,
        "policy_digest": D,
        "verifier_provenance_digest": D,
        "model_ids": ("model-a", "model-b"),
        "registry": _registry(),
        "execution_profile": _profile(),
        "max_parallelism": 2,
    }
    values.update(changes)
    return CapabilityCohortPlan(**values)


def _episode(
    task: CapabilityTaskSpec, model_id: str = "model-a", **changes: Any
) -> CapabilityEpisodeResult:
    values: dict[str, Any] = {
        "model_id": model_id,
        "task_digest": task.task_digest,
        "role": task.role,
        "status": CapabilityEpisodeStatus.PASSED,
        "started_at": NOW,
        "duration_ms": 100,
        "start_skew_ms": 5,
        "model_turn_count": 2,
        "input_token_count": 100,
        "output_token_count": 50,
        "correctness": 1.0,
        "completion": 1.0,
        "sustained_progress": 0.8,
        "context_retention": 0.9,
        "self_correction": 0.5,
        "tool_efficiency": 0.8,
        "safety": 1.0,
        "hidden_acceptance_ratio": 1.0,
        "sustained_progress_auc": 0.8,
        "longest_stagnation_ms": 0,
        "regression_count": 0,
        "noop_ratio": 0.0,
        "checkpoint_count": 1,
        "self_correction_count": 1,
        "tool_call_count": 1,
        "checkpoint_receipt_digests": (digest(("checkpoint", task.task_id)),),
        "tool_receipt_digests": (digest(("tool", task.task_id)),),
        "response_digest": digest(("response", task.task_id)),
        "verifier_model_id": "independent-verifier",
        "verifier_execution_identity": "verifier:one",
        "verifier_provenance_digest": D,
        "evidence_digest": digest(("evidence", task.task_id)),
        "acceptance_evidence_digest": D,
    }
    values.update(changes)
    return CapabilityEpisodeResult(**values)


def test_execution_profile_complete_boundary_rejection_matrix() -> None:
    profile = _profile()
    assert profile.profile_digest == _profile().profile_digest
    invalid: tuple[Callable[[], object], ...] = (
        lambda: _profile(profile_id=""),
        lambda: _profile(version=0),
        lambda: _profile(wall_budget_seconds=29),
        lambda: _profile(wall_budget_seconds=301),
        lambda: _profile(cancellation_grace_seconds=-1),
        lambda: _profile(cancellation_grace_seconds=31),
        lambda: _profile(max_model_turns=0),
        lambda: _profile(max_model_turns=17),
        lambda: _profile(max_retries=1),
        lambda: _profile(max_input_tokens_total=255),
        lambda: _profile(max_output_tokens_total=255),
        lambda: _profile(max_tool_calls=-1),
        lambda: _profile(max_tool_calls=65),
        lambda: _profile(allowed_tools=()),
        lambda: _profile(allowed_tools=("read", "read")),
    )
    for build in invalid:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()


def test_task_registry_and_cohort_reject_portability_duplicates_and_budget_drift() -> None:
    task = _task(AgentRole.IMPLEMENTER)
    assert task.task_digest == _task(AgentRole.IMPLEMENTER).task_digest
    invalid_tasks: tuple[Callable[[], object], ...] = (
        lambda: _task(AgentRole.IMPLEMENTER, task_id=""),
        lambda: _task(AgentRole.IMPLEMENTER, workload=""),
        lambda: _task(AgentRole.IMPLEMENTER, version=0),
        lambda: _task(AgentRole.IMPLEMENTER, fixture_source="/absolute"),
        lambda: _task(AgentRole.IMPLEMENTER, fixture_source="a\\b"),
        lambda: _task(AgentRole.IMPLEMENTER, fixture_source="a/../b"),
        lambda: _task(AgentRole.IMPLEMENTER, max_duration_seconds=29),
        lambda: _task(AgentRole.IMPLEMENTER, max_output_tokens=255),
        lambda: _task(AgentRole.IMPLEMENTER, required_checkpoints=("one",)),
        lambda: _task(AgentRole.IMPLEMENTER, required_checkpoints=("one", "one")),
        lambda: _task(AgentRole.IMPLEMENTER, expected_markers=()),
        lambda: _task(AgentRole.IMPLEMENTER, expected_markers=(" ",)),
        lambda: _task(AgentRole.IMPLEMENTER, forbidden_markers=("x", "x")),
        lambda: _task(AgentRole.IMPLEMENTER, minimum_self_corrections=-1),
        lambda: _task(AgentRole.IMPLEMENTER, max_tool_calls=-1),
        lambda: _task(
            AgentRole.IMPLEMENTER,
            route_dimensions=(
                RouteCapabilityDimension.TOOL,
                RouteCapabilityDimension.TOOL,
            ),
        ),
    )
    for build in invalid_tasks:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()

    registry = _registry()
    assert registry.registry_digest == _registry().registry_digest
    with pytest.raises(ValidationFailed, match="en az uc"):
        CapabilityTaskRegistry(0, registry.tasks)
    with pytest.raises(ValidationFailed, match="tekrarli"):
        CapabilityTaskRegistry(1, (registry.tasks[0], registry.tasks[0], registry.tasks[2]))
    with pytest.raises(PolicyViolation, match="implementer/reviewer/researcher"):
        CapabilityTaskRegistry(
            1,
            (
                _task(AgentRole.IMPLEMENTER, 1),
                _task(AgentRole.IMPLEMENTER, 2),
                _task(AgentRole.IMPLEMENTER, 3),
            ),
        )

    plan = _plan()
    assert plan.provider_call_budget == 24
    assert plan.maximum_wall_seconds == 135
    assert plan.suite_digest and plan.plan_digest
    invalid_plans: tuple[Callable[[], object], ...] = (
        lambda: _plan(source_revision=""),
        lambda: _plan(source_revision="https://remote"),
        lambda: _plan(model_ids=()),
        lambda: _plan(model_ids=("model-a", "model-a")),
        lambda: _plan(model_ids=("model-a", " ")),
        lambda: _plan(max_parallelism=0),
        lambda: _plan(max_parallelism=3),
        lambda: _plan(start_skew_budget_ms=-1),
        lambda: _plan(start_skew_budget_ms=2001),
        lambda: _plan(execution_profile=_profile(wall_budget_seconds=30)),
        lambda: _plan(execution_profile=_profile(max_output_tokens_total=512)),
        lambda: _plan(execution_profile=_profile(max_tool_calls=3)),
    )
    for build in invalid_plans:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()


def test_episode_model_result_and_aggregation_exact_binding_matrix() -> None:
    plan = _plan()
    episodes = tuple(_episode(task) for task in plan.registry.tasks)
    assert 0 <= episodes[0].capability_score <= 1
    invalid_episodes: tuple[Callable[[], object], ...] = (
        lambda: _episode(plan.registry.tasks[0], started_at=NOW.replace(tzinfo=None)),
        lambda: _episode(plan.registry.tasks[0], duration_ms=-1),
        lambda: _episode(plan.registry.tasks[0], model_turn_count=0),
        lambda: _episode(plan.registry.tasks[0], correctness=-0.1),
        lambda: _episode(plan.registry.tasks[0], safety=1.1),
        lambda: _episode(plan.registry.tasks[0], checkpoint_count=-1),
        lambda: _episode(plan.registry.tasks[0], checkpoint_count=2),
        lambda: _episode(plan.registry.tasks[0], tool_call_count=2),
        lambda: _episode(plan.registry.tasks[0], verifier_model_id="model-a"),
        lambda: _episode(plan.registry.tasks[0], verifier_execution_identity=" "),
    )
    for build in invalid_episodes:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()

    result = aggregate_capability_episodes(plan, "model-a", episodes)
    assert result.completion_rate == 1.0 and result.mean_duration_ms == 100
    invalid_results: tuple[Callable[[], object], ...] = (
        lambda: CapabilityModelResult("", (D,), 0.5, (), 1.0, 1.0, D),
        lambda: CapabilityModelResult("model", (D,), 1.1, (), 1.0, 1.0, D),
        lambda: CapabilityModelResult("model", (D,), 0.5, (), -0.1, 1.0, D),
        lambda: CapabilityModelResult(
            "model",
            (D,),
            0.5,
            ((AgentRole.IMPLEMENTER, 0.5), (AgentRole.IMPLEMENTER, 0.6)),
            1.0,
            1.0,
            D,
        ),
        lambda: CapabilityModelResult(
            "model", (D,), 0.5, ((AgentRole.IMPLEMENTER, 1.1),), 1.0, 1.0, D
        ),
    )
    for build in invalid_results:
        with pytest.raises(ValidationFailed):
            build()

    with pytest.raises(PolicyViolation, match="model seti"):
        aggregate_capability_episodes(plan, "unknown", episodes)
    with pytest.raises(PolicyViolation, match="task coverage"):
        aggregate_capability_episodes(plan, "model-a", episodes[:-1])
    duplicate = (*episodes, episodes[0])
    with pytest.raises(PolicyViolation, match="tekilligi"):
        aggregate_capability_episodes(plan, "model-a", duplicate)
    wrong_model = (replace(episodes[0], model_id="model-b"), *episodes[1:])
    with pytest.raises(PolicyViolation, match="tekilligi"):
        aggregate_capability_episodes(plan, "model-a", cast(Any, wrong_model))
    wrong_role = (replace(episodes[0], role=AgentRole.REVIEWER), *episodes[1:])
    with pytest.raises(PolicyViolation, match="task-role"):
        aggregate_capability_episodes(plan, "model-a", cast(Any, wrong_role))
