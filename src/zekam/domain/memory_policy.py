"""Versioned privacy and storage policy for the Memory Continuity Plane."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.session_continuity import DataClassification

MEMORY_POLICY_SCHEMA = "zekam-memory-continuity-policy/v1"


class MemoryContinuityMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"


class MemoryStorageClass(StrEnum):
    POSTGRESQL_RLS = "postgresql-rls"
    LOCAL_CAS = "local-cas"
    DENIED = "denied"


class MemoryRetentionClass(StrEnum):
    DURABLE = "durable"
    BOUNDED = "bounded"
    UNTIL_REVOKED = "until-revoked"
    PROHIBITED = "prohibited"


class HumanReviewLevel(StrEnum):
    NONE = "none"
    STANDARD = "standard"
    REQUIRED = "required"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ClassificationPolicy:
    classification: DataClassification
    storage_class: MemoryStorageClass
    retention_class: MemoryRetentionClass
    retention_days: int | None
    projection_eligible: bool
    remote_model_eligible: bool
    cross_project_access: bool
    compiler_eligible: bool
    human_review: HumanReviewLevel

    def __post_init__(self) -> None:
        if self.retention_class is MemoryRetentionClass.BOUNDED:
            if self.retention_days is None or not 1 <= self.retention_days <= 3650:
                raise ValidationFailed("Bounded memory retention 1..3650 gun ister")
        elif self.retention_days is not None:
            raise ValidationFailed("Bounded olmayan retention gun tasiyamaz")
        if self.classification is not DataClassification.PUBLIC and self.projection_eligible:
            raise PolicyViolation("Public olmayan memory projection'a giremez")
        if (
            self.classification
            in {
                DataClassification.RESTRICTED,
                DataClassification.CONFIDENTIAL,
                DataClassification.LOCAL_ONLY,
                DataClassification.PII,
                DataClassification.CORPORATE_CONFIDENTIAL,
                DataClassification.SECRET,
                DataClassification.RAW_TRANSCRIPT,
                DataClassification.DIAGNOSTIC_PAYLOAD,
            }
            and self.remote_model_eligible
        ):
            raise PolicyViolation("Hassas memory remote model'e default eligible olamaz")
        if self.cross_project_access:
            raise PolicyViolation("Memory classification cross-project default-deny olmali")
        if self.classification is DataClassification.SECRET and (
            self.storage_class is not MemoryStorageClass.DENIED
            or self.retention_class is not MemoryRetentionClass.PROHIBITED
            or self.compiler_eligible
            or self.human_review is not HumanReviewLevel.CRITICAL
        ):
            raise PolicyViolation("Secret memory ingestion fail-closed olmali")
        if self.classification is DataClassification.RAW_TRANSCRIPT and (
            self.storage_class is not MemoryStorageClass.LOCAL_CAS
            or not self.compiler_eligible
            or self.human_review is not HumanReviewLevel.REQUIRED
        ):
            raise PolicyViolation("Raw transcript yalniz local candidate-only akisa girebilir")

    def body(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "storage_class": self.storage_class.value,
            "retention_class": self.retention_class.value,
            "retention_days": self.retention_days,
            "projection_eligible": self.projection_eligible,
            "remote_model_eligible": self.remote_model_eligible,
            "cross_project_access": self.cross_project_access,
            "compiler_eligible": self.compiler_eligible,
            "human_review": self.human_review.value,
        }


@dataclass(frozen=True, slots=True)
class MemoryContinuityPolicy:
    revision: int
    initial_mode: MemoryContinuityMode
    remote_calls_default: bool
    classifications: tuple[ClassificationPolicy, ...]

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationFailed("Memory policy revision pozitif olmali")
        if self.initial_mode is not MemoryContinuityMode.SHADOW:
            raise PolicyViolation("Memory continuity ilk deployment shadow olmali")
        if self.remote_calls_default:
            raise PolicyViolation("Memory continuity remote calls default-deny olmali")
        values = tuple(item.classification for item in self.classifications)
        if len(values) != len(set(values)) or frozenset(values) != frozenset(DataClassification):
            raise ValidationFailed("Memory policy exact classification setini ister")

    def policy_for(self, classification: DataClassification) -> ClassificationPolicy:
        for item in self.classifications:
            if item.classification is classification:
                return item
        raise NotFound(f"Memory classification policy bulunamadi: {classification.value}")

    def assert_projection_eligible(self, classification: DataClassification) -> None:
        if not self.policy_for(classification).projection_eligible:
            raise PolicyViolation("Memory classification public projection icin uygun degil")

    def assert_remote_model_eligible(self, classification: DataClassification) -> None:
        if not self.policy_for(classification).remote_model_eligible:
            raise PolicyViolation("Memory classification remote model icin uygun degil")

    def assert_compiler_eligible(self, classification: DataClassification) -> None:
        if not self.policy_for(classification).compiler_eligible:
            raise PolicyViolation("Memory classification compiler icin uygun degil")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "schema": MEMORY_POLICY_SCHEMA,
                "revision": self.revision,
                "initial_mode": self.initial_mode.value,
                "remote_calls_default": self.remote_calls_default,
                "classifications": [
                    item.body()
                    for item in sorted(self.classifications, key=lambda x: x.classification)
                ],
                "grants_authority": False,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MEMORY_POLICY_SCHEMA,
            "revision": self.revision,
            "initial_mode": self.initial_mode.value,
            "remote_calls_default": self.remote_calls_default,
            "classifications": [
                item.body() for item in sorted(self.classifications, key=lambda x: x.classification)
            ],
            "policy_digest": self.policy_digest,
            "grants_authority": False,
        }
