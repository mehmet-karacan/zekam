"""Local Infinity BGE provider contract and failure-boundary tests."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.embedding_provider import (
    EmbeddingDegradedState,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProviderKind,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding.infinity_bge import (
    LocalBGEConfiguration,
    build_local_bge_provider,
)

pytestmark = pytest.mark.unit

REVISION = "1" * 40


class FakeInfinityTransport:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.post_calls = 0

    def get(self, url: str, *, timeout_seconds: float) -> Any:
        del timeout_seconds
        if url.endswith("/health"):
            if self.mode == "unavailable":
                raise ConfigurationError("offline")
            return {"unix": 1.0}
        return {
            "data": [
                {
                    "id": "BAAI/bge-m3",
                    "stats": {
                        "queue_fraction": 0.0,
                        "queue_absolute": 0,
                        "results_pending": 0,
                        "batch_size": 8,
                    },
                    "object": "model",
                    "owned_by": "infinity",
                    "created": 1,
                    "backend": "torch",
                    "capabilities": ["embed"],
                }
            ],
            "object": "list",
        }

    def post(self, url: str, payload: bytes, *, timeout_seconds: float) -> Any:
        del url, timeout_seconds
        self.post_calls += 1
        if self.mode == "timeout":
            raise ConfigurationError("timeout")
        import json

        texts = json.loads(payload)["input"]
        rows = []
        for index, text in enumerate(texts):
            dimension = 3 if self.mode == "wrong-dimension" else 1024
            vector = [0.0] * dimension
            vector[0 if "negative" not in text else 1] = 1.0
            if self.mode == "non-finite":
                vector[0] = float("nan")
            rows.append({"object": "embedding", "embedding": vector, "index": index})
        if self.mode == "partial":
            rows.pop()
        if self.mode == "wrong-index" and rows:
            rows[0]["index"] = 7
        return {
            "object": "list",
            "data": rows,
            "model": "wrong/model" if self.mode == "wrong-model" else "BAAI/bge-m3",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
            "id": "test-response",
            "created": 1,
        }


def _cache(tmp_path: Path) -> Path:
    root = tmp_path / "models--BAAI--bge-m3"
    (root / "refs").mkdir(parents=True)
    (root / "refs" / "main").write_text(REVISION, encoding="ascii")
    snapshot = root / "snapshots" / REVISION
    blobs = root / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir()
    weight_payload = b"unit-test-bge-weights"
    weight_digest = hashlib.sha256(weight_payload).hexdigest()
    weight = blobs / weight_digest
    weight.write_bytes(weight_payload)
    (snapshot / "pytorch_model.bin").symlink_to(weight)
    (snapshot / "tokenizer.json").write_bytes(b"{}")
    (snapshot / "sentencepiece.bpe.model").write_bytes(b"sentencepiece")
    (snapshot / "config.json").write_bytes(b"{}")
    return root


def _fixture() -> EmbeddingProbeFixture:
    return EmbeddingProbeFixture(
        query="idempotent import",
        positive_passage="positive idempotent import",
        negative_passage="negative decimal arithmetic",
        source_refs=("adr-0006.md", "adr-0005.md"),
        source_digests=(digest_of_bytes(b"positive"), digest_of_bytes(b"negative")),
        classification=DataClassification.PUBLIC,
    )


def _provider(tmp_path: Path, transport: FakeInfinityTransport) -> Any:
    configuration = LocalBGEConfiguration(
        endpoint="http://127.0.0.1:7997", model_cache_root=_cache(tmp_path)
    )
    return build_local_bge_provider(configuration, transport=transport)


def test_probe_profile_batch_query_policy_and_health(tmp_path: Path) -> None:
    transport = FakeInfinityTransport()
    provider = _provider(tmp_path, transport)
    with pytest.raises(ConfigurationError, match=EmbeddingDegradedState.PROFILE_STALE.value):
        provider.describe()
    assert provider.health().degraded_state is EmbeddingDegradedState.PROFILE_STALE

    probe = provider.probe(_fixture())
    profile = provider.describe()
    policy = EmbeddingPolicy(DataClassification.INTERNAL, profile.profile_digest)
    documents = provider.embed_documents(("positive document", "negative document"), policy)
    query = provider.embed_query("positive query", policy)

    assert probe.semantic_margin == pytest.approx(1.0)
    assert profile.dimension == 1024
    assert profile.exact_model_id == "BAAI/bge-m3"
    assert profile.provider_identity_digest not in profile.profile_id
    assert documents.receipt.vector_count == 2
    assert query.receipt.vector_count == 1
    assert all(
        math.isclose(sum(value * value for value in vector), 1.0)
        for vector in (*documents.vectors, *query.vectors)
    )
    assert provider.health().healthy is True
    assert transport.post_calls == 4

    with pytest.raises(PolicyViolation, match="classification"):
        provider.embed_query(
            "secret",
            EmbeddingPolicy(DataClassification.SECRET, profile.profile_digest),
        )
    with pytest.raises(PolicyViolation, match="profile digest drift"):
        provider.embed_query(
            "drift",
            EmbeddingPolicy(DataClassification.PUBLIC, digest("wrong-profile")),
        )


def test_profile_identity_is_restart_stable_but_cross_device_and_remote_namespaced(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path, FakeInfinityTransport())
    first = provider.probe(_fixture()).profile
    second = provider.probe(_fixture()).profile
    assert first.profile_digest == second.profile_digest

    other_device = replace(first, device_scope="windows-amd64:remote")
    assert other_device.profile_digest != first.profile_digest

    remote = replace(
        first,
        profile_id="opencode-remote-bge-m3",
        provider_kind=EmbeddingProviderKind.REMOTE,
        provider_identity_digest=digest("remote-provider"),
        device_scope="windows-amd64:opencode-remote",
        data_classification_allowlist=(DataClassification.PUBLIC,),
    )
    assert remote.exact_model_id == first.exact_model_id
    assert remote.profile_digest not in {first.profile_digest, other_device.profile_digest}
    with pytest.raises(PolicyViolation, match="remote-disclosure-not-authorized"):
        remote.assert_policy(EmbeddingPolicy(DataClassification.PUBLIC, remote.profile_digest))
    remote.assert_policy(
        EmbeddingPolicy(
            DataClassification.PUBLIC,
            remote.profile_digest,
            remote_disclosure_authorized=True,
        )
    )
    with pytest.raises(ValidationFailed, match="never-outbound"):
        replace(
            remote,
            data_classification_allowlist=(DataClassification.PUBLIC, DataClassification.SECRET),
        )


@pytest.mark.parametrize(
    ("mode", "error", "message"),
    [
        ("wrong-dimension", ValidationFailed, "dimension-drift"),
        ("non-finite", ValidationFailed, "non-finite"),
        ("partial", ConfigurationError, "partial"),
        ("wrong-index", ConfigurationError, "row/index"),
        ("wrong-model", ConfigurationError, "model drift"),
    ],
)
def test_probe_fails_closed_on_malformed_provider_batch(
    tmp_path: Path, mode: str, error: type[Exception], message: str
) -> None:
    provider = _provider(tmp_path, FakeInfinityTransport(mode))
    with pytest.raises(error, match=message):
        provider.probe(_fixture())
    with pytest.raises(ConfigurationError, match="profile-stale"):
        provider.describe()


def test_unavailable_health_and_remote_endpoint_are_explicit(tmp_path: Path) -> None:
    provider = _provider(tmp_path, FakeInfinityTransport("unavailable"))
    assert provider.health().degraded_state is EmbeddingDegradedState.UNAVAILABLE
    with pytest.raises(ConfigurationError, match="loopback"):
        LocalBGEConfiguration(
            endpoint="https://models.example.test",
            model_cache_root=tmp_path,
        )


def test_provider_timeout_after_probe_fails_without_synthetic_fallback(tmp_path: Path) -> None:
    transport = FakeInfinityTransport()
    provider = _provider(tmp_path, transport)
    profile = provider.probe(_fixture()).profile
    transport.mode = "timeout"

    with pytest.raises(ConfigurationError, match="timeout"):
        provider.embed_query(
            "idempotent import",
            EmbeddingPolicy(DataClassification.PUBLIC, profile.profile_digest),
        )
    assert provider.describe().profile_digest == profile.profile_digest


@pytest.mark.parametrize("texts", [(), ("",), ("ok",) * 9])
def test_embedding_batch_rejects_empty_blank_and_overflow(
    tmp_path: Path, texts: tuple[str, ...]
) -> None:
    provider = _provider(tmp_path, FakeInfinityTransport())
    provider.probe(_fixture())
    profile = provider.describe()
    with pytest.raises(ValidationFailed, match="bounded"):
        provider.embed_documents(
            texts,
            EmbeddingPolicy(DataClassification.PUBLIC, profile.profile_digest),
        )
