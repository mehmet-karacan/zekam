"""Local multilingual E5 embedding provider backed by ONNX Runtime CPU."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import math
import os
import platform
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

MODEL_REPOSITORY = "Xenova/multilingual-e5-small"
EXACT_MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_FILE = "onnx/model_int8.onnx"
EXPECTED_MODEL_SHA256 = "4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c"
EMBEDDING_DIMENSION = 384
MAX_SEQUENCE_LENGTH = 512
MAX_BATCH_SIZE = 4
DEFAULT_INTRA_OP_THREADS = 2
DEFAULT_INTER_OP_THREADS = 1
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
_MAX_TEXT_BYTES = 256 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_REPEAT_DELTA = 1e-6
# INT8 kernels can vary slightly with the ONNX batch dimension even when the
# attention mask and sequence width are identical.  Keep the acceptance gate
# tight enough to catch real drift while allowing that bounded quantization
# variation.
_MAX_BATCH_DELTA = 0.02
_MIN_BATCH_COSINE = 0.995
_MIN_SEMANTIC_MARGIN = 0.01
_DEFAULT_ALLOWLIST = (
    DataClassification.PUBLIC,
    DataClassification.INTERNAL,
    DataClassification.CONFIDENTIAL,
    DataClassification.RESTRICTED,
    DataClassification.LOCAL_ONLY,
)


@dataclass(frozen=True, slots=True)
class LocalE5Configuration:
    """Pinned local E5 model and bounded CPU execution policy."""

    model_root: Path
    exact_model_id: str = EXACT_MODEL_ID
    revision: str = MODEL_REVISION
    dimension: int = EMBEDDING_DIMENSION
    max_sequence_length: int = MAX_SEQUENCE_LENGTH
    max_batch_size: int = MAX_BATCH_SIZE
    intra_op_num_threads: int = DEFAULT_INTRA_OP_THREADS
    inter_op_num_threads: int = DEFAULT_INTER_OP_THREADS
    query_prefix: str = QUERY_PREFIX
    passage_prefix: str = PASSAGE_PREFIX

    def __post_init__(self) -> None:
        if not isinstance(self.model_root, Path) or not self.model_root.is_absolute():
            raise ConfigurationError("Local E5 model root absolute olmali")
        if self.exact_model_id != EXACT_MODEL_ID or self.revision != MODEL_REVISION:
            raise ConfigurationError("Local E5 model kimligi sabit olmali")
        if self.dimension != EMBEDDING_DIMENSION:
            raise ConfigurationError("Local E5 dimension 384 olmali")
        if self.max_sequence_length != MAX_SEQUENCE_LENGTH:
            raise ConfigurationError("Local E5 sequence length 512 olmali")
        if type(self.max_batch_size) is not int or not 1 <= self.max_batch_size <= 32:
            raise ConfigurationError("Local E5 batch siniri gecersiz")
        if (
            type(self.intra_op_num_threads) is not int
            or self.intra_op_num_threads < 1
            or type(self.inter_op_num_threads) is not int
            or self.inter_op_num_threads < 1
        ):
            raise ConfigurationError("Local E5 CPU thread siniri gecersiz")
        if self.query_prefix != QUERY_PREFIX or self.passage_prefix != PASSAGE_PREFIX:
            raise ConfigurationError("Local E5 prefix sozlesmesi drift etti")

    @property
    def model_path(self) -> Path:
        return self.model_root / MODEL_FILE

    @property
    def manifest_path(self) -> Path:
        return self.model_root / "install-manifest.json"


def default_local_e5_configuration() -> LocalE5Configuration:
    """Resolve the installed user-scoped model without changing system settings."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "Zekam" / "models" / MODEL_REPOSITORY.replace("/", "-")
    else:
        root = (
            Path.home()
            / ".local"
            / "share"
            / "Zekam"
            / "models"
            / MODEL_REPOSITORY.replace("/", "-")
        )
    return LocalE5Configuration(model_root=root / MODEL_REVISION)


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _private_regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not _is_link_or_reparse(metadata)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & 0x400
    )


def _read_manifest(path: Path, configuration: LocalE5Configuration) -> dict[str, Any]:
    if not _private_regular(path) or path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ConfigurationError("Local E5 install manifest regular/bounded olmali")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Local E5 install manifest okunamadi") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != "zekam-local-embedding-install/v1"
        or document.get("repository") != MODEL_REPOSITORY
        or document.get("revision") != configuration.revision
        or document.get("model_ref") != configuration.exact_model_id
        or document.get("model_file") != MODEL_FILE
        or not isinstance(document.get("files"), list)
    ):
        raise ConfigurationError("Local E5 install manifest kimlik drift etti")
    return document


def _manifest_file_record(manifest: dict[str, Any], relative: str) -> dict[str, Any]:
    for item in manifest["files"]:
        if isinstance(item, dict) and item.get("path") == relative:
            return item
    raise ConfigurationError(f"Local E5 manifest dosyasi eksik: {relative}")


def _assert_model_files(configuration: LocalE5Configuration) -> tuple[dict[str, Any], str, str]:
    root = configuration.model_root
    try:
        root.lstat()
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Local E5 model root bulunamadi") from exc
    if not root.is_dir() or _is_link_or_reparse(root.lstat()):
        raise ConfigurationError("Local E5 model root regular dizin olmali")
    manifest = _read_manifest(configuration.manifest_path, configuration)
    model_path = configuration.model_path
    tokenizer_path = root / "tokenizer.json"
    if not _private_regular(model_path) or not _private_regular(tokenizer_path):
        raise ConfigurationError("Local E5 model/tokenizer regular dosya olmali")
    for path in (model_path, tokenizer_path):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ConfigurationError("Local E5 model yolu root disinda") from exc
    model_sha = _sha256_file(model_path)
    model_record = _manifest_file_record(manifest, MODEL_FILE)
    if (
        model_sha != EXPECTED_MODEL_SHA256
        or model_record.get("sha256") != EXPECTED_MODEL_SHA256
        or model_record.get("upstream_sha256") != EXPECTED_MODEL_SHA256
        or model_record.get("size_bytes") != model_path.stat().st_size
    ):
        raise ConfigurationError("Local E5 model SHA256/size dogrulamasi basarisiz")
    tokenizer_sha = _sha256_file(tokenizer_path)
    tokenizer_record = _manifest_file_record(manifest, "tokenizer.json")
    if tokenizer_record.get("sha256") != tokenizer_sha:
        raise ConfigurationError("Local E5 tokenizer SHA256 drift etti")
    return manifest, model_sha, tokenizer_sha


def _load_runtime(model_path: Path, configuration: LocalE5Configuration) -> tuple[Any, Any, str]:
    try:
        numpy = importlib.import_module("numpy")
        onnxruntime = importlib.import_module("onnxruntime")
        tokenizers = importlib.import_module("tokenizers")
    except ImportError as exc:
        raise ConfigurationError(
            "Local E5 icin onnxruntime, tokenizers ve numpy kurulumu gerekli"
        ) from exc
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = configuration.intra_op_num_threads
    options.inter_op_num_threads = configuration.inter_op_num_threads
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = onnxruntime.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    if session.get_providers() != ["CPUExecutionProvider"]:
        raise ConfigurationError("Local E5 yalniz CPUExecutionProvider kullanmali")
    inputs = {item.name for item in session.get_inputs()}
    if inputs != {"input_ids", "attention_mask", "token_type_ids"}:
        raise ConfigurationError("Local E5 ONNX input sozlesmesi drift etti")
    tokenizer = tokenizers.Tokenizer.from_file(str(model_path.parent.parent / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=configuration.max_sequence_length)
    return session, (tokenizer, numpy), onnxruntime.__version__


class LocalOnnxE5Provider:
    """Verified local multilingual E5 provider; no remote fallback exists."""

    def __init__(self, configuration: LocalE5Configuration) -> None:
        self._configuration = configuration
        manifest, model_sha, tokenizer_sha = _assert_model_files(configuration)
        self._manifest = manifest
        self._model_sha = model_sha
        self._tokenizer_sha = tokenizer_sha
        self._session, runtime_parts, self._runtime_version = _load_runtime(
            configuration.model_path, configuration
        )
        self._tokenizer, self._numpy = runtime_parts
        self._profile: EmbeddingProfile | None = None
        self._profile_identity = digest(
            {
                "adapter": "onnxruntime-cpu-e5/v1",
                "repository": MODEL_REPOSITORY,
                "exact_model_id": configuration.exact_model_id,
                "revision": configuration.revision,
                "model_sha256": model_sha,
                "provider": "CPUExecutionProvider",
                "intra_op_num_threads": configuration.intra_op_num_threads,
                "inter_op_num_threads": configuration.inter_op_num_threads,
            }
        )

    @property
    def runtime_version(self) -> str:
        return self._runtime_version

    def describe(self) -> EmbeddingProfile:
        if self._profile is None:
            raise ConfigurationError(EmbeddingDegradedState.PROFILE_STALE.value)
        return self._profile

    def _tokenized(self, texts: tuple[str, ...]) -> dict[str, Any]:
        if (
            not texts
            or len(texts) > self._configuration.max_batch_size
            or any(
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > _MAX_TEXT_BYTES
                for text in texts
            )
        ):
            raise ValidationFailed("Local E5 batch bounded non-empty tuple olmali")
        encoded = self._tokenizer.encode_batch(list(texts))
        # Keep padding bounded to the longest item in this batch.  The INT8
        # graph's small batch-shape drift is covered by the probe tolerance
        # above; fixed 512-token padding would make short CPU queries needlessly
        # expensive.
        width = max(len(item.ids) for item in encoded)
        ids = self._numpy.zeros((len(encoded), width), dtype=self._numpy.int64)
        masks = self._numpy.zeros((len(encoded), width), dtype=self._numpy.int64)
        types = self._numpy.zeros((len(encoded), width), dtype=self._numpy.int64)
        for row, item in enumerate(encoded):
            length = len(item.ids)
            ids[row, :length] = item.ids
            masks[row, :length] = item.attention_mask
            types[row, :length] = item.type_ids
        return {"input_ids": ids, "attention_mask": masks, "token_type_ids": types}

    def _vectors(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        inputs = self._tokenized(texts)
        output = self._session.run(["last_hidden_state"], inputs)[0]
        if output.ndim != 3 or output.shape[2] != self._configuration.dimension:
            raise ValidationFailed(EmbeddingDegradedState.DIMENSION_DRIFT.value)
        mask = inputs["attention_mask"].astype(self._numpy.float32)[..., None]
        pooled = (output * mask).sum(axis=1) / self._numpy.maximum(mask.sum(axis=1), 1e-9)
        norms = self._numpy.linalg.norm(pooled, axis=1, keepdims=True)
        normalized = pooled / self._numpy.maximum(norms, 1e-12)
        if not self._numpy.isfinite(normalized).all():
            raise ValidationFailed("Local E5 non-finite vector")
        vectors = tuple(tuple(float(value) for value in row) for row in normalized)
        for vector in vectors:
            if len(vector) != self._configuration.dimension:
                raise ValidationFailed(EmbeddingDegradedState.DIMENSION_DRIFT.value)
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
                raise ValidationFailed("Local E5 vector normalize olmali")
        return vectors

    @staticmethod
    def _score(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    @staticmethod
    def _max_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return max(abs(a - b) for a, b in zip(left, right, strict=True))

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult:
        if fixture.classification not in _DEFAULT_ALLOWLIST:
            raise PolicyViolation("Local E5 probe classification izinli degil")
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
            max_repeat_delta > _MAX_REPEAT_DELTA
            or max_batch_delta > _MAX_BATCH_DELTA
            or batch_cosine < _MIN_BATCH_COSINE
        ):
            raise ValidationFailed("Local E5 determinism drift")
        if semantic_margin <= _MIN_SEMANTIC_MARGIN:
            raise ValidationFailed("Local E5 semantic margin yetersiz")
        evidence = {
            "schema": "zekam-local-onnx-e5-probe/v1",
            "provider_identity_digest": self._profile_identity,
            "model_revision": self._configuration.revision,
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
            "latency_ms": max(0, (time.monotonic_ns() - started) // 1_000_000),
            "provider_call_count": 0,
        }
        evidence_digest = digest(evidence)
        verified_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self._profile = EmbeddingProfile(
            profile_id=f"local-multilingual-e5-small-{self._configuration.revision[:12]}-cpu",
            display_name="Windows yerel multilingual-e5-small via ONNX Runtime CPU",
            provider_kind=EmbeddingProviderKind.LOCAL,
            provider_identity_digest=self._profile_identity,
            exact_model_id=self._configuration.exact_model_id,
            model_revision_fingerprint=digest(
                {
                    "repository": MODEL_REPOSITORY,
                    "revision": self._configuration.revision,
                    "model_sha256": self._model_sha,
                    "dimension": self._configuration.dimension,
                }
            ),
            dimension=self._configuration.dimension,
            vector_dtype="float32",
            normalized=True,
            distance_metric="cosine",
            query_prefix=self._configuration.query_prefix,
            passage_prefix=self._configuration.passage_prefix,
            preprocessor_digest=digest(
                {
                    "config_sha256": _manifest_file_record(self._manifest, "config.json").get(
                        "sha256"
                    ),
                    "query_prefix": self._configuration.query_prefix,
                    "passage_prefix": self._configuration.passage_prefix,
                    "pooling": "attention-mask-aware-average",
                    "normalize_embeddings": True,
                }
            ),
            tokenizer_digest=digest(
                {
                    "tokenizer_sha256": self._tokenizer_sha,
                    "sentencepiece_sha256": _manifest_file_record(
                        self._manifest, "sentencepiece.bpe.model"
                    ).get("sha256"),
                }
            ),
            batch_policy_digest=digest(
                {
                    "max_batch_size": self._configuration.max_batch_size,
                    "max_sequence_length": self._configuration.max_sequence_length,
                    "intra_op_num_threads": self._configuration.intra_op_num_threads,
                    "inter_op_num_threads": self._configuration.inter_op_num_threads,
                }
            ),
            device_scope=f"{platform.system().casefold()}-{platform.machine().casefold()}:cpu",
            data_classification_allowlist=_DEFAULT_ALLOWLIST,
            verified_at=verified_at,
            probe_evidence_digest=evidence_digest,
        )
        for vector in batch:
            self._profile.validate_vector(vector)
        return EmbeddingProbeResult(
            self._profile,
            semantic_margin,
            positive_score,
            negative_score,
            max_repeat_delta,
            max_batch_delta,
            batch_cosine,
            int(evidence["latency_ms"]),
            evidence_digest,
            0,
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
        return self._embed(texts, policy, EmbeddingPurpose.DOCUMENT, PASSAGE_PREFIX)

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        return self._embed((text,), policy, EmbeddingPurpose.QUERY, QUERY_PREFIX)

    def health(self) -> EmbeddingHealth:
        try:
            if self._session.get_providers() != ["CPUExecutionProvider"]:
                raise ConfigurationError("Local E5 CPU provider unavailable")
            if self._profile is None:
                return EmbeddingHealth(
                    False,
                    None,
                    EmbeddingDegradedState.PROFILE_STALE,
                    digest({"provider_identity_digest": self._profile_identity, "profile": None}),
                )
            return EmbeddingHealth(
                True,
                self._profile.profile_digest,
                None,
                digest(
                    {
                        "provider_identity_digest": self._profile_identity,
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
                    {"provider_identity_digest": self._profile_identity, "health": "unavailable"}
                ),
            )


def build_local_e5_provider(
    configuration: LocalE5Configuration | None = None,
) -> LocalOnnxE5Provider:
    """Build a pinned, local-only E5 provider."""

    return LocalOnnxE5Provider(configuration or default_local_e5_configuration())
