"""Receipt-bound OpenCode remote semantic embedding provider.

The provider never owns a credential or a raw HTTP transport.  Every request is
materialized as an exact ``PreparedProviderContractCall`` and must return a
durable effect claim/terminal receipt pair from an injected execution boundary.
"""

from __future__ import annotations

import datetime as dt
import math
import platform
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingDegradedState,
    EmbeddingHealth,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProbeResult,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.opencode_embedding import (
    OPENCODE_EMBEDDING_SECRET_REF_NAME,
    OpenCodeEmbeddingConfiguration,
)
from zekam.application.provider_adapter import (
    ProviderCall,
    ProviderCallResult,
    openai_embedding_payload,
    reviewed_endpoint_digest,
)
from zekam.application.provider_contract_execution import (
    PreparedProviderContractCall,
    ProviderCallPlan,
)
from zekam.application.provider_contract_runner import RuntimeProviderContractRunner
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import Modality
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim, EffectReceipt, ReceiptStatus
from zekam.domain.security import (
    NEVER_OUTBOUND,
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretRef,
)
from zekam.domain.work import EffectKind

MAX_BATCH_SIZE = 64
MAX_TEXT_BYTES = 256 * 1024
MAX_REPEAT_DELTA = 5e-4
MAX_BATCH_DELTA = 5e-4
MIN_REPEAT_COSINE = 0.99999
MIN_BATCH_COSINE = 0.99999
MIN_SEMANTIC_MARGIN = 0.05
_DEFAULT_ALLOWLIST = (DataClassification.PUBLIC, DataClassification.INTERNAL)


@dataclass(frozen=True, slots=True)
class OpenCodeEmbeddingExecution:
    """One authorized remote result with its durable effect settlement."""

    prepared: PreparedProviderContractCall
    claim: EffectClaim
    receipt: EffectReceipt
    provider_result: ProviderCallResult
    authorization: Authorization
    secret_ref: SecretRef
    persistence_proof_digest: str

    def assert_valid(self, expected: PreparedProviderContractCall) -> None:
        plan = expected.plan
        if (
            self.prepared.plan != plan
            or self.prepared.call.request_identity != expected.call.request_identity
            or self.prepared.call.payload_digest != expected.call.payload_digest
        ):
            raise PolicyViolation("OpenCode embedding executor prepared call drift")
        if self.claim.effect_digest != plan.effect_request.effect_digest:
            raise PolicyViolation("OpenCode embedding claim effect digest drift")
        if self.authorization.plan_digest != plan.authorization_plan_digest:
            raise PolicyViolation("OpenCode embedding authorization plan digest drift")
        if self.authorization.effect_digest != plan.effect_request.effect_digest:
            raise PolicyViolation("OpenCode embedding authorization effect digest drift")
        if self.secret_ref.id not in self.authorization.scope.secret_ref_ids:
            raise PolicyViolation("OpenCode embedding SecretRef authorization disinda")
        if (
            self.authorization.scope.body()
            != AuthorizationScope(
                allowed_resources=(plan.target, plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(plan.provider_ref,),
                secret_ref_ids=(self.secret_ref.id,),
                data_classifications=plan.data_classifications,
            ).body()
        ):
            raise PolicyViolation("OpenCode embedding authorization scope drift")
        if (
            self.claim.authorization_digest != self.authorization.authorization_digest
            or self.claim.operation != f"provider-contract:{plan.call_id}"
            or self.claim.resources != parse_requests(write=(plan.call_resource,))
            or self.claim.realm_id != self.authorization.realm_id
            or self.claim.realm_id != self.secret_ref.realm_id
            or self.claim.fencing_token < 1
        ):
            raise PolicyViolation("OpenCode embedding claim authorization binding drift")
        if self.receipt.status is not ReceiptStatus.COMPLETED:
            raise PolicyViolation("OpenCode embedding terminal success receipt ister")
        if self.receipt.claim_id != self.claim.id or self.receipt.realm_id != self.claim.realm_id:
            raise PolicyViolation("OpenCode embedding receipt/claim binding drift")
        if self.receipt.result_digest != self.provider_result.response_digest:
            raise PolicyViolation("OpenCode embedding receipt/response binding drift")
        if self.provider_result.authorization_id != self.authorization.id:
            raise PolicyViolation("OpenCode embedding provider authorization id drift")
        if self.provider_result.response_digest != digest(dict(self.provider_result.response)):
            raise PolicyViolation("OpenCode embedding response digest drift")
        expected_persistence = digest(
            {
                "claim_digest": self.claim.claim_digest,
                "receipt_id": str(self.receipt.id),
                "result_digest": self.provider_result.response_digest,
            }
        )
        if self.persistence_proof_digest != expected_persistence:
            raise PolicyViolation("OpenCode embedding persistence proof drift")


class OpenCodeEmbeddingExecutor(Protocol):
    """Claim-before-effect execution boundary (normally the runtime runner)."""

    def invoke(self, prepared: PreparedProviderContractCall) -> OpenCodeEmbeddingExecution: ...


@dataclass(frozen=True, slots=True)
class OpenCodeRuntimeInvocation:
    runner: RuntimeProviderContractRunner
    secret_ref: SecretRef
    authorization: Authorization
    consumed_by: str


class OpenCodeRuntimeInvocationFactory(Protocol):
    """Create one fresh job/authorization-bound runtime per exact provider call."""

    def __call__(self, prepared: PreparedProviderContractCall) -> OpenCodeRuntimeInvocation: ...


@dataclass(slots=True)
class RuntimeOpenCodeEmbeddingExecutor:
    """Production bridge with ledger readback after runner claim/effect/receipt."""

    invocation_factory: OpenCodeRuntimeInvocationFactory

    def invoke(self, prepared: PreparedProviderContractCall) -> OpenCodeEmbeddingExecution:
        invocation = self.invocation_factory(prepared)
        result = invocation.runner.invoke(
            prepared,
            secret_ref=invocation.secret_ref,
            authorization=invocation.authorization,
            consumed_by=invocation.consumed_by,
        )
        persisted_claim = next(
            (
                item
                for item in invocation.runner.host.ledger.claims_for_job(result.claim.job_id)
                if item.id == result.claim.id
            ),
            None,
        )
        persisted_receipt = invocation.runner.host.ledger.receipt_for_claim(result.claim.id)
        if persisted_claim != result.claim or persisted_receipt != result.receipt:
            raise PolicyViolation("OpenCode embedding durable ledger readback drift")
        execution = OpenCodeEmbeddingExecution(
            prepared=prepared,
            claim=result.claim,
            receipt=result.receipt,
            provider_result=result.provider_result,
            authorization=invocation.authorization,
            secret_ref=invocation.secret_ref,
            persistence_proof_digest=digest(
                {
                    "claim_digest": result.claim.claim_digest,
                    "receipt_id": str(result.receipt.id),
                    "result_digest": result.provider_result.response_digest,
                }
            ),
        )
        execution.assert_valid(prepared)
        return execution


class OpenCodeRemoteEmbeddingProvider:
    """OpenCode remote adapter implementing the production ``EmbeddingProvider`` port."""

    def __init__(
        self,
        configuration: OpenCodeEmbeddingConfiguration,
        executor: OpenCodeEmbeddingExecutor,
        *,
        dimension: int,
        max_batch_size: int = MAX_BATCH_SIZE,
        data_classification_allowlist: tuple[DataClassification, ...] = _DEFAULT_ALLOWLIST,
    ) -> None:
        if type(dimension) is not int or dimension < 1:
            raise ValidationFailed("OpenCode embedding dimension pozitif integer olmali")
        if type(max_batch_size) is not int or not 1 <= max_batch_size <= MAX_BATCH_SIZE:
            raise ValidationFailed("OpenCode embedding batch limiti gecersiz")
        if (
            not data_classification_allowlist
            or len(set(data_classification_allowlist)) != len(data_classification_allowlist)
            or any(item in NEVER_OUTBOUND for item in data_classification_allowlist)
        ):
            raise ValidationFailed("OpenCode embedding remote classification allowlist gecersiz")
        self._configuration = configuration
        self._executor = executor
        self._dimension = dimension
        self._max_batch_size = max_batch_size
        self._allowlist = data_classification_allowlist
        self._profile: EmbeddingProfile | None = None

    def describe(self) -> EmbeddingProfile:
        if self._profile is None:
            raise ConfigurationError(EmbeddingDegradedState.PROFILE_STALE.value)
        return self._profile

    def _prepare(
        self,
        texts: tuple[str, ...],
        *,
        purpose: EmbeddingPurpose,
        classification: DataClassification,
    ) -> PreparedProviderContractCall:
        if (
            not isinstance(texts, tuple)
            or not texts
            or len(texts) > self._max_batch_size
            or any(
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > MAX_TEXT_BYTES
                for text in texts
            )
        ):
            raise ValidationFailed("OpenCode embedding batch exact bounded tuple olmali")
        if classification not in self._allowlist:
            raise PolicyViolation("OpenCode embedding classification provider disinda")
        payload = openai_embedding_payload(self._configuration.selected_model_id, texts)
        fixture_digest = digest(
            {
                "purpose": purpose.value,
                "classification": classification.value,
                "texts": list(texts),
            }
        )
        endpoint_path = urlsplit(self._configuration.embedding_endpoint).path
        call_id = (
            f"opencode-embedding-{purpose.value}-{fixture_digest.removeprefix('sha256:')[:20]}"
        )
        plan = ProviderCallPlan(
            call_id=call_id,
            modality=Modality.EMBEDDING,
            model_id=self._configuration.canonical_model_id,
            provider_ref=self._configuration.provider_id,
            endpoint_ref=f"opencode:{self._configuration.provider_id}:embeddings",
            operation="embeddings",
            secret_ref_name=OPENCODE_EMBEDDING_SECRET_REF_NAME,
            request_format="json",
            fixture_digest=fixture_digest,
            payload_digest=digest(dict(payload)),
            endpoint_binding_digest=reviewed_endpoint_digest(
                self._configuration.embedding_endpoint, path_hint=endpoint_path
            ),
            endpoint_path_hint=endpoint_path,
            data_classifications=(classification,),
        )
        call = ProviderCall(
            provider_ref=plan.provider_ref,
            endpoint_ref=plan.endpoint_ref,
            operation=plan.operation,
            request_identity=plan.call_id,
            payload=payload,
            data_categories=plan.data_classifications,
            retention_assumption="explicit-project-disclosure",
            region="configured-provider",
            endpoint_path_hint=plan.endpoint_path_hint,
            endpoint_binding_digest=plan.endpoint_binding_digest,
            authorization_plan_digest=plan.authorization_plan_digest,
            authorization_resource=plan.call_resource,
        )
        return PreparedProviderContractCall(plan, call)

    def _vectors(
        self,
        texts: tuple[str, ...],
        *,
        purpose: EmbeddingPurpose,
        classification: DataClassification,
    ) -> tuple[tuple[tuple[float, ...], ...], OpenCodeEmbeddingExecution]:
        prepared = self._prepare(texts, purpose=purpose, classification=classification)
        execution = self._executor.invoke(prepared)
        execution.assert_valid(prepared)
        rows = execution.provider_result.response.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValidationFailed("OpenCode embedding partial batch reddedildi")
        indexed: dict[int, tuple[float, ...]] = {}
        for position, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
                raise ValidationFailed("OpenCode embedding response vector sekli gecersiz")
            index = row.get("index", position)
            if type(index) is not int or index in indexed or not 0 <= index < len(rows):
                raise ValidationFailed("OpenCode embedding response indexleri exact olmali")
            raw_vector = row["embedding"]
            if len(raw_vector) != self._dimension or any(
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in raw_vector
            ):
                raise ValidationFailed(EmbeddingDegradedState.DIMENSION_DRIFT.value)
            vector = tuple(float(value) for value in raw_vector)
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0.0:
                raise ValidationFailed("OpenCode embedding zero vector reddedildi")
            indexed[index] = tuple(value / norm for value in vector)
        if set(indexed) != set(range(len(rows))):
            raise ValidationFailed("OpenCode embedding response indexleri tam olmali")
        return tuple(indexed[index] for index in range(len(rows))), execution

    @staticmethod
    def _max_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return max(abs(a - b) for a, b in zip(left, right, strict=True))

    @staticmethod
    def _score(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult:
        if fixture.classification is not DataClassification.PUBLIC:
            raise PolicyViolation("OpenCode embedding probe public fixture ister")
        started = time.monotonic_ns()
        batch, batch_execution = self._vectors(
            (fixture.query, fixture.query, fixture.positive_passage, fixture.negative_passage),
            purpose=EmbeddingPurpose.PROBE,
            classification=fixture.classification,
        )
        single_rows, single_execution = self._vectors(
            (fixture.query,),
            purpose=EmbeddingPurpose.PROBE,
            classification=fixture.classification,
        )
        max_repeat_delta = self._max_delta(batch[0], batch[1])
        max_batch_delta = self._max_delta(batch[0], single_rows[0])
        repeat_cosine = self._score(batch[0], batch[1])
        batch_cosine = self._score(batch[0], single_rows[0])
        positive_score = self._score(batch[0], batch[2])
        negative_score = self._score(batch[0], batch[3])
        semantic_margin = positive_score - negative_score
        if (
            max_repeat_delta > MAX_REPEAT_DELTA
            or max_batch_delta > MAX_BATCH_DELTA
            or repeat_cosine < MIN_REPEAT_COSINE
            or batch_cosine < MIN_BATCH_COSINE
        ):
            raise ValidationFailed("OpenCode embedding determinism drift")
        if semantic_margin <= MIN_SEMANTIC_MARGIN:
            raise ValidationFailed("OpenCode embedding semantic margin yetersiz")
        latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        response_fingerprint = digest(
            {
                "batch": batch_execution.provider_result.response_digest,
                "single": single_execution.provider_result.response_digest,
                "dimension": self._dimension,
            }
        )
        evidence_body = {
            "schema": "zekam-opencode-remote-embedding-probe/v1",
            "provider_identity_digest": self._configuration.endpoint_identity.identity_digest,
            "exact_model_id": self._configuration.selected_model_id,
            "canonical_model_id": self._configuration.canonical_model_id,
            "public_probe_fingerprint": response_fingerprint,
            "source_refs": list(fixture.source_refs),
            "source_digests": list(fixture.source_digests),
            "dimension": self._dimension,
            "semantic_margin": semantic_margin,
            "max_repeat_delta": max_repeat_delta,
            "max_batch_delta": max_batch_delta,
            "repeat_cosine": repeat_cosine,
            "batch_cosine": batch_cosine,
            "claim_digests": [
                batch_execution.claim.claim_digest,
                single_execution.claim.claim_digest,
            ],
            "receipt_ids": [
                str(batch_execution.receipt.id),
                str(single_execution.receipt.id),
            ],
            "provider_call_count": 2,
        }
        evidence_digest = digest(evidence_body)
        device_scope = f"{platform.system().casefold()}-{platform.machine().casefold()}:opencode"
        profile = EmbeddingProfile(
            profile_id=(
                "opencode-remote-"
                f"{self._configuration.canonical_model_id[:12]}-{response_fingerprint[7:19]}"
            ),
            display_name="Windows OpenCode remote embedding",
            provider_kind=EmbeddingProviderKind.REMOTE,
            provider_identity_digest=digest(
                {
                    "provider_id": self._configuration.provider_id,
                    "endpoint_identity_digest": (
                        self._configuration.endpoint_identity.identity_digest
                    ),
                }
            ),
            exact_model_id=self._configuration.selected_model_id,
            model_revision_fingerprint=response_fingerprint,
            dimension=self._dimension,
            vector_dtype="float32",
            normalized=True,
            distance_metric="cosine",
            query_prefix="",
            passage_prefix="",
            preprocessor_digest=digest({"provider-managed": True, "prefixes": "none"}),
            tokenizer_digest=digest({"provider-managed": True, "tokenizer": "undisclosed"}),
            batch_policy_digest=digest({"max_batch_size": self._max_batch_size}),
            device_scope=device_scope,
            data_classification_allowlist=self._allowlist,
            verified_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            probe_evidence_digest=evidence_digest,
        )
        for vector in batch:
            profile.validate_vector(vector)
        self._profile = profile
        return EmbeddingProbeResult(
            profile,
            semantic_margin,
            positive_score,
            negative_score,
            max_repeat_delta,
            max_batch_delta,
            batch_cosine,
            latency_ms,
            evidence_digest,
            2,
        )

    def _embed(
        self,
        texts: tuple[str, ...],
        policy: EmbeddingPolicy,
        purpose: EmbeddingPurpose,
    ) -> EmbeddingBatch:
        profile = self.describe()
        profile.assert_policy(policy)
        started = time.monotonic_ns()
        vectors, _ = self._vectors(
            texts,
            purpose=purpose,
            classification=policy.classification,
        )
        latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        for vector in vectors:
            profile.validate_vector(vector)
        return EmbeddingBatch(
            vectors,
            EmbeddingReceipt(
                purpose=purpose,
                profile_digest=profile.profile_digest,
                input_digest=digest({"texts": list(texts), "purpose": purpose.value}),
                output_digest=digest(vectors),
                vector_count=len(vectors),
                dimension=profile.dimension,
                latency_ms=latency_ms,
                provider_call_count=1,
            ),
        )

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        return self._embed(texts, policy, EmbeddingPurpose.DOCUMENT)

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        return self._embed((text,), policy, EmbeddingPurpose.QUERY)

    def health(self) -> EmbeddingHealth:
        if self._profile is None:
            return EmbeddingHealth(
                False,
                None,
                EmbeddingDegradedState.PROFILE_STALE,
                digest({"health": "profile-stale"}),
            )
        return EmbeddingHealth(
            True,
            self._profile.profile_digest,
            None,
            digest(
                {
                    "health": "probe-qualified",
                    "profile_digest": self._profile.profile_digest,
                }
            ),
        )
