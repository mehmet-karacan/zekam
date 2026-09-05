"""WP-01 operational SQLite spike with local durability fault probes.

The suite uses only CPython's bundled SQLite.  It does not install packages,
contact a provider, inspect PostgreSQL, or claim Windows execution from macOS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from benchmarks.suites.wp01_operational_fixture import SCHEMA as _SCHEMA
from benchmarks.suites.wp01_operational_fixture import WorkloadSize
from benchmarks.suites.wp01_platform import current_acceptance_platform
from zekam.application.technology_bakeoff import (
    assess_sqlite_wal_safety,
    canonical_json_digest,
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    passed: bool
    detail: str


class _NetworkDenied(RuntimeError):
    pass


@contextmanager
def _deny_network() -> Iterator[dict[str, int]]:
    attempts = {"count": 0}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def denied_connect(sock: socket.socket, address: object) -> None:
        del sock, address
        attempts["count"] += 1
        raise _NetworkDenied("network access denied by WP-01 bake-off")

    def denied_connect_ex(sock: socket.socket, address: object) -> int:
        denied_connect(sock, address)
        return 1

    def denied_create_connection(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        attempts["count"] += 1
        raise _NetworkDenied("network access denied by WP-01 bake-off")

    socket.socket.connect = denied_connect  # type: ignore[assignment]
    socket.socket.connect_ex = denied_connect_ex  # type: ignore[assignment]
    socket.create_connection = denied_create_connection
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 2500")
    return connection


def _bootstrap(path: Path, *, wal_safe: bool) -> dict[str, Any]:
    with _connect(path) as connection:
        requested_journal = "wal" if wal_safe else "delete"
        journal_mode = str(
            connection.execute(f"pragma journal_mode = {requested_journal}").fetchone()[0]
        )
        connection.execute("pragma synchronous = full")
        connection.executescript(_SCHEMA)
        connection.execute("insert into system_meta(key, value) values ('schema_version', '1')")
        connection.commit()
        foreign_keys = int(connection.execute("pragma foreign_keys").fetchone()[0])
        synchronous = int(connection.execute("pragma synchronous").fetchone()[0])
    return {
        "concurrency_profile": (
            "multi-connection-wal" if wal_safe else "single-writer-rollback-journal"
        ),
        "foreign_keys": foreign_keys,
        "journal_mode": journal_mode,
        "single_writer_coordinator": not wal_safe,
        "synchronous": synchronous,
    }


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_primary_workload(path: Path, size: WorkloadSize) -> dict[str, float]:
    durations: dict[str, float] = {}
    with _connect(path) as connection:
        started = time.perf_counter()
        connection.executemany(
            "insert into project(id, slug, active) values (?, ?, 1)",
            ((index, f"project-{index:05d}") for index in range(1, size.project_rows + 1)),
        )
        connection.commit()
        durations["project_insert_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        connection.executemany(
            "insert into work_item(id, project_id, state, payload_digest) values (?, ?, ?, ?)",
            (
                (index, index, "ready", _digest_text(f"work:{index}"))
                for index in range(1, size.work_rows + 1)
            ),
        )
        connection.commit()
        durations["work_insert_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        created_at = "2026-09-02T00:00:00+00:00"
        chunk_size = 5_000
        for start in range(1, size.event_rows + 1, chunk_size):
            stop = min(start + chunk_size, size.event_rows + 1)
            connection.executemany(
                "insert into work_event(sequence, work_id, event_type, payload_digest, created_at) "
                "values (?, ?, 'observed', ?, ?)",
                (
                    (
                        index,
                        ((index - 1) % size.work_rows) + 1,
                        _digest_text(f"event:{index}"),
                        created_at,
                    )
                    for index in range(start, stop)
                ),
            )
            connection.commit()
        durations["event_insert_seconds"] = time.perf_counter() - started
    return durations


def _constraint_probes(path: Path) -> dict[str, ProbeResult]:
    probes: dict[str, ProbeResult] = {}
    with _connect(path) as connection:
        cases = {
            "foreign_key": (
                "insert into work_item(id, project_id, state, payload_digest) "
                "values (-1, -1, 'ready', ?)",
                (_digest_text("foreign-key"),),
            ),
            "unique": (
                "insert into project(id, slug, active) values (-1, 'project-00001', 1)",
                (),
            ),
            "check": (
                "insert into project(id, slug, active) values (-2, 'invalid-bool', 2)",
                (),
            ),
        }
        for name, (statement, parameters) in cases.items():
            try:
                connection.execute(statement, parameters)
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                probes[name] = ProbeResult(True, "rejected-with-integrity-error")
            else:
                probes[name] = ProbeResult(False, "invalid-row-accepted")

        claim_key = "claim-1"
        payload = _digest_text("claim-payload")
        connection.execute(
            "insert into idempotency_claim(claim_key, payload_digest, created_at) values (?, ?, ?)",
            (claim_key, payload, "2026-09-02T00:00:00+00:00"),
        )
        connection.commit()
        connection.execute(
            "insert into idempotency_claim(claim_key, payload_digest, created_at) values (?, ?, ?) "
            "on conflict(claim_key) do nothing",
            (claim_key, payload, "2026-09-02T00:00:01+00:00"),
        )
        connection.commit()
        row = connection.execute(
            "select payload_digest from idempotency_claim where claim_key = ?", (claim_key,)
        ).fetchone()
        probes["idempotent_replay"] = ProbeResult(
            row is not None and row[0] == payload,
            "same-payload-remains-single-row",
        )
        drift = _digest_text("different-payload")
        probes["payload_drift"] = ProbeResult(
            row is not None and row[0] != drift,
            "different-payload-detected-before-mutation",
        )
    return probes


def _serialized_producers(
    path: Path, *, rows: int, first_sequence: int, work_rows: int
) -> dict[str, Any]:
    messages: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=2_048)
    producer_count = 4
    stop_reading = threading.Event()
    read_observations: list[int] = []
    errors: list[str] = []

    def producer(producer_id: int, count: int, offset: int) -> None:
        for local_index in range(count):
            messages.put((producer_id, offset + local_index))
        messages.put(None)

    def reader() -> None:
        try:
            with _connect(path, read_only=True) as connection:
                while not stop_reading.is_set():
                    row = connection.execute("select count(*) from work_event").fetchone()
                    read_observations.append(int(row[0]))
                    time.sleep(0.002)
        except (OSError, sqlite3.DatabaseError) as exc:
            errors.append(type(exc).__name__)

    base, remainder = divmod(rows, producer_count)
    producers: list[threading.Thread] = []
    cursor = 0
    for producer_id in range(producer_count):
        count = base + (1 if producer_id < remainder else 0)
        thread = threading.Thread(
            target=producer,
            args=(producer_id, count, cursor),
            name=f"wp01-producer-{producer_id}",
        )
        cursor += count
        producers.append(thread)
    read_thread = threading.Thread(target=reader, name="wp01-concurrent-reader")
    started = time.perf_counter()
    read_thread.start()
    for thread in producers:
        thread.start()
    completed = 0
    inserted = 0
    created_at = "2026-09-02T00:00:01+00:00"
    with _connect(path) as connection:
        while completed < producer_count:
            message = messages.get(timeout=10)
            if message is None:
                completed += 1
                continue
            producer_id, ordinal = message
            sequence = first_sequence + ordinal
            connection.execute(
                "insert into work_event(sequence, work_id, event_type, payload_digest, created_at) "
                "values (?, ?, 'producer', ?, ?)",
                (
                    sequence,
                    (ordinal % work_rows) + 1,
                    _digest_text(f"p:{producer_id}:{ordinal}"),
                    created_at,
                ),
            )
            inserted += 1
            if inserted % 1_000 == 0:
                connection.commit()
        connection.commit()
    for thread in producers:
        thread.join(timeout=10)
    stop_reading.set()
    read_thread.join(timeout=10)
    return {
        "duration_seconds": time.perf_counter() - started,
        "errors": errors,
        "inserted": inserted,
        "producer_count": producer_count,
        "read_observation_count": len(read_observations),
        "read_observation_monotonic": all(
            left <= right for left, right in pairwise(read_observations)
        ),
    }


def _child_uncommitted(path: Path, ready_path: Path) -> int:
    with _connect(path) as connection:
        connection.execute("begin immediate")
        connection.execute(
            "insert into idempotency_claim(claim_key, payload_digest, created_at) values (?, ?, ?)",
            ("killed-claim", _digest_text("killed"), "2026-09-02T00:00:02+00:00"),
        )
        ready_path.write_text("ready", encoding="ascii")
        time.sleep(30)
        connection.commit()
    return 0


def _child_backup(source_path: Path, target_path: Path, ready_path: Path) -> int:
    signalled = False

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal signalled
        del status, remaining, total
        if not signalled:
            ready_path.write_text("ready", encoding="ascii")
            signalled = True
        time.sleep(0.01)

    with _connect(source_path, read_only=True) as source, _connect(target_path) as target:
        source.backup(target, pages=1, progress=progress)
    return 0


def _kill_uncommitted_writer(path: Path, root: Path) -> ProbeResult:
    ready = root / "uncommitted.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmarks.suites.wp01_sqlite_operational",
            "--child-uncommitted",
            str(path),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        process.wait(timeout=5)
        return ProbeResult(False, "child-did-not-reach-uncommitted-state")
    process.kill()
    process.wait(timeout=5)
    with _connect(path) as connection:
        row = connection.execute(
            "select count(*) from idempotency_claim where claim_key = 'killed-claim'"
        ).fetchone()
        integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
    passed = int(row[0]) == 0 and integrity == "ok"
    return ProbeResult(passed, "uncommitted-row-absent-and-integrity-ok")


def _kill_snapshot_process(path: Path, root: Path) -> ProbeResult:
    ready = root / "snapshot.ready"
    partial = root / "partial-snapshot.sqlite3"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmarks.suites.wp01_sqlite_operational",
            "--child-backup",
            str(path),
            str(partial),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        process.wait(timeout=5)
        return ProbeResult(False, "snapshot-child-did-not-start-copy")
    process.kill()
    process.wait(timeout=5)
    with _connect(path, read_only=True) as connection:
        source_integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
    partial_is_complete = False
    if partial.exists():
        try:
            partial_is_complete = _logical_digest(partial) == _logical_digest(path)
        except sqlite3.DatabaseError:
            partial_is_complete = False
    return ProbeResult(
        source_integrity == "ok" and not partial_is_complete,
        "source-intact-and-partial-snapshot-not-publishable",
    )


def _disk_full_probe(root: Path) -> ProbeResult:
    path = root / "disk-full.sqlite3"
    with _connect(path) as connection:
        connection.execute("create table payload(value blob not null) strict")
        connection.commit()
        pages = int(connection.execute("pragma page_count").fetchone()[0])
        connection.execute(f"pragma max_page_count = {pages + 1}")
        rejected = False
        try:
            connection.execute("begin immediate")
            for _ in range(32):
                connection.execute("insert into payload(value) values (zeroblob(8192))")
            connection.commit()
        except sqlite3.OperationalError as exc:
            rejected = "full" in str(exc).lower()
            connection.rollback()
        integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
        count = int(connection.execute("select count(*) from payload").fetchone()[0])
    return ProbeResult(rejected and integrity == "ok" and count == 0, "full-rejected-rollback-ok")


def _read_only_directory_probe(root: Path) -> ProbeResult:
    directory = root / "read-only"
    directory.mkdir()
    if os.name == "nt":
        database = directory / "forbidden.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("create table payload(value integer)")
            connection.commit()
        os.chmod(database, stat.S_IREAD)
        rejected = False
        try:
            with sqlite3.connect(database) as connection:
                connection.execute("insert into payload(value) values (1)")
                connection.commit()
        except sqlite3.OperationalError:
            rejected = True
        finally:
            os.chmod(database, stat.S_IREAD | stat.S_IWRITE)
        return ProbeResult(rejected, "windows-read-only-file-write-rejected")
    directory.chmod(0o500)
    rejected = False
    try:
        connection = sqlite3.connect(directory / "forbidden.sqlite3")
        try:
            connection.execute("create table unexpected(value integer)")
            connection.commit()
        finally:
            connection.close()
    except sqlite3.OperationalError:
        rejected = True
    finally:
        directory.chmod(0o700)
    return ProbeResult(rejected, "read-only-directory-write-rejected")


def _child_windows_lock(path: Path) -> int:
    if os.name != "nt":
        return 4
    import msvcrt

    with path.open("r+b", buffering=0) as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 0
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    return 3


def _windows_file_lock_probe(root: Path) -> ProbeResult:
    if os.name != "nt":
        return ProbeResult(True, "not-applicable-non-windows")
    import msvcrt

    path = root / "windows-file-lock.bin"
    with path.open("w+b", buffering=0) as handle:
        handle.write(b"0")
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            child = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.suites.wp01_sqlite_operational",
                    "--child-windows-lock",
                    str(path),
                ],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                timeout=10,
                check=False,
            )
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    return ProbeResult(child.returncode == 0, f"child-exit-{child.returncode}")


def _atomic_replace_probe(root: Path) -> ProbeResult:
    target = root / "atomic-target.json"
    staging = root / "atomic-staging.json"
    target.write_text('{"generation":1}\n', encoding="ascii")
    staging.write_text('{"generation":2}\n', encoding="ascii")
    os.replace(staging, target)
    passed = target.read_text(encoding="ascii") == '{"generation":2}\n' and not staging.exists()
    return ProbeResult(passed, "replace-published-complete-generation")


def _schema_fingerprint(path: Path) -> str:
    with _connect(path, read_only=True) as connection:
        rows = connection.execute(
            "select type, name, tbl_name, sql from sqlite_master "
            "where name not like 'sqlite_%' order by type, name"
        ).fetchall()
    return canonical_json_digest(rows)


def _copy_via_backup(source_path: Path, target_path: Path) -> None:
    with _connect(source_path, read_only=True) as source, _connect(target_path) as target:
        source.backup(target)


def _schema_drift_probe(path: Path, root: Path) -> ProbeResult:
    copy = root / "schema-drift.sqlite3"
    _copy_via_backup(path, copy)
    before = _schema_fingerprint(copy)
    with _connect(copy) as connection:
        connection.execute(
            "create trigger unexpected_trigger after insert on project begin select 1; end"
        )
        connection.commit()
    after = _schema_fingerprint(copy)
    return ProbeResult(before != after, "unexpected-trigger-changes-schema-fingerprint")


def _corruption_probe(path: Path, root: Path) -> ProbeResult:
    copy = root / "corrupt.sqlite3"
    _copy_via_backup(path, copy)
    original_size = copy.stat().st_size
    with copy.open("r+b") as handle:
        handle.truncate(max(128, original_size // 2))
        handle.flush()
        os.fsync(handle.fileno())
    detected = False
    try:
        with _connect(copy, read_only=True) as connection:
            result = str(connection.execute("pragma integrity_check").fetchone()[0])
            detected = result != "ok"
    except sqlite3.DatabaseError:
        detected = True
    return ProbeResult(detected, "truncation-detected")


def _logical_digest(path: Path) -> str:
    payload: dict[str, list[list[Any]]] = {}
    with _connect(path, read_only=True) as connection:
        for table, ordering in (
            ("system_meta", "key"),
            ("project", "id"),
            ("work_item", "id"),
            ("work_event", "sequence"),
            ("idempotency_claim", "claim_key"),
        ):
            rows = connection.execute(f"select * from {table} order by {ordering}").fetchall()
            payload[table] = [list(row) for row in rows]
    return canonical_json_digest(payload)


def _backup_restore_probe(path: Path, root: Path) -> dict[str, Any]:
    backup = root / "backup.sqlite3"
    started = time.perf_counter()
    with _connect(path, read_only=True) as source, _connect(backup) as target:
        source.backup(target)
    duration = time.perf_counter() - started
    source_digest = _logical_digest(path)
    backup_digest = _logical_digest(backup)
    with _connect(backup, read_only=True) as connection:
        integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
    return {
        "duration_seconds": duration,
        "integrity": integrity,
        "passed": source_digest == backup_digest and integrity == "ok",
        "restored_digest": backup_digest,
        "source_digest": source_digest,
    }


def _row_counts(path: Path) -> dict[str, int]:
    with _connect(path, read_only=True) as connection:
        return {
            table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("project", "work_item", "work_event", "idempotency_claim")
        }


def run_sqlite_operational_bakeoff(
    *, root: Path, size: WorkloadSize | None = None
) -> dict[str, Any]:
    """Execute the macOS-local operational spike and return canonical evidence."""
    if size is None:
        size = WorkloadSize()
    size.validate()
    root.mkdir(parents=True, exist_ok=False)
    database = root / "operational.sqlite3"
    started_at = datetime.now(UTC).isoformat()
    wal_safety = assess_sqlite_wal_safety(sqlite3.sqlite_version)
    with _deny_network() as network_attempts:
        runtime = _bootstrap(database, wal_safe=wal_safety.safe_for_multi_connection_wal)
        durations = _insert_primary_workload(database, size)
        constraints = _constraint_probes(database)
        producers = _serialized_producers(
            database,
            rows=size.producer_rows,
            first_sequence=size.event_rows + 1,
            work_rows=size.work_rows,
        )
        crash = _kill_uncommitted_writer(database, root)
        snapshot_kill = _kill_snapshot_process(database, root)
        disk_full = _disk_full_probe(root)
        read_only = _read_only_directory_probe(root)
        windows_file_lock = _windows_file_lock_probe(root)
        atomic_replace = _atomic_replace_probe(root)
        schema_drift = _schema_drift_probe(database, root)
        corruption = _corruption_probe(database, root)
        backup = _backup_restore_probe(database, root)
        counts = _row_counts(database)
    machine = platform.machine().lower()
    current_platform = current_acceptance_platform()
    runtime_profile_safe = (
        runtime["journal_mode"] == "wal"
        if wal_safety.safe_for_multi_connection_wal
        else runtime["journal_mode"] != "wal" and runtime["single_writer_coordinator"] is True
    )
    local_probes_pass = (
        runtime_profile_safe
        and all(probe.passed for probe in constraints.values())
        and producers["inserted"] == size.producer_rows
        and not producers["errors"]
        and producers["read_observation_count"] > 0
        and producers["read_observation_monotonic"]
        and crash.passed
        and snapshot_kill.passed
        and disk_full.passed
        and read_only.passed
        and windows_file_lock.passed
        and atomic_replace.passed
        and schema_drift.passed
        and corruption.passed
        and backup["passed"]
        and network_attempts["count"] == 0
    )
    evidence: dict[str, Any] = {
        "schema": "zekam-wp01-sqlite-operational-bakeoff/v1",
        "candidate": "cpython-sqlite",
        "engine_kind": "operational",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "environment": {
            "machine": machine,
            "platform": sys.platform,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "runtime": runtime,
        "wal_safety": asdict(wal_safety),
        "workload": asdict(size),
        "durations": durations,
        "row_counts": counts,
        "probes": {
            "backup_restore": backup,
            "atomic_replace": asdict(atomic_replace),
            "constraints": {name: asdict(result) for name, result in constraints.items()},
            "corruption": asdict(corruption),
            "disk_full": asdict(disk_full),
            "network_attempts": network_attempts["count"],
            "read_only_directory": asdict(read_only),
            "schema_drift": asdict(schema_drift),
            "serialized_producers": producers,
            "snapshot_process_kill": asdict(snapshot_kill),
            "uncommitted_process_kill": asdict(crash),
            "windows_file_lock": asdict(windows_file_lock),
        },
        "executed_platforms": [current_platform] if current_platform is not None else [],
        "hard_gates": {
            "crash_integrity": crash.passed,
            "macos_arm64": current_platform == "macos-arm64" and local_probes_pass,
            "no_server_or_docker": True,
            "offline_runtime": network_attempts["count"] == 0,
            "persistent_local_state": backup["passed"],
            "rebuild_or_restore": backup["passed"],
            "reproducible_install": current_platform is not None,
            "windows_x64": current_platform == "windows-x64" and local_probes_pass,
        },
        "measured": True,
        "local_pass": local_probes_pass and current_platform is not None,
        "selection_status": "local-platform-measured-pending-cross-platform-merge",
    }
    evidence["artifact_digest"] = canonical_json_digest(evidence)
    return evidence


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        evidence,
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(payload + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--project-rows", type=int, default=10_000)
    parser.add_argument("--work-rows", type=int, default=10_000)
    parser.add_argument("--event-rows", type=int, default=100_000)
    parser.add_argument("--producer-rows", type=int, default=10_000)
    parser.add_argument("--child-uncommitted", nargs=2, metavar=("DATABASE", "READY"))
    parser.add_argument("--child-windows-lock", type=Path)
    parser.add_argument(
        "--child-backup",
        nargs=3,
        metavar=("SOURCE", "TARGET", "READY"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.child_uncommitted is not None:
        database, ready = args.child_uncommitted
        return _child_uncommitted(Path(database), Path(ready))
    if args.child_windows_lock is not None:
        return _child_windows_lock(args.child_windows_lock)
    if args.child_backup is not None:
        source, target, ready = args.child_backup
        return _child_backup(Path(source), Path(target), Path(ready))
    if args.output is None:
        raise SystemExit("--output is required")
    size = WorkloadSize(
        project_rows=args.project_rows,
        work_rows=args.work_rows,
        event_rows=args.event_rows,
        producer_rows=args.producer_rows,
    )
    if args.root is None:
        with tempfile.TemporaryDirectory(prefix="zekam-wp01-sqlite-") as temporary:
            evidence = run_sqlite_operational_bakeoff(root=Path(temporary) / "run", size=size)
    else:
        evidence = run_sqlite_operational_bakeoff(root=args.root, size=size)
    _write_evidence(args.output, evidence)
    return 0 if evidence["local_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
