"""Offline loopback Infinity adapter for the Mac local BGE-M3 runtime."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import platform
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
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
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.security import DataClassification

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REPEAT_DELTA = 1e-6
MAX_BATCH_DELTA = 5e-4
MIN_BATCH_COSINE = 0.99999
MIN_SEMANTIC_MARGIN = 0.05
_DEFAULT_ALLOWLIST = (
    DataClassification.PUBLIC,
    DataClassification.INTERNAL,
    DataClassification.CONFIDENTIAL,
    DataClassification.RESTRICTED,
    DataClassification.LOCAL_ONLY,
)


class InfinityJsonTransport(Protocol):
    def get(self, url: str, *, timeout_seconds: float) -> Any: ...

    def post(self, url: str, payload: bytes, *, timeout_seconds: float) -> Any: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _exact_json(payload: bytes) -> Any:
    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ConfigurationError("Infinity response duplicate JSON key tasiyor")
            document[key] = value
        return document

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=exact_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Infinity response strict JSON olmali") from exc


class LoopbackInfinityJsonTransport:
    """Bounded JSON transport that rejects redirects and non-loopback effects."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _read(self, request: urllib.request.Request, *, timeout_seconds: float) -> Any:
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    raise ConfigurationError("Infinity response status 200 olmali")
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
                    raise ConfigurationError("Infinity response oversized")
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise PolicyViolation("Infinity redirect yasak") from exc
            raise ConfigurationError("Infinity HTTP provider failure") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConfigurationError("Infinity local provider unavailable") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ConfigurationError("Infinity response oversized")
        return _exact_json(payload)

    def get(self, url: str, *, timeout_seconds: float) -> Any:
        request = urllib.request.Request(url, method="GET")
        return self._read(request, timeout_seconds=timeout_seconds)

    def post(self, url: str, payload: bytes, *, timeout_seconds: float) -> Any:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read(request, timeout_seconds=timeout_seconds)


@dataclass(frozen=True, slots=True)
class LocalBGEConfiguration:
    endpoint: str
    model_cache_root: Path
    exact_model_id: str = "BAAI/bge-m3"
    dimension: int = 1024
    timeout_seconds: float = 30.0
    max_batch_size: int = 8
    query_prefix: str = ""
    passage_prefix: str = ""
    compute_dtype: str = "float16"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port is None
        ):
            raise ConfigurationError("Local BGE endpoint exact IPv4 loopback olmali")
        if not self.model_cache_root.is_absolute():
            raise ConfigurationError("Local BGE model cache absolute olmali")
        if self.exact_model_id != "BAAI/bge-m3" or self.dimension != 1024:
            raise ConfigurationError("Local BGE exact model/dimension drift")
        if (
            not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or type(self.max_batch_size) is not int
            or not 1 <= self.max_batch_size <= 128
        ):
            raise ConfigurationError("Local BGE timeout/batch policy gecersiz")
        if self.compute_dtype not in {"float16", "float32"}:
            raise ConfigurationError("Local BGE compute dtype gecersiz")

    @property
    def base_url(self) -> str:
        return self.endpoint.rstrip("/")


@dataclass(frozen=True, slots=True)
class LocalBGEDiscovery:
    revision: str
    model_revision_fingerprint: str
    tokenizer_digest: str
    preprocessor_digest: str
    provider_identity_digest: str
    runtime_evidence_digest: str
    device_scope: str
    backend: str
    batch_size: int


def default_mac_bge_configuration() -> LocalBGEConfiguration:
    return LocalBGEConfiguration(
        endpoint="http://127.0.0.1:7997",
        model_cache_root=Path.home() / ".cache" / "huggingface" / "hub" / "models--BAAI--bge-m3",
    )


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _resolved_model_file(snapshot: Path, cache_root: Path, name: str) -> Path:
    candidate = snapshot / name
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(cache_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Local BGE {name} cache disinda/eksik") from exc
    if not resolved.is_file():
        raise ConfigurationError(f"Local BGE {name} regular file olmali")
    return resolved


def discover_local_bge(
    configuration: LocalBGEConfiguration,
    transport: InfinityJsonTransport,
) -> LocalBGEDiscovery:
    """Fingerprint the local model bytes and exact live Infinity capability."""

    configured_root = configuration.model_cache_root
    if configured_root.is_symlink():
        raise ConfigurationError("Local BGE cache root regular directory olmali")
    cache_root = configured_root.resolve(strict=True)
    if not cache_root.is_dir():
        raise ConfigurationError("Local BGE cache root regular directory olmali")
    revision_file = cache_root / "refs" / "main"
    if revision_file.is_symlink() or not revision_file.is_file():
        raise ConfigurationError("Local BGE refs/main regular file olmali")
    revision = revision_file.read_text(encoding="ascii").strip()
    if _REVISION.fullmatch(revision) is None:
        raise ConfigurationError("Local BGE revision exact commit olmali")
    snapshot = cache_root / "snapshots" / revision
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise ConfigurationError("Local BGE revision snapshot eksik")
    weight = _resolved_model_file(snapshot, cache_root, "pytorch_model.bin")
    tokenizer = _resolved_model_file(snapshot, cache_root, "tokenizer.json")
    sentencepiece = _resolved_model_file(snapshot, cache_root, "sentencepiece.bpe.model")
    config = _resolved_model_file(snapshot, cache_root, "config.json")
    weight_sha = _sha256_file(weight)
    if weight.name != weight_sha:
        raise ConfigurationError("Local BGE weight content-address digest drift")
    tokenizer_digest = digest(
        {
            "tokenizer_sha256": _sha256_file(tokenizer),
            "sentencepiece_sha256": _sha256_file(sentencepiece),
        }
    )
    preprocessor_digest = digest(
        {
            "config_sha256": _sha256_file(config),
            "query_prefix": configuration.query_prefix,
            "passage_prefix": configuration.passage_prefix,
            "normalize_embeddings": True,
        }
    )
    model_revision_fingerprint = digest(
        {
            "exact_model_id": configuration.exact_model_id,
            "revision": revision,
            "weight_sha256": weight_sha,
            "weight_size": weight.stat().st_size,
        }
    )
    catalog = transport.get(
        f"{configuration.base_url}/models", timeout_seconds=configuration.timeout_seconds
    )
    if not isinstance(catalog, dict) or set(catalog) != {"data", "object"}:
        raise ConfigurationError("Infinity model catalog exact object olmali")
    models = catalog.get("data")
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        raise ConfigurationError("Infinity exact tek model sunmali")
    model = models[0]
    if (
        model.get("id") != configuration.exact_model_id
        or model.get("object") != "model"
        or model.get("owned_by") != "infinity"
        or model.get("capabilities") != ["embed"]
        or model.get("backend") != "torch"
        or not isinstance(model.get("stats"), dict)
    ):
        raise ConfigurationError("Infinity model capability drift")
    batch_size = model["stats"].get("batch_size")
    if type(batch_size) is not int or batch_size < 1:
        raise ConfigurationError("Infinity runtime batch size gecersiz")
    device_scope = f"{platform.system().casefold()}-{platform.machine().casefold()}:mps"
    provider_identity_digest = digest(
        {
            "adapter": "infinity-loopback-bge/v1",
            "endpoint": configuration.base_url,
            "exact_model_id": configuration.exact_model_id,
            "model_revision_fingerprint": model_revision_fingerprint,
            "backend": model["backend"],
            "compute_dtype": configuration.compute_dtype,
            "device_scope": device_scope,
        }
    )
    runtime_evidence_digest = digest(
        {
            "catalog": catalog,
            "provider_identity_digest": provider_identity_digest,
        }
    )
    return LocalBGEDiscovery(
        revision,
        model_revision_fingerprint,
        tokenizer_digest,
        preprocessor_digest,
        provider_identity_digest,
        runtime_evidence_digest,
        device_scope,
        str(model["backend"]),
        batch_size,
    )


class LocalInfinityBGEProvider:
    """Verified local semantic provider; no synthetic vector fallback exists."""

    def __init__(
        self,
        configuration: LocalBGEConfiguration,
        discovery: LocalBGEDiscovery,
        transport: InfinityJsonTransport,
    ) -> None:
        self._configuration = configuration
        self._discovery = discovery
        self._transport = transport
        self._profile: EmbeddingProfile | None = None

    def describe(self) -> EmbeddingProfile:
        if self._profile is None:
            raise ConfigurationError(EmbeddingDegradedState.PROFILE_STALE.value)
        return self._profile

    def _vectors(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if (
            not isinstance(texts, tuple)
            or not texts
            or len(texts) > self._configuration.max_batch_size
            or any(
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > 256 * 1024
                for text in texts
            )
        ):
            raise ValidationFailed("Embedding batch exact bounded non-empty tuple olmali")
        payload = json.dumps(
            {"model": self._configuration.exact_model_id, "input": list(texts)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        document = self._transport.post(
            f"{self._configuration.base_url}/embeddings",
            payload,
            timeout_seconds=self._configuration.timeout_seconds,
        )
        if not isinstance(document, dict) or set(document) != {
            "object",
            "data",
            "model",
            "usage",
            "id",
            "created",
        }:
            raise ConfigurationError("Infinity embedding response exact object olmali")
        if document["object"] != "list" or document["model"] != self._configuration.exact_model_id:
            raise ConfigurationError("Infinity embedding response model drift")
        rows = document["data"]
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ConfigurationError("Infinity partial embedding batch reddedildi")
        vectors: list[tuple[float, ...]] = []
        for expected_index, row in enumerate(rows):
            if (
                not isinstance(row, dict)
                or set(row) != {"object", "embedding", "index"}
                or row["object"] != "embedding"
                or row["index"] != expected_index
                or not isinstance(row["embedding"], list)
            ):
                raise ConfigurationError("Infinity embedding row/index contract drift")
            if len(row["embedding"]) != self._configuration.dimension:
                raise ValidationFailed(EmbeddingDegradedState.DIMENSION_DRIFT.value)
            if any(
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in row["embedding"]
            ):
                raise ValidationFailed("Infinity embedding non-finite/wrong-type vector")
            vector = tuple(float(value) for value in row["embedding"])
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0:
                raise ValidationFailed("Infinity zero embedding vector")
            normalized = tuple(value / norm for value in vector)
            vectors.append(normalized)
        return tuple(vectors)

    @staticmethod
    def _max_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return max(abs(a - b) for a, b in zip(left, right, strict=True))

    @staticmethod
    def _score(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult:
        if fixture.classification not in _DEFAULT_ALLOWLIST:
            raise PolicyViolation("Local BGE probe classification izinli degil")
        started = time.monotonic_ns()
        batch = self._vectors(
            (
                f"{self._configuration.query_prefix}{fixture.query}",
                f"{self._configuration.query_prefix}{fixture.query}",
                f"{self._configuration.passage_prefix}{fixture.positive_passage}",
                f"{self._configuration.passage_prefix}{fixture.negative_passage}",
            )
        )
        single = self._vectors((f"{self._configuration.query_prefix}{fixture.query}",))[0]
        max_repeat_delta = self._max_delta(batch[0], batch[1])
        max_batch_delta = self._max_delta(batch[0], single)
        batch_cosine = self._score(batch[0], single)
        positive_score = self._score(batch[0], batch[2])
        negative_score = self._score(batch[0], batch[3])
        semantic_margin = positive_score - negative_score
        if (
            max_repeat_delta > MAX_REPEAT_DELTA
            or max_batch_delta > MAX_BATCH_DELTA
            or batch_cosine < MIN_BATCH_COSINE
        ):
            raise ValidationFailed("Local BGE determinism drift")
        if semantic_margin <= MIN_SEMANTIC_MARGIN:
            raise ValidationFailed("Local BGE semantic margin yetersiz")
        latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        evidence_body = {
            "schema": "zekam-local-bge-probe/v1",
            "provider_identity_digest": self._discovery.provider_identity_digest,
            "runtime_evidence_digest": self._discovery.runtime_evidence_digest,
            "model_revision_fingerprint": self._discovery.model_revision_fingerprint,
            "source_refs": list(fixture.source_refs),
            "source_digests": list(fixture.source_digests),
            "fixture_digest": digest(
                {
                    "query": fixture.query,
                    "positive": fixture.positive_passage,
                    "negative": fixture.negative_passage,
                }
            ),
            "dimension": self._configuration.dimension,
            "positive_score": positive_score,
            "negative_score": negative_score,
            "semantic_margin": semantic_margin,
            "max_repeat_delta": max_repeat_delta,
            "max_batch_delta": max_batch_delta,
            "batch_cosine": batch_cosine,
            "latency_ms": latency_ms,
            "provider_call_count": 2,
        }
        evidence_digest = digest(evidence_body)
        verified_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        profile_id = (
            "local-bge-m3-"
            f"{self._discovery.revision[:12]}-{self._discovery.device_scope.replace(':', '-')}"
        )
        profile = EmbeddingProfile(
            profile_id=profile_id,
            display_name="Mac local BGE-M3 via Infinity",
            provider_kind=EmbeddingProviderKind.LOCAL,
            provider_identity_digest=self._discovery.provider_identity_digest,
            exact_model_id=self._configuration.exact_model_id,
            model_revision_fingerprint=self._discovery.model_revision_fingerprint,
            dimension=self._configuration.dimension,
            vector_dtype="float32",
            normalized=True,
            distance_metric="cosine",
            query_prefix=self._configuration.query_prefix,
            passage_prefix=self._configuration.passage_prefix,
            preprocessor_digest=self._discovery.preprocessor_digest,
            tokenizer_digest=self._discovery.tokenizer_digest,
            batch_policy_digest=digest(
                {
                    "max_batch_size": self._configuration.max_batch_size,
                    "runtime_batch_size": self._discovery.batch_size,
                    "timeout_seconds": self._configuration.timeout_seconds,
                }
            ),
            device_scope=self._discovery.device_scope,
            data_classification_allowlist=_DEFAULT_ALLOWLIST,
            verified_at=verified_at,
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
        prefix: str,
    ) -> EmbeddingBatch:
        profile = self.describe()
        profile.assert_policy(policy)
        prepared = tuple(f"{prefix}{text}" for text in texts)
        started = time.monotonic_ns()
        vectors = self._vectors(prepared)
        latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        for vector in vectors:
            profile.validate_vector(vector)
        receipt = EmbeddingReceipt(
            purpose=purpose,
            profile_digest=profile.profile_digest,
            input_digest=digest({"texts": list(texts), "purpose": purpose.value}),
            output_digest=digest(vectors),
            vector_count=len(vectors),
            dimension=profile.dimension,
            latency_ms=latency_ms,
            provider_call_count=1,
        )
        return EmbeddingBatch(vectors, receipt)

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        return self._embed(texts, policy, EmbeddingPurpose.DOCUMENT, self.describe().passage_prefix)

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        return self._embed((text,), policy, EmbeddingPurpose.QUERY, self.describe().query_prefix)

    def health(self) -> EmbeddingHealth:
        try:
            document = self._transport.get(
                f"{self._configuration.base_url}/health",
                timeout_seconds=self._configuration.timeout_seconds,
            )
            if not isinstance(document, dict) or set(document) != {"unix"}:
                raise ConfigurationError("Infinity health response drift")
            if self._profile is None:
                return EmbeddingHealth(
                    False,
                    None,
                    EmbeddingDegradedState.PROFILE_STALE,
                    digest({"health": document, "profile": None}),
                )
            return EmbeddingHealth(
                True,
                self._profile.profile_digest,
                None,
                digest(
                    {
                        "health": document,
                        "profile_digest": self._profile.profile_digest,
                    }
                ),
            )
        except (ConfigurationError, PolicyViolation, ValidationFailed):
            return EmbeddingHealth(
                False,
                self._profile.profile_digest if self._profile is not None else None,
                EmbeddingDegradedState.UNAVAILABLE,
                digest(
                    {
                        "health": "unavailable",
                        "provider_identity": self._discovery.provider_identity_digest,
                    }
                ),
            )


def build_local_bge_provider(
    configuration: LocalBGEConfiguration,
    *,
    transport: InfinityJsonTransport | None = None,
) -> LocalInfinityBGEProvider:
    selected_transport = transport or LoopbackInfinityJsonTransport()
    discovery = discover_local_bge(configuration, selected_transport)
    return LocalInfinityBGEProvider(configuration, discovery, selected_transport)
