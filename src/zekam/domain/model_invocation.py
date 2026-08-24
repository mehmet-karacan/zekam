"""Universal model invocation identity and process-local gateway permit."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class GatewaySourceLabel(StrEnum):
    OPENCODE_EMBEDDING = "opencode-embedding"
    PROVIDER_CONTRACT = "provider-contract"
    MODEL_CAMPAIGN = "model-campaign"
    MODEL_CAPABILITY = "model-capability"
    MODEL_BENCHMARK = "model-benchmark"


class GatewayBindingStatus(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"


class GatewayMode(StrEnum):
    AUDIT = "audit"
    ENFORCE = "enforce"


_PERMIT_SEAL = object()


@dataclass(frozen=True, slots=True)
class GatewayTransportProvenance:
    """Secret-free child transport binding derived from a sealed parent permit."""

    manifest_digest: str
    attempt_id: UUID
    claim_id: UUID

    def __post_init__(self) -> None:
        parse_digest(self.manifest_digest)


@dataclass(frozen=True, slots=True)
class GatewayInvocationPermit:
    manifest_id: UUID
    manifest_digest: str
    attempt_id: UUID
    claim_id: UUID
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        parse_digest(self.manifest_digest)
        if self._seal is not _PERMIT_SEAL:
            raise PolicyViolation("Gateway permit yalniz kanonik gateway tarafindan verilebilir")

    def assert_for(self, manifest: ModelRequestManifest) -> None:
        if self.manifest_id != manifest.id or self.manifest_digest != manifest.manifest_digest:
            raise PolicyViolation("Gateway permit manifest ile eslesmiyor")

    def transport_provenance(self, manifest: ModelRequestManifest) -> GatewayTransportProvenance:
        self.assert_for(manifest)
        return GatewayTransportProvenance(self.manifest_digest, self.attempt_id, self.claim_id)


def _issue_gateway_permit(
    manifest: ModelRequestManifest, *, attempt_id: UUID, claim_id: UUID
) -> GatewayInvocationPermit:
    """Process-local capability; serialized data tek basina permit uretemez."""

    manifest.assert_digest()
    return GatewayInvocationPermit(
        manifest.id, manifest.manifest_digest, attempt_id, claim_id, _PERMIT_SEAL
    )


@dataclass(frozen=True, slots=True)
class ModelRequestManifest:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    step_id: str
    execution_envelope_id: UUID | None
    execution_envelope_digest: str | None
    run_id: UUID | None
    job_id: UUID
    attempt_id: UUID
    assignment_id: UUID | None
    role: str | None
    risk: str
    route_decision_digest: str | None
    model_id: str
    provider_ref: str
    context_manifest_digest: str | None
    context_fragment_set_digest: str | None
    model_visible_payload_digest: str | None
    context_packet_digest: str | None
    checkpoint_digest: str | None
    source_revision: str | None
    policy_digest: str | None
    payload_digest: str
    authorization_scope_digest: str | None
    output_schema_digest: str | None
    idempotency_key: str
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_cost_micros: int | None
    deadline: dt.datetime
    route_expires_at: dt.datetime | None
    source_label: GatewaySourceLabel
    missing_bindings: tuple[str, ...] = ()
    tool_contract_digest: str | None = None
    environment_digest: str | None = None
    permission_profile_digest: str | None = None
    tool_set_digest: str | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    manifest_digest: str = ""

    def __post_init__(self) -> None:
        if (
            self.deadline.tzinfo is None
            or self.created_at.tzinfo is None
            or (self.route_expires_at is not None and self.route_expires_at.tzinfo is None)
        ):
            raise ValidationFailed("Model manifest zamanlari timezone-aware olmali")
        if self.deadline <= self.created_at:
            raise ValidationFailed("Model manifest deadline gelecekte olmali")
        if self.route_expires_at is not None and self.route_expires_at <= self.created_at:
            raise ValidationFailed("Model route expiry gelecekte olmali")
        for label, value in (
            ("step", self.step_id),
            ("risk", self.risk),
            ("model", self.model_id),
            ("provider", self.provider_ref),
            ("idempotency", self.idempotency_key),
        ):
            if not value.strip():
                raise ValidationFailed(f"Model manifest {label} bos olamaz")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValidationFailed("Model manifest risk gecersiz")
        for limit in (self.max_input_tokens, self.max_output_tokens, self.max_cost_micros):
            if limit is not None and limit <= 0:
                raise ValidationFailed("Model manifest limitleri pozitif olmali")
        if tuple(sorted(set(self.missing_bindings))) != self.missing_bindings:
            raise ValidationFailed("Eksik binding listesi unique ve sirali olmali")
        required_bindings = {
            "execution_envelope_digest": self.execution_envelope_digest,
            "execution_envelope_id": self.execution_envelope_id,
            "run_id": self.run_id,
            "assignment_id": self.assignment_id,
            "role": self.role,
            "route_decision_digest": self.route_decision_digest,
            "route_expires_at": self.route_expires_at,
            "context_manifest_digest": self.context_manifest_digest,
            "context_fragment_set_digest": self.context_fragment_set_digest,
            "model_visible_payload_digest": self.model_visible_payload_digest,
            "context_packet_digest": self.context_packet_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "policy_digest": self.policy_digest,
            "authorization_scope_digest": self.authorization_scope_digest,
            "output_schema_digest": self.output_schema_digest,
            "source_revision": self.source_revision,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micros": self.max_cost_micros,
        }
        expected_missing = tuple(
            sorted(name for name, binding in required_bindings.items() if binding is None)
        )
        if self.missing_bindings != expected_missing:
            raise ValidationFailed("Eksik binding listesi manifest alanlariyla exact eslesmeli")
        digest_values: tuple[str | None, ...] = (
            self.execution_envelope_digest,
            self.route_decision_digest,
            self.context_manifest_digest,
            self.context_fragment_set_digest,
            self.model_visible_payload_digest,
            self.context_packet_digest,
            self.checkpoint_digest,
            self.policy_digest,
            self.payload_digest,
            self.authorization_scope_digest,
            self.output_schema_digest,
            self.tool_contract_digest,
            self.environment_digest,
            self.permission_profile_digest,
            self.tool_set_digest,
        )
        for digest_value in digest_values:
            if digest_value is not None:
                parse_digest(digest_value)
        if (
            self.model_visible_payload_digest is not None
            and self.model_visible_payload_digest != self.payload_digest
        ):
            raise PolicyViolation("Model-visible payload digest request payload ile eslesmiyor")
        if self.manifest_digest:
            self.assert_digest()

    @property
    def binding_status(self) -> GatewayBindingStatus:
        return GatewayBindingStatus.UNBOUND if self.missing_bindings else GatewayBindingStatus.BOUND

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-request/v2",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id),
            "step_id": self.step_id,
            "execution_envelope_id": (
                None if self.execution_envelope_id is None else str(self.execution_envelope_id)
            ),
            "execution_envelope_digest": self.execution_envelope_digest,
            "run_id": None if self.run_id is None else str(self.run_id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "assignment_id": None if self.assignment_id is None else str(self.assignment_id),
            "role": self.role,
            "risk": self.risk,
            "route_decision_digest": self.route_decision_digest,
            "model_id": self.model_id,
            "provider_ref": self.provider_ref,
            "context_manifest_digest": self.context_manifest_digest,
            "context_fragment_set_digest": self.context_fragment_set_digest,
            "model_visible_payload_digest": self.model_visible_payload_digest,
            "context_packet_digest": self.context_packet_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "payload_digest": self.payload_digest,
            "authorization_scope_digest": self.authorization_scope_digest,
            "output_schema_digest": self.output_schema_digest,
            "idempotency_key": self.idempotency_key,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
            "route_expires_at": self.route_expires_at,
            "source_label": self.source_label.value,
            "missing_bindings": self.missing_bindings,
            "tool_contract_digest": self.tool_contract_digest,
            "environment_digest": self.environment_digest,
            "permission_profile_digest": self.permission_profile_digest,
            "tool_set_digest": self.tool_set_digest,
            "created_at": self.created_at,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    def assert_digest(self) -> None:
        parse_digest(self.manifest_digest)
        if self.manifest_digest != self.computed_digest:
            raise PolicyViolation("Model request manifest supplied digest mismatch")

    @classmethod
    def create(cls, **values: Any) -> ModelRequestManifest:
        values.setdefault("context_fragment_set_digest", None)
        values.setdefault("model_visible_payload_digest", None)
        item = cls(id=values.pop("id", uuid4()), manifest_digest="", **values)
        fields = {
            name: getattr(item, name)
            for name in item.__dataclass_fields__
            if name != "manifest_digest"
        }
        return cls(**fields, manifest_digest=item.computed_digest)
