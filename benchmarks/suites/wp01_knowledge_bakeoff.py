"""Measured local knowledge-index bake-off for LanceDB, Zvec and sqlite-vec."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import socket
import sqlite3
import statistics
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from benchmarks.suites.wp01_knowledge_corpus import (
    DIMENSION,
    canonical_digest,
    decode_vector,
    exact_rank,
    lexical_rank,
    load_corpus,
    rrf_paths,
)
from benchmarks.suites.wp01_platform import current_acceptance_platform
from zekam.application.technology_bakeoff import assess_sqlite_wal_safety

SCHEMA = "zekam-wp01-knowledge-bakeoff/v1"
NO_ANSWER_THRESHOLD = 0.50


@dataclass(frozen=True, slots=True)
class IndexRecord:
    record_id: str
    project_id: str
    source_path: str
    source_digest: str
    text: str
    vector: list[float]


@dataclass(frozen=True, slots=True)
class SearchHit:
    record_id: str
    project_id: str
    source_path: str
    source_digest: str
    similarity: float


class Adapter(Protocol):
    version: str

    def build(self, records: Iterator[IndexRecord], *, count: int) -> None: ...

    def add(self, record: IndexRecord) -> None: ...

    def search(self, vector: list[float], *, project_id: str, limit: int) -> list[SearchHit]: ...

    def count(self) -> int: ...

    def close(self) -> None: ...


@contextmanager
def _deny_network() -> Iterator[dict[str, int]]:
    attempts = {"count": 0}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def denied_connect(sock: socket.socket, address: object) -> None:
        del sock, address
        attempts["count"] += 1
        raise RuntimeError("network denied during local knowledge bake-off")

    def denied_connect_ex(sock: socket.socket, address: object) -> int:
        denied_connect(sock, address)
        return 1

    def denied_create(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        attempts["count"] += 1
        raise RuntimeError("network denied during local knowledge bake-off")

    socket.socket.connect = denied_connect  # type: ignore[assignment]
    socket.socket.connect_ex = denied_connect_ex  # type: ignore[assignment]
    socket.create_connection = denied_create
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def _batches(records: Iterator[IndexRecord], size: int = 1_000) -> Iterator[list[IndexRecord]]:
    batch: list[IndexRecord] = []
    for record in records:
        batch.append(record)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


class LanceAdapter:
    def __init__(self, root: Path, *, create: bool) -> None:
        self._module: Any = importlib.import_module("lancedb")
        self.version = importlib.metadata.version("lancedb")
        self._root = root
        self._db: Any = self._module.connect(str(root))
        self._table: Any | None = None
        if not create:
            self._table = self._db.open_table("chunks")

    @staticmethod
    def _row(record: IndexRecord) -> dict[str, Any]:
        return {
            "id": record.record_id,
            "project_id": record.project_id,
            "source_path": record.source_path,
            "source_digest": record.source_digest,
            "text": record.text,
            "vector": record.vector,
        }

    def build(self, records: Iterator[IndexRecord], *, count: int) -> None:
        first = True
        for batch in _batches(records):
            rows = [self._row(record) for record in batch]
            if first:
                self._table = self._db.create_table("chunks", data=rows, mode="create")
                first = False
            else:
                if self._table is None:
                    raise AssertionError("LanceDB table missing after first batch")
                self._table.add(rows)
        if self._table is None:
            raise ValueError("LanceDB build received no records")
        self._table.create_scalar_index("project_id", replace=True)
        if count >= 256:
            partitions = max(16, min(256, int(math.sqrt(count))))
            self._table.create_index(
                metric="cosine",
                index_type="IVF_FLAT",
                num_partitions=partitions,
                vector_column_name="vector",
                replace=True,
            )

    def add(self, record: IndexRecord) -> None:
        assert self._table is not None
        self._table.add([self._row(record)])

    def search(self, vector: list[float], *, project_id: str, limit: int) -> list[SearchHit]:
        assert self._table is not None
        safe_project = project_id.replace("'", "''")
        rows = (
            self._table.search(vector, vector_column_name="vector")
            .where(f"project_id = '{safe_project}'", prefilter=True)
            .select(["id", "project_id", "source_path", "source_digest"])
            .limit(limit)
            .to_list()
        )
        return [
            SearchHit(
                record_id=str(row["id"]),
                project_id=str(row["project_id"]),
                source_path=str(row["source_path"]),
                source_digest=str(row["source_digest"]),
                similarity=max(-1.0, min(1.0, 1.0 - float(row["_distance"]))),
            )
            for row in rows
        ]

    def count(self) -> int:
        assert self._table is not None
        return int(self._table.count_rows())

    def close(self) -> None:
        self._table = None
        self._db = None


class ZvecAdapter:
    def __init__(self, root: Path, *, create: bool) -> None:
        self._module: Any = importlib.import_module("zvec")
        self.version = importlib.metadata.version("zvec")
        self._root = root
        if create:
            fields = [
                self._module.FieldSchema(
                    "project_id",
                    self._module.DataType.STRING,
                    index_param=self._module.InvertIndexParam(),
                ),
                self._module.FieldSchema("source_path", self._module.DataType.STRING),
                self._module.FieldSchema("source_digest", self._module.DataType.STRING),
            ]
            vector = self._module.VectorSchema(
                "vector",
                self._module.DataType.VECTOR_FP32,
                DIMENSION,
                index_param=self._module.HnswIndexParam(
                    metric_type=self._module.MetricType.COSINE,
                    m=16,
                    ef_construction=100,
                ),
            )
            schema = self._module.CollectionSchema(name="chunks", fields=fields, vectors=vector)
            self._collection: Any = self._module.create_and_open(path=str(root), schema=schema)
        else:
            self._collection = self._module.open(str(root))

    def _doc(self, record: IndexRecord) -> Any:
        return self._module.Doc(
            id=record.record_id,
            fields={
                "project_id": record.project_id,
                "source_path": record.source_path,
                "source_digest": record.source_digest,
            },
            vectors={"vector": record.vector},
        )

    def build(self, records: Iterator[IndexRecord], *, count: int) -> None:
        del count
        for batch in _batches(records):
            self._collection.insert([self._doc(record) for record in batch])
        self._collection.flush()

    def add(self, record: IndexRecord) -> None:
        self._collection.insert(self._doc(record))
        self._collection.flush()

    def search(self, vector: list[float], *, project_id: str, limit: int) -> list[SearchHit]:
        if "'" in project_id or "\\" in project_id:
            raise ValueError("unsafe Zvec filter value")
        query = self._module.Query(field_name="vector", vector=vector)
        rows = self._collection.query(
            query,
            topk=limit,
            filter=f"project_id = '{project_id}'",
            output_fields=["project_id", "source_path", "source_digest"],
        )
        hits: list[SearchHit] = []
        for row in rows:
            fields = row.fields
            hits.append(
                SearchHit(
                    record_id=str(row.id),
                    project_id=str(fields["project_id"]),
                    source_path=str(fields["source_path"]),
                    source_digest=str(fields["source_digest"]),
                    similarity=max(-1.0, min(1.0, 1.0 - float(row.score))),
                )
            )
        return hits

    def count(self) -> int:
        return int(self._collection.stats.doc_count)

    def close(self) -> None:
        self._collection.close()


class SQLiteVecAdapter:
    def __init__(self, root: Path, *, create: bool) -> None:
        self._module: Any = importlib.import_module("sqlite_vec")
        self.version = importlib.metadata.version("sqlite-vec")
        root.mkdir(parents=True, exist_ok=True)
        self._lock_descriptor = os.open(
            root / "knowledge.writer.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if os.fstat(self._lock_descriptor).st_size == 0:
            os.write(self._lock_descriptor, b"0")
            os.fsync(self._lock_descriptor)
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                os.lseek(self._lock_descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self._lock_descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_descriptor)
            self._lock_descriptor = -1
            raise RuntimeError("sqlite-vec benchmark single writer already active") from None
        self._path = root / "knowledge.sqlite3"
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("pragma foreign_keys = on")
        self._connection.execute("pragma busy_timeout = 2500")
        self.journal_mode = (
            "wal"
            if assess_sqlite_wal_safety(sqlite3.sqlite_version).safe_for_multi_connection_wal
            else "delete"
        )
        actual_mode = str(
            self._connection.execute(f"pragma journal_mode = {self.journal_mode}").fetchone()[0]
        ).casefold()
        if actual_mode != self.journal_mode:
            self.close()
            raise RuntimeError("sqlite-vec benchmark journal policy uygulanamadi")
        self.writer_coordination = "single-writer-file-lock"
        self._connection.enable_load_extension(True)
        self._module.load(self._connection)
        self._connection.enable_load_extension(False)
        if create:
            self._connection.executescript(
                f"""
                pragma synchronous = full;
                create table chunks (
                    id text primary key,
                    project_id text not null,
                    source_path text not null,
                    source_digest text not null,
                    body text not null
                ) strict;
                create virtual table vectors using vec0(
                    id text primary key,
                    embedding float[{DIMENSION}],
                    project_id text partition key
                );
                create index chunks_project_idx on chunks(project_id, id);
                """
            )
            self._connection.commit()

    def build(self, records: Iterator[IndexRecord], *, count: int) -> None:
        del count
        for batch in _batches(records, size=500):
            self._connection.executemany(
                "insert into chunks(id, project_id, source_path, source_digest, body) "
                "values (?, ?, ?, ?, ?)",
                (
                    (
                        record.record_id,
                        record.project_id,
                        record.source_path,
                        record.source_digest,
                        record.text,
                    )
                    for record in batch
                ),
            )
            self._connection.executemany(
                "insert into vectors(id, embedding, project_id) values (?, ?, ?)",
                (
                    (
                        record.record_id,
                        self._module.serialize_float32(record.vector),
                        record.project_id,
                    )
                    for record in batch
                ),
            )
            self._connection.commit()

    def add(self, record: IndexRecord) -> None:
        try:
            self._connection.execute("begin immediate")
            self._connection.execute(
                "insert into chunks(id, project_id, source_path, source_digest, body) "
                "values (?, ?, ?, ?, ?)",
                (
                    record.record_id,
                    record.project_id,
                    record.source_path,
                    record.source_digest,
                    record.text,
                ),
            )
            self._connection.execute(
                "insert into vectors(id, embedding, project_id) values (?, ?, ?)",
                (
                    record.record_id,
                    self._module.serialize_float32(record.vector),
                    record.project_id,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def search(self, vector: list[float], *, project_id: str, limit: int) -> list[SearchHit]:
        rows = self._connection.execute(
            "select c.id, c.project_id, c.source_path, c.source_digest, v.distance "
            "from vectors v join chunks c on c.id = v.id "
            "where v.embedding match ? and k = ? and v.project_id = ? order by v.distance",
            (self._module.serialize_float32(vector), limit, project_id),
        ).fetchall()
        return [
            SearchHit(
                record_id=str(row[0]),
                project_id=str(row[1]),
                source_path=str(row[2]),
                source_digest=str(row[3]),
                similarity=max(-1.0, min(1.0, 1.0 - (float(row[4]) ** 2) / 2.0)),
            )
            for row in rows
        ]

    def count(self) -> int:
        return int(self._connection.execute("select count(*) from chunks").fetchone()[0])

    def close(self) -> None:
        self._connection.close()
        if self._lock_descriptor >= 0:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                os.lseek(self._lock_descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self._lock_descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            os.close(self._lock_descriptor)
            self._lock_descriptor = -1


def _adapter(candidate: str, root: Path, *, create: bool) -> Adapter:
    if candidate == "lancedb":
        return LanceAdapter(root, create=create)
    if candidate == "zvec":
        return ZvecAdapter(root, create=create)
    if candidate == "sqlite-vec":
        return SQLiteVecAdapter(root, create=create)
    raise ValueError(f"unsupported candidate: {candidate}")


def _record_stream(chunks: list[dict[str, Any]], target_count: int) -> Iterator[IndexRecord]:
    vectors = [decode_vector(chunk["vector_b64"]) for chunk in chunks]
    for index in range(target_count):
        source_index = index % len(chunks)
        chunk = chunks[source_index]
        yield IndexRecord(
            record_id=f"r{index:09d}",
            project_id=str(chunk["project_id"]),
            source_path=str(chunk["source_path"]),
            source_digest=str(chunk["source_digest"]),
            text=str(chunk["text"]),
            vector=vectors[source_index],
        )


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[position]


def _evaluate(
    adapter: Adapter, corpus: dict[str, Any], *, repeat_queries: int = 3
) -> dict[str, Any]:
    chunks = corpus["chunks"]
    source_digest_by_path = {
        str(chunk["source_path"]): str(chunk["source_digest"])
        for chunk in chunks
        if chunk["project_id"] == "zekam"
    }
    cases: list[dict[str, Any]] = []
    latencies: list[float] = []
    for query in corpus["queries"]:
        vector = decode_vector(query["vector_b64"])
        hits: list[SearchHit] = []
        for _ in range(repeat_queries):
            started = time.perf_counter()
            hits = adapter.search(vector, project_id=str(query["project_id"]), limit=10)
            latencies.append(time.perf_counter() - started)
        lexical_indices = lexical_rank(str(query["text"]), chunks, limit=10)
        exact_indices = exact_rank(str(query["text"]), chunks, limit=10)
        exact_paths = [str(chunks[index]["source_path"]) for index in exact_indices]
        lexical_only = [str(chunks[index]["source_path"]) for index in lexical_indices]
        lexical_paths = list(dict.fromkeys([*exact_paths, *lexical_only]))[:10]
        dense_paths = [hit.source_path for hit in hits]
        fusion_paths = rrf_paths(dense_paths, lexical_paths)
        expected = list(query["expected_paths"])
        top_similarity = hits[0].similarity if hits else -1.0
        abstained = not hits or (top_similarity < NO_ANSWER_THRESHOLD and not exact_paths)
        citation_valid = all(
            hit.project_id == query["project_id"]
            and source_digest_by_path.get(hit.source_path) == hit.source_digest
            for hit in hits
        )
        cases.append(
            {
                "abstained": abstained,
                "case_id": query["case_id"],
                "citation_valid": citation_valid,
                "dense_paths": dense_paths,
                "exact_paths": exact_paths,
                "expected_paths": expected,
                "fusion_paths": fusion_paths,
                "lexical_paths": lexical_paths,
                "query_class": query["query_class"],
                "top_similarity": top_similarity,
            }
        )
    answerable = [case for case in cases if case["expected_paths"]]

    def recall(channel: str, k: int) -> float:
        return sum(
            1
            for case in answerable
            if any(path in case["expected_paths"] for path in case[channel][:k])
        ) / len(answerable)

    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for case in answerable:
        ranks = [
            index + 1
            for index, path in enumerate(case["fusion_paths"])
            if path in case["expected_paths"]
        ]
        reciprocal_ranks.append(1.0 / ranks[0] if ranks else 0.0)
        relevance = [
            1.0 if path in case["expected_paths"] else 0.0 for path in case["fusion_paths"]
        ]
        dcg = sum(value / math.log2(index + 2) for index, value in enumerate(relevance))
        ideal_count = min(10, max(1, sum(1 for value in relevance if value)))
        idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
        ndcgs.append(dcg / idcg if idcg else 0.0)
    exact_case_ids = {"plsql-object", "path-and-function", "jira-id", "exact-semantic-conflict"}
    exact_cases = [case for case in cases if case["case_id"] in exact_case_ids]
    no_answer_cases = [case for case in cases if not case["expected_paths"]]
    abstained_cases = [case for case in cases if case["abstained"]]
    return {
        "cases": cases,
        "metrics": {
            "citation_precision": sum(case["citation_valid"] for case in cases) / len(cases),
            "dense_recall_at_1": recall("dense_paths", 1),
            "dense_recall_at_5": recall("dense_paths", 5),
            "dense_recall_at_10": recall("dense_paths", 10),
            "exact_top_1": sum(
                bool(case["exact_paths"]) and case["exact_paths"][0] in case["expected_paths"]
                for case in exact_cases
            )
            / len(exact_cases),
            "fusion_recall_at_1": recall("fusion_paths", 1),
            "fusion_recall_at_5": recall("fusion_paths", 5),
            "fusion_recall_at_10": recall("fusion_paths", 10),
            "lexical_recall_at_1": recall("lexical_paths", 1),
            "mrr": statistics.fmean(reciprocal_ranks),
            "ndcg_at_10": statistics.fmean(ndcgs),
            "no_answer_recall": sum(case["abstained"] for case in no_answer_cases)
            / len(no_answer_cases),
            "no_answer_precision": (
                sum(not case["expected_paths"] for case in abstained_cases) / len(abstained_cases)
                if abstained_cases
                else 1.0
            ),
            "query_p50_ms": _percentile(latencies, 0.50) * 1_000,
            "query_p95_ms": _percentile(latencies, 0.95) * 1_000,
        },
    }


def _tree_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _tree_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _activate(root: Path, generation: str, manifest_digest: str) -> None:
    document = {
        "generation": generation,
        "manifest_digest": manifest_digest,
        "schema": "zekam-index-active-pointer/v1",
    }
    temporary = root / "active.json.tmp"
    active = root / "active.json"
    temporary.write_text(
        json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, active)


def run_bakeoff(
    *, candidate: str, corpus_path: Path, root: Path, target_count: int, rebuild: bool
) -> dict[str, Any]:
    if candidate not in {"lancedb", "zvec", "sqlite-vec"}:
        raise ValueError("unsupported knowledge candidate")
    if type(target_count) is not int or target_count < 100:
        raise ValueError("target_count must be an integer >= 100")
    if root.exists():
        raise ValueError("bake-off root must not exist")
    root.mkdir(parents=True)
    corpus = load_corpus(corpus_path)
    generation_one = root / "generation-0001"
    started = time.perf_counter()
    with _deny_network() as network_attempts:
        adapter = _adapter(candidate, generation_one, create=True)
        adapter.build(_record_stream(corpus["chunks"], target_count), count=target_count)
        build_seconds = time.perf_counter() - started
        if adapter.count() != target_count:
            raise ValueError("candidate row count mismatch after build")
        update_record = next(_record_stream(corpus["chunks"], 1))
        update_record = IndexRecord(
            record_id="update-proof",
            project_id=update_record.project_id,
            source_path=update_record.source_path,
            source_digest=update_record.source_digest,
            text=update_record.text,
            vector=update_record.vector,
        )
        update_started = time.perf_counter()
        adapter.add(update_record)
        update_seconds = time.perf_counter() - update_started
        if adapter.count() != target_count + 1:
            raise ValueError("candidate update count mismatch")
        evaluation = _evaluate(adapter, corpus)
        adapter.close()

        manifest_one = _tree_manifest(generation_one)
        manifest_one_digest = canonical_digest(manifest_one)
        _activate(root, "generation-0001", manifest_one_digest)
        restarted = _adapter(candidate, generation_one, create=False)
        restart_evaluation = _evaluate(restarted, corpus, repeat_queries=1)
        restarted.close()
        restart_parity = [case["dense_paths"] for case in restart_evaluation["cases"]] == [
            case["dense_paths"] for case in evaluation["cases"]
        ]

        rebuild_seconds: float | None = None
        rebuild_parity: bool | None = None
        corruption_detected: bool | None = None
        if rebuild:
            generation_two = root / "generation-0002"
            rebuild_started = time.perf_counter()
            rebuilt = _adapter(candidate, generation_two, create=True)
            rebuilt.build(_record_stream(corpus["chunks"], target_count), count=target_count)
            rebuild_seconds = time.perf_counter() - rebuild_started
            rebuilt_evaluation = _evaluate(rebuilt, corpus, repeat_queries=1)
            rebuilt.close()
            rebuild_parity = [case["dense_paths"] for case in rebuilt_evaluation["cases"]] == [
                case["dense_paths"] for case in evaluation["cases"]
            ]
            before = _tree_manifest(generation_two)
            corruptible = next(
                (
                    path
                    for path in sorted(generation_two.rglob("*"))
                    if path.is_file() and path.stat().st_size > 512
                ),
                None,
            )
            if corruptible is None:
                raise ValueError("candidate index has no corruptible file")
            with corruptible.open("r+b") as handle:
                handle.seek(128)
                original = handle.read(1)
                handle.seek(128)
                handle.write(bytes([original[0] ^ 0xFF]))
                handle.flush()
                os.fsync(handle.fileno())
            corruption_detected = canonical_digest(before) != canonical_digest(
                _tree_manifest(generation_two)
            )
    environment = {
        "machine": platform.machine().lower(),
        "platform": sys.platform,
        "python": platform.python_version(),
    }
    current_platform = current_acceptance_platform()
    metrics = evaluation["metrics"]
    cross_project_case = next(
        case for case in evaluation["cases"] if case["case_id"] == "cross-project-leakage"
    )
    leakage_prevented = all(
        path != "security-fixture/cross-project-decoy.txt"
        for path in cross_project_case["dense_paths"]
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate": candidate,
        "candidate_version": adapter.version,
        "corpus_digest": corpus["corpus_digest"],
        "dimension": DIMENSION,
        "distance_metric": "cosine",
        "environment": environment,
        "target_count": target_count,
        "unique_real_chunk_count": len(corpus["chunks"]),
        "scale_profile": "cyclic-repetition-of-real-bge-embedded-zekam-chunks",
        "indexed_count": target_count + 1,
        "build_seconds": build_seconds,
        "update_seconds": update_seconds,
        "rebuild_seconds": rebuild_seconds,
        "disk_bytes": _tree_size(generation_one),
        "network_attempts": network_attempts["count"],
        "sqlite_runtime": (
            {
                "journal_mode": getattr(adapter, "journal_mode", None),
                "writer_coordination": getattr(adapter, "writer_coordination", None),
                "wal_reset_safe": assess_sqlite_wal_safety(
                    sqlite3.sqlite_version
                ).safe_for_multi_connection_wal,
            }
            if candidate == "sqlite-vec"
            else None
        ),
        "quality": evaluation,
        "restart_parity": restart_parity,
        "rebuild_parity": rebuild_parity,
        "corruption_detected": corruption_detected,
        "generation_activation": {
            "active": "generation-0001",
            "manifest_digest": manifest_one_digest,
            "partial_generation_queryable": False,
        },
        "hard_gates": {
            "citation_binding": metrics["citation_precision"] == 1.0,
            "corruption_detection": corruption_detected is True if rebuild else None,
            "cross_project_filter_before_limit": leakage_prevented,
            "hidden_network_calls": network_attempts["count"] == 0,
            "macos_arm64": current_platform == "macos-arm64",
            "persistent_restart": restart_parity,
            "rebuild_from_manifest": rebuild_parity,
            "windows_x64": current_platform == "windows-x64",
            "safe_sqlite_journal_policy": (
                candidate != "sqlite-vec"
                or getattr(adapter, "journal_mode", None) == "wal"
                or (
                    getattr(adapter, "journal_mode", None) == "delete"
                    and getattr(adapter, "writer_coordination", None) == "single-writer-file-lock"
                )
            ),
        },
        "selection_status": "local-platform-measured-pending-cross-platform-merge",
    }
    result["artifact_digest"] = canonical_digest(result)
    return result


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=("lancedb", "zvec", "sqlite-vec"), required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target-count", type=int, required=True)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_bakeoff(
        candidate=args.candidate,
        corpus_path=args.corpus,
        root=args.root,
        target_count=args.target_count,
        rebuild=args.rebuild,
    )
    _write_result(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
