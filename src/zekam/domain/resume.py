"""Authority-free, deterministic resume planning contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.checkpoint_v2 import OpenEffect, Resumability, StaleDigestBindings
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ResumeDisposition(StrEnum):
    SAFE_CONTINUE = "safe-continue"
    SAFE_RECOMPILE = "safe-recompile"
    SAFE_REPLAN = "safe-replan"
    WAITING = "waiting"
    RECOVERY_REQUIRED = "recovery-required"
    MANUAL_REVIEW = "manual-review"
    DENIED = "denied"
    ALREADY_COMPLETED = "already-completed"


class DriftDecision(StrEnum):
    RECOMPILE = "recompile"
    REPLAN = "replan"
    REAUTHORIZE = "reauthorize"
    RECONCILE = "reconcile"
    MANUAL_REVIEW = "manual-review"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class StaleDimension:
    dimension: str
    checkpoint_value: str
    current_value: str
    decision: DriftDecision
    reason_code: str

    def __post_init__(self) -> None:
        if not self.dimension.strip():
            raise ValidationFailed("Stale dimension bos olamaz")
        if not self.reason_code.strip():
            raise ValidationFailed("Stale dimension reason code bos olamaz")

    def body(self) -> dict[str, str]:
        return {
            "dimension": self.dimension,
            "checkpoint": self.checkpoint_value,
            "current": self.current_value,
            "decision": self.decision.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    claim_id: UUID
    effect_digest: str
    reason_code: str

    def __post_init__(self) -> None:
        parse_digest(self.effect_digest)
        if not self.reason_code.strip():
            raise ValidationFailed("Reconciliation reason code bos olamaz")

    def body(self) -> dict[str, str]:
        return {
            "claim_id": str(self.claim_id),
            "effect_digest": self.effect_digest,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ResumeAction:
    action_id: str
    kind: str
    depends_on: tuple[str, ...]
    resource: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.kind.strip():
            raise ValidationFailed("Resume action kimlik ve tur ister")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValidationFailed("Resume action dependency tekil olmali")

    def body(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "depends_on": list(self.depends_on),
            "resource": self.resource,
        }


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    run_id: UUID
    job_id: UUID
    attempt_id: UUID | None
    assignment_id: UUID
    execution_envelope_id: UUID
    execution_envelope_digest: str
    observed_lease_id: UUID | None
    observed_fencing_token: int | None
    job_state: str
    lease_expires_at: dt.datetime | None
    deadline: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.execution_envelope_digest)
        observed = (self.attempt_id, self.observed_lease_id, self.observed_fencing_token)
        if any(value is None for value in observed) and any(
            value is not None for value in observed
        ):
            raise ValidationFailed("Runtime lease observation exact veya tamamen bos olmali")
        if self.observed_fencing_token is not None and self.observed_fencing_token < 1:
            raise ValidationFailed("Runtime observation fencing token pozitif olmali")
        if self.deadline.tzinfo is None or (
            self.lease_expires_at is not None and self.lease_expires_at.tzinfo is None
        ):
            raise ValidationFailed("Runtime observation zamanlari timezone-aware olmali")

    def body(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "job_id": str(self.job_id),
            "attempt_id": None if self.attempt_id is None else str(self.attempt_id),
            "assignment_id": str(self.assignment_id),
            "execution_envelope_id": str(self.execution_envelope_id),
            "execution_envelope_digest": self.execution_envelope_digest,
            "observed_lease_id": (
                None if self.observed_lease_id is None else str(self.observed_lease_id)
            ),
            "observed_fencing_token": self.observed_fencing_token,
            "job_state": self.job_state,
            "lease_expires_at": self.lease_expires_at,
            "deadline": self.deadline,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ResumeObservation:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    work_state: str
    checkpoint_id: UUID
    checkpoint_digest: str
    checkpoint_revision: int
    checkpoint_key: str
    plan_id: UUID
    plan_digest: str
    current_plan_id: UUID
    current_plan_digest: str
    checkpoint_bindings: StaleDigestBindings
    current_bindings: StaleDigestBindings
    pending_steps: tuple[str, ...]
    next_step_id: str | None
    open_effects: tuple[OpenEffect, ...]
    checkpoint_integrity: bool
    resumability: Resumability
    logical_read_resources: tuple[str, ...]
    logical_write_resources: tuple[str, ...]
    runtime: RuntimeObservation
    target_client_id: str
    required_route_role: str | None
    context_recipe: str | None
    observed_at: dt.datetime
    valid_until: dt.datetime
    legacy_limited: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.checkpoint_digest)
        parse_digest(self.plan_digest)
        parse_digest(self.current_plan_digest)
        if self.checkpoint_revision < 1:
            raise ValidationFailed("Checkpoint revision pozitif olmali")
        if self.observed_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValidationFailed("Resume observation zamanlari timezone-aware olmali")
        if not self.target_client_id.strip():
            raise ValidationFailed("Resume target client kimligi ister")
        if self.next_step_id is not None and self.next_step_id not in self.pending_steps:
            raise ValidationFailed("Resume next step pending partition icinde olmali")


@dataclass(frozen=True, slots=True)
class ResumePlan:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    checkpoint_id: UUID
    checkpoint_digest: str
    checkpoint_revision: int
    selected_checkpoint_reason: str
    disposition: ResumeDisposition
    stale_dimensions: tuple[StaleDimension, ...]
    reconciliation_actions: tuple[ReconciliationAction, ...]
    reacquire_resources: tuple[str, ...]
    logical_read_resources: tuple[str, ...]
    logical_write_resources: tuple[str, ...]
    runtime: RuntimeObservation
    target_client_id: str
    next_step_id: str | None
    context_recipe: str | None
    required_route_role: str | None
    actions: tuple[ResumeAction, ...]
    blockers: tuple[str, ...]
    observed_at: dt.datetime
    valid_until: dt.datetime
    grants_authority: bool = False
    carries_active_lease: bool = False
    approval_inherited: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority or self.carries_active_lease or self.approval_inherited:
            raise PolicyViolation("Resume plan authority, active lease veya approval tasiyamaz")
        parse_digest(self.checkpoint_digest)
        if self.observed_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValidationFailed("Resume plan zamanlari timezone-aware olmali")
        if not self.target_client_id.strip():
            raise ValidationFailed("Resume plan target client ister")
        ids = tuple(item.action_id for item in self.actions)
        if len(ids) != len(set(ids)):
            raise ValidationFailed("Resume action kimlikleri tekil olmali")
        known: set[str] = set()
        for action in self.actions:
            if not set(action.depends_on).issubset(known):
                raise ValidationFailed("Resume action DAG ileri veya bilinmeyen bag tasiyamaz")
            known.add(action.action_id)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-resume-plan/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "selected_checkpoint": {
                "checkpoint_id": str(self.checkpoint_id),
                "checkpoint_digest": self.checkpoint_digest,
                "revision": self.checkpoint_revision,
                "reason": self.selected_checkpoint_reason,
            },
            "disposition": self.disposition.value,
            "stale_dimensions": [item.body() for item in self.stale_dimensions],
            "reconciliation_actions": [item.body() for item in self.reconciliation_actions],
            "reacquire_resources": list(self.reacquire_resources),
            "logical_resources": {
                "read": list(self.logical_read_resources),
                "write": list(self.logical_write_resources),
            },
            "runtime": self.runtime.body() | {"snapshot_digest": self.runtime.snapshot_digest},
            "target_client_id": self.target_client_id,
            "next_step_id": self.next_step_id,
            "context_recipe": self.context_recipe,
            "required_route_role": self.required_route_role,
            "actions": [item.body() for item in self.actions],
            "blockers": list(self.blockers),
            "observed_at": self.observed_at,
            "valid_until": self.valid_until,
            "grants_authority": False,
            "carries_active_lease": False,
            "approval_inherited": False,
            "dry_run": True,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "resume_plan_digest": self.plan_digest,
        }
