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
    def __init__(
        self,
        *,
        fault: str | None = None,
        fault_call: int | None = None,
    ) -> None:
        self.fault = fault
        # fault_call: hatanin uretilecegi calisma sirasi (0 tabanli); None = her cagida.
        self.fault_call = fault_call
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
        fault = self.fault if self.fault_call in (None, len(self.prepared) - 1) else None
        if fault == "index-missing":
            rows[-1].pop("index")
        elif fault == "index-duplicate":
            rows[-1]["index"] = 0
        elif fault == "index-out-of-range":
            rows[-1]["index"] = len(rows)
        elif fault == "index-shuffled":
            for index in range(len(rows) // 2):
                rows[index], rows[len(rows) - 1 - index] = (
                    rows[len(rows) - 1 - index],
                    rows[index],
                )
        elif fault == "partial":
            rows.pop()
        elif fault == "nan":
            rows[0]["embedding"][0] = float("nan")
        elif fault == "dimension":
            rows[0]["embedding"].append(0.0)
        elif fault == "repeat-small" and len(rows) > 1:
            rows[1]["embedding"] = [0.9999, 0.0001, 0.0]
        elif fault == "repeat-large" and len(rows) > 1:
            rows[1]["embedding"] = [0.95, 0.05, 0.0]
        elif fault == "profile-jitter":
            for row in rows:
                row["embedding"][0] += 0.0001
        elif fault == "profile-shift":
            for row in rows:
                row["embedding"][1] += 0.2
        response: dict[str, Any] = {"data": rows, "model": MODEL}
        if fault == "model-missing":
            response.pop("model")
        elif fault == "model-mismatch":
            response["model"] = "openai/BAAI/bge-m3-alt"
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


def test_remote_profile_identity_ignores_probe_vectors_but_evidence_records_them(
    tmp_path: Path,
) -> None:
    """RAG26-R02: identity = provider/endpoint/model/dimension, never probe vectors."""
    baseline = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(), dimension=3
    ).probe(_fixture())
    shifted = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="profile-shift"), dimension=3
    ).probe(_fixture())

    assert baseline.profile.profile_digest == shifted.profile.profile_digest
    assert baseline.profile.model_revision_fingerprint == shifted.profile.model_revision_fingerprint
    assert baseline.evidence_digest != shifted.evidence_digest


def test_rounding_boundary_jitter_does_not_change_profile_identity(
    tmp_path: Path,
) -> None:
    """B06 WP1 regression: expected to FAIL on baseline, PASS after WP2.

    The task's synthetic counter-example: two normalized vectors within numeric
    tolerance (u=(0.00049, sqrt(1-0.00049^2), 0...), v=(0.00051,
    sqrt(1-0.00051^2), 0...); max_delta<0.0005, cosine>0.99999) must NOT produce
    a different profile_identity/digest.  On the current baseline the probe
    compatibility fingerprint uses ``round(value*1000)`` which flips 0->1 across
    the boundary, so two tolerance-accepted spaces hash to different identities
    => FAILS.  WP2 must use a tolerance-aware compatibility comparison instead.
    """
    import math as _math

    from zekam.domain.canonical import digest as _digest
    from zekam.infrastructure.query_measurement import (
        quantized_vector_fingerprint,
        vectors_compatible,
    )

    u = (0.00049, _math.sqrt(1.0 - 0.00049**2), 0.0)
    v = (0.00051, _math.sqrt(1.0 - 0.00051**2), 0.0)

    max_delta = max(abs(a - b) for a, b in zip(u, v, strict=True))
    cosine = sum(a * b for a, b in zip(u, v, strict=True))  # both unit-norm
    assert max_delta < 0.0005
    assert cosine > 0.99999

    # The counter-example must be tolerance-compatible...
    assert vectors_compatible(u, v, max_delta=0.0005, min_cosine=0.99999)

    # The WP1 baseline used `round(value*1000)` inline, which flips 0.00049 -> 0
    # and 0.00051 -> 1 across the midpoint, so two tolerance-accepted vectors
    # hashed to *different* identities.  This is both mathematically unsatisfiable
    # and contrary to task authority B06 #1 (do not use rounded-vector hash
    # equality as tolerance compatibility).  WP2 uses the production
    # tolerance-aware fingerprint; two tolerance-accepted vectors must yield the
    # same identity digest.
    quantized_u = quantized_vector_fingerprint(u)
    quantized_v = quantized_vector_fingerprint(v)
    # The tolerance-aware production fingerprint must be stable under jitter...
    assert quantized_u == quantized_v
    assert _digest({"v": quantized_u}) == _digest({"v": quantized_v})
    # ...and the material-change path must still change identity (a real
    # embedding-space shift moves a component across a bucket boundary).
    shifted = quantized_vector_fingerprint((0.5, _math.sqrt(1.0 - 0.5**2), 0.0))
    assert quantized_u != shifted


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


def test_document_effect_identity_includes_qualified_profile(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    provider.probe(_fixture())
    first = provider._prepare(
        ("same document",),
        purpose=EmbeddingPurpose.DOCUMENT,
        classification=DataClassification.INTERNAL,
    )
    assert provider._profile is not None
    provider._profile = replace(
        provider._profile,
        model_revision_fingerprint=digest("changed-model-revision"),
    )
    second = provider._prepare(
        ("same document",),
        purpose=EmbeddingPurpose.DOCUMENT,
        classification=DataClassification.INTERNAL,
    )
    assert first.plan.effect_request.effect_digest != second.plan.effect_request.effect_digest


def test_remote_probe_rejects_non_public_fixture(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    with pytest.raises(PolicyViolation, match="public fixture"):
        provider.probe(replace(_fixture(), classification=DataClassification.INTERNAL))


def test_probe_rejects_missing_index(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="index-missing"), dimension=3
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.probe(_fixture())


def test_probe_rejects_duplicate_index(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="index-duplicate"), dimension=3
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.probe(_fixture())


def test_probe_rejects_out_of_range_index(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="index-out-of-range"), dimension=3
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.probe(_fixture())


def test_probe_accepts_shuffled_data_with_correct_explicit_indexes(tmp_path: Path) -> None:
    """Data satirlari karisik ama explicit index dogruysa, vektorlar dogru slotta doner."""
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="index-shuffled"), dimension=3
    )
    probe = provider.probe(_fixture())
    # index-shuffled satirlari cift basina ters cevirmekle karistirir; explicit indexler
    # dogru oldugu icin provider index bazli slota yerlestirir ve determinism check gecmeli.
    assert probe.max_repeat_delta <= 5e-4
    assert probe.batch_cosine > 0.99999


def test_embed_documents_rejects_missing_index(tmp_path: Path) -> None:
    executor = _Executor(fault="index-missing", fault_call=2)
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.embed_documents(("internal project", "active release"), policy)


def test_embed_documents_rejects_duplicate_index(tmp_path: Path) -> None:
    executor = _Executor(fault="index-duplicate", fault_call=2)
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.embed_documents(("internal project", "active release"), policy)


def test_embed_documents_rejects_out_of_range_index(tmp_path: Path) -> None:
    executor = _Executor(fault="index-out-of-range", fault_call=2)
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    with pytest.raises(ValidationFailed, match="indexleri exact"):
        provider.embed_documents(("internal project", "active release"), policy)


def test_embed_documents_accepts_shuffled_data_with_correct_explicit_indexes(
    tmp_path: Path,
) -> None:
    """embed_documents: karisik data satirlari, dogru explicit index -> dogru slot."""
    executor = _Executor(fault="index-shuffled")
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    # 2 satirli batch: executor [1, 0] sirasinda doner (index=1: "active release",
    # index=0: "internal project"). Provider index bazli slot'a yerlestirmeli:
    # slot 0 = "internal project" -> [1, 0, 0],
    # slot 1 = "active release" -> normalize([0.9, 0.1, 0]).
    batch = provider.embed_documents(("internal project", "active release"), policy)
    assert batch.vectors[0] == (1.0, 0.0, 0.0)
    norm = (0.9**2 + 0.1**2) ** 0.5
    assert batch.vectors[1] == (0.9 / norm, 0.1 / norm, 0.0)


def test_probe_rejects_missing_model_field(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="model-missing"), dimension=3
    )
    with pytest.raises(PolicyViolation, match="model kimligi uyusmuyor"):
        provider.probe(_fixture())


def test_probe_rejects_mismatched_model_field(tmp_path: Path) -> None:
    provider = OpenCodeRemoteEmbeddingProvider(
        _configuration(tmp_path), _Executor(fault="model-mismatch"), dimension=3
    )
    with pytest.raises(PolicyViolation, match="model kimligi uyusmuyor"):
        provider.probe(_fixture())


def test_embed_documents_rejects_missing_model_field(tmp_path: Path) -> None:
    executor = _Executor(fault="model-missing", fault_call=2)
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    with pytest.raises(PolicyViolation, match="model kimligi uyusmuyor"):
        provider.embed_documents(("internal project", "active release"), policy)


def test_embed_documents_rejects_mismatched_model_field(tmp_path: Path) -> None:
    executor = _Executor(fault="model-mismatch", fault_call=2)
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), executor, dimension=3)
    probe = provider.probe(_fixture())
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    with pytest.raises(PolicyViolation, match="model kimligi uyusmuyor"):
        provider.embed_documents(("internal project", "active release"), policy)


def test_document_journal_recovers_vectors_without_provider_reexecution(tmp_path: Path) -> None:
    from zekam.infrastructure.local_file_security import restrict_private_tree
    from zekam.infrastructure.opencode_provider_ledger import (
        SQLiteDocumentEmbeddingJournal,
        SQLiteProviderLedgerHost,
        _work,
    )

    private = tmp_path / "private"
    private.mkdir()
    restrict_private_tree(private)
    project_id = uuid4()
    host = SQLiteProviderLedgerHost(private / "provider.sqlite3", uuid4())
    calls = 0

    def invocation(prepared: PreparedProviderContractCall) -> OpenCodeRuntimeInvocation:
        nonlocal calls
        calls += 1
        work = _work(host.realm_id, project_id)
        if prepared.plan.call_id.startswith("opencode-embedding-document-"):
            host.register(work, document_call=prepared, profile=provider.describe())
        else:
            host.register(work)
        secret = SecretRef.create(
            realm_id=host.realm_id,
            name="opencode-litellm-embedding",
            provider=prepared.plan.provider_ref,
            purpose="embedding",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator="TEST_JOURNAL_KEY",
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
                secret_ref_ids=(secret.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=5),
        )
        return OpenCodeRuntimeInvocation(
            RuntimeProviderContractRunner(host=host, work=work, client=_RuntimeClient()),
            secret,
            authorization,
            "journal-test",
        )

    configuration = _configuration(tmp_path)
    provider = OpenCodeRemoteEmbeddingProvider(
        configuration,
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=3,
        document_journal=SQLiteDocumentEmbeddingJournal(host, project_id),
    )
    profile = provider.probe(_fixture()).profile
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    texts = ("internal project journal source", "active release journal source")
    first = provider.embed_documents(texts, policy)
    assert calls == 3
    restarted_host = SQLiteProviderLedgerHost(host.path, host.realm_id)
    restarted = OpenCodeRemoteEmbeddingProvider(
        configuration,
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=3,
        document_journal=SQLiteDocumentEmbeddingJournal(restarted_host, project_id),
    )
    restarted._profile = profile
    recovered = restarted.embed_documents(texts, policy)
    assert calls == 3
    assert recovered.vectors == first.vectors
    assert recovered.receipt.provider_call_count == 0
    assert recovered.receipt.output_digest == first.receipt.output_digest
    prepared = restarted._prepare(
        texts, purpose=EmbeddingPurpose.DOCUMENT, classification=policy.classification
    )
    assert SQLiteDocumentEmbeddingJournal(restarted_host, uuid4()).load(prepared, profile) is None
    reordered = restarted.embed_documents(tuple(reversed(texts)), policy)
    assert reordered.vectors == tuple(reversed(first.vectors))
    assert reordered.receipt.provider_call_count == 0
    single = restarted.recover_documents((texts[0],), policy)
    assert single is not None
    assert single.vectors == (first.vectors[0],)
    assert single.receipt.provider_call_count == 0
    assert restarted.recover_documents(("never completed input",), policy) is None
    assert calls == 3
    with restarted_host._connect() as db:
        stored = db.execute("select vectors_json from provider_document_vector_result").fetchone()[
            0
        ]
        assert all(text not in stored for text in texts)
        manifest = db.execute(
            "select input_digests_json from provider_document_input_manifest"
        ).fetchone()[0]
        assert all(text not in manifest for text in texts)
        db.execute("drop trigger provider_document_vector_result_no_update")
        db.execute(
            "update provider_document_vector_result set vectors_digest=?", (digest("tampered"),)
        )
    with pytest.raises(PolicyViolation, match="digest drift"):
        restarted.embed_documents(texts, policy)
    assert calls == 3


@pytest.mark.parametrize("purpose_name", ["query", "document"])
def test_zero_call_embedding_receipt_requires_document_reconciliation(purpose_name: str) -> None:
    from zekam.application.embedding_provider import EmbeddingReceipt

    values = {
        "purpose": EmbeddingPurpose(purpose_name),
        "profile_digest": digest("profile"),
        "input_digest": digest("input"),
        "output_digest": digest("output"),
        "vector_count": 1,
        "dimension": 3,
        "latency_ms": 0,
        "provider_call_count": 0,
    }
    with pytest.raises(ValidationFailed):
        EmbeddingReceipt(**values)
    values["reconciliation_evidence_digest"] = digest("durable-reconciliation")
    if purpose_name == "query":
        with pytest.raises(ValidationFailed):
            EmbeddingReceipt(**values)
    else:
        assert EmbeddingReceipt(**values).provider_call_count == 0


@pytest.mark.parametrize("outcome", ["receiptless", "unknown", "failed", "completed"])
def test_regrouped_document_claims_check_input_overlap_atomically(
    tmp_path: Path, outcome: str
) -> None:
    from zekam.domain.runtime import FailureCategory
    from zekam.infrastructure.local_file_security import restrict_private_tree
    from zekam.infrastructure.opencode_provider_ledger import SQLiteProviderLedgerHost, _work

    private = tmp_path / "private"
    private.mkdir()
    restrict_private_tree(private)
    host = SQLiteProviderLedgerHost(private / "provider.sqlite3", uuid4())
    project_id = uuid4()
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    profile = provider.probe(_fixture()).profile

    def prepared(texts):
        return provider._prepare(
            texts, purpose=EmbeddingPurpose.DOCUMENT, classification=DataClassification.INTERNAL
        )

    def claim(project, call):
        work = _work(host.realm_id, project)
        host.register(work, document_call=call, profile=profile)
        return host.claim_effect(
            work,
            operation=f"provider-contract:{call.plan.call_id}",
            effect_digest=call.plan.effect_request.effect_digest,
            authorization_digest=digest("fixture"),
            idempotency_key=digest(str(work.job.id)),
            resources=parse_requests(write=("provider:embedding",)),
            adapter_digest=digest("adapter"),
        )

    first = claim(project_id, prepared(("uncertain regroup input", "another input")))
    if outcome == "completed":
        host.record_success(
            first, result_digest=digest("result"), adapter_evidence_digest=digest("evidence")
        )
    elif outcome != "receiptless":
        host.record_failure(
            first,
            category=FailureCategory.UNKNOWN_EFFECT
            if outcome == "unknown"
            else FailureCategory.PROVIDER,
            failure_digest=digest("fixture-failure"),
        )
    regrouped = prepared(("new input", "uncertain regroup input"))
    before = host.summary()["claims"]
    if outcome == "failed":
        claim(project_id, regrouped)
        assert host.summary()["claims"] == before + 1
    else:
        with pytest.raises(PolicyViolation, match="uzlastirilmali"):
            claim(project_id, regrouped)
        assert host.summary()["claims"] == before
    claim(uuid4(), regrouped)
    claim(project_id, prepared(("disjoint input",)))


def test_document_manifest_rejects_payload_profile_drift_before_persistence(tmp_path: Path) -> None:
    from zekam.infrastructure.local_file_security import restrict_private_tree
    from zekam.infrastructure.opencode_provider_ledger import SQLiteProviderLedgerHost, _work

    private = tmp_path / "private"
    private.mkdir()
    restrict_private_tree(private)
    host = SQLiteProviderLedgerHost(private / "provider.sqlite3", uuid4())
    provider = OpenCodeRemoteEmbeddingProvider(_configuration(tmp_path), _Executor(), dimension=3)
    profile = provider.probe(_fixture()).profile
    call = provider._prepare(
        ("original input",),
        purpose=EmbeddingPurpose.DOCUMENT,
        classification=DataClassification.INTERNAL,
    )
    changed = replace(
        call,
        call=replace(
            call.call, payload={"model": profile.exact_model_id, "input": ["changed input"]}
        ),
    )
    with pytest.raises(PolicyViolation, match="drift"):
        host.register(_work(host.realm_id, uuid4()), document_call=changed, profile=profile)
    assert host.summary()["claims"] == 0
    with host._connect() as db:
        assert db.execute("select count(*) from provider_job").fetchone()[0] == 0
