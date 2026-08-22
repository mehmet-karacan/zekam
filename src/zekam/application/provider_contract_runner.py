"""Exact canli provider contract cagrisinin claim/receipt execution siniri.

Bu servis authorization uretmez ve retry yapmaz. Caller her call plan icin ayri
ISSUED authorization ile mevcut ``ClaimedWork`` saglar. Transport yalniz effect
claim kalici olduktan sonra cagrilir; her sonuc terminal receipt alir.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from zekam.application.execution import ExecutionHost
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.provider_adapter import (
    AuthorizedProviderClient,
    MultipartProviderCall,
    ProviderCallResult,
)
from zekam.application.provider_contract_execution import PreparedProviderContractCall
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim, EffectReceipt, FailureCategory
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretRef,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork


@dataclass(frozen=True, slots=True)
class ProviderContractExecutionResult:
    call_id: str
    claim: EffectClaim
    receipt: EffectReceipt
    provider_result: ProviderCallResult


def verify_exact_provider_authorization(
    prepared: PreparedProviderContractCall,
    authorization: Authorization,
    secret_ref: SecretRef,
) -> None:
    """Job/transport mutation oncesinde exact authority bindingini dogrular."""

    plan = prepared.plan
    call = prepared.call
    if not plan.runtime_bound:
        raise PolicyViolation("Provider contract call runtime-bound olmali")
    if call.request_identity != plan.call_id or call.payload_digest != plan.payload_digest:
        raise PolicyViolation("Provider contract payload/call identity drift")
    if call.authorization_plan_digest != plan.authorization_plan_digest:
        raise PolicyViolation("Provider contract call plan digest drift")
    if authorization.rejection_reason(dt.datetime.now(dt.UTC)) is not None:
        raise PolicyViolation("Provider contract authorization issued ve gecerli olmali")
    if authorization.plan_digest != plan.authorization_plan_digest:
        raise PolicyViolation("Provider contract authorization plan digest mismatch")
    if authorization.effect_digest != plan.effect_request.effect_digest:
        raise PolicyViolation("Provider contract authorization effect digest mismatch")
    if secret_ref.id not in authorization.scope.secret_ref_ids:
        raise PolicyViolation("Provider contract SecretRef authorization scope disinda")
    if (
        authorization.scope.body()
        != AuthorizationScope(
            allowed_resources=(plan.target, plan.call_resource),
            allowed_effects=("provider-call",),
            provider_refs=(plan.provider_ref,),
            secret_ref_ids=(secret_ref.id,),
            data_classifications=(DataClassification.PUBLIC,),
        ).body()
    ):
        raise PolicyViolation("Provider contract authorization scope exact degil")


@dataclass(slots=True)
class RuntimeProviderContractRunner:
    host: ExecutionHost
    work: ClaimedWork
    client: AuthorizedProviderClient
    defer_job_recovery: bool = False

    @staticmethod
    def _failure_category(exc: Exception) -> FailureCategory:
        if isinstance(exc, AuthorizationRequired):
            return FailureCategory.AUTHORIZATION
        if isinstance(exc, PolicyViolation):
            return FailureCategory.POLICY
        if isinstance(exc, ValidationFailed):
            return FailureCategory.VALIDATION
        if isinstance(exc, ProbeUnavailable):
            return FailureCategory.PROVIDER
        return FailureCategory.ADAPTER

    def _verify_exact_authorization(
        self,
        prepared: PreparedProviderContractCall,
        authorization: Authorization,
        secret_ref: SecretRef,
    ) -> None:
        verify_exact_provider_authorization(prepared, authorization, secret_ref)

    def invoke(
        self,
        prepared: PreparedProviderContractCall,
        *,
        secret_ref: SecretRef,
        authorization: Authorization,
        consumed_by: str,
    ) -> ProviderContractExecutionResult:
        """Tek exact call'i bir kez yurutur; hicbir exception sessiz retry edilmez."""

        self._verify_exact_authorization(prepared, authorization, secret_ref)
        plan = prepared.plan
        for existing in self.host.ledger.claims_for_job(self.work.job.id):
            if existing.effect_digest == plan.effect_request.effect_digest:
                raise PolicyViolation("Provider contract exact call silent retry yasak")
        claim = self.host.claim_effect(
            self.work,
            operation=f"provider-contract:{plan.call_id}",
            effect_digest=plan.effect_request.effect_digest,
            authorization_digest=authorization.authorization_digest,
            authorization_id=authorization.id,
            idempotency_key=plan.authorization_plan_digest,
            resources=parse_requests(write=(plan.call_resource,)),
            adapter_digest=digest(
                {
                    "adapter": "authorized-provider-client",
                    "call_plan_digest": plan.authorization_plan_digest,
                }
            ),
        )
        try:
            if isinstance(prepared.call, MultipartProviderCall):
                result = self.client.invoke_multipart(
                    prepared.call,
                    secret_ref=secret_ref,
                    authorization=authorization,
                    consumed_by=consumed_by,
                )
            else:
                result = self.client.invoke(
                    prepared.call,
                    secret_ref=secret_ref,
                    authorization=authorization,
                    consumed_by=consumed_by,
                )
        except Exception as exc:
            self.host.record_failure(
                claim,
                category=self._failure_category(exc),
                failure_digest=digest({"error_type": type(exc).__name__, "call_id": plan.call_id}),
            )
            if not self.defer_job_recovery:
                self.host.jobs.mark_recovery_required(
                    self.work.job.id, "provider-contract-effect-failed-no-silent-retry"
                )
            raise
        receipt = self.host.record_success(
            claim,
            result_digest=result.response_digest,
            adapter_evidence_digest=digest(
                {
                    "outbound_request_id": str(result.outbound_request_id),
                    "authorization_id": str(result.authorization_id),
                    "response_digest": result.response_digest,
                }
            ),
        )
        return ProviderContractExecutionResult(plan.call_id, claim, receipt, result)
