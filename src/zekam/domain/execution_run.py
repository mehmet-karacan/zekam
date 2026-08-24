"""Canonical execution run, context packet and fully-bound envelope contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ExecutionRunState(StrEnum):
    PREPARED = "prepared"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class CheckpointDisposition(StrEnum):
    BOUND = "bound"
    NOT_APPLICABLE_GENESIS = "not-applicable-genesis"


@dataclass(frozen=True, slots=True)
class ProviderBindingSnapshot:
    id: UUID
    realm_id: UUID
    model_id: str
    provider_ref: str
    endpoint_ref: str
    operation: str
    captured_at: dt.datetime
    expires_at: dt.datetime
    binding_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Provider binding authority uretemez")
        if self.captured_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValidationFailed("Provider binding zamanlari timezone-aware olmali")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Provider binding expiry capture sonrasinda olmali")
        if any(
            not value.strip()
            for value in (self.model_id, self.provider_ref, self.endpoint_ref, self.operation)
        ):
            raise ValidationFailed("Provider binding kimlikleri bos olamaz")
        if self.binding_digest:
            parse_digest(self.binding_digest)
            if self.binding_digest != self.computed_digest:
                raise PolicyViolation("Provider binding supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-provider-binding-snapshot/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "model_id": self.model_id,
            "provider_ref": self.provider_ref,
            "endpoint_ref": self.endpoint_ref,
            "operation": self.operation,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ProviderBindingSnapshot:
        return cast(ProviderBindingSnapshot, _create_digest_bound(cls, "binding_digest", values))


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    client_id: str
    session_id: str | None
    source_revision: str
    policy_digest: str
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micros: int
    deadline: dt.datetime
    created_at: dt.datetime
    run_digest: str
    state: ExecutionRunState = ExecutionRunState.PREPARED
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Execution run authority uretemez")
        if self.state is not ExecutionRunState.PREPARED:
            raise ValidationFailed("Yeni execution run prepared olmali")
        if self.deadline.tzinfo is None or self.created_at.tzinfo is None:
            raise ValidationFailed("Execution run zamanlari timezone-aware olmali")
        if self.deadline <= self.created_at:
            raise ValidationFailed("Execution run deadline gelecekte olmali")
        if not self.client_id.strip() or not self.source_revision.strip():
            raise ValidationFailed("Execution run client/source revision bos olamaz")
        if self.session_id is not None and not self.session_id.strip():
            raise ValidationFailed("Execution run session bos olamaz")
        parse_digest(self.policy_digest)
        if min(self.max_input_tokens, self.max_output_tokens, self.max_cost_micros) <= 0:
            raise ValidationFailed("Execution run butceleri pozitif olmali")
        if self.run_digest:
            parse_digest(self.run_digest)
            if self.run_digest != self.computed_digest:
                raise PolicyViolation("Execution run supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-execution-run/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id),
            "client_id": self.client_id,
            "session_id": self.session_id,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "state": self.state.value,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ExecutionRun:
        return cast(ExecutionRun, _create_digest_bound(cls, "run_digest", values))


@dataclass(frozen=True, slots=True)
class ContextPacketSection:
    candidate_id: str
    content_digest: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or self.ordinal < 1:
            raise ValidationFailed("Context packet section kimlik/ordinal gecersiz")
        parse_digest(self.content_digest)

    def body(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "content_digest": self.content_digest,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class ContextPacket:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    manifest_id: UUID
    manifest_digest: str
    sections: tuple[ContextPacketSection, ...]
    created_at: dt.datetime
    packet_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Context packet authority uretemez")
        if self.created_at.tzinfo is None or not self.sections:
            raise ValidationFailed("Context packet section ve timezone ister")
        if tuple(row.ordinal for row in self.sections) != tuple(range(1, len(self.sections) + 1)):
            raise ValidationFailed("Context packet ordinal exact ve kesintisiz olmali")
        if len({row.candidate_id for row in self.sections}) != len(self.sections):
            raise ValidationFailed("Context packet candidate kimlikleri tekil olmali")
        parse_digest(self.manifest_digest)
        if self.packet_digest:
            parse_digest(self.packet_digest)
            if self.packet_digest != self.computed_digest:
                raise PolicyViolation("Context packet supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-packet/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "manifest_id": str(self.manifest_id),
            "manifest_digest": self.manifest_digest,
            "sections": [row.body() for row in self.sections],
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ContextPacket:
        return cast(ContextPacket, _create_digest_bound(cls, "packet_digest", values))


@dataclass(frozen=True, slots=True)
class ExecutionEnvelope:
    id: UUID
    realm_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_id: UUID
    lease_id: UUID
    fencing_token: int
    request_ordinal: int
    idempotency_key: str
    assignment_id: UUID
    role: str
    route_decision_id: UUID
    route_decision_digest: str
    route_expires_at: dt.datetime
    model_id: str
    provider_binding_id: UUID
    provider_binding_digest: str
    provider_ref: str
    context_manifest_id: UUID
    context_manifest_digest: str
    context_packet_id: UUID
    context_packet_digest: str
    checkpoint_id: UUID | None
    checkpoint_digest: str | None
    checkpoint_disposition: CheckpointDisposition
    source_revision: str
    policy_digest: str
    authorization_scope_digest: str
    output_schema_digest: str
    payload_digest: str
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micros: int
    deadline: dt.datetime
    created_at: dt.datetime
    envelope_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Execution envelope authority uretemez")
        if any(
            value.tzinfo is None
            for value in (self.created_at, self.deadline, self.route_expires_at)
        ):
            raise ValidationFailed("Execution envelope zamanlari timezone-aware olmali")
        if self.deadline <= self.created_at or self.deadline > self.route_expires_at:
            raise ValidationFailed("Execution envelope deadline/route expiry gecersiz")
        if (
            min(self.fencing_token, self.request_ordinal) < 1
            or min(self.max_input_tokens, self.max_output_tokens, self.max_cost_micros) <= 0
        ):
            raise ValidationFailed("Execution envelope fence/butce pozitif olmali")
        if any(
            not value.strip()
            for value in (
                self.idempotency_key,
                self.role,
                self.model_id,
                self.provider_ref,
                self.source_revision,
            )
        ):
            raise ValidationFailed("Execution envelope kimlikleri bos olamaz")
        if self.checkpoint_disposition is CheckpointDisposition.BOUND:
            if self.checkpoint_id is None or self.checkpoint_digest is None:
                raise ValidationFailed("Bound execution envelope checkpoint ister")
        elif self.checkpoint_id is not None or self.checkpoint_digest is not None:
            raise ValidationFailed("Genesis execution envelope checkpoint tasiyamaz")
        for value in (
            self.route_decision_digest,
            self.context_manifest_digest,
            self.context_packet_digest,
            self.provider_binding_digest,
            self.policy_digest,
            self.authorization_scope_digest,
            self.output_schema_digest,
            self.payload_digest,
        ):
            parse_digest(value)
        if self.checkpoint_digest is not None:
            parse_digest(self.checkpoint_digest)
        if self.envelope_digest:
            parse_digest(self.envelope_digest)
            if self.envelope_digest != self.computed_digest:
                raise PolicyViolation("Execution envelope supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-execution-envelope/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "run_id": str(self.run_id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "lease_id": str(self.lease_id),
            "fencing_token": self.fencing_token,
            "request_ordinal": self.request_ordinal,
            "idempotency_key": self.idempotency_key,
            "assignment_id": str(self.assignment_id),
            "role": self.role,
            "route_decision_id": str(self.route_decision_id),
            "route_decision_digest": self.route_decision_digest,
            "route_expires_at": self.route_expires_at,
            "model_id": self.model_id,
            "provider_binding_id": str(self.provider_binding_id),
            "provider_binding_digest": self.provider_binding_digest,
            "provider_ref": self.provider_ref,
            "context_manifest_id": str(self.context_manifest_id),
            "context_manifest_digest": self.context_manifest_digest,
            "context_packet_id": str(self.context_packet_id),
            "context_packet_digest": self.context_packet_digest,
            "checkpoint_id": None if self.checkpoint_id is None else str(self.checkpoint_id),
            "checkpoint_digest": self.checkpoint_digest,
            "checkpoint_disposition": self.checkpoint_disposition.value,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "authorization_scope_digest": self.authorization_scope_digest,
            "output_schema_digest": self.output_schema_digest,
            "payload_digest": self.payload_digest,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ExecutionEnvelope:
        return cast(ExecutionEnvelope, _create_digest_bound(cls, "envelope_digest", values))


def _create_digest_bound(cls: Any, digest_field: str, values: dict[str, Any]) -> Any:
    item = cls(id=values.pop("id", uuid4()), **{digest_field: ""}, **values)
    fields = {
        name: getattr(item, name) for name in item.__dataclass_fields__ if name != digest_field
    }
    return cls(**fields, **{digest_field: item.computed_digest})
