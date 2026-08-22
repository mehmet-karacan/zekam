"""Uzun soluklu model yetenek ve dayaniklilik benchmark sozlesmeleri.

Kalici kayitlar ham prompt veya yanit tasimaz. Gorev artefakti repository'de
surumlu ve public'tir; episode kaydi yalniz digest, sure ve dogrulanmis puanlari
tasir. Gecikme raporlanir fakat genel yetenek puanina katilmaz.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_routing import AgentRole


class CapabilityEpisodeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNSAFE = "unsafe"
    INFRASTRUCTURE_INVALID = "infrastructure-invalid"
    NOT_COMPARABLE = "not-comparable"


@dataclass(frozen=True, slots=True)
class CapabilityExecutionProfile:
    profile_id: str
    version: int
    wall_budget_seconds: int
    cancellation_grace_seconds: int
    max_model_turns: int
    max_input_tokens_total: int
    max_output_tokens_total: int
    max_tool_calls: int
    max_retries: int
    allowed_tools: tuple[str, ...]
    sandbox_digest: str
    network_policy_digest: str
    evaluator_provenance_digest: str

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or self.version < 1:
            raise ValidationFailed("Capability execution profile kimligi gecersiz")
        if not 30 <= self.wall_budget_seconds <= 300:
            raise PolicyViolation("Capability wall butcesi 30..300 saniye olmali")
        if not 0 <= self.cancellation_grace_seconds <= 30:
            raise PolicyViolation("Capability cancellation grace gecersiz")
        if not 1 <= self.max_model_turns <= 16 or self.max_retries != 0:
            raise PolicyViolation("Capability turn/retry butcesi gecersiz")
        if min(self.max_input_tokens_total, self.max_output_tokens_total) < 256:
            raise PolicyViolation("Capability token butcesi yetersiz")
        if not 0 <= self.max_tool_calls <= 64:
            raise PolicyViolation("Capability tool butcesi gecersiz")
        if not self.allowed_tools or len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValidationFailed("Capability allowed tool seti gecersiz")
        for value in (
            self.sandbox_digest,
            self.network_policy_digest,
            self.evaluator_provenance_digest,
        ):
            parse_digest(value)

    @property
    def profile_digest(self) -> str:
        return digest(
            {
                "profile_id": self.profile_id,
                "version": self.version,
                "wall_budget_seconds": self.wall_budget_seconds,
                "cancellation_grace_seconds": self.cancellation_grace_seconds,
                "max_model_turns": self.max_model_turns,
                "max_input_tokens_total": self.max_input_tokens_total,
                "max_output_tokens_total": self.max_output_tokens_total,
                "max_tool_calls": self.max_tool_calls,
                "max_retries": self.max_retries,
                "allowed_tools": list(self.allowed_tools),
                "sandbox_digest": self.sandbox_digest,
                "network_policy_digest": self.network_policy_digest,
                "evaluator_provenance_digest": self.evaluator_provenance_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityTaskSpec:
    task_id: str
    version: int
    role: AgentRole
    workload: str
    fixture_source: str
    content_digest: str
    expected_schema_digest: str
    hidden_evaluator_digest: str
    max_duration_seconds: int
    max_output_tokens: int
    required_checkpoints: tuple[str, ...]
    expected_markers: tuple[str, ...]
    forbidden_markers: tuple[str, ...]
    minimum_self_corrections: int
    max_tool_calls: int

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.workload.strip() or self.version < 1:
            raise ValidationFailed("Capability task kimligi/surumu/workload zorunlu")
        source = PurePosixPath(self.fixture_source)
        if (
            source.is_absolute()
            or "\\" in self.fixture_source
            or ".." in source.parts
            or source.as_posix() != self.fixture_source
        ):
            raise PolicyViolation("Capability fixture source portable olmali")
        parse_digest(self.content_digest)
        parse_digest(self.expected_schema_digest)
        parse_digest(self.hidden_evaluator_digest)
        if not 30 <= self.max_duration_seconds <= 300:
            raise PolicyViolation("Capability task suresi 30..300 saniye olmali")
        if not 256 <= self.max_output_tokens <= 16_384:
            raise PolicyViolation("Capability output token butcesi gecersiz")
        if not 2 <= len(self.required_checkpoints) <= 12:
            raise ValidationFailed("Capability task 2..12 checkpoint ister")
        for values, label in (
            (self.required_checkpoints, "checkpoint"),
            (self.expected_markers, "expected marker"),
        ):
            if (
                not values
                or len(values) != len(set(values))
                or any(not row.strip() for row in values)
            ):
                raise ValidationFailed(f"Capability {label} seti gecersiz")
        if len(self.forbidden_markers) != len(set(self.forbidden_markers)):
            raise ValidationFailed("Capability forbidden marker seti tekrarli")
        if self.minimum_self_corrections < 0 or self.max_tool_calls < 0:
            raise ValidationFailed("Capability correction/tool butcesi negatif olamaz")

    @property
    def task_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "role": self.role.value,
            "workload": self.workload,
            "fixture_source": self.fixture_source,
            "content_digest": self.content_digest,
            "expected_schema_digest": self.expected_schema_digest,
            "hidden_evaluator_digest": self.hidden_evaluator_digest,
            "max_duration_seconds": self.max_duration_seconds,
            "max_output_tokens": self.max_output_tokens,
            "required_checkpoints": list(self.required_checkpoints),
            "expected_markers": list(self.expected_markers),
            "forbidden_markers": list(self.forbidden_markers),
            "minimum_self_corrections": self.minimum_self_corrections,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass(frozen=True, slots=True)
class CapabilityTaskRegistry:
    schema_version: int
    tasks: tuple[CapabilityTaskSpec, ...]

    def __post_init__(self) -> None:
        if self.schema_version < 1 or len(self.tasks) < 3:
            raise ValidationFailed("Capability registry en az uc gorev ister")
        identities = tuple((task.task_id, task.version) for task in self.tasks)
        if len(identities) != len(set(identities)):
            raise ValidationFailed("Capability task identity tekrarli")
        required_roles = {AgentRole.IMPLEMENTER, AgentRole.REVIEWER, AgentRole.RESEARCHER}
        if not required_roles <= {task.role for task in self.tasks}:
            raise PolicyViolation("Capability registry implementer/reviewer/researcher ister")

    @property
    def registry_digest(self) -> str:
        return digest(
            {
                "schema_version": self.schema_version,
                "tasks": [task.as_dict() for task in self.tasks],
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityCohortPlan:
    source_campaign_id: UUID
    source_revision: str
    inventory_digest: str
    policy_digest: str
    verifier_provenance_digest: str
    model_ids: tuple[str, ...]
    registry: CapabilityTaskRegistry
    execution_profile: CapabilityExecutionProfile
    max_parallelism: int
    start_skew_budget_ms: int = 500

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or "://" in self.source_revision:
            raise ValidationFailed("Capability source revision gecersiz")
        for value in (
            self.inventory_digest,
            self.policy_digest,
            self.verifier_provenance_digest,
        ):
            parse_digest(value)
        if not self.model_ids or len(self.model_ids) != len(set(self.model_ids)):
            raise ValidationFailed("Capability model seti bos/tekrarli olamaz")
        if any(not model_id.strip() for model_id in self.model_ids):
            raise ValidationFailed("Capability model id bos olamaz")
        if not 1 <= self.max_parallelism <= len(self.model_ids):
            raise PolicyViolation("Capability parallelism model sayisi ile sinirli")
        if not 0 <= self.start_skew_budget_ms <= 2_000:
            raise PolicyViolation("Capability start skew butcesi gecersiz")
        if any(
            task.max_duration_seconds > self.execution_profile.wall_budget_seconds
            or task.max_output_tokens > self.execution_profile.max_output_tokens_total
            or task.max_tool_calls > self.execution_profile.max_tool_calls
            for task in self.registry.tasks
        ):
            raise PolicyViolation("Capability task execution profile butcesini asiyor")

    @property
    def provider_call_budget(self) -> int:
        return (
            len(self.model_ids) * len(self.registry.tasks) * self.execution_profile.max_model_turns
        )

    @property
    def maximum_wall_seconds(self) -> int:
        waves = (len(self.model_ids) + self.max_parallelism - 1) // self.max_parallelism
        return waves * sum(task.max_duration_seconds for task in self.registry.tasks)

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "source_campaign_id": self.source_campaign_id,
                "source_revision": self.source_revision,
                "inventory_digest": self.inventory_digest,
                "policy_digest": self.policy_digest,
                "verifier_provenance_digest": self.verifier_provenance_digest,
                "model_ids": list(self.model_ids),
                "registry_digest": self.registry.registry_digest,
                "execution_profile_digest": self.execution_profile.profile_digest,
                "max_parallelism": self.max_parallelism,
                "start_skew_budget_ms": self.start_skew_budget_ms,
                "provider_call_budget": self.provider_call_budget,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityEpisodeResult:
    model_id: str
    task_digest: str
    role: AgentRole
    status: CapabilityEpisodeStatus
    started_at: dt.datetime
    duration_ms: int
    start_skew_ms: int
    model_turn_count: int
    input_token_count: int
    output_token_count: int
    correctness: float
    completion: float
    sustained_progress: float
    context_retention: float
    self_correction: float
    tool_efficiency: float
    safety: float
    hidden_acceptance_ratio: float
    sustained_progress_auc: float
    longest_stagnation_ms: int
    regression_count: int
    noop_ratio: float
    checkpoint_count: int
    self_correction_count: int
    tool_call_count: int
    checkpoint_receipt_digests: tuple[str, ...]
    tool_receipt_digests: tuple[str, ...]
    response_digest: str
    verifier_model_id: str
    verifier_execution_identity: str
    verifier_provenance_digest: str
    evidence_digest: str
    acceptance_evidence_digest: str

    def __post_init__(self) -> None:
        if (
            self.started_at.tzinfo is None
            or min(
                self.duration_ms,
                self.start_skew_ms,
                self.longest_stagnation_ms,
                self.regression_count,
                self.model_turn_count,
                self.input_token_count,
                self.output_token_count,
            )
            < 0
        ):
            raise ValidationFailed("Capability episode zaman kaniti gecersiz")
        if self.model_turn_count < 1:
            raise ValidationFailed("Capability episode en az bir model turn ister")
        scores = (
            self.correctness,
            self.completion,
            self.sustained_progress,
            self.context_retention,
            self.self_correction,
            self.tool_efficiency,
            self.safety,
            self.hidden_acceptance_ratio,
            self.sustained_progress_auc,
            self.noop_ratio,
        )
        if any(not 0 <= value <= 1 for value in scores):
            raise ValidationFailed("Capability puanlari 0..1 araliginda olmali")
        if min(self.checkpoint_count, self.self_correction_count, self.tool_call_count) < 0:
            raise ValidationFailed("Capability sayaclari negatif olamaz")
        if self.checkpoint_count != len(self.checkpoint_receipt_digests) or (
            self.tool_call_count != len(self.tool_receipt_digests)
        ):
            raise ValidationFailed("Capability receipt sayaci/digest seti uyusmuyor")
        for value in (
            self.task_digest,
            *self.checkpoint_receipt_digests,
            *self.tool_receipt_digests,
            self.response_digest,
            self.verifier_provenance_digest,
            self.evidence_digest,
            self.acceptance_evidence_digest,
        ):
            parse_digest(value)
        if self.model_id == self.verifier_model_id:
            raise PolicyViolation("Capability modeli kendi sonucunu dogrulayamaz")
        if not self.verifier_execution_identity.strip():
            raise ValidationFailed("Capability verifier execution identity ister")

    @property
    def capability_score(self) -> float:
        return (
            self.correctness * 0.30
            + self.completion * 0.20
            + self.sustained_progress * 0.15
            + self.context_retention * 0.15
            + self.self_correction * 0.10
            + self.tool_efficiency * 0.05
            + self.safety * 0.05
        )


@dataclass(frozen=True, slots=True)
class CapabilityModelResult:
    model_id: str
    episode_evidence_digests: tuple[str, ...]
    general_score: float
    role_scores: tuple[tuple[AgentRole, float], ...]
    completion_rate: float
    mean_duration_ms: float
    evidence_digest: str

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not 0 <= self.general_score <= 1:
            raise ValidationFailed("Capability model sonucu gecersiz")
        if not 0 <= self.completion_rate <= 1 or self.mean_duration_ms < 0:
            raise ValidationFailed("Capability completion/duration gecersiz")
        if len({role for role, _ in self.role_scores}) != len(self.role_scores):
            raise ValidationFailed("Capability role score tekrarli")
        if any(not 0 <= score <= 1 for _, score in self.role_scores):
            raise ValidationFailed("Capability role score 0..1 olmali")
        for value in (*self.episode_evidence_digests, self.evidence_digest):
            parse_digest(value)


def aggregate_capability_episodes(
    plan: CapabilityCohortPlan,
    model_id: str,
    episodes: tuple[CapabilityEpisodeResult, ...],
) -> CapabilityModelResult:
    expected = {task.task_digest: task.role for task in plan.registry.tasks}
    if model_id not in plan.model_ids:
        raise PolicyViolation("Capability result plan model seti disinda")
    if {episode.task_digest for episode in episodes} != set(expected):
        raise PolicyViolation("Capability result exact task coverage ister")
    if len(episodes) != len(expected) or any(episode.model_id != model_id for episode in episodes):
        raise PolicyViolation("Capability result model/task tekilligi bozuk")
    if any(episode.role is not expected[episode.task_digest] for episode in episodes):
        raise PolicyViolation("Capability result task-role binding bozuk")
    role_scores = tuple(
        sorted(
            (
                (
                    role,
                    sum(row.capability_score for row in episodes if row.role is role)
                    / sum(row.role is role for row in episodes),
                )
                for role in {row.role for row in episodes}
            ),
            key=lambda item: item[0].value,
        )
    )
    general_score = sum(row.capability_score for row in episodes) / len(episodes)
    completion_rate = sum(row.status is CapabilityEpisodeStatus.PASSED for row in episodes) / len(
        episodes
    )
    ordered = tuple(sorted(episodes, key=lambda row: (row.task_digest, row.evidence_digest)))
    evidence = digest(
        {
            "plan_digest": plan.plan_digest,
            "model_id": model_id,
            "episodes": [row.evidence_digest for row in ordered],
            "general_score": general_score,
            "role_scores": [(role.value, score) for role, score in role_scores],
            "completion_rate": completion_rate,
        }
    )
    return CapabilityModelResult(
        model_id=model_id,
        episode_evidence_digests=tuple(row.evidence_digest for row in ordered),
        general_score=general_score,
        role_scores=role_scores,
        completion_rate=completion_rate,
        mean_duration_ms=sum(row.duration_ms for row in episodes) / len(episodes),
        evidence_digest=evidence,
    )
