"""Persisted agent runtime residency and safe-reload contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ResidencyState(StrEnum):
    LOADED = "loaded"
    IDLE = "idle"
    EVICTED = "evicted"
    CLOSING = "closing"
    DEAD = "dead"


class ReloadDisposition(StrEnum):
    LOADED = "loaded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AssignmentRuntimeSnapshot:
    id: UUID
    realm_id: UUID
    edge_id: UUID
    assignment_id: UUID
    execution_envelope_id: UUID
    role: str
    model_id: str
    provider_binding_id: UUID
    provider_binding_digest: str
    route_decision_id: UUID
    route_decision_digest: str
    environment_snapshot_digest: str
    permission_profile_digest: str
    config_effective_digest: str
    source_revision: str
    policy_digest: str
    created_at: dt.datetime
    snapshot_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Runtime snapshot authority uretemez")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Runtime snapshot zamani timezone-aware olmali")
        if any(not value.strip() for value in (self.role, self.model_id, self.source_revision)):
            raise ValidationFailed("Runtime snapshot kimlikleri bos olamaz")
        for value in (
            self.provider_binding_digest,
            self.route_decision_digest,
            self.environment_snapshot_digest,
            self.permission_profile_digest,
            self.config_effective_digest,
            self.policy_digest,
        ):
            parse_digest(value)
        if self.snapshot_digest:
            parse_digest(self.snapshot_digest)
            if self.snapshot_digest != self.computed_digest:
                raise PolicyViolation("Runtime snapshot digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-assignment-runtime-snapshot/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "edge_id": str(self.edge_id),
            "assignment_id": str(self.assignment_id),
            "execution_envelope_id": str(self.execution_envelope_id),
            "role": self.role,
            "model_id": self.model_id,
            "provider_binding_id": str(self.provider_binding_id),
            "provider_binding_digest": self.provider_binding_digest,
            "route_decision_id": str(self.route_decision_id),
            "route_decision_digest": self.route_decision_digest,
            "environment_snapshot_digest": self.environment_snapshot_digest,
            "permission_profile_digest": self.permission_profile_digest,
            "config_effective_digest": self.config_effective_digest,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> AssignmentRuntimeSnapshot:
        values.setdefault("id", uuid4())
        values["snapshot_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "snapshot_digest": draft.computed_digest})


@dataclass(frozen=True, slots=True)
class ReloadRequest:
    realm_id: UUID
    edge_id: UUID
    current_environment_snapshot_digest: str
    current_route_decision_id: UUID
    current_provider_binding_id: UUID
    runtime_session_ref: str
    requested_at: dt.datetime
    request_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Reload request authority uretemez")
        if not self.runtime_session_ref.strip():
            raise ValidationFailed("Reload runtime session ref bos olamaz")
        if self.requested_at.tzinfo is None:
            raise ValidationFailed("Reload zamani timezone-aware olmali")
        parse_digest(self.current_environment_snapshot_digest)
        if self.request_digest:
            parse_digest(self.request_digest)
            if self.request_digest != self.computed_digest:
                raise PolicyViolation("Reload request digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-agent-reload-request/v1",
            "realm_id": str(self.realm_id),
            "edge_id": str(self.edge_id),
            "current_environment_snapshot_digest": self.current_environment_snapshot_digest,
            "current_route_decision_id": str(self.current_route_decision_id),
            "current_provider_binding_id": str(self.current_provider_binding_id),
            "runtime_session_ref": self.runtime_session_ref,
            "requested_at": self.requested_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ReloadRequest:
        values["request_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "request_digest": draft.computed_digest})


@dataclass(frozen=True, slots=True)
class ReloadResult:
    disposition: ReloadDisposition
    state: ResidencyState
    generation: int
    reason: str | None = None

    @property
    def loaded(self) -> bool:
        return self.disposition is ReloadDisposition.LOADED
