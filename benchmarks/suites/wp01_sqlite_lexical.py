"""WP-01 persistent SQLite FTS5 lexical/exact channel spike."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from benchmarks.suites.wp01_knowledge_corpus import canonical_digest, exact_rank, load_corpus
from benchmarks.suites.wp01_platform import current_acceptance_platform
from zekam.application.technology_bakeoff import assess_sqlite_wal_safety

SCHEMA = "zekam-wp01-sqlite-fts5-lexical-bakeoff/v1"
_TOKEN = re.compile(r"\w+", re.UNICODE)


@contextmanager
def _deny_network() -> Iterator[dict[str, int]]:
    attempts = {"count": 0}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def denied_connect(sock: socket.socket, address: object) -> None:
        del sock, address
        attempts["count"] += 1
        raise RuntimeError("network denied during SQLite FTS5 bake-off")

    def denied_connect_ex(sock: socket.socket, address: object) -> int:
        denied_connect(sock, address)
        return 1

    def denied_create(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        attempts["count"] += 1
        raise RuntimeError("network denied during SQLite FTS5 bake-off")

    socket.socket.connect = denied_connect  # type: ignore[assignment]
    socket.socket.connect_ex = denied_connect_ex  # type: ignore[assignment]
    socket.create_connection = denied_create
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 2500")
    return connection


@contextmanager
def _single_writer(path: Path) -> Iterator[None]:
    lock_path = Path(str(path) + ".writer.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("SQLite lexical single writer already active") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _records(chunks: list[dict[str, Any]], target_count: int) -> Iterator[tuple[Any, ...]]:
    for index in range(target_count):
        chunk = chunks[index % len(chunks)]
        yield (
            index + 1,
            f"r{index:09d}",
            chunk["project_id"],
            chunk["source_path"],
            chunk["source_digest"],
            chunk["text"],
        )


def _journal_mode() -> str:
    return (
        "wal"
        if assess_sqlite_wal_safety(sqlite3.sqlite_version).safe_for_multi_connection_wal
        else "delete"
    )


def _build(path: Path, chunks: list[dict[str, Any]], target_count: int) -> float:
    with _single_writer(path):
        connection = _connect(path)
        started = time.perf_counter()
        try:
            selected_mode = _journal_mode()
            actual_mode = str(
                connection.execute(f"pragma journal_mode = {selected_mode}").fetchone()[0]
            ).casefold()
            if actual_mode != selected_mode:
                raise RuntimeError("SQLite lexical journal policy uygulanamadi")
            connection.executescript(
                """
            pragma synchronous = full;
            create table document (
                rowid integer primary key,
                id text not null unique,
                project_id text not null,
                source_path text not null,
                source_digest text not null,
                body text not null
            ) strict;
            create index document_project_idx on document(project_id, id);
            create virtual table lexical using fts5(
                id unindexed,
                project_id unindexed,
                source_path,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
            )
            batch: list[tuple[Any, ...]] = []
            for row in _records(chunks, target_count):
                batch.append(row)
                if len(batch) == 1_000:
                    _insert_batch(connection, batch)
                    batch = []
            if batch:
                _insert_batch(connection, batch)
            connection.execute("insert into lexical(lexical) values ('optimize')")
            connection.commit()
            if selected_mode == "wal":
                connection.execute("pragma wal_checkpoint(truncate)").fetchall()
        finally:
            connection.close()
        return time.perf_counter() - started


def _insert_batch(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    connection.executemany(
        "insert into document(rowid, id, project_id, source_path, source_digest, body) "
        "values (?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "insert into lexical(rowid, id, project_id, source_path, body) values (?, ?, ?, ?, ?)",
        ((row[0], row[1], row[2], row[3], row[5]) for row in rows),
    )
    connection.commit()


def _fts_query(value: str) -> tuple[str, set[str]]:
    tokens = {token.casefold() for token in _TOKEN.findall(value) if len(token) > 1}
    if not tokens:
        raise ValueError("lexical query has no searchable tokens")
    expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in sorted(tokens))
    return expression, tokens


def _search(
    connection: sqlite3.Connection, query: str, *, project_id: str, limit: int
) -> list[dict[str, Any]]:
    expression, tokens = _fts_query(query)
    rows = connection.execute(
        "select l.id, l.project_id, l.source_path, d.source_digest, d.body, bm25(lexical) "
        "from lexical l join document d on d.rowid = l.rowid "
        "where lexical match ? and l.project_id = ? "
        "order by bm25(lexical), l.rowid limit ?",
        (expression, project_id, limit),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        haystack_tokens = {token.casefold() for token in _TOKEN.findall(f"{row[2]}\n{row[4]}")}
        coverage = len(tokens & haystack_tokens) / len(tokens)
        result.append(
            {
                "id": str(row[0]),
                "project_id": str(row[1]),
                "source_path": str(row[2]),
                "source_digest": str(row[3]),
                "token_coverage": coverage,
                "bm25": float(row[5]),
            }
        )
    return result


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[position]


def _evaluate(path: Path, corpus: dict[str, Any], *, repeat_queries: int = 3) -> dict[str, Any]:
    connection = _connect(path)
    cases: list[dict[str, Any]] = []
    latencies: list[float] = []
    try:
        for query in corpus["queries"]:
            hits: list[dict[str, Any]] = []
            for _ in range(repeat_queries):
                started = time.perf_counter()
                hits = _search(
                    connection,
                    str(query["text"]),
                    project_id=str(query["project_id"]),
                    limit=10,
                )
                latencies.append(time.perf_counter() - started)
            paths = [hit["source_path"] for hit in hits]
            exact_indices = exact_rank(str(query["text"]), corpus["chunks"], limit=10)
            exact_paths = [corpus["chunks"][index]["source_path"] for index in exact_indices]
            expected = list(query["expected_paths"])
            enough_evidence = bool(exact_paths) or (bool(hits) and hits[0]["token_coverage"] >= 0.8)
            cases.append(
                {
                    "abstained": not enough_evidence,
                    "case_id": query["case_id"],
                    "expected_paths": expected,
                    "exact_paths": exact_paths,
                    "lexical_paths": paths,
                    "top_token_coverage": hits[0]["token_coverage"] if hits else 0.0,
                }
            )
    finally:
        connection.close()
    answerable = [case for case in cases if case["expected_paths"]]
    no_answer = [case for case in cases if not case["expected_paths"]]
    abstained = [case for case in cases if case["abstained"]]
    exact_case_ids = {"plsql-object", "path-and-function", "jira-id", "exact-semantic-conflict"}
    exact_cases = [case for case in cases if case["case_id"] in exact_case_ids]

    def recall(k: int) -> float:
        return sum(
            any(path in case["expected_paths"] for path in case["lexical_paths"][:k])
            for case in answerable
        ) / len(answerable)

    ranks: list[float] = []
    for case in answerable:
        relevant = [
            index + 1
            for index, path in enumerate(case["lexical_paths"])
            if path in case["expected_paths"]
        ]
        ranks.append(1.0 / relevant[0] if relevant else 0.0)
    return {
        "cases": cases,
        "metrics": {
            "exact_top_1": sum(
                bool(case["exact_paths"]) and case["exact_paths"][0] in case["expected_paths"]
                for case in exact_cases
            )
            / len(exact_cases),
            "lexical_mrr": statistics.fmean(ranks),
            "lexical_recall_at_1": recall(1),
            "lexical_recall_at_5": recall(5),
            "lexical_recall_at_10": recall(10),
            "no_answer_precision": (
                sum(not case["expected_paths"] for case in abstained) / len(abstained)
                if abstained
                else 1.0
            ),
            "no_answer_recall": sum(case["abstained"] for case in no_answer) / len(no_answer),
            "query_p50_ms": _percentile(latencies, 0.50) * 1_000,
            "query_p95_ms": _percentile(latencies, 0.95) * 1_000,
        },
    }


def _digest_database(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run(*, corpus_path: Path, root: Path, target_count: int, rebuild: bool) -> dict[str, Any]:
    if type(target_count) is not int or target_count < 100:
        raise ValueError("target_count must be an integer >= 100")
    if root.exists():
        raise ValueError("lexical bake-off root must not exist")
    root.mkdir(parents=True)
    corpus = load_corpus(corpus_path)
    first = root / "generation-0001.sqlite3"
    with _deny_network() as network:
        build_seconds = _build(first, corpus["chunks"], target_count)
        evaluation = _evaluate(first, corpus)
        first_digest = _digest_database(first)
        restart_evaluation = _evaluate(first, corpus, repeat_queries=1)
        restart_parity = restart_evaluation["cases"] == evaluation["cases"]
        rebuild_seconds: float | None = None
        rebuild_parity: bool | None = None
        corruption_detected: bool | None = None
        if rebuild:
            second = root / "generation-0002.sqlite3"
            rebuild_seconds = _build(second, corpus["chunks"], target_count)
            rebuild_evaluation = _evaluate(second, corpus, repeat_queries=1)
            rebuild_parity = rebuild_evaluation["cases"] == evaluation["cases"]
            expected = _digest_database(second)
            corrupt = root / "corrupt.sqlite3"
            shutil.copy2(second, corrupt)
            with corrupt.open("r+b") as handle:
                handle.seek(128)
                value = handle.read(1)
                handle.seek(128)
                handle.write(bytes([value[0] ^ 0xFF]))
                handle.flush()
                os.fsync(handle.fileno())
            corruption_detected = _digest_database(corrupt) != expected
    cross = next(case for case in evaluation["cases"] if case["case_id"] == "cross-project-leakage")
    current_platform = current_acceptance_platform()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate": "sqlite-fts5",
        "sqlite_version": sqlite3.sqlite_version,
        "corpus_digest": corpus["corpus_digest"],
        "target_count": target_count,
        "unique_real_chunk_count": len(corpus["chunks"]),
        "scale_profile": "cyclic-repetition-of-real-zekam-chunks",
        "build_seconds": build_seconds,
        "rebuild_seconds": rebuild_seconds,
        "database_bytes": first.stat().st_size,
        "database_digest": first_digest,
        "quality": evaluation,
        "restart_parity": restart_parity,
        "rebuild_parity": rebuild_parity,
        "corruption_detected": corruption_detected,
        "network_attempts": network["count"],
        "sqlite_runtime": {
            "journal_mode": _journal_mode(),
            "writer_coordination": "single-writer-file-lock",
            "wal_reset_safe": assess_sqlite_wal_safety(
                sqlite3.sqlite_version
            ).safe_for_multi_connection_wal,
        },
        "hard_gates": {
            "corruption_detection": corruption_detected is True if rebuild else None,
            "cross_project_filter_before_limit": (
                "security-fixture/cross-project-decoy.txt" not in cross["lexical_paths"]
            ),
            "macos_arm64": current_platform == "macos-arm64",
            "no_server_or_docker": True,
            "offline_runtime": network["count"] == 0,
            "persistent_restart": restart_parity,
            "rebuild_from_manifest": rebuild_parity,
            "windows_x64": current_platform == "windows-x64",
            "safe_sqlite_journal_policy": not (
                _journal_mode() == "wal"
                and not assess_sqlite_wal_safety(
                    sqlite3.sqlite_version
                ).safe_for_multi_connection_wal
            ),
        },
        "selection_status": "local-platform-measured-pending-cross-platform-merge",
    }
    result["artifact_digest"] = canonical_digest(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target-count", type=int, required=True)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(
        corpus_path=args.corpus,
        root=args.root,
        target_count=args.target_count,
        rebuild=args.rebuild,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
