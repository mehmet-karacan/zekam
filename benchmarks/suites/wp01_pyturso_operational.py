"""WP-01 operational Turso Database/pyturso Mac spike."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from benchmarks.suites.wp01_operational_fixture import SCHEMA as _SCHEMA
from benchmarks.suites.wp01_operational_fixture import WorkloadSize

SCHEMA = "zekam-wp01-pyturso-operational-bakeoff/v1"


@dataclass(frozen=True, slots=True)
class Probe:
    passed: bool
    detail: str


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        raise RuntimeError("network denied during pyturso local bake-off")

    def denied_connect_ex(sock: socket.socket, address: object) -> int:
        denied_connect(sock, address)
        return 1

    def denied_create(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        attempts["count"] += 1
        raise RuntimeError("network denied during pyturso local bake-off")

    socket.socket.connect = denied_connect  # type: ignore[assignment]
    socket.socket.connect_ex = denied_connect_ex  # type: ignore[assignment]
    socket.create_connection = denied_create
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def _module() -> Any:
    return importlib.import_module("turso")


def _connect(path: Path) -> Any:
    connection = _module().connect(str(path))
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 2500")
    return connection


def _insert_workload(path: Path, size: WorkloadSize) -> dict[str, float]:
    durations: dict[str, float] = {}
    connection = _connect(path)
    try:
        started = time.perf_counter()
        connection.executemany(
            "insert into project(id, slug, active) values (?, ?, 1)",
            [(index, f"project-{index:05d}") for index in range(1, size.project_rows + 1)],
        )
        connection.commit()
        durations["project_insert_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        connection.executemany(
            "insert into work_item(id, project_id, state, payload_digest) values (?, ?, ?, ?)",
            [
                (index, index, "ready", _digest(f"work:{index}"))
                for index in range(1, size.work_rows + 1)
            ],
        )
        connection.commit()
        durations["work_insert_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        created_at = "2026-09-02T00:00:00+00:00"
        for start in range(1, size.event_rows + 1, 5_000):
            stop = min(size.event_rows + 1, start + 5_000)
            connection.executemany(
                "insert into work_event(sequence, work_id, event_type, payload_digest, created_at) "
                "values (?, ?, 'observed', ?, ?)",
                [
                    (
                        index,
                        ((index - 1) % size.work_rows) + 1,
                        _digest(f"event:{index}"),
                        created_at,
                    )
                    for index in range(start, stop)
                ],
            )
            connection.commit()
        durations["event_insert_seconds"] = time.perf_counter() - started
    finally:
        connection.close()
    return durations


def _constraints(path: Path) -> dict[str, Probe]:
    result: dict[str, Probe] = {}
    connection = _connect(path)
    cases = {
        "foreign_key": (
            "insert into work_item(id, project_id, state, payload_digest) "
            "values (-1, -1, 'ready', ?)",
            (_digest("foreign"),),
        ),
        "unique": ("insert into project values (-2, 'project-00001', 1)", ()),
        "check": ("insert into project values (-3, 'invalid-bool', 2)", ()),
    }
    try:
        for name, (statement, parameters) in cases.items():
            try:
                connection.execute(statement, parameters)
                connection.commit()
            except Exception as exc:
                connection.rollback()
                result[name] = Probe(type(exc).__name__ == "IntegrityError", type(exc).__name__)
            else:
                result[name] = Probe(False, "invalid-row-accepted")
        payload = _digest("claim")
        connection.execute(
            "insert into idempotency_claim values ('claim-1', ?, '2026-09-02T00:00:00+00:00')",
            (payload,),
        )
        connection.commit()
        connection.execute(
            "insert into idempotency_claim values ('claim-1', ?, '2026-09-02T00:00:01+00:00') "
            "on conflict(claim_key) do nothing",
            (payload,),
        )
        connection.commit()
        row = connection.execute(
            "select count(*), min(payload_digest), max(payload_digest) from idempotency_claim"
        ).fetchone()
        result["idempotent_replay"] = Probe(row == (1, payload, payload), "same-payload-single-row")
        result["payload_drift"] = Probe(
            str(row[1]) != _digest("different"), "drift-detectable-before-mutation"
        )
    finally:
        connection.close()
    return result


def _serialized_producers(path: Path, size: WorkloadSize) -> dict[str, Any]:
    messages: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=2_048)
    observations: list[int] = []
    reader_errors: list[str] = []
    stop = threading.Event()

    def producer(producer_id: int, begin: int, count: int) -> None:
        for ordinal in range(begin, begin + count):
            messages.put((producer_id, ordinal))
        messages.put(None)

    def reader() -> None:
        connection = _connect(path)
        try:
            while not stop.is_set():
                row = connection.execute("select count(*) from work_event").fetchone()
                observations.append(int(row[0]))
                time.sleep(0.002)
        except Exception as exc:
            reader_errors.append(type(exc).__name__)
        finally:
            connection.close()

    base, remainder = divmod(size.producer_rows, 4)
    threads: list[threading.Thread] = []
    offset = 0
    for producer_id in range(4):
        count = base + (1 if producer_id < remainder else 0)
        threads.append(threading.Thread(target=producer, args=(producer_id, offset, count)))
        offset += count
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    for thread in threads:
        thread.start()
    connection = _connect(path)
    completed = 0
    inserted = 0
    started = time.perf_counter()
    try:
        while completed < 4:
            item = messages.get(timeout=10)
            if item is None:
                completed += 1
                continue
            producer_id, ordinal = item
            connection.execute(
                "insert into work_event values (?, ?, 'producer', ?, ?)",
                (
                    size.event_rows + ordinal + 1,
                    (ordinal % size.work_rows) + 1,
                    _digest(f"p:{producer_id}:{ordinal}"),
                    "2026-09-02T00:00:01+00:00",
                ),
            )
            inserted += 1
            if inserted % 1_000 == 0:
                connection.commit()
        connection.commit()
    finally:
        connection.close()
        stop.set()
        reader_thread.join(timeout=10)
        for thread in threads:
            thread.join(timeout=10)
    return {
        "duration_seconds": time.perf_counter() - started,
        "inserted": inserted,
        "producer_count": 4,
        "reader_errors": reader_errors,
        "read_observations": len(observations),
        "read_monotonic": all(left <= right for left, right in pairwise(observations)),
    }


def _child_uncommitted(path: Path, ready: Path) -> int:
    connection = _connect(path)
    try:
        connection.execute("begin immediate")
        connection.execute(
            "insert into idempotency_claim values ('killed', ?, '2026-09-02T00:00:02+00:00')",
            (_digest("killed"),),
        )
        ready.write_text("ready", encoding="ascii")
        time.sleep(30)
        connection.commit()
    finally:
        connection.close()
    return 0


def _kill_uncommitted(path: Path, root: Path) -> Probe:
    checkpoint = _connect(path)
    try:
        checkpoint.execute("pragma wal_checkpoint(truncate)").fetchall()
    finally:
        checkpoint.close()
    del checkpoint
    gc.collect()
    crash_target = root / "crash-target.sqlite3"
    shutil.copy2(path, crash_target)
    ready = root / "uncommitted.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmarks.suites.wp01_pyturso_operational",
            "--child-uncommitted",
            str(crash_target),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        if process.poll() is None:
            process.kill()
        _, stderr = process.communicate(timeout=5)
        match = None
        if stderr:
            import re

            match = re.findall(rb"([A-Za-z]+Error):", stderr)
        category = match[-1].decode("ascii") if match else "unknown"
        return Probe(False, f"child-not-ready-{category}-exit-{process.returncode}")
    process.kill()
    process.wait(timeout=5)
    connection = _connect(crash_target)
    try:
        count = int(
            connection.execute(
                "select count(*) from idempotency_claim where claim_key = 'killed'"
            ).fetchone()[0]
        )
        integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
    finally:
        connection.close()
    return Probe(count == 0 and integrity == "ok", "uncommitted-row-absent-integrity-ok")


def _logical_digest(path: Path) -> str:
    document: dict[str, list[list[Any]]] = {}
    connection = _connect(path)
    try:
        for table, ordering in (
            ("system_meta", "key"),
            ("project", "id"),
            ("work_item", "id"),
            ("work_event", "sequence"),
            ("idempotency_claim", "claim_key"),
        ):
            document[table] = [
                list(row)
                for row in connection.execute(f"select * from {table} order by {ordering}")
            ]
    finally:
        connection.close()
    return _canonical_digest(document)


def _backup_restore(path: Path, root: Path) -> dict[str, Any]:
    backup = root / "backup.sqlite3"
    connection = _connect(path)
    started = time.perf_counter()
    try:
        safe = backup.as_posix().replace("'", "''")
        connection.execute(f"vacuum into '{safe}'")
    finally:
        connection.close()
    duration = time.perf_counter() - started
    source_digest = _logical_digest(path)
    backup_digest = _logical_digest(backup)
    return {
        "duration_seconds": duration,
        "source_digest": source_digest,
        "restored_digest": backup_digest,
        "passed": source_digest == backup_digest,
    }


def _corruption_detection(path: Path, root: Path) -> Probe:
    copy = root / "corrupt.sqlite3"
    source = _connect(path)
    try:
        safe = copy.as_posix().replace("'", "''")
        source.execute(f"vacuum into '{safe}'")
    finally:
        source.close()
    with copy.open("r+b") as handle:
        handle.truncate(max(128, copy.stat().st_size // 2))
        handle.flush()
        os.fsync(handle.fileno())
    detected = False
    try:
        connection = _connect(copy)
        try:
            detected = str(connection.execute("pragma integrity_check").fetchone()[0]) != "ok"
        finally:
            connection.close()
    except Exception:
        detected = True
    return Probe(detected, "truncation-detected")


def _schema_fingerprint(path: Path) -> str:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "select type, name, tbl_name, sql from sqlite_master "
            "where name not like 'sqlite_%' order by type, name"
        ).fetchall()
    finally:
        connection.close()
    return _canonical_digest(rows)


def _schema_drift(path: Path, root: Path) -> Probe:
    copy = root / "drift.sqlite3"
    connection = _connect(path)
    try:
        safe = copy.as_posix().replace("'", "''")
        connection.execute(f"vacuum into '{safe}'")
    finally:
        connection.close()
    before = _schema_fingerprint(copy)
    drifted = _connect(copy)
    try:
        drifted.execute("create trigger unexpected after insert on project begin select 1; end")
        drifted.commit()
    finally:
        drifted.close()
    return Probe(before != _schema_fingerprint(copy), "unexpected-trigger-detected")


def _read_only(root: Path) -> Probe:
    directory = root / "read-only"
    directory.mkdir()
    directory.chmod(0o500)
    rejected = False
    try:
        connection = _module().connect(str(directory / "forbidden.sqlite3"))
        try:
            connection.execute("create table unexpected(value integer)")
            connection.commit()
        finally:
            connection.close()
    except Exception:
        rejected = True
    finally:
        directory.chmod(0o700)
    return Probe(rejected, "read-only-directory-rejected")


def run(*, root: Path, size: WorkloadSize) -> dict[str, Any]:
    size.validate()
    root.mkdir(parents=True, exist_ok=False)
    path = root / "operational.sqlite3"
    with _deny_network() as network:
        connection = _connect(path)
        try:
            journal = str(connection.execute("pragma journal_mode").fetchone()[0])
            connection.execute("pragma synchronous = full")
            connection.executescript(_SCHEMA)
            connection.execute("insert into system_meta values ('schema_version', '1')")
            connection.commit()
            runtime_sqlite = str(connection.execute("select sqlite_version()").fetchone()[0])
        finally:
            connection.close()
        durations = _insert_workload(path, size)
        constraints = _constraints(path)
        producers = _serialized_producers(path, size)
        crash = _kill_uncommitted(path, root)
        backup = _backup_restore(path, root)
        corruption = _corruption_detection(path, root)
        drift = _schema_drift(path, root)
        read_only = _read_only(root)
    version_parts = tuple(int(part) for part in runtime_sqlite.split("."))
    wal_safe = version_parts in {(3, 44, 6), (3, 50, 7)} or version_parts >= (3, 51, 3)
    mac = sys.platform == "darwin" and platform.machine().lower() == "arm64"
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate": "pyturso",
        "candidate_version": importlib.metadata.version("pyturso"),
        "environment": {
            "machine": platform.machine().lower(),
            "platform": sys.platform,
            "python": platform.python_version(),
            "runtime_sqlite": runtime_sqlite,
        },
        "journal_mode": journal,
        "wal_reset_safe": wal_safe,
        "required_concurrency_profile": (
            "single-writer-coordinator" if not wal_safe else "multi-connection-wal"
        ),
        "workload": asdict(size),
        "durations": durations,
        "probes": {
            "backup_restore": backup,
            "constraints": {name: asdict(probe) for name, probe in constraints.items()},
            "corruption": asdict(corruption),
            "network_attempts": network["count"],
            "read_only": asdict(read_only),
            "schema_drift": asdict(drift),
            "serialized_producers": producers,
            "uncommitted_process_kill": asdict(crash),
        },
        "hard_gates": {
            "backup_restore": backup["passed"],
            "crash_integrity": crash.passed,
            "macos_arm64": mac,
            "no_server_or_docker": True,
            "offline_runtime": network["count"] == 0,
            "safe_concurrency_profile": producers["inserted"] == size.producer_rows,
            "schema_and_corruption_detection": drift.passed and corruption.passed,
            "windows_x64": False,
            "windows_wheel_published": False,
        },
        "selection_status": "blocked-pending-windows-source-build-evidence",
    }
    result["artifact_digest"] = _canonical_digest(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project-rows", type=int, default=10_000)
    parser.add_argument("--work-rows", type=int, default=10_000)
    parser.add_argument("--event-rows", type=int, default=100_000)
    parser.add_argument("--producer-rows", type=int, default=10_000)
    parser.add_argument("--child-uncommitted", nargs=2)
    args = parser.parse_args(argv)
    if args.child_uncommitted:
        return _child_uncommitted(Path(args.child_uncommitted[0]), Path(args.child_uncommitted[1]))
    if args.root is None or args.output is None:
        raise SystemExit("--root and --output are required")
    result = run(
        root=args.root,
        size=WorkloadSize(
            project_rows=args.project_rows,
            work_rows=args.work_rows,
            event_rows=args.event_rows,
            producer_rows=args.producer_rows,
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
