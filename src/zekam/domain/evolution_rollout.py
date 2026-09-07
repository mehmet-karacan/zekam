"""Typed local rollout plans and independently signed execution observations."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class RolloutStage(StrEnum):
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVATION = "activation"
    ROLLBACK = "rollback"


class RolloutStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


@dataclass(frozen=True, slots=True)
class RolloutPlan:
    candidate_digest: str
    evaluation_digest: str
    stage: RolloutStage
    logical_resource: str
    pointer_name: str
    expected_before_digest: str
    candidate_artifact_digest: str
    last_known_good_digest: str
    fixture_digest: str
    harness_digest: str
    resource_manifest_digest: str
    observation_inputs: tuple[str, ...]
    minimum_observations: int
    grant_digest: str
    authorization_digest: str

    def __post_init__(self) -> None:
        if type(self.stage) is not RolloutStage:
            raise ValidationFailed("Rollout exact stage required")
        if not _NAME.fullmatch(self.logical_resource) or not _NAME.fullmatch(self.pointer_name):
            raise ValidationFailed("Rollout bounded logical resource required")
        if "/" in self.pointer_name or "\\" in self.pointer_name:
            raise PolicyViolation("Rollout pointer must be an exact local filename")
        for field in (
            "candidate_digest",
            "evaluation_digest",
            "expected_before_digest",
            "candidate_artifact_digest",
            "last_known_good_digest",
            "fixture_digest",
            "harness_digest",
            "resource_manifest_digest",
            "grant_digest",
            "authorization_digest",
        ):
            parse_digest(getattr(self, field))
        if (
            not self.observation_inputs
            or tuple(sorted(set(self.observation_inputs))) != self.observation_inputs
            or any(not isinstance(value, str) for value in self.observation_inputs)
        ):
            raise ValidationFailed("Rollout observations must be non-empty canonical digests")
        for value in self.observation_inputs:
            parse_digest(value)
        if (
            type(self.minimum_observations) is not int
            or isinstance(self.minimum_observations, bool)
            or not 1 <= self.minimum_observations <= len(self.observation_inputs)
        ):
            raise ValidationFailed("Rollout minimum observation bound invalid")
        if self.stage is RolloutStage.ROLLBACK:
            if self.expected_before_digest != self.candidate_artifact_digest:
                raise PolicyViolation("Rollback must compare-and-swap the exact candidate")
        elif self.expected_before_digest != self.last_known_good_digest:
            raise PolicyViolation("Rollout must start from the exact last-known-good pointer")

    def body(self) -> dict[str, Any]:
        return self.intent_body() | {"authorization_digest": self.authorization_digest}

    def intent_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-local-rollout-plan/v1",
            "workload_classification": "local-deterministic-fixture",
            "production_traffic": False,
            "candidate_digest": self.candidate_digest,
            "evaluation_digest": self.evaluation_digest,
            "stage": self.stage.value,
            "logical_resource": self.logical_resource,
            "pointer_name": self.pointer_name,
            "expected_before_digest": self.expected_before_digest,
            "candidate_artifact_digest": self.candidate_artifact_digest,
            "last_known_good_digest": self.last_known_good_digest,
            "fixture_digest": self.fixture_digest,
            "harness_digest": self.harness_digest,
            "resource_manifest_digest": self.resource_manifest_digest,
            "observation_inputs": list(self.observation_inputs),
            "minimum_observations": self.minimum_observations,
            "grant_digest": self.grant_digest,
        }

    @property
    def intent_digest(self) -> str:
        return digest(self.intent_body())

    @property
    def plan_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class RolloutWorkerIdentity:
    assignment_id: UUID
    role: str
    process_id: int
    process_start_token: str
    implementation_digest: str
    challenge_digest: str
    boundary_receipt: str

    def __post_init__(self) -> None:
        if self.role not in {"executor", "verifier"}:
            raise ValidationFailed("Rollout worker role invalid")
        if type(self.process_id) is not int or self.process_id <= 0:
            raise ValidationFailed("Rollout process identity invalid")
        for value in (self.implementation_digest, self.challenge_digest):
            parse_digest(value)
        if not self.process_start_token:
            raise ValidationFailed("Rollout process incarnation required")
        if not self.boundary_receipt.startswith("ed25519:"):
            raise ValidationFailed("Rollout boundary receipt must be asymmetric")

    def boundary_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-rollout-worker-boundary/v1",
            "assignment_id": str(self.assignment_id),
            "role": self.role,
            "process_id": self.process_id,
            "process_start_token": self.process_start_token,
            "implementation_digest": self.implementation_digest,
            "challenge_digest": self.challenge_digest,
        }


@dataclass(frozen=True, slots=True)
class RolloutObservation:
    plan_digest: str
    stage: RolloutStage
    status: RolloutStatus
    before_digest: str
    after_digest: str
    evidence_digest: str
    observation_count: int
    production_effect_count: int
    worker: RolloutWorkerIdentity
    started_at: dt.datetime
    finished_at: dt.datetime
    execution_receipt: str

    def __post_init__(self) -> None:
        if type(self.stage) is not RolloutStage or type(self.status) is not RolloutStatus:
            raise ValidationFailed("Rollout observation exact enum required")
        for value in (
            self.plan_digest,
            self.before_digest,
            self.after_digest,
            self.evidence_digest,
        ):
            parse_digest(value)
        if (
            type(self.observation_count) is not int
            or self.observation_count < 0
            or type(self.production_effect_count) is not int
            or self.production_effect_count not in {0, 1}
        ):
            raise ValidationFailed("Rollout observation count invalid")
        if self.stage in {RolloutStage.SHADOW, RolloutStage.CANARY} and (
            self.production_effect_count or self.before_digest != self.after_digest
        ):
            raise PolicyViolation("Shadow/canary cannot change the active pointer")
        if self.status is not RolloutStatus.COMPLETED and self.production_effect_count:
            raise PolicyViolation("Failed rollout cannot claim a production effect")
        if (
            self.started_at.tzinfo is None
            or self.finished_at.tzinfo is None
            or self.finished_at <= self.started_at
        ):
            raise ValidationFailed("Rollout observation chronology invalid")
        if self.worker.role != "executor" or not self.execution_receipt.startswith("ed25519:"):
            raise ValidationFailed("Rollout observation requires executor signature")

    def receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-local-rollout-observation/v1",
            "plan_digest": self.plan_digest,
            "stage": self.stage.value,
            "status": self.status.value,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "evidence_digest": self.evidence_digest,
            "observation_count": self.observation_count,
            "production_effect_count": self.production_effect_count,
            "worker": self.worker.boundary_body()
            | {"boundary_receipt": self.worker.boundary_receipt},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @property
    def observation_digest(self) -> str:
        return digest(self.receipt_body() | {"execution_receipt": self.execution_receipt})


@dataclass(frozen=True, slots=True)
class RolloutVerification:
    observation_digest: str
    plan_digest: str
    accepted: bool
    status: RolloutStatus
    readback_digest: str
    worker: RolloutWorkerIdentity
    verified_at: dt.datetime
    verification_receipt: str

    def __post_init__(self) -> None:
        for value in (self.observation_digest, self.plan_digest, self.readback_digest):
            parse_digest(value)
        if type(self.accepted) is not bool or self.worker.role != "verifier":
            raise ValidationFailed("Rollout independent verification invalid")
        if self.verified_at.tzinfo is None or not self.verification_receipt.startswith("ed25519:"):
            raise ValidationFailed("Rollout verification signature required")

    def receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-local-rollout-verification/v1",
            "observation_digest": self.observation_digest,
            "plan_digest": self.plan_digest,
            "accepted": self.accepted,
            "status": self.status.value,
            "readback_digest": self.readback_digest,
            "worker": self.worker.boundary_body()
            | {"boundary_receipt": self.worker.boundary_receipt},
            "verified_at": self.verified_at,
        }
