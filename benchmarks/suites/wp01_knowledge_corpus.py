"""Deterministic real-Zekam corpus and loopback BGE-M3 evidence for WP-01."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import struct
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "zekam-wp01-knowledge-corpus/v1"
MODEL_ID = "BAAI/bge-m3"
DIMENSION = 1024
CHUNK_CHARACTERS = 1_600
CHUNK_OVERLAP = 240

SOURCE_PATHS = (
    "docs/MEASURED_LOOP_RUNBOOK.md",
    "docs/SISTEM_UCTAN_UCA_KABUL_DURUMU.md",
    "docs/DOGAL_DIL_VE_ARASTIRMA.md",
    "docs/KNOWLEDGE_INGESTION.md",
    "docs/GELISTIRME_KURULUMU.md",
    "docs/CONTEXT_COMPILER_VE_CONTINUITY.md",
    "docs/adr/ADR-0001-second-lifecycle-harness.md",
    "src/zekam/application/jira_issue_routing.py",
    "src/zekam/application/oracle_metadata_index.py",
    "src/zekam/application/project_knowledge_index.py",
    "src/zekam/domain/intake.py",
    "migrations/0046_compaction_checkpoint_compiler.sql",
)


@dataclass(frozen=True, slots=True)
class CorpusChunk:
    chunk_id: str
    project_id: str
    source_path: str
    source_digest: str
    text: str
    vector_b64: str


@dataclass(frozen=True, slots=True)
class QueryCase:
    case_id: str
    query_class: str
    text: str
    expected_paths: list[str]
    project_id: str
    vector_b64: str


QUERY_SPECS = (
    (
        "tr-natural-recovery",
        "turkish-natural-language",
        "Bir effect claim var ama terminal receipt yoksa güvenli toparlanma nasıl yapılır?",
        ("docs/MEASURED_LOOP_RUNBOOK.md",),
        "zekam",
    ),
    (
        "en-technical-compaction",
        "english-technical-documentation",
        "How does the PreCompact lifecycle event become a durable checkpoint compiler outbox?",
        (
            "docs/adr/ADR-0001-second-lifecycle-harness.md",
            "migrations/0046_compaction_checkpoint_compiler.sql",
        ),
        "zekam",
    ),
    (
        "plsql-object",
        "oracle-plsql-object-name",
        "work.compile_pre_compact_outbox",
        ("migrations/0046_compaction_checkpoint_compiler.sql",),
        "zekam",
    ),
    (
        "path-and-function",
        "file-path-class-function-identifier",
        "src/zekam/application/jira_issue_routing.py resolve_jira_issue",
        ("src/zekam/application/jira_issue_routing.py",),
        "zekam",
    ),
    (
        "jira-id",
        "jira-demand-defect-id",
        "SKYRSM-5661",
        ("docs/SISTEM_UCTAN_UCA_KABUL_DURUMU.md",),
        "zekam",
    ),
    (
        "typo-compaction",
        "typo-fuzzy-query",
        "compation chekpoint compiller",
        (
            "docs/adr/ADR-0001-second-lifecycle-harness.md",
            "migrations/0046_compaction_checkpoint_compiler.sql",
        ),
        "zekam",
    ),
    (
        "semantic-effect-proof",
        "semantic-different-words",
        "Dış işlem başladı fakat sonuç kanıtı kayboldu; yeniden çalıştırmadan ne yapılmalı?",
        ("docs/MEASURED_LOOP_RUNBOOK.md",),
        "zekam",
    ),
    (
        "exact-semantic-conflict",
        "exact-keyword-semantic-conflict",
        "anaphora-unresolved",
        ("src/zekam/domain/intake.py", "docs/DOGAL_DIL_VE_ARASTIRMA.md"),
        "zekam",
    ),
    (
        "no-answer",
        "no-answer",
        "Zekam'ın Mars seralarındaki mor muz sulama protokolü nedir?",
        (),
        "zekam",
    ),
    (
        "cross-project-leakage",
        "cross-project-leakage-attempt",
        "CROSS-PROJECT-SECRET-7788",
        (),
        "zekam",
    ),
)


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _source_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _assert_safe_source(repo_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("corpus source path must be portable and relative")
    current = repo_root.resolve()
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"corpus source cannot be a symlink: {relative}")
    resolved = current.resolve(strict=True)
    resolved.relative_to(repo_root.resolve())
    if not resolved.is_file():
        raise ValueError(f"corpus source must be a regular file: {relative}")
    return resolved


def _chunk_text(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        stop = min(start + CHUNK_CHARACTERS, len(normalized))
        if stop < len(normalized):
            newline = normalized.rfind("\n", start + CHUNK_CHARACTERS // 2, stop)
            if newline > start:
                stop = newline
        piece = normalized[start:stop].strip()
        if piece:
            chunks.append(piece)
        if stop >= len(normalized):
            break
        next_start = max(start + 1, stop - CHUNK_OVERLAP)
        start = next_start
    return chunks


def source_chunks(repo_root: Path) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    for relative in SOURCE_PATHS:
        source = _assert_safe_source(repo_root, relative)
        payload = source.read_bytes()
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError(f"corpus source exceeds limit: {relative}")
        text = payload.decode("utf-8")
        digest = _source_digest(payload)
        for index, piece in enumerate(_chunk_text(text)):
            chunks.append(
                {
                    "chunk_id": f"zekam:{relative}:{index:04d}",
                    "project_id": "zekam",
                    "source_path": relative,
                    "source_digest": digest,
                    "text": piece,
                }
            )
    decoy = "CROSS-PROJECT-SECRET-7788 belongs only to the isolated other project."
    chunks.append(
        {
            "chunk_id": "other:security-decoy:0000",
            "project_id": "other",
            "source_path": "security-fixture/cross-project-decoy.txt",
            "source_digest": _source_digest(decoy.encode("utf-8")),
            "text": decoy,
        }
    )
    identifiers = [chunk["chunk_id"] for chunk in chunks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("corpus chunk identifiers are not unique")
    return chunks


def _encode_vector(values: list[float]) -> str:
    if len(values) != DIMENSION:
        raise ValueError("BGE vector dimension mismatch")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("BGE vector contains non-finite values")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("BGE vector norm must be finite and positive")
    normalized = [value / norm for value in values]
    return base64.b64encode(struct.pack(f"<{DIMENSION}f", *normalized)).decode("ascii")


def decode_vector(encoded: str) -> list[float]:
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("vector is not canonical base64") from exc
    expected = DIMENSION * 4
    if len(payload) != expected:
        raise ValueError("vector byte length mismatch")
    values = list(struct.unpack(f"<{DIMENSION}f", payload))
    if any(not math.isfinite(value) for value in values):
        raise ValueError("decoded vector contains non-finite values")
    return values


def _validate_loopback_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("BGE endpoint must be an explicit IPv4 loopback HTTP origin")
    return endpoint.rstrip("/")


def embed_batches(
    texts: list[str], *, endpoint: str, model: str = MODEL_ID, batch_size: int = 32
) -> list[str]:
    endpoint = _validate_loopback_endpoint(endpoint)
    if model != MODEL_ID:
        raise ValueError("unexpected BGE model identity")
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("embedding input must be a non-empty text list")
    if type(batch_size) is not int or not 1 <= batch_size <= 64:
        raise ValueError("embedding batch size must be 1..64")
    encoded: list[str] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        body = json.dumps(
            {"input": batch, "model": model},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint + "/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise ValueError(f"BGE response status is {response.status}")
            document = json.loads(response.read().decode("utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            raise ValueError("BGE response shape is invalid")
        rows = document["data"]
        if len(rows) != len(batch):
            raise ValueError("BGE partial batch response rejected")
        by_index: dict[int, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("index")) is not int:
                raise ValueError("BGE response index is invalid")
            index = row["index"]
            vector = row.get("embedding")
            if index in by_index or not isinstance(vector, list):
                raise ValueError("BGE response contains duplicate index or invalid vector")
            if any(type(value) not in {int, float} for value in vector):
                raise ValueError("BGE vector contains invalid numeric type")
            by_index[index] = [float(value) for value in vector]
        if set(by_index) != set(range(len(batch))):
            raise ValueError("BGE response indices are incomplete")
        encoded.extend(_encode_vector(by_index[index]) for index in range(len(batch)))
    return encoded


def build_corpus_document(repo_root: Path, *, endpoint: str) -> dict[str, Any]:
    raw_chunks = source_chunks(repo_root)
    query_rows = list(QUERY_SPECS)
    all_texts = [chunk["text"] for chunk in raw_chunks] + [row[2] for row in query_rows]
    all_vectors = embed_batches(all_texts, endpoint=endpoint)
    chunk_vectors = all_vectors[: len(raw_chunks)]
    query_vectors = all_vectors[len(raw_chunks) :]
    chunks = [
        asdict(CorpusChunk(**chunk, vector_b64=vector))
        for chunk, vector in zip(raw_chunks, chunk_vectors, strict=True)
    ]
    queries = [
        asdict(
            QueryCase(
                case_id=case_id,
                query_class=query_class,
                text=text,
                expected_paths=list(expected_path),
                project_id=project_id,
                vector_b64=vector,
            )
        )
        for (case_id, query_class, text, expected_path, project_id), vector in zip(
            query_rows, query_vectors, strict=True
        )
    ]
    duplicate_vectors = embed_batches(
        [raw_chunks[0]["text"], raw_chunks[0]["text"]], endpoint=endpoint
    )
    left = decode_vector(duplicate_vectors[0])
    right = decode_vector(duplicate_vectors[1])
    cosine = math.fsum(a * b for a, b in zip(left, right, strict=True))
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "embedding_profile": {
            "dimension": DIMENSION,
            "distance_metric": "cosine",
            "exact_model_id": MODEL_ID,
            "normalized": True,
            "passage_prefix": "",
            "provider_kind": "local-loopback",
            "query_prefix": "",
            "vector_dtype": "float32-little-endian-base64",
        },
        "source_paths": list(SOURCE_PATHS),
        "chunks": chunks,
        "queries": queries,
        "probe": {
            "duplicate_cosine": cosine,
            "duplicate_semantically_equivalent": cosine >= 0.999999,
            "non_finite_count": 0,
        },
    }
    document["corpus_digest"] = canonical_digest(document)
    return document


def write_corpus(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_corpus(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate corpus JSON key: {key}")
            result[key] = value
        return result

    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("corpus schema mismatch")
    expected_digest = document.pop("corpus_digest", None)
    actual_digest = canonical_digest(document)
    document["corpus_digest"] = expected_digest
    if expected_digest != actual_digest:
        raise ValueError("corpus digest mismatch")
    chunks = document.get("chunks")
    queries = document.get("queries")
    if not isinstance(chunks, list) or not chunks or not isinstance(queries, list) or not queries:
        raise ValueError("corpus chunks and queries must be non-empty lists")
    for row in [*chunks, *queries]:
        if not isinstance(row, dict):
            raise ValueError("corpus row must be an object")
        decode_vector(row["vector_b64"])
    return document


_TOKEN = re.compile(r"[\w./#-]+", re.UNICODE)


def lexical_rank(query: str, chunks: list[dict[str, Any]], *, limit: int = 10) -> list[int]:
    query_tokens = {token.casefold() for token in _TOKEN.findall(query) if len(token) > 1}
    scored: list[tuple[float, int]] = []
    for index, chunk in enumerate(chunks):
        if chunk["project_id"] != "zekam":
            continue
        text = f"{chunk['source_path']}\n{chunk['text']}".casefold()
        tokens = _TOKEN.findall(text)
        token_set = set(tokens)
        overlap = len(query_tokens & token_set)
        phrase = 1 if query.casefold() in text else 0
        score = float(overlap + phrase * (len(query_tokens) + 3))
        if score > 0:
            scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], chunks[item[1]]["chunk_id"]))
    return [index for _, index in scored[:limit]]


def exact_rank(query: str, chunks: list[dict[str, Any]], *, limit: int = 10) -> list[int]:
    needle = query.casefold().strip()
    if not needle:
        return []
    structured = any(marker in needle for marker in ("/", ".", "_", "#", "-"))
    parts = needle.split()
    matches = [
        index
        for index, chunk in enumerate(chunks)
        if chunk["project_id"] == "zekam"
        and (
            needle in f"{chunk['source_path']}\n{chunk['text']}".casefold()
            or (
                structured
                and all(
                    part in f"{chunk['source_path']}\n{chunk['text']}".casefold() for part in parts
                )
            )
        )
    ]
    matches.sort(key=lambda index: chunks[index]["chunk_id"])
    return matches[:limit]


def rrf_paths(
    dense_paths: list[str], lexical_paths: list[str], *, limit: int = 10, k: int = 60
) -> list[str]:
    scores: dict[str, float] = {}
    for channel in (dense_paths, lexical_paths):
        for rank, path in enumerate(channel, start=1):
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank)
    ordered_scores: list[tuple[str, float]] = list(scores.items())
    ordered_scores.sort(key=lambda item: (-item[1], item[0]))
    return [path for path, _ in ordered_scores[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = build_corpus_document(args.repo_root, endpoint=args.endpoint)
    write_corpus(args.output, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
