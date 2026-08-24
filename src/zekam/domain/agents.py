"""Canonical agent assignment and invocation contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class AssignmentRole(StrEnum):
    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    SYNTHESIZER = "synthesizer"
    VERIFIER = "verifier"


class AssignmentStatus(StrEnum):
    READY = "ready"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_ASSIGNMENT_STATUSES = frozenset(
    {AssignmentStatus.COMPLETED, AssignmentStatus.FAILED, AssignmentStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    role: AssignmentRole
    agent_ref: str
    instruction_digest: str
    context_manifest_digest: str
    assignment_digest: str
    status: AssignmentStatus = AssignmentStatus.READY
    parent_assignment_id: UUID | None = None
    plan_id: UUID | None = None
    step_id: str | None = None
    risk: str = "medium"
    read_resources: tuple[str, ...] = ()
    write_resources: tuple[str, ...] = ()
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.min.replace(tzinfo=dt.UTC))

    def __post_init__(self) -> None:
        if not self.agent_ref.strip():
            raise ValidationFailed("agent_ref bos olamaz")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValidationFailed("assignment risk gecersiz")
        parse_digest(self.instruction_digest)
        parse_digest(self.context_manifest_digest)
        parse_digest(self.assignment_digest)
        if self.role is AssignmentRole.COORDINATOR and self.parent_assignment_id is not None:
            raise PolicyViolation("Koordinator child olamaz")

    @property
    def is_child(self) -> bool:
        return self.parent_assignment_id is not None and self.role is not AssignmentRole.COORDINATOR

    def identity_body(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id) if self.plan_id else None,
            "step_id": self.step_id,
            "parent_assignment_id": str(self.parent_assignment_id)
            if self.parent_assignment_id
            else None,
            "role": self.role.value,
            "agent_ref": self.agent_ref,
            "risk": self.risk,
            "instruction_digest": self.instruction_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "read_resources": sorted(self.read_resources),
            "write_resources": sorted(self.write_resources),
        }

    def assert_digest(self) -> None:
        if digest(self.identity_body()) != self.assignment_digest:
            raise ValidationFailed("Assignment supplied digest canonical identity ile uyusmuyor")


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    id: UUID
    realm_id: UUID
    assignment_id: UUID
    client_id: str
    execution_identity: str
    invocation_digest: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.execution_identity.strip():
            raise ValidationFailed("invocation kimligi bos olamaz")
        parse_digest(self.invocation_digest)

    def assert_digest(self) -> None:
        body = {
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "assignment_id": str(self.assignment_id),
            "client_id": self.client_id,
            "execution_identity": self.execution_identity,
        }
        if digest(body) != self.invocation_digest:
            raise ValidationFailed("Invocation supplied digest canonical identity ile uyusmuyor")
