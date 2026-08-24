"""Checkpoint v2: authority-free, append-only execution state manifest."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.resources import LogicalResource
from zekam.domain.work import EffectKind

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SECRET_MARKERS = (
    "-----begin ",
    "api_key=",
    "apikey=",
    "password=",
    "raw_prompt",
    "secret=",
    "token=",
)


def _portable(value: str, label: str) -> str:
    candidate = value.strip()
    lowered = candidate.lower()
    if not candidate:
        raise ValidationFailed(f"{label} bos olamaz")
    if (
        _WINDOWS_ABSOLUTE.match(candidate)
        or candidate.startswith(("/", "\\\\"))
        or "\\" in candidate
        or ".." in candidate.replace("\\", "/").split("/")
    ):
        raise PolicyViolation(f"{label} portable olmali")
    if any(marker in lowered for marker in _SECRET_MARKERS) or lowered.startswith("sk-"):
        raise PolicyViolation(f"{label} secret veya raw prompt tasiyamaz")
    return candidate


def _digests(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValidationFailed(f"{label} tekil olmali")
    for value in values:
        parse_digest(value)


class OpenEffectState(StrEnum):
    STARTED_NO_TERMINAL_RECEIPT = "started-no-terminal-receipt"
    FAILED_RECONCILIATION = "failed-reconciliation"
    UNKNOWN = "unknown"


class SandboxDisposition(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    CLEAN = "clean"
    DIRTY = "dirty"


class Resumability(StrEnum):
    SAFE_CONTINUE = "safe-continue"
    RECONCILIATION_REQUIRED = "reconciliation-required"
    MANUAL_REVIEW = "manual-review"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StepResultV2:
    step_id: str
    result_digest: str
    effect_kind: EffectKind
    job_id: UUID
    attempt_id: UUID
    assignment_id: UUID
    execution_envelope_id: UUID
    execution_envelope_digest: str
    receipt_refs: tuple[UUID, ...] = ()
    verification_refs: tuple[UUID, ...] = ()
    verification_required: bool = False

    def __post_init__(self) -> None:
        _portable(self.step_id, "Step result kimligi")
        parse_digest(self.result_digest)
        parse_digest(self.execution_envelope_digest)
        if len(self.receipt_refs) != len(set(self.receipt_refs)):
            raise ValidationFailed("Step receipt referanslari tekil olmali")
        if len(self.verification_refs) != len(set(self.verification_refs)):
            raise ValidationFailed("Step verification referanslari tekil olmali")
        if self.effect_kind is not EffectKind.NONE and not self.receipt_refs:
            raise PolicyViolation("Effect ureten completed step terminal receipt ister")
        if self.verification_required and not self.verification_refs:
            raise PolicyViolation("Completed step gerekli verification referansini tasimali")

    def body(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "result_digest": self.result_digest,
            "effect_kind": self.effect_kind.value,
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "assignment_id": str(self.assignment_id),
            "execution_envelope_id": str(self.execution_envelope_id),
            "execution_envelope_digest": self.execution_envelope_digest,
            "receipt_refs": [str(value) for value in self.receipt_refs],
            "verification_refs": [str(value) for value in self.verification_refs],
            "verification_required": self.verification_required,
        }


@dataclass(frozen=True, slots=True)
class OpenEffect:
    claim_id: UUID
    effect_digest: str
    state: OpenEffectState

    def __post_init__(self) -> None:
        parse_digest(self.effect_digest)

    def body(self) -> dict[str, str]:
        return {
            "claim_id": str(self.claim_id),
            "effect_digest": self.effect_digest,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class StaleDigestBindings:
    routing_context_snapshot_id: UUID
    source_revision: str
    policy_digest: str
    capability_profile_digest: str
    dependency_snapshot_digest: str
    migration_head_digest: str
    model_route_decision_digest: str
    context_manifest_digest: str
    context_packet_digest: str
    architecture_digest: str
    rules_digest: str
    test_suite_digest: str
    model_inventory_digest: str
    journal_head_digest: str

    def __post_init__(self) -> None:
        _portable(self.source_revision, "Source revision")
        for value in (
            self.policy_digest,
            self.capability_profile_digest,
            self.dependency_snapshot_digest,
            self.migration_head_digest,
            self.model_route_decision_digest,
            self.context_manifest_digest,
            self.context_packet_digest,
            self.architecture_digest,
            self.rules_digest,
            self.test_suite_digest,
            self.model_inventory_digest,
            self.journal_head_digest,
        ):
            parse_digest(value)

    def body(self) -> dict[str, str]:
        return {
            "routing_context_snapshot_id": str(self.routing_context_snapshot_id),
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "capability_profile_digest": self.capability_profile_digest,
            "dependency_snapshot_digest": self.dependency_snapshot_digest,
            "migration_head_digest": self.migration_head_digest,
            "model_route_decision_digest": self.model_route_decision_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "context_packet_digest": self.context_packet_digest,
            "architecture_digest": self.architecture_digest,
            "rules_digest": self.rules_digest,
            "test_suite_digest": self.test_suite_digest,
            "model_inventory_digest": self.model_inventory_digest,
            "journal_head_digest": self.journal_head_digest,
        }


@dataclass(frozen=True, slots=True)
class SandboxBindingV2:
    disposition: SandboxDisposition
    sandbox_id: str | None = None
    base_revision: str | None = None
    patch_digest: str | None = None
    dirty_state_digest: str | None = None

    def __post_init__(self) -> None:
        values = (self.sandbox_id, self.base_revision, self.patch_digest, self.dirty_state_digest)
        if self.disposition is SandboxDisposition.NOT_APPLICABLE:
            if any(value is not None for value in values):
                raise ValidationFailed("Not-applicable sandbox binding alan tasiyamaz")
            return
        if self.sandbox_id is None or self.base_revision is None:
            raise ValidationFailed("Sandbox identity ve base revision zorunludur")
        _portable(self.sandbox_id, "Sandbox identity")
        _portable(self.base_revision, "Sandbox base revision")
        if self.disposition is SandboxDisposition.CLEAN:
            if self.patch_digest is not None or self.dirty_state_digest is not None:
                raise ValidationFailed("Clean sandbox patch veya dirty digest tasiyamaz")
        elif self.patch_digest is None or self.dirty_state_digest is None:
            raise ValidationFailed("Dirty sandbox patch ve dirty state digest ister")
        for value in (self.patch_digest, self.dirty_state_digest):
            if value is not None:
                parse_digest(value)

    def body(self) -> dict[str, str | None]:
        return {
            "disposition": self.disposition.value,
            "sandbox_id": self.sandbox_id,
            "base_revision": self.base_revision,
            "patch_digest": self.patch_digest,
            "dirty_state_digest": self.dirty_state_digest,
        }


@dataclass(frozen=True, slots=True)
class RecoveryDirectiveV2:
    kind: str
    reason: str
    evidence_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _portable(self.kind, "Recovery kind")
        _portable(self.reason, "Recovery reason")
        _digests(self.evidence_digests, "Recovery evidence digest")

    def body(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "evidence_digests": list(self.evidence_digests),
        }


@dataclass(frozen=True, slots=True)
class NextSafeActionV2:
    kind: str
    step_id: str
    reason: str

    def __post_init__(self) -> None:
        _portable(self.kind, "Next action kind")
        _portable(self.step_id, "Next action step")
        _portable(self.reason, "Next action reason")

    def body(self) -> dict[str, str]:
        return {"kind": self.kind, "step_id": self.step_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CheckpointV2:
    checkpoint_id: UUID
    checkpoint_key: str
    revision: int
    previous_checkpoint_id: UUID | None
    previous_checkpoint_digest: str | None
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    intent_digest: str
    plan_id: UUID
    plan_digest: str
    step_id: str
    run_id: UUID
    job_id: UUID
    attempt_id: UUID
    assignment_id: UUID
    execution_envelope_id: UUID
    execution_envelope_digest: str
    route_decision_id: UUID
    context_manifest_id: UUID
    context_packet_id: UUID
    bindings: StaleDigestBindings
    plan_steps: tuple[str, ...]
    completed_steps: tuple[str, ...]
    pending_steps: tuple[str, ...]
    step_results: tuple[StepResultV2, ...]
    open_effects: tuple[OpenEffect, ...]
    logical_read_resources: tuple[str, ...]
    logical_write_resources: tuple[str, ...]
    sandbox: SandboxBindingV2
    tokens_used: int
    cost_micros_used: int
    attempts_used: int
    deadline: dt.datetime
    rollback_or_recovery: tuple[RecoveryDirectiveV2, ...]
    resumability: Resumability
    next_safe_action: NextSafeActionV2 | None
    created_at: dt.datetime
    observed_lease_id: UUID
    observed_fencing_token: int
    test_and_eval_digests: tuple[str, ...] = ()
    grants_authority: bool = False
    carries_active_lease: bool = False
    approval_inherited: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority or self.carries_active_lease or self.approval_inherited:
            raise PolicyViolation("Checkpoint authority, active lease veya approval tasiyamaz")
        _portable(self.checkpoint_key, "Checkpoint key")
        _portable(self.step_id, "Current step")
        if self.revision < 1:
            raise ValidationFailed("Checkpoint revision pozitif olmali")
        previous = (self.previous_checkpoint_id, self.previous_checkpoint_digest)
        if self.revision == 1 and previous != (None, None):
            raise ValidationFailed("Ilk checkpoint previous binding tasiyamaz")
        if self.revision > 1 and any(value is None for value in previous):
            raise ValidationFailed("Checkpoint revision zinciri previous identity ve digest ister")
        if self.previous_checkpoint_digest is not None:
            parse_digest(self.previous_checkpoint_digest)
        for value in (self.intent_digest, self.plan_digest, self.execution_envelope_digest):
            parse_digest(value)
        if self.deadline.tzinfo is None or self.created_at.tzinfo is None:
            raise ValidationFailed("Checkpoint zamanlari timezone-aware olmali")
        if self.tokens_used < 0 or self.cost_micros_used < 0 or self.attempts_used < 0:
            raise ValidationFailed("Checkpoint budget tuketimi negatif olamaz")
        if not isinstance(self.observed_lease_id, UUID):
            raise ValidationFailed("Observed lease identity zorunludur")
        if not isinstance(self.observed_fencing_token, int) or self.observed_fencing_token < 1:
            raise ValidationFailed("Observed fencing token pozitif olmali")
        self._validate_partition()
        self._validate_resources()
        _digests(self.test_and_eval_digests, "Test/eval digest")
        claims = [item.claim_id for item in self.open_effects]
        if len(claims) != len(set(claims)):
            raise ValidationFailed("Open effect claim referanslari tekil olmali")

    def _validate_partition(self) -> None:
        plan = set(self.plan_steps)
        completed = set(self.completed_steps)
        pending = set(self.pending_steps)
        if len(plan) != len(self.plan_steps) or any(not item.strip() for item in self.plan_steps):
            raise ValidationFailed("Checkpoint plan step kimlikleri tekil ve dolu olmali")
        if completed & pending or completed | pending != plan:
            raise ValidationFailed("Checkpoint completed/pending exact plan partition olmali")
        result_steps = [item.step_id for item in self.step_results]
        if len(result_steps) != len(set(result_steps)) or set(result_steps) != completed:
            raise ValidationFailed("Completed step'ler exact StepResultV2 ister")
        if pending and self.next_safe_action is None:
            raise ValidationFailed("Pending checkpoint next safe action ister")
        if not pending and self.next_safe_action is not None:
            raise ValidationFailed("Terminal checkpoint next safe action tasiyamaz")
        if self.next_safe_action is not None and self.next_safe_action.step_id not in pending:
            raise ValidationFailed("Next safe action pending bir step'e bagli olmali")

    def _validate_resources(self) -> None:
        reads = tuple(LogicalResource.parse(value).text for value in self.logical_read_resources)
        writes = tuple(LogicalResource.parse(value).text for value in self.logical_write_resources)
        if reads != tuple(sorted(set(reads))) or writes != tuple(sorted(set(writes))):
            raise ValidationFailed("Logical resources canonical, sirali ve tekil olmali")
        if set(reads) & set(writes):
            raise ValidationFailed("Ayni logical resource read ve write olamaz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-checkpoint/v2",
            "checkpoint_id": str(self.checkpoint_id),
            "checkpoint_key": self.checkpoint_key,
            "revision": self.revision,
            "previous_checkpoint_id": (
                None if self.previous_checkpoint_id is None else str(self.previous_checkpoint_id)
            ),
            "previous_checkpoint_digest": self.previous_checkpoint_digest,
            "identity": {
                "realm_id": str(self.realm_id),
                "project_id": str(self.project_id),
                "work_item_id": str(self.work_item_id),
                "intent_digest": self.intent_digest,
                "plan_id": str(self.plan_id),
                "plan_digest": self.plan_digest,
                "step_id": self.step_id,
                "run_id": str(self.run_id),
                "job_id": str(self.job_id),
                "attempt_id": str(self.attempt_id),
                "assignment_id": str(self.assignment_id),
                "execution_envelope_id": str(self.execution_envelope_id),
                "execution_envelope_digest": self.execution_envelope_digest,
                "route_decision_id": str(self.route_decision_id),
                "context_manifest_id": str(self.context_manifest_id),
                "context_packet_id": str(self.context_packet_id),
            },
            "bindings": self.bindings.body(),
            "progress": {
                "plan_steps": list(self.plan_steps),
                "completed_steps": list(self.completed_steps),
                "pending_steps": list(self.pending_steps),
                "step_results": [item.body() for item in self.step_results],
                "open_effects": [item.body() for item in self.open_effects],
            },
            "logical_resources": {
                "read": list(self.logical_read_resources),
                "write": list(self.logical_write_resources),
            },
            "sandbox": self.sandbox.body(),
            "budget": {
                "tokens_used": self.tokens_used,
                "cost_micros_used": self.cost_micros_used,
                "attempts_used": self.attempts_used,
                "deadline": self.deadline,
            },
            "test_and_eval_digests": list(self.test_and_eval_digests),
            "rollback_or_recovery": [item.body() for item in self.rollback_or_recovery],
            "resumability": self.resumability.value,
            "next_safe_action": (
                None if self.next_safe_action is None else self.next_safe_action.body()
            ),
            "lease_observation": {
                "lease_id": str(self.observed_lease_id),
                "fencing_token": self.observed_fencing_token,
            },
            "created_at": self.created_at,
            "grants_authority": False,
            "carries_active_lease": False,
            "approval_inherited": False,
        }

    @property
    def checkpoint_digest(self) -> str:
        return digest(self.body())
