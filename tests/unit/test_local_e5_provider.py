from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from zekam.application.embedding_provider import (
    EmbeddingPolicy,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
)
from zekam.domain.canonical import digest
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding import onnx_e5 as subject

pytestmark = pytest.mark.unit

np = pytest.importorskip("numpy")


def _manifest() -> dict[str, object]:
    return {
        "schema": "zekam-local-embedding-install/v1",
        "repository": subject.MODEL_REPOSITORY,
        "source_revision": subject.MODEL_REVISION,
        "revision": subject.MODEL_REVISION,
        "model_ref": subject.EXACT_MODEL_ID,
        "model_file": subject.MODEL_FILE,
        "files": [
            {"path": subject.MODEL_FILE, "sha256": subject.EXPECTED_MODEL_SHA256},
            {"path": "tokenizer.json", "sha256": digest("tokenizer")},
            {"path": "config.json", "sha256": digest("config")},
            {"path": "sentencepiece.bpe.model", "sha256": digest("sentencepiece")},
        ],
    }


class _Tokenizer:
    def encode_batch(self, texts: list[str]) -> list[SimpleNamespace]:
        del texts
        return [
            SimpleNamespace(ids=[1, 2], attention_mask=[1, 0], type_ids=[0, 0]),
            SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 0]),
        ]


class _Session:
    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def get_inputs(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="input_ids"),
            SimpleNamespace(name="attention_mask"),
            SimpleNamespace(name="token_type_ids"),
        ]

    def run(self, _outputs: list[str], inputs: dict[str, object]) -> list[object]:
        batch, width = inputs["input_ids"].shape
        output = np.zeros((batch, width, subject.EMBEDDING_DIMENSION), dtype=np.float32)
        output[:, 0, 0] = 1.0
        output[:, 1, 1] = 2.0
        return [output]


def _provider(monkeypatch: pytest.MonkeyPatch, tmp_path) -> subject.LocalOnnxE5Provider:
    monkeypatch.setattr(subject, "_assert_model_files", lambda _configuration: (
        _manifest(),
        subject.EXPECTED_MODEL_SHA256,
        digest("tokenizer"),
    ))
    monkeypatch.setattr(
        subject,
        "_load_runtime",
        lambda _model_path, _configuration: (_Session(), (_Tokenizer(), np), "test"),
    )
    return subject.LocalOnnxE5Provider(subject.LocalE5Configuration(tmp_path))


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="local-e5-test",
        display_name="Local E5 test",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id=subject.EXACT_MODEL_ID,
        model_revision_fingerprint=digest("revision"),
        dimension=subject.EMBEDDING_DIMENSION,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix=subject.QUERY_PREFIX,
        passage_prefix=subject.PASSAGE_PREFIX,
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="windows-x86_64:cpu",
        data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
        verified_at="2026-10-17T00:00:00Z",
        probe_evidence_digest=digest("probe"),
    )


def test_configuration_enforces_pinned_cpu_batch_contract(tmp_path) -> None:
    with pytest.raises(subject.ConfigurationError, match="1..4"):
        subject.LocalE5Configuration(tmp_path, max_batch_size=5)
    configuration = subject.LocalE5Configuration(tmp_path)
    assert configuration.max_batch_size == 4
    assert configuration.intra_op_num_threads == 2
    assert configuration.inter_op_num_threads == 1
    assert configuration.query_prefix == "query: "
    assert configuration.passage_prefix == "passage: "


def test_manifest_requires_the_complete_pinned_file_set(tmp_path) -> None:
    manifest_path = tmp_path / "install-manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    with pytest.raises(subject.ConfigurationError, match="dosya kumesi"):
        subject._read_manifest(manifest_path, subject.LocalE5Configuration(tmp_path))


def test_manifest_accepts_legacy_missing_upstream_digests(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The local pinned digest remains authoritative for older installs."""

    def file_sha(relative: str) -> str:
        return hashlib.sha256(relative.encode("utf-8")).hexdigest()

    records = {
        relative: {
            "path": relative,
            "size_bytes": 1,
            "sha256": subject.EXPECTED_MODEL_SHA256
            if relative == subject.MODEL_FILE
            else file_sha(relative),
            "upstream_sha256": None,
        }
        for relative in subject._REQUIRED_INSTALL_FILES
    }
    manifest = _manifest() | {"files": list(records.values())}
    manifest_path = tmp_path / "install-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for relative in subject._REQUIRED_INSTALL_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    expected = {
        relative: (
            1,
            subject.EXPECTED_MODEL_SHA256
            if relative == subject.MODEL_FILE
            else file_sha(relative),
        )
        for relative in subject._REQUIRED_INSTALL_FILES
    }
    monkeypatch.setattr(
        subject,
        "_sha256_file_with_size",
        lambda path: expected[str(path.relative_to(tmp_path)).replace("\\", "/")],
    )

    parsed, model_sha, tokenizer_sha = subject._assert_model_files(
        subject.LocalE5Configuration(tmp_path)
    )

    assert parsed["files"] == list(records.values())
    assert model_sha == subject.EXPECTED_MODEL_SHA256
    assert tokenizer_sha == file_sha("tokenizer.json")


def test_install_file_rejects_a_reparse_parent(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / "onnx", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")

    assert not subject._private_install_file(tmp_path, subject.MODEL_FILE)


def test_mask_aware_pooling_returns_normalized_384_vectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    provider = _provider(monkeypatch, tmp_path)

    vectors = provider._vectors(("first", "second"))

    assert len(vectors) == 2
    assert all(len(vector) == subject.EMBEDDING_DIMENSION for vector in vectors)
    assert vectors[0][0] == pytest.approx(1.0)
    assert vectors[0][1] == pytest.approx(0.0)
    assert vectors[1][0] == pytest.approx(1 / (5**0.5))
    assert vectors[1][1] == pytest.approx(2 / (5**0.5))
    assert all(sum(value * value for value in vector) == pytest.approx(1.0) for vector in vectors)


def test_query_and_document_prefixes_are_bound_to_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    provider = _provider(monkeypatch, tmp_path)
    profile = _profile()
    provider._profile = profile
    captured: list[tuple[str, ...]] = []

    def vectors(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        captured.append(texts)
        return ((1.0,) + (0.0,) * (subject.EMBEDDING_DIMENSION - 1),) * len(texts)

    monkeypatch.setattr(provider, "_vectors", vectors)
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, profile.profile_digest)

    query = provider.embed_query("soru", policy)
    documents = provider.embed_documents(("belge",), policy)

    assert captured == [("query: soru",), ("passage: belge",)]
    assert query.receipt.purpose is EmbeddingPurpose.QUERY
    assert documents.receipt.purpose is EmbeddingPurpose.DOCUMENT
    assert query.receipt.dimension == subject.EMBEDDING_DIMENSION
    assert documents.receipt.dimension == subject.EMBEDDING_DIMENSION
