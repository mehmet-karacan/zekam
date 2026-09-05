"""Adversarial branch coverage for dormant V4 SQLite authority boundaries."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_source_authority import (
    FileIdentity,
    LocalBindingRevision,
)
from zekam.application.local_continuity_v4_writer import (
    CurrentSourceSnapshot,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import local_continuity_source_authority as authority_module
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    SQLiteLocalSourceAuthority,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)

pytestmark = pytest.mark.unit

NOW = "2026-09-03T12:00:00+00:00"
LATER = "2026-09-03T12:00:01+00:00"
EXPIRY = "2026-09-03T12:01:00+00:00"


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        "018f0000-0000-7000-8000-000000000001",
        "external",
        "018f0000-0000-7000-8000-000000000002",
        "018f0000-0000-7000-8000-000000000003",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000004",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


class _Source:
    def snapshot(self, binding: ContinuityBinding) -> CurrentSourceSnapshot:
        return CurrentSourceSnapshot(binding.source_snapshot_id, "HEAD", digest("source"))

    def resolve_fragment(self, *_values: object) -> object:
        raise AssertionError("not used")

    def assert_current(self, *_values: object) -> None:
        return None


class _Barrier:
    @contextlib.contextmanager
    def frozen(self, _value: object) -> Any:
        yield object()


def _writer(path: Path, **changes: object) -> SQLiteDormantV4CloseWriter:
    values: dict[str, object] = {
        "source": _Source(),
        "spool": _Barrier(),
        "projections": _Barrier(),
    }
    values.update(changes)
    return SQLiteDormantV4CloseWriter(path, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, 0, 30001, 1.5])
def test_writer_constructor_rejects_invalid_timeout(tmp_path: Path, timeout: object) -> None:
    with pytest.raises(ValidationFailed):
        _writer(tmp_path / "db.sqlite3", busy_timeout_ms=timeout)


def test_writer_constructor_rejects_relative_path_and_incomplete_ports(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        _writer(Path("relative.sqlite3"))
    for field in ("source", "spool", "projections"):
        with pytest.raises(ValidationFailed):
            _writer(tmp_path / "db.sqlite3", **{field: object()})


@pytest.mark.parametrize("kind", ["spool", "projections"])
@pytest.mark.parametrize("failure", [OSError("io"), TimeoutError("late")])
def test_writer_freeze_ports_sanitize_operating_failures(
    tmp_path: Path, kind: str, failure: BaseException
) -> None:
    class BrokenContext:
        def __enter__(self) -> object:
            raise failure

        def __exit__(self, *_values: object) -> None:
            return None

    class Broken:
        def frozen(self, _value: object) -> BrokenContext:
            return BrokenContext()

    writer = _writer(tmp_path / "db.sqlite3", **{kind: Broken()})
    context = (
        writer._frozen_spool(_binding())
        if kind == "spool"
        else writer._frozen_projections(object())  # type: ignore[arg-type]
    )
    with pytest.raises(PolicyViolation, match="evidence unavailable"), context:
        pass


def test_writer_freeze_ports_preserve_policy_violation(tmp_path: Path) -> None:
    marker = PolicyViolation("specific")

    class BrokenContext:
        def __enter__(self) -> object:
            raise marker

        def __exit__(self, *_values: object) -> None:
            return None

    class Broken:
        def frozen(self, _value: object) -> BrokenContext:
            return BrokenContext()

    writer = _writer(tmp_path / "db.sqlite3", spool=Broken(), projections=Broken())
    with pytest.raises(PolicyViolation, match="specific"), writer._frozen_spool(_binding()):
        pass
    with (
        pytest.raises(PolicyViolation, match="specific"),
        writer._frozen_projections(object()),  # type: ignore[arg-type]
    ):
        pass


def test_writer_connect_rejects_missing_and_symlink_database(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "missing.sqlite3")
    with pytest.raises(ConfigurationError):
        writer._connect()
    victim = tmp_path / "victim.sqlite3"
    sqlite3.connect(victim).close()
    writer.path.symlink_to(victim)
    with pytest.raises(ConfigurationError):
        writer._connect(read_only=True)


def test_writer_source_port_type_and_failure_boundaries(tmp_path: Path) -> None:
    class Wrong(_Source):
        def snapshot(self, _binding: ContinuityBinding) -> Any:
            return object()

    class Failing(_Source):
        def snapshot(self, _binding: ContinuityBinding) -> CurrentSourceSnapshot:
            raise OSError("secret path")

        def assert_current(self, *_values: object) -> None:
            raise TimeoutError("secret path")

    with pytest.raises(ValidationFailed):
        _writer(tmp_path / "db", source=Wrong())._source_snapshot(_binding())
    writer = _writer(tmp_path / "db", source=Failing())
    with pytest.raises(PolicyViolation, match="snapshot unavailable"):
        writer._source_snapshot(_binding())
    with pytest.raises(PolicyViolation, match="current source unavailable"):
        writer._assert_source_current(
            _binding(), CurrentSourceSnapshot(_binding().source_snapshot_id, "HEAD", digest("x"))
        )


def _row(db: sqlite3.Connection, values: dict[str, object]) -> sqlite3.Row:
    names = tuple(values)
    return cast(
        sqlite3.Row,
        db.execute(
            "select " + ",".join(f"? as {name}" for name in names), tuple(values.values())
        ).fetchone(),
    )


def test_writer_revision_verifier_accepts_exact_and_rejects_null_and_drift() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    body = {"revision_digest": "", "state": "hydrated", "revision_number": 1}
    body["revision_digest"] = digest({"state": "hydrated", "revision_number": 1})
    valid = _row(db, {**body, "body_json": canonical_json(body)})
    assert SQLiteDormantV4CloseWriter._verified_revision(valid) is valid
    with pytest.raises(PolicyViolation, match="missing"):
        SQLiteDormantV4CloseWriter._verified_revision(None)
    for changed in (
        {**body, "body_json": "{}"},
        {**body, "revision_digest": digest("wrong"), "body_json": canonical_json(body)},
        {**body, "body_json": b"not-text"},
    ):
        with pytest.raises(PolicyViolation):
            SQLiteDormantV4CloseWriter._verified_revision(_row(db, changed))
    db.close()


@pytest.mark.parametrize(
    "value",
    [None, 1, "", " padded", "x" * 513],
)
def test_writer_runtime_identity_rejects_wrong_or_unbounded_values(value: object) -> None:
    with pytest.raises(PolicyViolation):
        SQLiteDormantV4CloseWriter._runtime_identity(value, "owner")


@pytest.mark.parametrize(
    "value",
    [None, "not-time", "2026-09-03T12:00:00", "2026-09-03T15:00:00+03:00"],
)
def test_writer_runtime_time_rejects_wrong_noncanonical_values(value: object) -> None:
    with pytest.raises(PolicyViolation):
        SQLiteDormantV4CloseWriter._runtime_time(value, "job")
    assert SQLiteDormantV4CloseWriter._runtime_time(NOW, "job").isoformat() == NOW


def _delivery_db() -> tuple[sqlite3.Connection, sqlite3.Row]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "create table local_outbox_delivery(outbox_id text,state text,updated_at text,"
        "claim_id text,owner_id text,owner_pid,owner_token text,expires_at text,"
        "fencing_counter);"
        "create table local_outbox_receipt(id text,outbox_id text,claim_id text,"
        "fencing_token,evidence_digest text,status text,created_at text);"
        "create table local_recovery_case(id text,job_id text,outbox_id text,"
        "effect_claim_id text,case_kind text,evidence_digest text,state text,"
        "created_at text,resolved_at text);"
        "create table local_recovery_resolution(recovery_case_id text,outcome text,"
        "created_at text);"
    )
    row = _row(db, {"id": "outbox", "job_id": "job", "created_at": NOW})
    return db, row


def test_writer_delivery_pending_and_incomplete_graph() -> None:
    db, row = _delivery_db()
    db.execute(
        "insert into local_outbox_delivery values(?,?,?,?,?,?,?,?,?)",
        ("outbox", "pending", NOW, None, None, None, None, None, 0),
    )
    assert (
        SQLiteDormantV4CloseWriter._delivery_state(
            db,
            row,
            trusted_now=SQLiteDormantV4CloseWriter._runtime_time(LATER, "now"),
            expected_pending_at=NOW,
        )
        == "pending"
    )
    db.execute("update local_outbox_delivery set fencing_counter=1")
    with pytest.raises(PolicyViolation, match="pending delivery drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            db, row, trusted_now=SQLiteDormantV4CloseWriter._runtime_time(LATER, "now")
        )
    db.execute("delete from local_outbox_delivery")
    with pytest.raises(PolicyViolation, match="incomplete"):
        SQLiteDormantV4CloseWriter._delivery_state(
            db, row, trusted_now=SQLiteDormantV4CloseWriter._runtime_time(LATER, "now")
        )
    db.close()


def test_writer_delivery_claimed_and_direct_terminal_causality() -> None:
    db, row = _delivery_db()
    claim = "018f0000-0000-7000-8000-000000000010"
    db.execute(
        "insert into local_outbox_delivery values(?,?,?,?,?,?,?,?,?)",
        ("outbox", "claimed", LATER, claim, "owner", 22, "token", EXPIRY, 1),
    )
    trusted = SQLiteDormantV4CloseWriter._runtime_time("2026-09-03T12:00:02+00:00", "now")
    assert SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted) == "claimed"
    db.execute("update local_outbox_delivery set state='delivered',updated_at=?", (LATER,))
    db.execute(
        "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
        (
            "018f0000-0000-7000-8000-000000000011",
            "outbox",
            claim,
            1,
            digest("evidence"),
            "delivered",
            LATER,
        ),
    )
    assert SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted) == "delivered"
    db.execute("update local_outbox_receipt set fencing_token=2")
    with pytest.raises(PolicyViolation, match="receipt drift"):
        SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted)
    db.close()


@pytest.mark.parametrize("resolved", [False, True])
def test_writer_delivery_unknown_recovery_open_and_resolved(resolved: bool) -> None:
    db, row = _delivery_db()
    claim = "018f0000-0000-7000-8000-000000000012"
    receipt = "018f0000-0000-7000-8000-000000000013"
    evidence = digest("unknown")
    recipe = digest(
        {
            "case_kind": "outbox-delivery-unknown",
            "outbox_id": "outbox",
            "claim_id": claim,
            "receipt_evidence": evidence,
        }
    )
    state = "delivered" if resolved else "recovery-required"
    updated = "2026-09-03T12:00:02+00:00" if resolved else LATER
    db.execute(
        "insert into local_outbox_delivery values(?,?,?,?,?,?,?,?,?)",
        ("outbox", state, updated, claim, "owner", 22, "token", EXPIRY, 1),
    )
    db.execute(
        "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
        (receipt, "outbox", claim, 1, evidence, "unknown", LATER),
    )
    db.execute(
        "insert into local_recovery_case values(?,?,?,?,?,?,?,?,?)",
        (
            "case",
            "job",
            "outbox",
            None,
            "outbox-delivery-unknown",
            recipe,
            "resolved" if resolved else "open",
            LATER,
            updated if resolved else None,
        ),
    )
    if resolved:
        db.execute(
            "insert into local_recovery_resolution values(?,?,?)",
            ("case", "delivered", updated),
        )
    trusted = SQLiteDormantV4CloseWriter._runtime_time("2026-09-03T12:00:03+00:00", "now")
    assert SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted) == state
    db.execute("update local_recovery_case set job_id='other'")
    with pytest.raises(PolicyViolation, match="recovery graph drift"):
        SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=trusted)
    db.close()


def test_writer_delivery_rejects_expired_claim_and_missing_terminal_receipt() -> None:
    db, row = _delivery_db()
    claim = "018f0000-0000-7000-8000-000000000014"
    db.execute(
        "insert into local_outbox_delivery values(?,?,?,?,?,?,?,?,?)",
        ("outbox", "claimed", LATER, claim, "owner", 22, "token", EXPIRY, 1),
    )
    after_expiry = SQLiteDormantV4CloseWriter._runtime_time("2026-09-03T12:02:00+00:00", "now")
    with pytest.raises(PolicyViolation, match="terminal evidence"):
        SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=after_expiry)
    db.execute("update local_outbox_delivery set state='failed'")
    with pytest.raises(PolicyViolation, match="receipt missing"):
        SQLiteDormantV4CloseWriter._delivery_state(db, row, trusted_now=after_expiry)
    db.close()


def test_writer_capacity_and_empty_tail_boundaries() -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(
        "create table local_runtime_config(singleton integer,max_pending_outbox integer);"
        "create table local_outbox_delivery(state text);"
        "insert into local_runtime_config values(1,2);"
        "insert into local_outbox_delivery values('pending');"
    )
    SQLiteDormantV4CloseWriter._capacity(db, 1)
    with pytest.raises(PolicyViolation, match="capacity"):
        SQLiteDormantV4CloseWriter._capacity(db, 2)
    db.execute("delete from local_runtime_config")
    with pytest.raises(PolicyViolation, match="capacity"):
        SQLiteDormantV4CloseWriter._capacity(db, 0)
    tail = SQLiteDormantV4CloseWriter._tail([])
    assert tail.sequence == 0 and tail.event_digest is None
    db.close()


def _authority_layout(tmp_path: Path) -> SQLiteLocalSourceAuthority:
    home = tmp_path / "home"
    (home / "yerel").mkdir(parents=True, mode=0o700)
    (home / "state").mkdir(mode=0o700)
    operational = home / "state" / "operational.db"
    operational.write_bytes(b"sqlite-placeholder")
    operational.chmod(0o600)
    return SQLiteLocalSourceAuthority(home, operational)


def test_source_authority_constructor_rejects_nonfixed_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        SQLiteLocalSourceAuthority(Path("relative"), Path("relative/state/operational.db"))
    home = tmp_path / "home"
    with pytest.raises(ValidationFailed, match="fixed operational"):
        SQLiteLocalSourceAuthority(home, tmp_path / "other.db")


def test_source_authority_bootstrap_restart_and_corrupt_schema(tmp_path: Path) -> None:
    store = _authority_layout(tmp_path)
    timestamp = "2026-09-04T00:00:00.000000Z"
    store._bootstrap(timestamp)
    original = store.path.read_bytes()
    store._bootstrap(timestamp)
    assert store.path.read_bytes() == original
    with sqlite3.connect(store.path) as db:
        db.execute("create table unexpected(value text)")
    assert store._classify(_candidate(store)) is None


def test_source_authority_validate_rejects_trailing_bytes_and_orphan_revision(
    tmp_path: Path,
) -> None:
    store = _authority_layout(tmp_path)
    store._bootstrap("2026-09-04T00:00:00.000000Z")
    with store.path.open("ab") as stream:
        stream.write(b"trailing")
    connect = cast(Callable[..., sqlite3.Connection], authority_module.__dict__["_connect"])
    with (
        closing(connect(store.path, readonly=True)) as db,
        pytest.raises(PolicyViolation, match="physical size"),
    ):
        authority_module._validate(db)
    store.path.write_bytes(store.path.read_bytes()[:-8])
    with sqlite3.connect(store.path) as db:
        local_instance_id = db.execute(
            "select local_instance_id from local_source_authority_meta"
        ).fetchone()[0]
    candidate = _candidate(store, local_instance_id=local_instance_id)
    with sqlite3.connect(store.path) as db:
        db.execute("pragma foreign_keys=on")
        values = cast(
            Callable[[LocalBindingRevision], tuple[object, ...]],
            authority_module.__dict__["_source_authority_revision_values"],
        )
        db.execute(
            "insert into local_source_binding_revision values("
            + ",".join("?" for _ in range(29))
            + ")",
            values(candidate),
        )
    with (
        closing(connect(store.path, readonly=True)) as db,
        pytest.raises(PolicyViolation, match="cardinality"),
    ):
        authority_module._validate(db, physical=False)


def test_source_authority_preflight_rejects_mode_sidefile_and_symlink(tmp_path: Path) -> None:
    store = _authority_layout(tmp_path)
    store._bootstrap("2026-09-04T00:00:00.000000Z")
    store.path.chmod(0o644)
    with pytest.raises(PolicyViolation, match="mode"):
        store._preflight(create=False)
    store.path.chmod(0o600)
    Path(str(store.path) + "-wal").write_bytes(b"")
    with pytest.raises(PolicyViolation, match="side file"):
        store._preflight(create=False)
    Path(str(store.path) + "-wal").unlink()
    original = store.path.with_suffix(".original")
    store.path.rename(original)
    store.path.symlink_to(original)
    with pytest.raises(PolicyViolation):
        store._preflight(create=False)


def test_source_authority_bootstrap_rejects_residue_census_and_content(tmp_path: Path) -> None:
    store = _authority_layout(tmp_path)
    for index in range(3):
        residue = store.path.parent / f".source-authority.sqlite3.bootstrap-{index}"
        residue.write_bytes(b"")
        residue.chmod(0o600)
    with pytest.raises(PolicyViolation, match="census"):
        store._bootstrap("2026-09-04T00:00:00.000000Z")
    for item in store.path.parent.glob(".source-authority.sqlite3.bootstrap-*"):
        item.unlink()
    residue = store.path.parent / ".source-authority.sqlite3.bootstrap-bad"
    residue.write_bytes(b"partial")
    residue.chmod(0o600)
    with pytest.raises(PolicyViolation, match="residue"):
        store._bootstrap("2026-09-04T00:00:00.000000Z")


def test_source_authority_bootstrap_short_write_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _authority_layout(tmp_path)
    module_os = cast(Any, authority_module.__dict__["os"])
    monkeypatch.setattr(module_os, "write", lambda *_values: 0)
    with pytest.raises(PolicyViolation, match="bootstrap failed"):
        store._bootstrap("2026-09-04T00:00:00.000000Z")
    assert not store.path.exists()
    assert not list(store.path.parent.glob(".source-authority.sqlite3.bootstrap-*"))


def _candidate(
    store: SQLiteLocalSourceAuthority, *, local_instance_id: str | None = None
) -> LocalBindingRevision:
    identity = FileIdentity(1, 2, os.geteuid(), os.getegid(), 0o100600, 1, 0)
    root = FileIdentity(3, 4, os.geteuid(), os.getegid(), 0o40700, 1, 0)
    return LocalBindingRevision(
        "device",
        local_instance_id or str(uuid4()),
        identity,
        digest("parents"),
        "018f0000-0000-7000-8000-000000000020",
        "018f0000-0000-7000-8000-000000000021",
        str(store.home),
        root,
        digest("portable"),
        None,
        1,
        "2026-09-04T00:00:00.000000Z",
    )


def test_source_authority_validated_candidate_rejects_body_and_column_drift() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with pytest.raises(PolicyViolation, match="body drift"):
        authority_module._validated_candidate(_row(db, {"body_blob": b"{}"}))
    with pytest.raises(PolicyViolation, match="body drift"):
        authority_module._validated_candidate(_row(db, {"body_blob": "not-bytes"}))
    db.close()


def test_source_authority_sync_closes_descriptors_when_second_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _authority_layout(tmp_path)
    store._bootstrap("2026-09-04T00:00:00.000000Z")
    module_os = cast(Any, authority_module.__dict__["os"])
    original = module_os.fsync
    calls = 0

    def fail_second(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("parent sync")
        original(descriptor)

    monkeypatch.setattr(module_os, "fsync", fail_second)
    with pytest.raises(OSError):
        store._sync()
    assert calls == 2
