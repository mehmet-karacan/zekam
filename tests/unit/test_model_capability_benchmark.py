from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.model_capability_benchmark import (
    CapabilityCheckpointReceipt,
    CapabilityCohortRunner,
    CapabilityFixture,
    CapabilityResponse,
    CapabilityToolReceipt,
    CapabilityVerifier,
    capability_acceptance_evidence_digest,
    load_capability_registry,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    aggregate_capability_episodes,
)

ROOT = Path(__file__).resolve().parents[2]


def _loaded() -> tuple[Any, Any, dict[str, CapabilityFixture]]:
    return load_capability_registry(
        ROOT / "config" / "model_capability_benchmark.yaml",
        repository_root=ROOT,
    )


def _plan(model_ids: tuple[str, ...] = ("model-a", "model-b")) -> CapabilityCohortPlan:
    registry, profile, _ = _loaded()
    return CapabilityCohortPlan(
        source_campaign_id=uuid4(),
        source_revision="revision-1",
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        verifier_provenance_digest=digest("campaign-verifier"),
        model_ids=model_ids,
        registry=registry,
        execution_profile=profile,
        max_parallelism=len(model_ids),
    )


class FakeAdapter:
    adapter_identity = "fake-capability-adapter"

    def __init__(self) -> None:
        self.starts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def execute(
        self,
        *,
        model_id: str,
        task: Any,
        fixture: Any,
        profile: Any,
        turn_index: int,
        prior_response_digest: str | None,
        cancellation: threading.Event,
    ) -> CapabilityResponse:
        del prior_response_digest
        assert not cancellation.is_set()
        with self._lock:
            self.starts.setdefault(task.task_id, []).append(time.monotonic())
        time.sleep(0.02)
        payload = fixture.payload
        response = CapabilityResponse(
            payload={
                "status": "completed",
                "markers": list(payload["expected_markers"]),
                "artifact_digest": digest((model_id, task.task_digest, turn_index, "artifact")),
            },
            duration_ms=20,
            input_tokens=100,
            output_tokens=200,
            provider_latency_ms=10,
            checkpoint_receipts=tuple(
                CapabilityCheckpointReceipt(
                    name=name,
                    elapsed_ms=(turn_index - 1) * 20 + index * 5,
                    artifact_digest=digest((model_id, task.task_digest, turn_index, name)),
                    acceptance_passed=index,
                    acceptance_total=len(payload["required_checkpoints"]),
                )
                for index, name in enumerate(payload["required_checkpoints"], start=1)
            ),
            tool_receipts=(
                CapabilityToolReceipt(
                    "read", digest((model_id, task.task_digest, turn_index, "read"))
                ),
                CapabilityToolReceipt(
                    "test", digest((model_id, task.task_digest, turn_index, "test"))
                ),
            ),
            self_correction_count=1,
            hidden_acceptance_passed=4,
            hidden_acceptance_total=4,
            regression_count=0,
            unsafe=False,
            acceptance_evidence_digest=digest("pending-acceptance"),
        )
        return replace(
            response,
            acceptance_evidence_digest=capability_acceptance_evidence_digest(
                task, response, profile.evaluator_provenance_digest
            ),
        )


def _verifier() -> CapabilityVerifier:
    return CapabilityVerifier(
        model_id="independent-verifier",
        execution_identity="separate-execution-slot",
        provenance_digest=digest("independent-capability-verifier-v1"),
    )


def test_registry_and_exact_plan_budget() -> None:
    plan = _plan()
    assert len(plan.registry.tasks) == 3
    assert plan.provider_call_budget == 48
    assert plan.maximum_wall_seconds == 900
    assert plan.execution_profile.wall_budget_seconds == 300
    assert plan.execution_profile.max_retries == 0

    seven_model_plan = _plan(tuple(f"model-{index}" for index in range(7)))
    assert seven_model_plan.provider_call_budget == 168
    assert seven_model_plan.max_parallelism == 7


def test_parallel_runner_starts_each_model_in_same_task_wave() -> None:
    plan = _plan(("model-a", "model-b", "model-c"))
    _, _, fixtures = _loaded()
    adapter = FakeAdapter()
    results = CapabilityCohortRunner(adapter, _verifier()).run(plan, fixtures)

    assert len(results) == 9
    assert all(result.status is CapabilityEpisodeStatus.PASSED for result in results)
    assert all(result.start_skew_ms <= plan.start_skew_budget_ms for result in results)
    assert all(max(starts) - min(starts) < 0.1 for starts in adapter.starts.values())
    assert all("payload" not in repr(result).lower() for result in results)


def test_aggregate_is_ability_first_and_latency_is_report_only() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    episodes = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)
    result = aggregate_capability_episodes(plan, "model-a", episodes)
    slower = tuple(replace(row, duration_ms=row.duration_ms * 10) for row in episodes)
    slower_result = aggregate_capability_episodes(plan, "model-a", slower)

    assert result.general_score == slower_result.general_score
    assert result.mean_duration_ms < slower_result.mean_duration_ms
    assert {role.value for role, _ in result.role_scores} == {
        "implementer",
        "reviewer",
        "researcher",
    }


def test_self_verifier_and_incomplete_task_coverage_fail_closed() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    episodes = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)
    with pytest.raises(PolicyViolation, match="exact task coverage"):
        aggregate_capability_episodes(plan, "model-a", episodes[:-1])

    first: CapabilityEpisodeResult = episodes[0]
    with pytest.raises(PolicyViolation, match="kendi sonucunu"):
        replace(first, verifier_model_id="model-a")


def test_response_shape_and_progress_regression_fail_closed() -> None:
    task = _plan(("model-a",)).registry.tasks[0]
    response = CapabilityResponse(
        payload={"status": "completed"},
        duration_ms=1,
        input_tokens=1,
        output_tokens=1,
        provider_latency_ms=1,
        checkpoint_receipts=(
            CapabilityCheckpointReceipt("empty", 0, digest("empty-checkpoint"), 0, 1),
        ),
        tool_receipts=(),
        self_correction_count=0,
        hidden_acceptance_passed=0,
        hidden_acceptance_total=0,
        regression_count=0,
        unsafe=False,
        acceptance_evidence_digest=digest("invalid-shape"),
    )
    with pytest.raises(ValidationFailed, match="exact shape"):
        _verifier().verify(tested_model_id="model-a", task=task, response=response)

    _, _, fixtures = _loaded()
    fixture = fixtures[task.task_digest]
    payload = {
        "status": "completed",
        "markers": list(fixture.payload["expected_markers"]),
        "artifact_digest": digest("artifact"),
    }
    regressed = replace(
        response,
        payload=payload,
        checkpoint_receipts=(
            CapabilityCheckpointReceipt("first", 1, digest("first"), 1, 2),
            CapabilityCheckpointReceipt("second", 2, digest("second"), 0, 2),
        ),
        hidden_acceptance_passed=1,
        hidden_acceptance_total=1,
    )
    regressed = replace(
        regressed,
        acceptance_evidence_digest=capability_acceptance_evidence_digest(
            task, regressed, _verifier().provenance_digest
        ),
    )
    with pytest.raises(ValidationFailed, match="geriye"):
        _verifier().verify(
            tested_model_id="model-a",
            task=task,
            response=regressed,
        )


def test_registry_digest_drift_and_repository_escape_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "registry.yaml"
    config.write_text(
        (ROOT / "config" / "model_capability_benchmark.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="repository disinda"):
        load_capability_registry(config, repository_root=tmp_path)


def test_episode_evidence_contains_only_digests_and_metrics() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    result = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)[0]
    assert result.evidence_digest.startswith("sha256:")
    assert result.response_digest.startswith("sha256:")
    assert result.capability_score > 0.8
    assert result.started_at.tzinfo is dt.UTC


class BlockingAdapter(FakeAdapter):
    def execute(
        self,
        *,
        model_id: str,
        task: Any,
        fixture: Any,
        profile: Any,
        turn_index: int,
        prior_response_digest: str | None,
        cancellation: threading.Event,
    ) -> CapabilityResponse:
        del model_id, task, fixture, profile, turn_index, prior_response_digest
        cancellation.wait(timeout=1)
        raise PolicyViolation("injected cancellation observed")


def test_hard_deadline_sets_cooperative_cancellation() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    started = time.monotonic()
    with pytest.raises(PolicyViolation, match="hard deadline"):
        CapabilityCohortRunner(BlockingAdapter(), _verifier(), timeout_scale=0.001).run(
            plan, fixtures
        )
    assert time.monotonic() - started < 1


class TokenOverflowAdapter(FakeAdapter):
    def execute(self, **kwargs: Any) -> CapabilityResponse:
        return replace(
            super().execute(**kwargs),
            output_tokens=1_000_000,
        )


def test_token_budget_is_enforced_before_evaluation() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    with pytest.raises(PolicyViolation, match="token butcesi"):
        CapabilityCohortRunner(TokenOverflowAdapter(), _verifier()).run(plan, fixtures)


def test_checkpoint_receipts_must_arrive_in_strict_time_order() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    response = FakeAdapter().execute(
        model_id="model-a",
        task=plan.registry.tasks[0],
        fixture=fixtures[plan.registry.tasks[0].task_digest],
        profile=plan.execution_profile,
        turn_index=1,
        prior_response_digest=None,
        cancellation=threading.Event(),
    )
    with pytest.raises(ValidationFailed, match="sirali artmali"):
        replace(response, checkpoint_receipts=tuple(reversed(response.checkpoint_receipts)))


class NeverCompleteAdapter(FakeAdapter):
    def execute(self, **kwargs: Any) -> CapabilityResponse:
        response = super().execute(**kwargs)
        task = kwargs["task"]
        profile = kwargs["profile"]
        turn_index = kwargs["turn_index"]
        response = replace(
            response,
            payload={**response.payload, "status": "continue"},
            checkpoint_receipts=tuple(
                replace(
                    receipt,
                    acceptance_passed=(turn_index - 1) * len(task.required_checkpoints) + index,
                    acceptance_total=(profile.max_model_turns * len(task.required_checkpoints)),
                )
                for index, receipt in enumerate(response.checkpoint_receipts, start=1)
            ),
        )
        return replace(
            response,
            acceptance_evidence_digest=capability_acceptance_evidence_digest(
                task, response, profile.evaluator_provenance_digest
            ),
        )


def test_turn_exhaustion_cannot_become_passed() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    results = CapabilityCohortRunner(NeverCompleteAdapter(), _verifier()).run(plan, fixtures)
    assert len(results) == 3
    assert all(result.status is CapabilityEpisodeStatus.FAILED for result in results)
    assert all(
        result.model_turn_count == plan.execution_profile.max_model_turns for result in results
    )
