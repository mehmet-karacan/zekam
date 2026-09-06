"""Windows/OpenCode production embedding adapter contract tests."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.embedding_provider import (
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingPurpose,
)
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_embedding import (
    OpenCodeEmbeddingConfiguration,
    load_opencode_embedding_configuration,
)
from zekam.application.provider_adapter import ProviderCall, ProviderCallResult
from zekam.application.provider_contract_execution import PreparedProviderContractCall
from zekam.application.provider_contract_runner import RuntimeProviderContractRunner
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim, EffectReceipt
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind
from zekam.infrastructure.embedding.opencode_remote import (
    OpenCodeEmbeddingExecution,
    OpenCodeRemoteEmbeddingProvider,
    OpenCodeRuntimeInvocation,
    RuntimeOpenCodeEmbeddingExecutor,
)

pytestmark = pytest.mark.unit

MODEL = "openai/BAAI/bge-m3"


def _configuration(tmp_path: Path) -> OpenCodeEmbeddingConfiguration:
    source = tmp_path / "opencode.json"
    source.write_text(
        json.dumps(
            {
                "enabled_providers": ["litellm"],
                "provider": {
                    "litellm": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "LiteLLM",
                        "options": {
                            "baseURL": "https://models.example.test/v1",
                            "apiKey": "{env:OPENCODE_LITELLM_KEY}",
                        },
                        "models": {MODEL: {"name": "BGE-M3"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return load_opencode_embedding_configuration(
        source,
        provider_id="litellm",
        selected_model_id=MODEL,
        inventory=load_inventory(),
    )


class _Executor:
    def __init__(self, *, fault: str | None = None) -> None:
        self.fault = fault
        self.prepared: list[PreparedProviderContractCall] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        if "cake" in lowered or "tarif" in lowered:
            return [0.0, 1.0, 0.0]
        if "release" in lowered or "active" in lowered:
            return [0.9, 0.1, 0.0]
        return [1.0, 0.0, 0.0]

    def invoke(self, prepared: PreparedProviderContractCall) -> OpenCodeEmbeddingExecution:
        self.prepared.append(prepared)
        payload = prepared.call.payload
        assert isinstance(payload, Mapping)
        values = payload["input"]
        assert isinstance(values, list)
        rows: list[dict[str, Any]] = [
            {"index": index, "embedding": self._vector(str(text))}
            for index, text in enumerate(values)
        ]
        if self.fault == "partial":
            rows.pop()
        elif self.fault == "nan":
            rows[0]["embedding"][0] = float("nan")
        elif self.fault == "dimension":
            rows[0]["embedding"].append(0.0)
        elif self.fault == "repeat-small" and len(rows) > 1:
            rows[1]["embedding"] = [0.9999, 0.0001, 0.0]
        elif self.fault == "repeat-large" and len(rows) > 1:
            rows[1]["embedding"] = [0.95, 0.05, 0.0]
        elif self.fault == "profile-jitter":
            for row in rows:
                row["embedding"][0] += 0.0001
        response = {"data": rows, "model": MODEL}
        response_digest = digest(response)
        realm_id = uuid4()
        secret_ref = SecretRef.create(
            realm_id=realm_id,
            name="opencode-litellm-embedding",
            provider=prepared.plan.provider_ref,
            purpose="embedding",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator="OPENCODE_LITELLM_KEY",
        )
        authorization = Authorization.issue(
            realm_id=realm_id,
            actor_id=uuid4(),
            plan_digest=prepared.plan.authorization_plan_digest,
            effect_digest=prepared.plan.effect_request.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(prepared.plan.target, prepared.plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(prepared.plan.provider_ref,),
                secret_ref_ids=(secret_ref.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=5),
        )
        claim = EffectClaim.create(
            realm_id=realm_id,
            job_id=uuid4(),
            attempt_id=uuid4(),
            operation=f"provider-contract:{prepared.plan.call_id}",
            effect_digest=prepared.plan.effect_request.effect_digest,
            authorization_digest=authorization.authorization_digest,
            idempotency_key=digest(prepared.plan.call_id),
            resources=parse_requests(write=(prepared.plan.call_resource,)),
            execution_identity="unit-executor",
            fencing_token=1,
            adapter_digest=digest("authorized-provider-client"),
        )
        result = ProviderCallResult(response, response_digest, uuid4(), authorization.id)
        receipt = EffectReceipt.completed(
            realm_id=claim.realm_id,
            claim=claim,
            result_digest=response_digest,
            adapter_evidence_digest=digest("gateway-terminal"),
        )
        if self.fault == "receipt":
            receipt = replace(receipt, claim_id=uuid4())
        returned_prepared = prepared
        if self.fault == "plan":
            returned_prepared = replace(
                prepared,
                plan=replace(
                    prepared.plan,
                    data_classifications=(DataClassification.INTERNAL,),
                ),
            )
        if self.fault == "authorization":
            claim = replace(claim, authorization_digest=digest("wrong-authorization"))
        return OpenCodeEmbeddingExecution(
            returned_prepared,
            claim,
            receipt,
            result,
            authorization,
            secret_ref,
            digest(
                {
                    "claim_digest": claim.claim_digest,
                    "receipt_id": str(receipt.id),
                    "result_digest": result.response_digest,
                }
            ),
        )


class _PersistentHost:
    def __init__(self) -> None:
        self.realm_id = uuid4()
        self.claims: list[EffectClaim] = []
        self.receipts: dict[object, EffectReceipt] = {}
        self.ledger = self
        self.jobs = self

    def claims_for_job(self, job_id: object) -> tuple[EffectClaim, ...]:
        return tuple(item for item in self.claims if item.job_id == job_id)

    def receipt_for_claim(self, claim_id: object) -> EffectReceipt | None:
        return self.receipts.get(claim_id)

    def claim_effect(self, work: object, **values: object) -> EffectClaim:
        claim = EffectClaim.create(
            realm_id=self.realm_id,
            job_id=work.job.id,  # type: ignore[attr-defined]
            attempt_id=work.attempt_id,  # type: ignore[attr-defined]
            operation=str(values["operation"]),
            effect_digest=str(values["effect_digest"]),
            authorization_digest=str(values["authorization_digest"]),
            idempotency_key=str(values["idempotency_key"]),
            resources=tuple(values["resources"]),  # type: ignore[arg-type]
            execution_identity="runtime-executor-test",
            fencing_token=1,
            adapter_digest=str(values["adapter_digest"]),
        )
        self.claims.append(claim)
        return claim

    def record_success(self, claim: EffectClaim, **values: object) -> EffectReceipt:
        receipt = EffectReceipt.completed(
            realm_id=self.realm_id,
            claim=claim,
            result_digest=str(values["result_digest"]),
            adapter_evidence_digest=str(values["adapter_evidence_digest"]),
        )
        self.receipts[claim.id] = receipt
        return receipt

    def record_failure(self, claim: EffectClaim, **values: object) -> EffectReceipt:
        del claim, values
        raise AssertionError("runtime success test must not fail")

    def mark_recovery_required(self, job_id: object, reason: str) -> None:
        del job_id, reason


class _RuntimeClient:
    def invoke(self, call: ProviderCall, **values: object) -> ProviderCallResult:
        payload = call.payload
        rows = [
            {"index": index, "embedding": _Executor._vector(str(text))}
            for index, text in enumerate(payload["input"])
        ]
        response = {"data": rows, "model": MODEL}
        authorization = values["authorization"]
        return ProviderCallResult(
            response,
            digest(response),
            uuid4(),
            authorization.id,  # type: ignore[attr-defined]
        )


def _fixture() -> EmbeddingProbeFixture:
    return EmbeddingProbeFixture(
        query="A product service validates versions.",
        positive_passage="The service checks whether a release is active.",
        negative_passage="A recipe explains how to bake cake.",
        source_refs=("source:a", "source:b"),
        source_digests=(digest("a"), digest("b")),
        classification=DataClassification.PUBLIC,
    )


def test_remote_provider_requires_probe_and_explicit_disclosure(tmp_path: Path) -> None:
    executor = _Executor()
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    assert not provider.health().healthy

    result = provider.probe(_fixture())

    assert result.semantic_margin > 0.05
    assert result.provider_call_count == 2
    assert provider.health().healthy
    policy = EmbeddingPolicy(DataClassification.INTERNAL, result.profile.profile_digest)
    with pytest.raises(PolicyViolation, match="remote-disclosure-not-authorized"):
        provider.embed_query("internal project", policy)
    authorized = replace(policy, remote_disclosure_authorized=True)
    batch = provider.embed_documents(("internal project", "active release"), authorized)
    assert batch.receipt.vector_count == 2
    assert batch.receipt.provider_call_count == 1
    assert executor.prepared[-1].plan.data_classifications == (DataClassification.INTERNAL,)
    assert executor.prepared[-1].call.data_categories == (DataClassification.INTERNAL,)


def test_runtime_executor_reads_back_durable_claim_and_receipt(tmp_path: Path) -> None:
    host = _PersistentHost()

    def invocation(prepared: PreparedProviderContractCall) -> OpenCodeRuntimeInvocation:
        secret_ref = SecretRef.create(
            realm_id=host.realm_id,
            name="opencode-litellm-embedding",
            provider=prepared.plan.provider_ref,
            purpose="embedding",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator="OPENCODE_LITELLM_KEY",
        )
        authorization = Authorization.issue(
            realm_id=host.realm_id,
            actor_id=uuid4(),
            plan_digest=prepared.plan.authorization_plan_digest,
            effect_digest=prepared.plan.effect_request.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(prepared.plan.target, prepared.plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(prepared.plan.provider_ref,),
                secret_ref_ids=(secret_ref.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=5),
        )
        runner = RuntimeProviderContractRunner(
            host=host,  # type: ignore[arg-type]
            work=SimpleNamespace(
                job=SimpleNamespace(id=uuid4()),
                attempt_id=uuid4(),
            ),  # type: ignore[arg-type]
            client=_RuntimeClient(),
        )
        return OpenCodeRuntimeInvocation(
            runner,
            secret_ref,
            authorization,
            "windows-runtime-test",
        )

    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path),
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=3,
    )
    result = provider.probe(_fixture())
    assert result.provider_call_count == 2
    assert len(host.claims) == 2
    assert len(host.receipts) == 2


def test_remote_provider_accepts_bounded_repeat_numeric_jitter(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="repeat-small"), dimension=3
    )
    result = provider.probe(_fixture())
    assert 0 < result.max_repeat_delta <= 5e-4


def test_remote_provider_rejects_material_repeat_numeric_drift(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="repeat-large"), dimension=3
    )
    with pytest.raises(ValidationFailed, match="determinism drift"):
        provider.probe(_fixture())


def test_remote_profile_identity_is_stable_across_bounded_probe_jitter(tmp_path: Path) -> None:
    baseline = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(), dimension=3
    ).probe(_fixture())
    jittered = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="profile-jitter"), dimension=3
    ).probe(_fixture())

    assert baseline.profile.profile_digest == jittered.profile.profile_digest
    assert (
        baseline.profile.model_revision_fingerprint == jittered.profile.model_revision_fingerprint
    )
    assert baseline.evidence_digest != jittered.evidence_digest


@pytest.mark.parametrize("fault", ["partial", "nan", "dimension"])
def test_remote_provider_rejects_invalid_or_partial_vectors(tmp_path: Path, fault: str) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault=fault), dimension=3
    )
    with pytest.raises(ValidationFailed):
        provider.probe(_fixture())


def test_remote_provider_rejects_unsettled_execution(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="receipt"), dimension=3
    )
    with pytest.raises(PolicyViolation, match="receipt/claim"):
        provider.probe(_fixture())


@pytest.mark.parametrize("fault", ["plan", "authorization"])
def test_remote_provider_rejects_executor_authority_drift(tmp_path: Path, fault: str) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault=fault), dimension=3
    )
    with pytest.raises(PolicyViolation):
        provider.probe(_fixture())


def test_classification_changes_authorization_and_effect_digests(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    public = provider._prepare(
        ("same text",),
        purpose=EmbeddingPurpose.PROBE,
        classification=DataClassification.PUBLIC,
    )
    internal = provider._prepare(
        ("same text",),
        purpose=EmbeddingPurpose.PROBE,
        classification=DataClassification.INTERNAL,
    )
    assert public.plan.authorization_plan_digest != internal.plan.authorization_plan_digest
    assert public.plan.effect_request.effect_digest != internal.plan.effect_request.effect_digest


def test_remote_probe_rejects_non_public_fixture(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    with pytest.raises(PolicyViolation, match="public fixture"):
        provider.probe(replace(_fixture(), classification=DataClassification.INTERNAL))
