from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.suites.wp01_knowledge_corpus import (
    DIMENSION,
    _chunk_text,
    _validate_loopback_endpoint,
    canonical_digest,
    decode_vector,
    exact_rank,
    lexical_rank,
    load_corpus,
    rrf_paths,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:7997",
        "http://localhost:7997",
        "http://0.0.0.0:7997",
        "http://127.0.0.1",
        "http://user@127.0.0.1:7997",
        "http://127.0.0.1:7997/path",
        "http://127.0.0.1:7997?token=x",
    ],
)
def test_loopback_endpoint_rejects_ambiguous_or_non_loopback_values(endpoint: str) -> None:
    with pytest.raises(ValueError):
        _validate_loopback_endpoint(endpoint)


def test_loopback_endpoint_accepts_exact_ipv4_origin() -> None:
    assert _validate_loopback_endpoint("http://127.0.0.1:7997") == "http://127.0.0.1:7997"


def test_chunking_is_bounded_and_overlapping() -> None:
    chunks = _chunk_text("line\n" * 1_000)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 1_600 for chunk in chunks)


def test_decode_vector_rejects_wrong_size_and_invalid_base64() -> None:
    with pytest.raises(ValueError):
        decode_vector("not-base64")
    with pytest.raises(ValueError):
        decode_vector("YQ==")


def test_lexical_rank_filters_other_project_before_limit() -> None:
    chunks = [
        {
            "chunk_id": "other",
            "project_id": "other",
            "source_path": "other.txt",
            "text": "EXACT-SECRET",
        },
        {
            "chunk_id": "zekam",
            "project_id": "zekam",
            "source_path": "zekam.txt",
            "text": "ordinary content",
        },
    ]
    assert lexical_rank("EXACT-SECRET", chunks, limit=1) == []
    assert exact_rank("EXACT-SECRET", chunks, limit=1) == []


def test_rrf_is_deterministic_and_combines_channels() -> None:
    assert rrf_paths(["dense", "shared"], ["lexical", "shared"]) == [
        "shared",
        "dense",
        "lexical",
    ]


def test_load_corpus_rejects_digest_drift_and_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_corpus(path)

    vector = "A" * ((DIMENSION * 4 + 2) // 3 * 4)
    document = {
        "schema": "zekam-wp01-knowledge-corpus/v1",
        "chunks": [{"vector_b64": vector}],
        "queries": [{"vector_b64": vector}],
        "corpus_digest": canonical_digest({"drift": True}),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_corpus(path)
