"""Universal model request manifest audit/enforce boundary."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from zekam.application.environment_snapshot_service import EnvironmentEffectGuard
from zekam.application.provider_adapter import (
    MultipartProviderCall,
    ProviderCall,
    ProviderCallResult,
)
from zekam.application.provider_contract_execution import PreparedProviderContractCall
from zekam.domain.canonical import digest
from zekam.domain.context_fragment import ModelVisiblePayloadBinding
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_invocation import (
    GatewayInvocationPermit,
    GatewayMode,
    GatewaySourceLabel,
    ModelRequestManifest,
    _issue_gateway_permit,
)
from zekam.domain.security import Authorization
from zekam.domain.tool_registry import ModelToolPayloadBinding
from zekam.infrastructure.postgres.model_invocation_repository import (
    ModelInvocationRepository,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ModelGatewayBindings:
    execution_envelope_id: UUID | None = None
    execution_envelope_digest: str | None = None
    run_id: UUID | None = None
    role: str | None = None
    route_decision_digest: str | None = None
    route_expires_at: dt.datetime | None = None
    context_manifest_digest: str | None = None
    context_packet_digest: str | None = None
    checkpoint_digest: str | None = None
    source_revision: str | None = None
    policy_digest: str | None = None
    output_schema_digest: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_micros: int | None = None
    deadline: dt.datetime | None = None
    turn_execution_snapshot_digest: str | None = None
    environment_digest: str | None = None
    permission_profile_digest: str | None = None
    tool_set_digest: str | None = None
    config_effective_digest: str | None = None
    hook_set_digest: str | None = None


@dataclass(slots=True)
class ModelGateway:
    repository: ModelInvocationRepository
    source_label: GatewaySourceLabel
    bindings: ModelGatewayBindings = ModelGatewayBindings()
    environment_guard: EnvironmentEffectGuard | None = None

    @classmethod
    def from_execution_envelope(
        cls,
        repository: ModelInvocationRepository,
        source_label: GatewaySourceLabel,
        envelope_id: UUID,
        *,
        environment_guard: EnvironmentEffectGuard | None = None,
    ) -> ModelGateway:
        return cls(
            repository=repository,
            source_label=source_label,
            bindings=ModelGatewayBindings(**repository.envelope_bindings(envelope_id)),
            environment_guard=environment_guard,
        )

    def prepare(
        self,
        prepared: PreparedProviderContractCall,
        work: ClaimedWork,
        authorization: Authorization,
        *,
        payload_binding: ModelVisiblePayloadBinding | None = None,
        tool_payload_binding: ModelToolPayloadBinding | None = None,
        now: dt.datetime | None = None,
    ) -> ModelRequestManifest:
        job = work.job
        if job.work_item_id is None or job.plan_id is None or job.step_id is None:
            raise PolicyViolation("Model gateway exact work/plan/step binding ister")
        moment = now or dt.datetime.now(dt.UTC)
        if self.bindings.deadline is not None and authorization.expires_at < self.bindings.deadline:
            raise PolicyViolation("Authorization execution envelope deadline'ini kapsamiyor")
        values = {
            "execution_envelope_id": self.bindings.execution_envelope_id,
            "execution_envelope_digest": self.bindings.execution_envelope_digest,
            "run_id": self.bindings.run_id,
            "assignment_id": job.assignment_id,
            "role": self.bindings.role,
            "route_decision_digest": self.bindings.route_decision_digest,
            "route_expires_at": self.bindings.route_expires_at,
            "context_manifest_digest": self.bindings.context_manifest_digest,
            "context_fragment_set_digest": (
                None if payload_binding is None else payload_binding.fragment_set_digest
            ),
            "model_visible_payload_digest": (
                None if payload_binding is None else payload_binding.request_payload_digest
            ),
            "context_packet_digest": self.bindings.context_packet_digest,
            "checkpoint_digest": self.bindings.checkpoint_digest,
            "policy_digest": self.bindings.policy_digest,
            "authorization_scope_digest": digest(authorization.scope.body()),
            "output_schema_digest": self.bindings.output_schema_digest,
            "source_revision": self.bindings.source_revision,
            "max_input_tokens": self.bindings.max_input_tokens,
            "max_output_tokens": self.bindings.max_output_tokens,
            "max_cost_micros": self.bindings.max_cost_micros,
            "turn_execution_snapshot_digest": self.bindings.turn_execution_snapshot_digest,
            "environment_digest": self.bindings.environment_digest,
            "permission_profile_digest": self.bindings.permission_profile_digest,
            "tool_set_digest": self.bindings.tool_set_digest,
            "tool_visible_payload_digest": (
                None
                if tool_payload_binding is None
                else tool_payload_binding.serialized_tools_digest
            ),
            "tool_visible_payload_mode": (
                None
                if tool_payload_binding is None
                else ("code-mode" if tool_payload_binding.code_mode else "direct")
            ),
            "config_effective_digest": self.bindings.config_effective_digest,
            "hook_set_digest": self.bindings.hook_set_digest,
        }
        environment_keys = {
            "turn_execution_snapshot_digest",
            "environment_digest",
            "permission_profile_digest",
            "tool_set_digest",
            "tool_visible_payload_digest",
            "tool_visible_payload_mode",
            "config_effective_digest",
            "hook_set_digest",
        }
        missing = tuple(
            sorted(
                key
                for key, value in values.items()
                if value is None
                and (self.bindings.execution_envelope_id is not None or key not in environment_keys)
            )
        )
        if (
            payload_binding is not None
            and payload_binding.request_payload_digest != prepared.call.payload_digest
        ):
            raise PolicyViolation("Model-visible payload digest provider call ile eslesmiyor")
        if (
            payload_binding is not None
            and payload_binding.context_manifest_digest != self.bindings.context_manifest_digest
        ):
            raise PolicyViolation("Context fragment set execution manifest ile eslesmiyor")
        if tool_payload_binding is not None:
            tool_payload_binding.assert_valid()
            if tool_payload_binding.request_payload_digest != prepared.call.payload_digest:
                raise PolicyViolation("Model tool payload provider call ile eslesmiyor")
            if tool_payload_binding.tool_set_digest != self.bindings.tool_set_digest:
                raise PolicyViolation("Model tool payload compiled set ile eslesmiyor")
        if (
            self.repository.mode() is GatewayMode.ENFORCE
            and self.bindings.tool_set_digest is not None
            and tool_payload_binding is None
        ):
            raise PolicyViolation("Gateway enforce kanonik model tool payload ister")
        return ModelRequestManifest.create(
            realm_id=job.realm_id,
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            plan_id=job.plan_id,
            step_id=job.step_id,
            job_id=job.id,
            attempt_id=work.attempt_id,
            risk=authorization.risk,
            model_id=prepared.plan.model_id,
            provider_ref=prepared.plan.provider_ref,
            payload_digest=prepared.call.payload_digest,
            idempotency_key=digest({"job_id": str(job.id), "call_id": prepared.plan.call_id}),
            deadline=(
                self.bindings.deadline
                if self.bindings.deadline is not None
                else min(authorization.expires_at, moment + dt.timedelta(minutes=15))
            ),
            source_label=self.source_label,
            missing_bindings=missing,
            created_at=moment,
            **values,
        )

    def invoke(
        self,
        manifest: ModelRequestManifest,
        *,
        claim_id: UUID,
        authorization: Authorization,
        call: ProviderCall | MultipartProviderCall,
        effect: Callable[[GatewayInvocationPermit], ProviderCallResult],
    ) -> tuple[UUID, ProviderCallResult]:
        if manifest.payload_digest != call.payload_digest:
            raise PolicyViolation("Model gateway manifest payload digest mismatch")
        if (
            manifest.model_visible_payload_digest is not None
            and manifest.model_visible_payload_digest != call.payload_digest
        ):
            raise PolicyViolation("Model gateway model-visible payload binding mismatch")
        manifest_id, _ = self.repository.store_manifest(manifest)
        if manifest_id != manifest.id:
            raise PolicyViolation("Model gateway manifest replay kimligi uyusmuyor")
        disposition = manifest.binding_status.value
        self.repository.record_audit(
            manifest_id=manifest.id,
            source_label=self.source_label.value,
            disposition=disposition,
            missing_bindings=manifest.missing_bindings,
            call_digest=digest(
                {"request_identity": call.request_identity, "provider": call.provider_ref}
            ),
            payload_digest=call.payload_digest,
        )
        if self.repository.mode() is GatewayMode.ENFORCE:
            if manifest.missing_bindings:
                raise PolicyViolation("Model gateway enforce eksik binding reddi")
            if self.environment_guard is None or manifest.execution_envelope_id is None:
                raise PolicyViolation("Model gateway enforce live environment force probe ister")
            self.environment_guard.assert_envelope_current(
                manifest.execution_envelope_id, now=dt.datetime.now(dt.UTC)
            )
            self.repository.assert_current_envelope(manifest)
            self.repository.assert_current_context_fragment_set(manifest)
            self.repository.assert_current_tool_set(manifest)
        ledger_attempt_id = self.repository.record_attempt(
            manifest_id=manifest.id,
            effect_claim_id=claim_id,
            authorization_id=authorization.id,
        )
        permit = _issue_gateway_permit(manifest, attempt_id=ledger_attempt_id, claim_id=claim_id)
        try:
            result = effect(permit)
        except Exception as exc:
            self.repository.record_result(
                manifest_id=manifest.id,
                attempt_id=ledger_attempt_id,
                effect_receipt_id=None,
                state="reconciliation-required",
                failure_digest=digest({"category": type(exc).__name__}),
            )
            raise
        return ledger_attempt_id, result

    def record_terminal(
        self,
        manifest: ModelRequestManifest,
        ledger_attempt_id: UUID,
        *,
        receipt_id: UUID | None,
        response_digest: str | None,
        failure_digest: str | None = None,
    ) -> None:
        self.repository.record_result(
            manifest_id=manifest.id,
            attempt_id=ledger_attempt_id,
            effect_receipt_id=receipt_id,
            state="verified" if receipt_id is not None else "reconciliation-required",
            response_digest=response_digest,
            failure_digest=failure_digest,
        )
