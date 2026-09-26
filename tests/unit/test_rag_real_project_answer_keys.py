"""ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001: real-project semantic answer-key.

Scope (AKTIF_GOREV.md section 5 "Test verisi ve gercekcilik"):

* Instead of the synthetic corpus (test_rag_quality_corpus.py), use real
  technical identifiers (functions/classes) extracted from Zekam's own source
  files via AST, together with their real source path + line range.
* Every question points to a real object in the source tree; the expected
  answer is verified by file path and line range.
* Git HEAD at test time is used as `source_revision`, so the answer key is
  bound to source and revision.
* Retrieval-only; no generated (LLM) answer.  Semantic questions still include
  the symbol name, so lexical/exact channels can resolve them.
"""

from __future__ import annotations

import ast
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from zekam.application.embedded_project_rag import EmbeddedProjectRAG
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingHealth,
    EmbeddingPolicy,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import (
    MAX_RECORDS_PER_GENERATION,
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "zekam-source-real-keys"
SOURCE_GLOB = "src/zekam/application/*.py"
CHUNK_LINES = 40
CHUNK_OVERLAP = 10
TARGET_SYMBOLS = 30


@dataclass(frozen=True)
class Symbol:
    """Technical symbol extracted from real Zekam source."""

    name: str
    relative_path: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class SourceChunk:
    """A line window of a source file."""

    chunk_id: str
    relative_path: str
    line_start: int
    line_end: int
    text: str


@dataclass(frozen=True)
class Question:
    """Single evaluation question."""

    query: str
    symbol: Symbol
    kind: str  # "exact" or "semantic"


def _git_head() -> str:
    """Canonical source revision; binds the answer key to source."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("Git HEAD cannot be read; answer key cannot bind to source revision.")
    return result.stdout.strip()


def _application_py_files(root: Path) -> tuple[Path, ...]:
    """List `src/zekam/application/*.py` files."""
    pattern = root / SOURCE_GLOB
    files = sorted(pattern.parent.glob(pattern.name))
    if len(files) < 3:
        pytest.skip(f"Not enough source files found: {pattern}")
    return tuple(files)


def _extract_symbols(file_path: Path, root: Path) -> tuple[Symbol, ...]:
    """Extract top-level function/class names from a Python file."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    relative = file_path.relative_to(root).as_posix()
    symbols: list[Symbol] = []
    # Only module-level definitions; nested definitions are skipped.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(
                Symbol(
                    name=node.name,
                    relative_path=relative,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
    return tuple(symbols)


def _chunk_file(file_path: Path, root: Path) -> tuple[SourceChunk, ...]:
    """Split a source file into overlapping line windows."""
    lines = file_path.read_text(encoding="utf-8").splitlines()
    relative = file_path.relative_to(root).as_posix()
    safe_id = relative.replace("/", "__").replace(".", "_")
    chunks: list[SourceChunk] = []
    start = 0
    while start < len(lines):
        end = min(start + CHUNK_LINES, len(lines))
        text = "\n".join(lines[start:end])
        line_start = start + 1
        line_end = end
        chunk_id = f"{safe_id}__{line_start}_{line_end}"
        chunks.append(
            SourceChunk(
                chunk_id=chunk_id,
                relative_path=relative,
                line_start=line_start,
                line_end=line_end,
                text=text,
            )
        )
        if end >= len(lines):
            break
        start = end - CHUNK_OVERLAP
    return tuple(chunks)


def _vector_for_chunk(seed: str) -> tuple[float, ...]:
    """Deterministic, normalized fixture vector (provider-free)."""
    h = hash(seed) & 0x7FFFFFFF
    idx_a = h % VECTOR_DIMENSION
    idx_b = (h * 7 + 3) % VECTOR_DIMENSION
    values = [0.0] * VECTOR_DIMENSION
    values[idx_a] = 1.0
    values[idx_b] = 0.25
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _select_symbols(symbols: tuple[Symbol, ...], count: int) -> tuple[Symbol, ...]:
    """Deterministic, stable subset selection."""
    if len(symbols) <= count:
        return symbols
    ordered = sorted(symbols, key=lambda s: (s.relative_path, s.line_start, s.name))
    step = len(ordered) // count
    return tuple(ordered[i * step] for i in range(count))


class _DeterministicProvider:
    """Provider-free deterministic embedding provider for the real-source test."""

    def __init__(self) -> None:
        self.profile = EmbeddingProfile(
            profile_id="real-keys-deterministic",
            display_name="Real keys deterministic fixture",
            provider_kind=EmbeddingProviderKind.LOCAL,
            provider_identity_digest=digest("real-keys-provider"),
            exact_model_id="BAAI/bge-m3",
            model_revision_fingerprint=digest("real-keys-revision"),
            dimension=VECTOR_DIMENSION,
            vector_dtype="float32",
            normalized=True,
            distance_metric="cosine",
            query_prefix="",
            passage_prefix="",
            preprocessor_digest=digest("preprocessor"),
            tokenizer_digest=digest("tokenizer"),
            batch_policy_digest=digest("batch"),
            device_scope="cpu",
            data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
            verified_at="2026-09-02T00:00:00Z",
            probe_evidence_digest=digest("real-keys-probe"),
        )

    def describe(self) -> EmbeddingProfile:
        return self.profile

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(
            True,
            self.profile.profile_digest,
            None,
            digest("healthy"),
        )

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        vector = _vector_for_chunk(f"query:{text}")
        return EmbeddingBatch(
            (vector,),
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.QUERY,
                profile_digest=self.profile.profile_digest,
                input_digest=digest(text),
                output_digest=digest(vector),
                vector_count=1,
                dimension=VECTOR_DIMENSION,
                latency_ms=1,
                provider_call_count=1,
            ),
        )

    def embed_documents(
        self, texts: tuple[str, ...], policy: EmbeddingPolicy
    ) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        vectors = tuple(_vector_for_chunk(f"doc:{i}:{text}") for i, text in enumerate(texts))
        return EmbeddingBatch(
            vectors,
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.DOCUMENT,
                profile_digest=self.profile.profile_digest,
                input_digest=digest(texts),
                output_digest=digest(vectors),
                vector_count=len(texts),
                dimension=VECTOR_DIMENSION,
                latency_ms=1,
                provider_call_count=1,
            ),
        )

    def probe(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("real-keys query test must not probe")


def _build_index(
    tmp_path: Path,
    files: tuple[Path, ...],
    symbols: tuple[Symbol, ...],
    revision: str,
    root: Path,
) -> SQLiteKnowledgeIndex:
    """Build a SQLiteKnowledgeIndex by chunking source files."""
    index = SQLiteKnowledgeIndex(tmp_path / "real_keys.sqlite3", create=True)

    source_digests: dict[str, str] = {}
    for file_path in files:
        relative = file_path.relative_to(root).as_posix()
        source_digests[relative] = digest_of_bytes(file_path.read_bytes())

    records: list[KnowledgeIndexRecord] = []
    chunk_order = 0
    for file_path in files:
        chunks = _chunk_file(file_path, root)
        for chunk in chunks:
            records.append(
                KnowledgeIndexRecord(
                    chunk_id=chunk.chunk_id,
                    project_id=PROJECT_ID,
                    source_revision=revision,
                    source_path=chunk.relative_path,
                    source_digest=source_digests[chunk.relative_path],
                    locator=Locator(
                        relative_path=chunk.relative_path,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                        object_name=None,
                    ),
                    text=chunk.text,
                    content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
                    chunk_order=chunk_order,
                    vector=_vector_for_chunk(chunk.chunk_id),
                )
            )
            chunk_order += 1

    if not records:
        pytest.skip("No chunks could be produced.")
    if len(records) > MAX_RECORDS_PER_GENERATION:
        pytest.skip(
            f"Chunk count {len(records)} exceeds limit {MAX_RECORDS_PER_GENERATION}."
        )

    tree_digest = digest(revision)
    index.build_generation(
        tuple(records),
        project_id=PROJECT_ID,
        source_revision=revision,
        tree_digest=tree_digest,
        source_manifest_digest=digest({"manifest": revision}),
        embedding_profile_digest=digest("real-keys-embedding-profile"),
        provider_profile_digest=_DeterministicProvider().profile.profile_digest,
        created_at="2026-09-26T00:00:00Z",
    )
    return index


def _symbol_to_question(symbol: Symbol) -> tuple[Question, Question]:
    """Generate exact and semantic questions for a symbol."""
    exact = Question(query=symbol.name, symbol=symbol, kind="exact")
    semantic = Question(
        query=f"what does {symbol.name} do",
        symbol=symbol,
        kind="semantic",
    )
    return exact, semantic


def _citation_matches(citation: dict[str, Any], symbol: Symbol) -> bool:
    """Does the citation locator intersect the symbol file/line range?"""
    locator = citation.get("locator", {})
    path = locator.get("relative_path")
    if path != symbol.relative_path:
        return False
    try:
        c_start = int(locator.get("line_start", 0))
        c_end = int(locator.get("line_end", 0))
    except (TypeError, ValueError):
        return False
    return c_start <= symbol.line_end and c_end >= symbol.line_start


def _evaluate_questions(
    rag: EmbeddedProjectRAG,
    questions: tuple[Question, ...],
    revision: str,
) -> dict[str, Any]:
    """Run questions and report source-revision + locator correctness."""
    total = len(questions)
    answered = 0
    located = 0
    revision_mismatches = 0
    per_kind: dict[str, dict[str, int]] = {
        "exact": {"total": 0, "located": 0},
        "semantic": {"total": 0, "located": 0},
    }

    for question in questions:
        result = rag.query(
            question.query,
            project_id=PROJECT_ID,
            expected_source_revision=revision,
            expected_tree_digest=digest(revision),
            token_budget=1200,
        )
        per_kind[question.kind]["total"] += 1
        if result.get("state") in ("answered",):
            answered += 1
        citations = result.get("citations", [])
        for citation in citations:
            if citation.get("source_revision") != revision:
                revision_mismatches += 1
        if any(_citation_matches(citation, question.symbol) for citation in citations):
            located += 1
            per_kind[question.kind]["located"] += 1

    return {
        "total": total,
        "answered": answered,
        "located": located,
        "revision_mismatches": revision_mismatches,
        "per_kind": per_kind,
    }


def test_real_project_answer_keys(tmp_path: Path) -> None:
    """Extract symbols from real Zekam source and verify retrieval locator accuracy."""
    root = Path(__file__).resolve().parents[2]
    revision = _git_head()
    files = _application_py_files(root)

    all_symbols: list[Symbol] = []
    for file_path in files:
        all_symbols.extend(_extract_symbols(file_path, root))
    if len(all_symbols) < TARGET_SYMBOLS // 2:
        pytest.skip(f"Not enough symbols found: {len(all_symbols)}")

    symbols = _select_symbols(tuple(all_symbols), TARGET_SYMBOLS)
    questions = tuple(
        question for symbol in symbols for question in _symbol_to_question(symbol)
    )

    index = _build_index(tmp_path, files, symbols, revision, root)
    try:
        provider = _DeterministicProvider()
        policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)
        rag = EmbeddedProjectRAG(index, provider, policy)
        metrics = _evaluate_questions(rag, questions, revision)

        print(
            f"\n[REAL-KEYS] revision={revision[:12]} symbols={len(symbols)} "
            f"questions={metrics['total']} located={metrics['located']} "
            f"answered={metrics['answered']} revision_mismatches={metrics['revision_mismatches']}"
        )

        exact = metrics["per_kind"]["exact"]
        semantic = metrics["per_kind"]["semantic"]
        assert exact["total"] > 0
        assert semantic["total"] > 0
        exact_rate = exact["located"] / exact["total"]
        semantic_rate = semantic["located"] / semantic["total"]
        assert exact_rate >= 0.65, f"exact located rate low: {exact_rate:.2f}"
        assert semantic_rate >= 0.45, f"semantic located rate low: {semantic_rate:.2f}"
        assert metrics["revision_mismatches"] == 0
    finally:
        index.close()
