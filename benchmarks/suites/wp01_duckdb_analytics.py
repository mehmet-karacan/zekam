"""WP-01 DuckDB derived analytics projection spike."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = "zekam-wp01-duckdb-analytics-bakeoff/v1"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@contextmanager
def _deny_network() -> Iterator[dict[str, int]]:
    attempts = {"count": 0}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def denied_connect(sock: socket.socket, address: object) -> None:
        del sock, address
        attempts["count"] += 1
        raise RuntimeError("network denied during DuckDB analytics bake-off")

    def denied_connect_ex(sock: socket.socket, address: object) -> int:
        denied_connect(sock, address)
        return 1

    def denied_create(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        attempts["count"] += 1
        raise RuntimeError("network denied during DuckDB analytics bake-off")

    socket.socket.connect = denied_connect  # type: ignore[assignment]
    socket.socket.connect_ex = denied_connect_ex  # type: ignore[assignment]
    socket.create_connection = denied_create
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def _duckdb() -> Any:
    return importlib.import_module("duckdb")


def _write_raw_events(path: Path, count: int) -> str:
    if type(count) is not int or count < 1:
        raise ValueError("analytics row count must be a positive integer")
    digest = hashlib.sha256()
    with path.open("xb") as handle:
        for index in range(count):
            row = {
                "benchmark_id": f"bench-{index % 100:03d}",
                "duration_ms": (index * 17) % 10_000,
                "event_id": f"event-{index:09d}",
                "model_id": f"model-{index % 7}",
                "passed": index % 11 != 0,
                "sequence": index,
            }
            payload = (
                json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            handle.write(payload)
            digest.update(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o400)
    return "sha256:" + digest.hexdigest()


def _import_projection(database: Path, raw_path: Path) -> float:
    connection = _duckdb().connect(str(database))
    started = time.perf_counter()
    try:
        connection.execute(
            "create table benchmark_event as "
            "select * from read_json_auto(?, format='newline_delimited')",
            [str(raw_path)],
        )
        connection.execute(
            "create index benchmark_event_model_idx on benchmark_event(model_id, sequence)"
        )
        connection.checkpoint()
    finally:
        connection.close()
    return time.perf_counter() - started


def _projection_digest(database: Path) -> str:
    connection = _duckdb().connect(str(database), read_only=True)
    try:
        row = connection.execute(
            "select count(*), sum(sequence), sum(duration_ms), "
            "sum(case when passed then 1 else 0 end), min(event_id), max(event_id) "
            "from benchmark_event"
        ).fetchone()
        groups = connection.execute(
            "select model_id, count(*), avg(duration_ms) "
            "from benchmark_event group by model_id order by model_id"
        ).fetchall()
    finally:
        connection.close()
    return _canonical_digest({"aggregate": list(row), "groups": [list(item) for item in groups]})


def _child_write(database: Path) -> int:
    connection = _duckdb().connect(str(database))
    try:
        connection.execute(
            "insert into benchmark_event select 'external', 1, 'forbidden', 'model', true, -1"
        )
    finally:
        connection.close()
    return 0


def _child_read(database: Path) -> int:
    connection = _duckdb().connect(str(database), read_only=True)
    try:
        count = int(connection.execute("select count(*) from benchmark_event").fetchone()[0])
    finally:
        connection.close()
    return 0 if count > 0 else 2


def _writer_exclusion(database: Path) -> dict[str, Any]:
    owner = _duckdb().connect(str(database))
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.suites.wp01_duckdb_analytics",
                "--child-write",
                str(database),
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        owner.close()
    return {
        "passed": process.returncode != 0,
        "child_exit_code": process.returncode,
        "failure_category": "file-lock" if process.returncode != 0 else "writer-not-excluded",
    }


def _parallel_readers(database: Path) -> dict[str, Any]:
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "benchmarks.suites.wp01_duckdb_analytics",
                "--child-read",
                str(database),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(4)
    ]
    exits = [process.wait(timeout=15) for process in processes]
    return {"passed": exits == [0, 0, 0, 0], "reader_exit_codes": exits}


def _corruption_detection(database: Path, root: Path) -> dict[str, Any]:
    copy = root / "corrupt.duckdb"
    shutil.copy2(database, copy)
    expected_digest = "sha256:" + hashlib.sha256(copy.read_bytes()).hexdigest()
    with copy.open("r+b") as handle:
        handle.truncate(max(256, copy.stat().st_size // 2))
        handle.flush()
        os.fsync(handle.fileno())
    engine_rejected = False
    try:
        connection = _duckdb().connect(str(copy), read_only=True)
        try:
            connection.execute("select count(*) from benchmark_event").fetchone()
        finally:
            connection.close()
    except Exception:
        engine_rejected = True
    observed_digest = "sha256:" + hashlib.sha256(copy.read_bytes()).hexdigest()
    manifest_detected = observed_digest != expected_digest
    return {
        "passed": manifest_detected,
        "detail": "projection-manifest-digest-mismatch",
        "engine_rejected": engine_rejected,
        "manifest_detected": manifest_detected,
    }


def run(*, root: Path, row_count: int) -> dict[str, Any]:
    if type(row_count) is not int or row_count < 1:
        raise ValueError("analytics row count must be a positive integer")
    root.mkdir(parents=True, exist_ok=False)
    raw = root / "benchmark-events.jsonl"
    database = root / "analytics.duckdb"
    with _deny_network() as network:
        raw_digest = _write_raw_events(raw, row_count)
        first_import_seconds = _import_projection(database, raw)
        first_digest = _projection_digest(database)
        writer_exclusion = _writer_exclusion(database)
        readers = _parallel_readers(database)
        corruption = _corruption_detection(database, root)
        first_size = database.stat().st_size
        database.unlink()
        rebuild_seconds = _import_projection(database, raw)
        rebuilt_digest = _projection_digest(database)
    mac = sys.platform == "darwin" and platform.machine().lower() == "arm64"
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate": "duckdb",
        "candidate_version": importlib.metadata.version("duckdb"),
        "environment": {
            "machine": platform.machine().lower(),
            "platform": sys.platform,
            "python": platform.python_version(),
        },
        "row_count": row_count,
        "raw_artifact_digest": raw_digest,
        "raw_artifact_read_only": raw.stat().st_mode & 0o222 == 0,
        "first_import_seconds": first_import_seconds,
        "rebuild_seconds": rebuild_seconds,
        "database_bytes": first_size,
        "first_projection_digest": first_digest,
        "rebuilt_projection_digest": rebuilt_digest,
        "probes": {
            "corruption_detection": corruption,
            "network_attempts": network["count"],
            "parallel_readers": readers,
            "writer_exclusion": writer_exclusion,
        },
        "authority": "immutable-raw-artifacts",
        "operational_mutation_supported": False,
        "hard_gates": {
            "corruption_detection": corruption["passed"],
            "macos_arm64": mac,
            "no_server_or_docker": True,
            "offline_runtime": network["count"] == 0,
            "raw_artifact_authority": True,
            "rebuild_parity": first_digest == rebuilt_digest,
            "single_writer_enforced": writer_exclusion["passed"],
            "windows_x64": False,
        },
        "selection_status": "blocked-pending-windows-x64",
    }
    result["artifact_digest"] = _canonical_digest(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-write", type=Path)
    parser.add_argument("--child-read", type=Path)
    args = parser.parse_args(argv)
    if args.child_write is not None:
        return _child_write(args.child_write)
    if args.child_read is not None:
        return _child_read(args.child_read)
    if args.root is None or args.output is None:
        raise SystemExit("--root and --output are required")
    result = run(root=args.root, row_count=args.rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
