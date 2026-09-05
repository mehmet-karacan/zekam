"""Independent adversarial checks for the dormant operational-v4 migrator."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.application.operational_store import OperationalBackupReceipt
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite import operational_backup
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema

pytestmark = pytest.mark.unit


class _Admission:
    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.cwd()

    def stop_new_admission(self) -> None:
        pass

    def drain_and_reap(self) -> None:
        pass

    def assert_no_admitted_authority(self) -> None:
        pass

    def release_admission(self) -> None:
        pass

    def mark_recovery_required(self) -> None:
        pass

    def trusted_home(self) -> Path:
        return self._home


def _migrate(path: Path, backup: Path, lock: Path) -> None:
    migration.migrate_v3_to_v4(
        path,
        backup,
        migration_lock=lock,
        admission=_Admission(path.parent),
        spool_targets=(),
    )


def test_migration_lock_hardlink_never_mutates_external_victim(tmp_path: Path) -> None:
    victim = tmp_path / "external-victim"
    lock = tmp_path / "migration.lock"
    victim.write_bytes(b"")
    os.link(victim, lock)

    with (
        pytest.raises(ConfigurationError, match=r"lock|identity|link"),
        migration._migration_lock(lock),
    ):
        pytest.fail("hard-linked migration lock was admitted")

    assert victim.read_bytes() == b""
    assert victim.stat().st_nlink == 2


def test_hardlinked_database_is_rejected_without_mutating_other_name(tmp_path: Path) -> None:
    original = tmp_path / "external-original.db"
    alias = tmp_path / "operational.db"
    schema.bootstrap(original)
    os.link(original, alias)
    before = original.read_bytes()

    with pytest.raises(ConfigurationError, match=r"link|identity|regular"):
        _migrate(alias, tmp_path / "backup.db", tmp_path / "migration.lock")

    assert original.read_bytes() == before
    assert schema.status(original).schema_version == 3


def test_lock_parent_swap_cannot_redirect_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_parent = tmp_path / "safe"
    displaced_parent = tmp_path / "displaced"
    attacker_parent = tmp_path / "attacker"
    safe_parent.mkdir()
    attacker_parent.mkdir()
    lock = safe_parent / "migration.lock"
    migration_os = cast(Any, migration).os
    real_open = migration_os.open
    swapped = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if Path(path) in {lock, Path(lock.name)} and not swapped:
            swapped = True
            safe_parent.rename(displaced_parent)
            os.symlink(attacker_parent, safe_parent)
        if dir_fd is None:
            return int(real_open(path, flags, mode))
        return int(real_open(path, flags, mode, dir_fd=dir_fd))

    monkeypatch.setattr(migration_os, "open", racing_open)
    with (
        pytest.raises(ConfigurationError, match=r"parent|identity|drift|unsafe|anchored"),
        migration._migration_lock(lock),
    ):
        pytest.fail("ancestor-swapped migration lock was admitted")

    assert not (attacker_parent / "migration.lock").exists()


def test_database_parent_swap_cannot_create_redirected_writer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_parent = tmp_path / "safe"
    displaced_parent = tmp_path / "displaced"
    attacker_parent = tmp_path / "attacker"
    backup_parent = tmp_path / "backups"
    safe_parent.mkdir()
    attacker_parent.mkdir()
    backup_parent.mkdir()
    database = safe_parent / "operational.db"
    schema.bootstrap(database)
    original_connect = migration._connect_existing_writer
    swapped = False

    def racing_connect(path: Path) -> sqlite3.Connection:
        nonlocal swapped
        if path == database and not swapped:
            swapped = True
            safe_parent.rename(displaced_parent)
            os.symlink(attacker_parent, safe_parent)
        return original_connect(path)

    monkeypatch.setattr(migration, "_connect_existing_writer", racing_connect)
    with pytest.raises(ConfigurationError, match=r"identity|drift|anchored|path|existing writer"):
        migration.migrate_v3_to_v4(
            database,
            backup_parent / "before-v4.db",
            migration_lock=tmp_path / "migration.lock",
            admission=_Admission(tmp_path),
            spool_targets=(),
        )

    assert schema.status(displaced_parent / "operational.db").schema_version == 3
    assert not (attacker_parent / "operational.db").exists()


def test_backup_parent_swap_never_writes_to_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    schema.bootstrap(source)
    safe_parent = tmp_path / "safe"
    displaced_parent = tmp_path / "displaced"
    attacker_parent = tmp_path / "attacker"
    safe_parent.mkdir()
    attacker_parent.mkdir()
    destination = safe_parent / "backup.db"
    original_write = operational_backup._write_all
    swapped = False

    def racing_write(descriptor: int, content: bytes) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            safe_parent.rename(displaced_parent)
            os.symlink(attacker_parent, safe_parent)
        original_write(descriptor, content)

    monkeypatch.setattr(operational_backup, "_write_all", racing_write)
    with pytest.raises(ConfigurationError):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()
    assert list(attacker_parent.iterdir()) == []


def test_backup_content_drift_before_writer_aborts_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "operational.db"
    backup = tmp_path / "before-v4.db"
    schema.bootstrap(database)
    original = operational_backup.SQLiteOperationalBackup.create_backup_anchored

    def tamper_after_backup(
        adapter: operational_backup.SQLiteOperationalBackup,
        destination: str,
        *,
        parent_descriptor: int,
    ) -> OperationalBackupReceipt:
        receipt = original(adapter, destination, parent_descriptor=parent_descriptor)
        with sqlite3.connect(backup) as connection:
            connection.execute("insert into zekam_meta values('tamper','after-backup')")
        return receipt

    monkeypatch.setattr(
        operational_backup.SQLiteOperationalBackup,
        "create_backup_anchored",
        tamper_after_backup,
    )
    with pytest.raises(ConfigurationError, match=r"backup|digest|drift|changed"):
        _migrate(database, backup, tmp_path / "migration.lock")

    assert schema.status(database).schema_version == 3


def test_backup_temporary_hardlink_race_never_publishes_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    external_alias = tmp_path / "external-alias.db"
    schema.bootstrap(source)
    original_write = operational_backup._write_all
    linked = False

    def racing_write(descriptor: int, content: bytes) -> None:
        nonlocal linked
        if not linked:
            linked = True
            candidates = list(tmp_path.glob(".backup.db.partial-*"))
            assert len(candidates) == 1
            candidate = candidates[0]
            os.link(candidate, external_alias)
        original_write(descriptor, content)

    monkeypatch.setattr(operational_backup, "_write_all", racing_write)
    with pytest.raises(ConfigurationError, match=r"link|identity|artifact"):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()
    assert external_alias.is_file()


def test_corrupted_anchored_write_is_verified_from_persisted_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    schema.bootstrap(source)
    original_write = operational_backup._write_all

    def corrupt_write(descriptor: int, content: bytes) -> None:
        assert content.startswith(b"SQLite format 3\x00")
        original_write(descriptor, b"X" + content[1:])

    monkeypatch.setattr(operational_backup, "_write_all", corrupt_write)
    with pytest.raises(ConfigurationError, match=r"serialized|integrity|parity|header"):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()


def test_anchored_backup_retries_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    schema.bootstrap(source)
    backup_os = cast(Any, operational_backup).os
    original_write = backup_os.write

    def short_write(descriptor: int, content: bytes | memoryview) -> int:
        return int(original_write(descriptor, content[:17]))

    monkeypatch.setattr(backup_os, "write", short_write)
    operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert schema.status(destination).schema_ok


def test_anchored_backup_zero_write_leaves_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    schema.bootstrap(source)
    backup_os = cast(Any, operational_backup).os
    monkeypatch.setattr(backup_os, "write", lambda _fd, _content: 0)

    with pytest.raises(ConfigurationError, match="write failed"):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()


def test_publication_fsync_failure_unlinks_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    schema.bootstrap(source)
    backup_os = cast(Any, operational_backup).os
    original_fsync = backup_os.fsync

    def fail_published_fsync(descriptor: int) -> None:
        if os.fstat(descriptor).st_nlink == 2:
            raise OSError("injected publication fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(backup_os, "fsync", fail_published_fsync)
    with pytest.raises(OSError, match="publication fsync"):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()


def test_backup_snapshot_size_cap_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    schema.bootstrap(source)
    monkeypatch.setattr(operational_backup, "_MAX_SNAPSHOT_BYTES", 1)

    with pytest.raises(
        ConfigurationError, match=r"snapshot too large|size out of bounds|integrity/schema"
    ):
        operational_backup.SQLiteOperationalBackup(source).create_backup(str(destination))

    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO regression")
def test_fifo_database_rejection_is_nonblocking(tmp_path: Path) -> None:
    fifo = tmp_path / "operational.fifo"
    os.mkfifo(fifo)
    script = r"""
import sys
from pathlib import Path
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite.operational_migration import migrate_v3_to_v4

class Admission:
    def stop_new_admission(self): pass
    def drain_and_reap(self): pass
    def assert_no_admitted_authority(self): pass
    def release_admission(self): pass
    def mark_recovery_required(self): pass
    def trusted_home(self): return Path(sys.argv[4])

try:
    migrate_v3_to_v4(
        Path(sys.argv[1]), Path(sys.argv[2]), migration_lock=Path(sys.argv[3]),
        admission=Admission(), spool_targets=(),
    )
except ConfigurationError:
    raise SystemExit(0)
raise SystemExit(2)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(fifo),
            str(tmp_path / "backup.db"),
            str(tmp_path / "migration.lock"),
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
