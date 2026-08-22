"""OpenCode model benchmark campaign contracts.

The campaign is a secret-free, revision-bound manifest around the existing
benchmark ledger.  It records only identities and digests; endpoint URLs,
credentials, fixture payloads, prompts and provider responses are deliberately
outside this aggregate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import MINIMUM_REPETITIONS

AUDIO_MODALITY = "audio_transcription"
AUDIO_EXCLUSION_REASON = "audio-user-scope-excluded"

_SECRET_PATTERN = re.compile(
    r"(?:\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bBearer\s+\S+|[A-Za-z0-9+/]{48,}={0,2})",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_METADATA = ("://", "api_key", "apikey", "credential", "password", "secret=")


def _safe_metadata(value: str, label: str, *, maximum: int = 256) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValidationFailed(f"{label} bos veya fazla uzun olamaz")
    lowered = normalized.lower()
    if (
        normalized.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE.match(normalized)
        or any(token in lowered for token in _FORBIDDEN_METADATA)
        or _SECRET_PATTERN.search(normalized)
    ):
        raise PolicyViolation(f"{label} endpoint, absolute path veya secret tasiyamaz")
    return normalized


def _digests(values: tuple[str, ...], label: str, *, required: bool = True) -> None:
    if required and not values:
        raise ValidationFailed(f"{label} en az bir digest ister")
    if len(values) != len(set(values)):
        raise ValidationFailed(f"{label} yinelenen digest tasiyamaz")
    for value in values:
        parse_digest(value)


class CampaignMemberDisposition(StrEnum):
    HEALTH_PENDING = "health-pending"
    EXCLUDED_AUDIO = "excluded-audio"


class CampaignMemberResultStage(StrEnum):
    HEALTH = "health"
    BENCHMARK = "benchmark"


class CampaignMemberResultStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


class CampaignOutcomeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


class QualificationAction(StrEnum):
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"


@dataclass(frozen=True, slots=True)
class CampaignContinuation:
    """Reviewed recovery continuation provenance.

    Source drift is never implicit: even when every provider-facing binding is
    unchanged, the caller must bind the old revision to explicit compatibility
    evidence and a continuation provenance digest.
    """

    parent_campaign_id: UUID
    parent_source_revision: str
    compatibility_evidence_digest: str
    continuation_provenance_digest: str
    maximum_tested_call_count: int
    maximum_provider_call_count: int

    def __post_init__(self) -> None:
        _safe_metadata(self.parent_source_revision, "Parent source revision", maximum=128)
        parse_digest(self.compatibility_evidence_digest)
        parse_digest(self.continuation_provenance_digest)
        if (
            self.maximum_tested_call_count < 0
            or self.maximum_provider_call_count < self.maximum_tested_call_count
        ):
            raise ValidationFailed("Continuation current call budget gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent_campaign_id": self.parent_campaign_id,
            "parent_source_revision": self.parent_source_revision,
            "compatibility_evidence_digest": self.compatibility_evidence_digest,
            "continuation_provenance_digest": self.continuation_provenance_digest,
            "maximum_tested_call_count": self.maximum_tested_call_count,
            "maximum_provider_call_count": self.maximum_provider_call_count,
        }


@dataclass(frozen=True, slots=True)
class ResultAdoption:
    """Exact immutable parent-result adoption proof."""

    adopted_from_result_id: UUID
    adoption_provenance_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.adoption_provenance_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted_from_result_id": self.adopted_from_result_id,
            "adoption_provenance_digest": self.adoption_provenance_digest,
        }


@dataclass(frozen=True, slots=True)
class ResultRecoveryEvidence:
    """Completed parent provider effect that failed before result projection."""

    recovered_from_claim_id: UUID
    recovered_from_receipt_id: UUID
    recovery_provenance_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.recovery_provenance_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recovered_from_claim_id": self.recovered_from_claim_id,
            "recovered_from_receipt_id": self.recovered_from_receipt_id,
            "recovery_provenance_digest": self.recovery_provenance_digest,
        }


@dataclass(frozen=True, slots=True)
class CampaignMember:
    """One configured OpenCode model and its exact benchmark fixture set."""

    configured_model_id: str
    canonical_model_id: str | None
    modality: str
    disposition: CampaignMemberDisposition
    fixture_digests: tuple[str, ...] = ()
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        _safe_metadata(self.configured_model_id, "Configured model id")
        _safe_metadata(self.modality, "Model modality", maximum=64)
        if self.canonical_model_id is not None:
            _safe_metadata(self.canonical_model_id, "Canonical model id")

        if self.modality == AUDIO_MODALITY:
            if self.disposition is not CampaignMemberDisposition.EXCLUDED_AUDIO:
                raise PolicyViolation("Audio model campaign kapsamindan excluded olmali")
            if self.fixture_digests:
                raise PolicyViolation("Excluded audio model benchmark fixture tasiyamaz")
            if self.exclusion_reason != AUDIO_EXCLUSION_REASON:
                raise PolicyViolation(
                    "Audio exclusion reason exact kullanici kapsamina bagli olmali"
                )
            return

        if self.disposition is not CampaignMemberDisposition.HEALTH_PENDING:
            raise PolicyViolation("Audio disi configured model sessizce excluded edilemez")
        if self.canonical_model_id is None:
            raise PolicyViolation("Configured model canonical inventory'de belirsiz")
        _digests(self.fixture_digests, "Member fixture set")
        if self.exclusion_reason is not None:
            raise ValidationFailed("Eligible member exclusion reason tasiyamaz")

    @property
    def suite_digest(self) -> str | None:
        if self.disposition is CampaignMemberDisposition.EXCLUDED_AUDIO:
            return None
        return digest(
            {
                "canonical_model_id": self.canonical_model_id,
                "fixture_digests": sorted(self.fixture_digests),
                "modality": self.modality,
            }
        )

    def tested_call_budget(self, repetitions: int) -> int:
        if repetitions < MINIMUM_REPETITIONS:
            raise ValidationFailed("Campaign repetition minimumu karsilanmadi")
        if self.disposition is CampaignMemberDisposition.EXCLUDED_AUDIO:
            return 0
        return len(self.fixture_digests) * repetitions

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured_model_id": self.configured_model_id,
            "canonical_model_id": self.canonical_model_id,
            "modality": self.modality,
            "disposition": self.disposition.value,
            "fixture_digests": sorted(self.fixture_digests),
            "exclusion_reason": self.exclusion_reason,
            "suite_digest": self.suite_digest,
        }


@dataclass(frozen=True, slots=True)
class OpenCodeBenchmarkCampaign:
    """Immutable campaign manifest bound to source, policy and Work revision."""

    campaign_key: str
    revision: int
    work_item_id: UUID
    task_plan_id: UUID
    source_revision: str
    provider_ref: str
    catalog_digest: str
    endpoint_identity_digest: str
    inventory_digest: str
    policy_digest: str
    fixture_registry_digest: str
    verifier_identity: str
    verifier_provenance_digest: str
    source_digest: str
    repetitions: int
    verifier_provider_calls_per_trial: int
    members: tuple[CampaignMember, ...]
    benchmark_suite_version: int = 1
    continuation: CampaignContinuation | None = None

    def __post_init__(self) -> None:
        _safe_metadata(self.campaign_key, "Campaign key", maximum=128)
        _safe_metadata(self.source_revision, "Source revision", maximum=128)
        _safe_metadata(self.provider_ref, "Provider ref", maximum=128)
        _safe_metadata(self.verifier_identity, "Verifier identity", maximum=256)
        if self.revision < 1:
            raise ValidationFailed("Campaign revision pozitif olmali")
        if self.benchmark_suite_version < 1:
            raise ValidationFailed("Benchmark suite version pozitif olmali")
        if self.continuation is not None and self.revision < 2:
            raise ValidationFailed("Continuation campaign revision en az 2 olmali")
        if self.continuation is not None and (
            self.continuation.maximum_tested_call_count > self.tested_call_budget
            or self.continuation.maximum_provider_call_count > self.provider_call_budget
        ):
            raise PolicyViolation("Continuation current budget full campaign budgetini asamaz")
        if self.repetitions < MINIMUM_REPETITIONS:
            raise ValidationFailed(f"Campaign en az {MINIMUM_REPETITIONS} repetition kullanmali")
        if self.verifier_provider_calls_per_trial not in (0, 1):
            raise ValidationFailed("Verifier provider call carpani yalnizca 0 veya 1 olabilir")
        for value in (
            self.catalog_digest,
            self.endpoint_identity_digest,
            self.inventory_digest,
            self.policy_digest,
            self.fixture_registry_digest,
            self.verifier_provenance_digest,
            self.source_digest,
        ):
            parse_digest(value)
        if not self.members:
            raise ValidationFailed("Campaign configured model listesi bos olamaz")
        member_targets = [
            (item.configured_model_id, item.canonical_model_id) for item in self.members
        ]
        if len(member_targets) != len(set(member_targets)):
            raise ValidationFailed("Configured/canonical campaign target pair tekil olmali")
        eligible_ids = [
            item.canonical_model_id
            for item in self.members
            if item.disposition is CampaignMemberDisposition.HEALTH_PENDING
        ]
        if not eligible_ids:
            raise PolicyViolation("Campaign health-passed benchmark adayi tasimali")
        if len(eligible_ids) != len(set(eligible_ids)):
            raise PolicyViolation("Configured modeller canonical inventory'de belirsiz/eslesik")

    @property
    def configured_model_count(self) -> int:
        return len({item.configured_model_id for item in self.members})

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def eligible_model_count(self) -> int:
        return sum(
            item.disposition is CampaignMemberDisposition.HEALTH_PENDING for item in self.members
        )

    @property
    def audio_excluded_count(self) -> int:
        return sum(
            item.disposition is CampaignMemberDisposition.EXCLUDED_AUDIO for item in self.members
        )

    @property
    def tested_call_budget(self) -> int:
        return sum(item.tested_call_budget(self.repetitions) for item in self.members)

    @property
    def health_call_budget(self) -> int:
        return self.eligible_model_count

    @property
    def provider_call_budget(self) -> int:
        return self.health_call_budget + self.tested_call_budget * (
            1 + self.verifier_provider_calls_per_trial
        )

    @property
    def current_tested_call_budget(self) -> int:
        if self.continuation is None:
            return self.tested_call_budget
        return self.continuation.maximum_tested_call_count

    @property
    def current_provider_call_budget(self) -> int:
        if self.continuation is None:
            return self.provider_call_budget
        return self.continuation.maximum_provider_call_count

    @property
    def campaign_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "campaign_key": self.campaign_key,
            "revision": self.revision,
            "work_item_id": self.work_item_id,
            "task_plan_id": self.task_plan_id,
            "source_revision": self.source_revision,
            "provider_ref": self.provider_ref,
            "catalog_digest": self.catalog_digest,
            "endpoint_identity_digest": self.endpoint_identity_digest,
            "inventory_digest": self.inventory_digest,
            "policy_digest": self.policy_digest,
            "fixture_registry_digest": self.fixture_registry_digest,
            "verifier_identity": self.verifier_identity,
            "verifier_provenance_digest": self.verifier_provenance_digest,
            "source_digest": self.source_digest,
            "repetitions": self.repetitions,
            "verifier_provider_calls_per_trial": self.verifier_provider_calls_per_trial,
            "members": sorted(
                (item.as_dict() for item in self.members),
                key=lambda item: str(item["configured_model_id"]),
            ),
            "configured_model_count": self.configured_model_count,
            "member_count": self.member_count,
            "eligible_model_count": self.eligible_model_count,
            "audio_excluded_count": self.audio_excluded_count,
            "health_call_budget": self.health_call_budget,
            "tested_call_budget": self.tested_call_budget,
            "provider_call_budget": self.provider_call_budget,
        }
        # Migration 18 campaign digest replay'leri degismemeli. Continuation
        # binding'i yeni schema profilinde acikca eklenir.
        if self.continuation is not None:
            payload["benchmark_suite_version"] = self.benchmark_suite_version
            payload["continuation"] = self.continuation.as_dict()
        return payload


@dataclass(frozen=True, slots=True)
class CampaignMemberPlan:
    benchmark_plan_id: UUID
    benchmark_plan_digest: str
    health_evidence_digest: str
    authorization_manifest_digest: str
    tested_call_budget: int
    provider_call_budget: int

    def __post_init__(self) -> None:
        parse_digest(self.benchmark_plan_digest)
        parse_digest(self.health_evidence_digest)
        parse_digest(self.authorization_manifest_digest)
        if self.tested_call_budget < 1 or self.provider_call_budget < self.tested_call_budget:
            raise ValidationFailed("Member plan exact pozitif call budget ister")

    @property
    def member_plan_digest(self) -> str:
        return digest(
            {
                "benchmark_plan_id": self.benchmark_plan_id,
                "benchmark_plan_digest": self.benchmark_plan_digest,
                "health_evidence_digest": self.health_evidence_digest,
                "authorization_manifest_digest": self.authorization_manifest_digest,
                "tested_call_budget": self.tested_call_budget,
                "provider_call_budget": self.provider_call_budget,
            }
        )


@dataclass(frozen=True, slots=True)
class CampaignMemberResult:
    stage: CampaignMemberResultStage
    status: CampaignMemberResultStatus
    evidence_digest: str
    actual_tested_call_count: int
    actual_provider_call_count: int
    aggregate_id: UUID | None = None
    failure_category: str | None = None
    adoption: ResultAdoption | None = None
    recovery_evidence: ResultRecoveryEvidence | None = None

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        if self.actual_tested_call_count < 0 or self.actual_provider_call_count < 0:
            raise ValidationFailed("Member result call count negatif olamaz")
        if self.actual_provider_call_count < self.actual_tested_call_count:
            raise ValidationFailed("Provider call count tested call count'tan az olamaz")
        if self.adoption is not None and self.recovery_evidence is not None:
            raise PolicyViolation("Result adoption ve claim recovery birlikte kullanilamaz")
        if self.adoption is not None:
            if self.status is CampaignMemberResultStatus.RECOVERY_REQUIRED:
                raise PolicyViolation("Recovery-required parent result adopt edilemez")
            if self.actual_tested_call_count or self.actual_provider_call_count:
                raise PolicyViolation("Adopted result yeni provider/tested call tasiyamaz")
        if self.recovery_evidence is not None:
            if (
                self.stage is not CampaignMemberResultStage.HEALTH
                or self.status is not CampaignMemberResultStatus.FAILED
                or self.failure_category != "health-contract-failed"
                or self.aggregate_id is not None
            ):
                raise PolicyViolation("Recovered claim yalniz exact failed health sonucu olabilir")
            if self.actual_tested_call_count or self.actual_provider_call_count:
                raise PolicyViolation("Recovered health sonucu yeni call tasiyamaz")
        if self.stage is CampaignMemberResultStage.HEALTH:
            if self.actual_tested_call_count != 0 or self.actual_provider_call_count > 1:
                raise ValidationFailed("Health result en fazla bir provider call tasiyabilir")
            if self.aggregate_id is not None:
                raise ValidationFailed("Health result benchmark aggregate tasiyamaz")
        elif self.status is CampaignMemberResultStatus.PASSED and self.aggregate_id is None:
            raise ValidationFailed("Passed benchmark member aggregate ister")
        if self.status is CampaignMemberResultStatus.PASSED:
            if self.failure_category is not None:
                raise ValidationFailed("Passed member failure tasiyamaz")
        else:
            if self.failure_category is None:
                raise ValidationFailed("Terminal failure sanitize category ister")
            _safe_metadata(self.failure_category, "Failure category", maximum=128)
        if (
            self.status is CampaignMemberResultStatus.RECOVERY_REQUIRED
            and self.aggregate_id is not None
        ):
            raise PolicyViolation("Recovery-required member aggregate yayimlayamaz")

    @property
    def result_digest(self) -> str:
        payload: dict[str, Any] = {
            "status": self.status.value,
            "stage": self.stage.value,
            "evidence_digest": self.evidence_digest,
            "actual_tested_call_count": self.actual_tested_call_count,
            "actual_provider_call_count": self.actual_provider_call_count,
            "aggregate_id": self.aggregate_id,
            "failure_category": self.failure_category,
        }
        # Migration 18 terminal result replay digest'leri degismemeli.
        if self.adoption is not None:
            payload["adoption"] = self.adoption.as_dict()
        if self.recovery_evidence is not None:
            payload["recovery_evidence"] = self.recovery_evidence.as_dict()
        return digest(payload)


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    status: CampaignOutcomeStatus
    passed_count: int
    failed_count: int
    recovery_required_count: int
    audio_excluded_count: int
    actual_tested_call_count: int
    actual_provider_call_count: int
    evidence_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        counts = (
            self.passed_count,
            self.failed_count,
            self.recovery_required_count,
            self.audio_excluded_count,
            self.actual_tested_call_count,
            self.actual_provider_call_count,
        )
        if any(value < 0 for value in counts):
            raise ValidationFailed("Campaign outcome count negatif olamaz")
        if self.actual_provider_call_count < self.actual_tested_call_count:
            raise ValidationFailed("Campaign provider call count tested count'tan az olamaz")
        if self.status is CampaignOutcomeStatus.PASSED:
            if self.passed_count < 1 or self.failed_count or self.recovery_required_count:
                raise ValidationFailed("Passed campaign yalnizca passed member tasimalli")
        elif self.status is CampaignOutcomeStatus.FAILED:
            if self.failed_count < 1 or self.recovery_required_count:
                raise ValidationFailed("Failed campaign en az bir failed member tasimalli")
        elif self.recovery_required_count < 1:
            raise ValidationFailed("Recovery-required outcome recovery member ister")

    @property
    def outcome_digest(self) -> str:
        return digest(
            {
                "status": self.status.value,
                "passed_count": self.passed_count,
                "failed_count": self.failed_count,
                "recovery_required_count": self.recovery_required_count,
                "audio_excluded_count": self.audio_excluded_count,
                "actual_tested_call_count": self.actual_tested_call_count,
                "actual_provider_call_count": self.actual_provider_call_count,
                "evidence_digest": self.evidence_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class QualificationEvent:
    action: QualificationAction
    model_id: str
    outcome_id: UUID
    evidence_digest: str
    aggregate_id: UUID | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _safe_metadata(self.model_id, "Qualification model id")
        parse_digest(self.evidence_digest)
        if self.action is QualificationAction.QUALIFIED:
            if self.aggregate_id is None or self.reason_code is not None:
                raise ValidationFailed("Qualified event aggregate ister ve reason tasiyamaz")
        else:
            if self.aggregate_id is not None or self.reason_code is None:
                raise ValidationFailed(
                    "Disqualified event aggregate tasiyamaz ve sanitize reason ister"
                )
            _safe_metadata(self.reason_code, "Qualification reason", maximum=128)

    @property
    def event_digest(self) -> str:
        return digest(
            {
                "action": self.action.value,
                "model_id": self.model_id,
                "outcome_id": self.outcome_id,
                "evidence_digest": self.evidence_digest,
                "aggregate_id": self.aggregate_id,
                "reason_code": self.reason_code,
            }
        )


@dataclass(frozen=True, slots=True)
class CampaignMemberRecord:
    id: UUID
    campaign_id: UUID
    member: CampaignMember
    tested_call_budget: int
    provider_call_budget: int


@dataclass(frozen=True, slots=True)
class CampaignMemberResultRecord:
    id: UUID
    campaign_id: UUID
    member_id: UUID
    configured_model_id: str
    canonical_model_id: str
    modality: str
    stage: CampaignMemberResultStage
    status: CampaignMemberResultStatus
    member_plan_id: UUID | None
    benchmark_plan_id: UUID | None
    benchmark_plan_digest: str | None
    aggregate_id: UUID | None
    evidence_digest: str
    result_digest: str
    failure_category: str | None
    actual_tested_call_count: int
    actual_provider_call_count: int


@dataclass(frozen=True, slots=True)
class CampaignStatus:
    campaign_id: UUID
    campaign_key: str
    revision: int
    campaign_digest: str
    outcome_id: UUID | None
    outcome_status: CampaignOutcomeStatus | None
    outcome_digest: str | None
    tested_call_budget: int
    provider_call_budget: int
    actual_tested_call_count: int | None
    actual_provider_call_count: int | None
    parent_campaign_id: UUID | None
    benchmark_suite_version: int
    continuation_provenance_digest: str | None
    compatibility_evidence_digest: str | None
    current_tested_call_budget: int
    current_provider_call_budget: int

    @property
    def terminal(self) -> bool:
        return self.outcome_id is not None
