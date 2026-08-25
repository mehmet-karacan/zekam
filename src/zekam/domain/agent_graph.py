"""Persisted agent runtime graph and root-scoped control contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ChildRuntimeStatus(StrEnum):
    RESERVED = "reserved"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_CHILD_STATUSES = frozenset(
    {ChildRuntimeStatus.COMPLETED, ChildRuntimeStatus.FAILED, ChildRuntimeStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class AgentGraphRoot:
    id: UUID
    realm_id: UUID
    run_id: UUID
    coordinator_assignment_id: UUID
    max_concurrency: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micros: int
    created_at: dt.datetime
    root_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Agent graph root authority uretemez")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Agent graph root zamani timezone-aware olmali")
        if (
            min(
                self.max_concurrency,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_cost_micros,
            )
            <= 0
        ):
            raise ValidationFailed("Agent graph limitleri pozitif olmali")
        if self.root_digest:
            parse_digest(self.root_digest)
            if self.root_digest != self.computed_digest:
                raise PolicyViolation("Agent graph root digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-agent-graph-root/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "run_id": str(self.run_id),
            "coordinator_assignment_id": str(self.coordinator_assignment_id),
            "max_concurrency": self.max_concurrency,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micros": self.max_cost_micros,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> AgentGraphRoot:
        values.setdefault("id", uuid4())
        values["root_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "root_digest": draft.computed_digest})


@dataclass(frozen=True, slots=True)
class SpawnEdge:
    id: UUID
    realm_id: UUID
    root_id: UUID
    parent_assignment_id: UUID
    child_assignment_id: UUID
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_micros: int
    created_at: dt.datetime
    edge_digest: str
    status: ChildRuntimeStatus = ChildRuntimeStatus.RESERVED
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Spawn edge authority uretemez")
        if self.parent_assignment_id == self.child_assignment_id:
            raise ValidationFailed("Agent kendi child'i olamaz")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Spawn edge zamani timezone-aware olmali")
        if (
            min(
                self.reserved_input_tokens,
                self.reserved_output_tokens,
                self.reserved_cost_micros,
            )
            <= 0
        ):
            raise ValidationFailed("Spawn rezervasyonlari pozitif olmali")
        if self.status is not ChildRuntimeStatus.RESERVED:
            raise ValidationFailed("Yeni spawn edge reserved olmali")
        if self.edge_digest:
            parse_digest(self.edge_digest)
            if self.edge_digest != self.computed_digest:
                raise PolicyViolation("Spawn edge digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-agent-spawn-edge/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "root_id": str(self.root_id),
            "parent_assignment_id": str(self.parent_assignment_id),
            "child_assignment_id": str(self.child_assignment_id),
            "reserved_input_tokens": self.reserved_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "reserved_cost_micros": self.reserved_cost_micros,
            "created_at": self.created_at,
            "status": ChildRuntimeStatus.RESERVED.value,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> SpawnEdge:
        values.setdefault("id", uuid4())
        values["edge_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "edge_digest": draft.computed_digest})


@dataclass(frozen=True, slots=True)
class AgentMessage:
    id: UUID
    realm_id: UUID
    root_id: UUID
    sender_assignment_id: UUID
    recipient_assignment_id: UUID
    context_type: str
    context_ref: str
    context_digest: str
    payload_schema: str
    payload: dict[str, Any]
    created_at: dt.datetime
    message_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Agent mesaji authority uretemez")
        if self.sender_assignment_id == self.recipient_assignment_id:
            raise ValidationFailed("Agent mesaji farkli alici ister")
        if not self.payload_schema.strip():
            raise ValidationFailed("Agent mesaji typed context ve payload schema ister")
        if self.context_type != "assignment-context-manifest":
            raise ValidationFailed("Agent mesaji canonical context type ister")
        if self.context_ref != str(self.recipient_assignment_id):
            raise ValidationFailed("Agent mesaji context ref recipient assignment ile eslesmeli")
        if not self.payload:
            raise ValidationFailed("Agent mesaji bos payload tasiyamaz")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Agent mesaji zamani timezone-aware olmali")
        parse_digest(self.context_digest)
        if self.message_digest:
            parse_digest(self.message_digest)
            if self.message_digest != self.computed_digest:
                raise PolicyViolation("Agent message digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-agent-message/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "root_id": str(self.root_id),
            "sender_assignment_id": str(self.sender_assignment_id),
            "recipient_assignment_id": str(self.recipient_assignment_id),
            "context": {
                "type": self.context_type,
                "ref": self.context_ref,
                "digest": self.context_digest,
            },
            "payload_schema": self.payload_schema,
            "payload": self.payload,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> AgentMessage:
        values.setdefault("id", uuid4())
        values["message_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "message_digest": draft.computed_digest})
