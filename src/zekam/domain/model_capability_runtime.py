"""Capability benchmark provider-call approval and runtime ledger contracts.

The reviewed live run is deliberately fixed: seven models, three public tasks and
eight continuity-derived provider turns per episode. Only bounded public state,
never a raw provider response, is carried into a later request.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed

EPISODE_COUNT = 21
SLOTS_PER_EPISODE = 8
MAX_PROVIDER_CALLS = EPISODE_COUNT * SLOTS_PER_EPISODE


class CapabilityRuntimeCallStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


class CapabilityRuntimeStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    RECOVERY_REQUIRED = "recovery-required"


class CapabilityRuntimeEpisodeStatus(StrEnum):
    SUCCESSFUL = "successful"
    MODEL_CONTRACT_FAILED = "model-contract-failed"
    RECOVERY_REQUIRED = "recovery-required"


MODEL_CONTRACT_FAILURE_REASONS = frozenset(
    {
        "malformed-model-response",
        "model-contract-failure",
        "model-response-contract",
        "continuity-contract-violation",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeApprovalManifest:
    cohort_id: UUID
    work_item_id: UUID
    task_plan_id: UUID
    coordinator_job_id: UUID
    source_revision: str
    model_ids: tuple[str, ...]
    task_digests: tuple[str, ...]
    approval_evidence_digest: str
    episode_count: int = EPISODE_COUNT
    slots_per_episode: int = SLOTS_PER_EPISODE
    max_provider_calls: int = MAX_PROVIDER_CALLS
    max_retries: int = 0

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or "://" in self.source_revision:
            raise ValidationFailed("Capability runtime source revision gecersiz")
        if len(self.model_ids) != 7 or len(set(self.model_ids)) != 7:
            raise ValidationFailed("Capability runtime tam yedi unique model ister")
        if any(not item.strip() for item in self.model_ids):
            raise ValidationFailed("Capability runtime model kimligi bos olamaz")
        if len(self.task_digests) != 3 or len(set(self.task_digests)) != 3:
            raise ValidationFailed("Capability runtime tam uc unique task ister")
        for value in (*self.task_digests, self.approval_evidence_digest):
            parse_digest(value)
        if (
            self.episode_count != EPISODE_COUNT
            or self.slots_per_episode != SLOTS_PER_EPISODE
            or self.max_provider_calls != MAX_PROVIDER_CALLS
            or self.max_retries != 0
        ):
            raise ValidationFailed("Capability runtime reviewed 21x8/168 retry-0 kapsamindan sapti")

    @property
    def manifest_digest(self) -> str:
        return digest(
            {
                "cohort_id": str(self.cohort_id),
                "work_item_id": str(self.work_item_id),
                "task_plan_id": str(self.task_plan_id),
                "coordinator_job_id": str(self.coordinator_job_id),
                "source_revision": self.source_revision,
                "model_ids": sorted(self.model_ids),
                "task_digests": sorted(self.task_digests),
                "episode_count": self.episode_count,
                "slots_per_episode": self.slots_per_episode,
                "max_provider_calls": self.max_provider_calls,
                "max_retries": self.max_retries,
                "approval_evidence_digest": self.approval_evidence_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeSlot:
    model_id: str
    task_digest: str
    turn_number: int
    ordinal: int
    job_id: UUID
    provider_ref: str
    backend_model: str
    endpoint_resource: str
    call_resource: str
    endpoint_identity_digest: str
    operation: str
    call_id: str
    fixture_digest: str
    fixture_identity_digest: str
    max_output_tokens: int
    request_template: dict[str, Any]
    request_template_digest: str
    derivation_rule_digest: str
    chain_seed_digest: str

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValidationFailed("Capability runtime slot model kimligi bos olamaz")
        if not all(
            value.strip()
            for value in (
                self.provider_ref,
                self.backend_model,
                self.endpoint_resource,
                self.call_resource,
                self.operation,
                self.call_id,
            )
        ):
            raise ValidationFailed("Capability runtime provider/endpoint/operation bos olamaz")
        for value in (
            self.task_digest,
            self.endpoint_identity_digest,
            self.fixture_digest,
            self.fixture_identity_digest,
            self.request_template_digest,
            self.derivation_rule_digest,
            self.chain_seed_digest,
        ):
            parse_digest(value)
        if not 1 <= self.turn_number <= SLOTS_PER_EPISODE:
            raise ValidationFailed("Capability runtime turn 1..8 olmali")
        if not 1 <= self.ordinal <= MAX_PROVIDER_CALLS:
            raise ValidationFailed("Capability runtime ordinal 1..168 olmali")
        if not 1 <= self.max_output_tokens <= 16384:
            raise ValidationFailed("Capability runtime output token cap gecersiz")
        if self.request_template.get("model") != self.backend_model:
            raise ValidationFailed("Capability runtime backend model/template mismatch")
        if digest(self.request_template) != self.request_template_digest:
            raise ValidationFailed("Capability runtime request template digest mismatch")

    @property
    def slot_digest(self) -> str:
        return digest(
            {
                "model_id": self.model_id,
                "task_digest": self.task_digest,
                "turn_number": self.turn_number,
                "ordinal": self.ordinal,
                "job_id": str(self.job_id),
                "provider_ref": self.provider_ref,
                "backend_model": self.backend_model,
                "endpoint_resource": self.endpoint_resource,
                "call_resource": self.call_resource,
                "endpoint_identity_digest": self.endpoint_identity_digest,
                "operation": self.operation,
                "call_id": self.call_id,
                "fixture_digest": self.fixture_digest,
                "fixture_identity_digest": self.fixture_identity_digest,
                "max_output_tokens": self.max_output_tokens,
                "request_template": self.request_template,
                "request_template_digest": self.request_template_digest,
                "derivation_rule_digest": self.derivation_rule_digest,
                "chain_seed_digest": self.chain_seed_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeDerivedAuthorization:
    authorization_id: UUID
    authorization_plan_digest: str
    authorization_digest: str
    request_body_digest: str
    effect_digest: str
    prior_response_chain_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.authorization_plan_digest,
            self.authorization_digest,
            self.request_body_digest,
            self.effect_digest,
            self.prior_response_chain_digest,
        ):
            parse_digest(value)

    @property
    def binding_digest(self) -> str:
        return digest(
            {
                "authorization_id": str(self.authorization_id),
                "authorization_plan_digest": self.authorization_plan_digest,
                "authorization_digest": self.authorization_digest,
                "request_body_digest": self.request_body_digest,
                "effect_digest": self.effect_digest,
                "prior_response_chain_digest": self.prior_response_chain_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeContinuityState:
    continuity_state: dict[str, Any]
    continuity_state_digest: str
    prior_result_digest: str
    derivation_attestation_digest: str
    checkpoint_id: UUID | None
    event_digest: str

    def __post_init__(self) -> None:
        if set(self.continuity_state) != {"facts", "open_questions", "risks", "next_action"}:
            raise ValidationFailed("Capability continuity exact typed keys ister")
        for value in (
            self.continuity_state_digest,
            self.prior_result_digest,
            self.derivation_attestation_digest,
            self.event_digest,
        ):
            parse_digest(value)


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeDerivation:
    request_body: dict[str, Any]
    request_body_digest: str
    authorization_plan_digest: str
    effect_digest: str
    effect_action: str
    claim_operation: str

    def __post_init__(self) -> None:
        for value in (
            self.request_body_digest,
            self.authorization_plan_digest,
            self.effect_digest,
        ):
            parse_digest(value)
        if not self.effect_action.startswith("provider-contract-call-"):
            raise ValidationFailed("Capability runtime effect action exact degil")
        if not self.claim_operation.startswith("provider-contract:"):
            raise ValidationFailed("Capability runtime claim operation exact degil")


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeTurnCheckpoint:
    continuity_state_id: UUID
    completed_turns: tuple[int, ...]
    pending_turns: tuple[int, ...]
    result_digest: str
    checkpoint_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.result_digest)
        parse_digest(self.checkpoint_digest)
        if set(self.completed_turns) & set(self.pending_turns):
            raise ValidationFailed("Capability turn checkpoint partition disjoint olmali")


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeCallOutcome:
    status: CapabilityRuntimeCallStatus
    claim_id: UUID
    checkpoint_id: UUID
    receipt_id: UUID | None
    result_digest: str | None
    failure_category: str | None
    evidence_digest: str
    completed_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        if self.result_digest is not None:
            parse_digest(self.result_digest)
        if self.completed_at.tzinfo is None:
            raise ValidationFailed("Capability runtime outcome timezone ister")
        if self.status is CapabilityRuntimeCallStatus.RECOVERY_REQUIRED:
            if self.receipt_id is not None or not self.failure_category:
                raise ValidationFailed("Recovery outcome receipt tasiyamaz ve failure ister")
        elif self.receipt_id is None:
            raise ValidationFailed("Terminal provider call receipt ister")
        if self.status is CapabilityRuntimeCallStatus.COMPLETED:
            if self.result_digest is None or self.failure_category is not None:
                raise ValidationFailed("Completed provider call exact result ister")
        elif self.failure_category is None:
            raise ValidationFailed("Non-success provider call failure category ister")


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeSkippedSlot:
    slot_id: UUID
    reason_code: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.reason_code not in MODEL_CONTRACT_FAILURE_REASONS:
            raise ValidationFailed("Skipped slot model-contract reason ister")
        parse_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeEpisodeOutcome:
    model_id: str
    task_digest: str
    job_id: UUID
    status: CapabilityRuntimeEpisodeStatus
    attempted_calls: int
    successful_calls: int
    failure_turn: int | None
    reason_code: str | None
    evidence_digest: str
    completed_at: dt.datetime

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValidationFailed("Capability episode model kimligi bos olamaz")
        parse_digest(self.task_digest)
        parse_digest(self.evidence_digest)
        if self.completed_at.tzinfo is None:
            raise ValidationFailed("Capability episode terminal timezone ister")
        if not 0 <= self.successful_calls <= self.attempted_calls <= SLOTS_PER_EPISODE:
            raise ValidationFailed("Capability episode call sayilari gecersiz")
        if self.status is CapabilityRuntimeEpisodeStatus.SUCCESSFUL:
            if (
                self.attempted_calls != SLOTS_PER_EPISODE
                or self.successful_calls != SLOTS_PER_EPISODE
                or self.failure_turn is not None
                or self.reason_code is not None
            ):
                raise ValidationFailed("Successful capability episode exact sekiz call ister")
        elif self.status is CapabilityRuntimeEpisodeStatus.MODEL_CONTRACT_FAILED:
            if (
                self.reason_code not in MODEL_CONTRACT_FAILURE_REASONS
                or self.failure_turn != self.attempted_calls
                or self.successful_calls != self.attempted_calls
                or self.attempted_calls < 1
            ):
                raise ValidationFailed("Model-contract terminal episode kaniti gecersiz")
        elif not self.reason_code:
            raise ValidationFailed("Recovery episode reason ister")


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeOutcome:
    status: CapabilityRuntimeStatus
    actual_provider_calls: int
    actual_retries: int
    call_evidence_digests: tuple[str, ...]
    evidence_digest: str
    completed_at: dt.datetime
    successful_episode_count: int = EPISODE_COUNT
    contract_failed_episode_count: int = 0
    skipped_slot_count: int = 0

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        for value in self.call_evidence_digests:
            parse_digest(value)
        if self.completed_at.tzinfo is None:
            raise ValidationFailed("Capability runtime aggregate timezone ister")
        if self.actual_retries != 0:
            raise ValidationFailed("Capability runtime retry yasak")
        if not 0 <= self.actual_provider_calls <= MAX_PROVIDER_CALLS:
            raise ValidationFailed("Capability runtime call budget asildi")
        if len(self.call_evidence_digests) != self.actual_provider_calls:
            raise ValidationFailed("Capability runtime evidence/call sayisi uyusmuyor")
        if len(set(self.call_evidence_digests)) != len(self.call_evidence_digests):
            raise ValidationFailed("Capability runtime evidence digestleri unique olmali")
        if self.status is CapabilityRuntimeStatus.COMPLETED:
            if (
                self.successful_episode_count + self.contract_failed_episode_count != EPISODE_COUNT
                or self.actual_provider_calls + self.skipped_slot_count != MAX_PROVIDER_CALLS
                or self.actual_provider_calls
                < self.successful_episode_count * SLOTS_PER_EPISODE
                + self.contract_failed_episode_count
            ):
                raise ValidationFailed("Completed capability runtime terminal 21x8 partition ister")
        elif (
            self.status is CapabilityRuntimeStatus.PARTIAL
            and self.actual_provider_calls >= MAX_PROVIDER_CALLS
        ):
            raise ValidationFailed("Partial capability runtime tam kapsam olamaz")

    @property
    def score_eligible(self) -> bool:
        return self.status is CapabilityRuntimeStatus.COMPLETED and self.actual_retries == 0

    @property
    def routing_eligible(self) -> bool:
        return False
