"""Gercek PostgreSQL uzerinde migration upgrade, head ve drift davranisi."""

from __future__ import annotations

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.config import DatabaseSettings
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import connect

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def blank_database(postgres_settings: DatabaseSettings):  # type: ignore[no-untyped-def]
    """Bos, migration uygulanmamis gecici veritabani."""
    name = f"zekam_mig_{secrets.token_hex(6)}"
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{name}"')
    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    try:
        yield scoped
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (name,),
            )
            cursor.execute(f'drop database if exists "{name}"')


def test_clean_upgrade_applies_every_migration(blank_database: DatabaseSettings) -> None:
    available = migrations.discover_migrations()
    with connect(blank_database) as connection:
        applied = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert [result.version for result in applied] == [m.version for m in available]
    assert current.head == available[-1].version
    assert current.pending == ()
    assert current.drift == ()
    assert current.is_current


def test_upgrade_is_idempotent(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        second = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert second == ()
    assert current.is_current


def _rollback_targets() -> list[int]:
    """Son uc migration'in surum numarasi.

    Sabit numara yazmak her yeni migration'da bu testleri kirdigi icin hedefler
    kesif sonucundan turetilir.
    """

    versions = [item.version for item in migrations.discover_migrations()]
    return versions[-3:]


@pytest.mark.parametrize("version", _rollback_targets())
def test_down_marks_pending_and_can_reapply(blank_database: DatabaseSettings, version: int) -> None:
    available = migrations.discover_migrations()
    later = [item.version for item in available if item.version >= version]
    with connect(blank_database) as connection:
        # Yalniz hedefe kadar yukselt: daha yuksek numarali migration uygulanmisken
        # aradan birini geri almak out-of-order duruma yol acar.
        migrations.upgrade(connection, target=version)
        rolled_back_result = migrations.downgrade(connection, target=version)
        assert rolled_back_result.version == version
        rolled_back = migrations.status(connection)
        assert rolled_back.head == version - 1
        assert [item.version for item in rolled_back.pending] == later
        with pytest.raises(ValidationFailed, match="mevcut migration head"):
            migrations.downgrade(connection, target=version)
        reapplied = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert [item.version for item in reapplied] == later
    assert current.is_current


def test_concurrent_downgrade_rechecks_head_under_lock(
    blank_database: DatabaseSettings,
) -> None:
    target = migrations.discover_migrations()[-1].version
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=target)

    barrier = threading.Barrier(2)

    def attempt() -> str:
        with connect(blank_database) as connection:
            barrier.wait(timeout=5)
            try:
                migrations.downgrade(connection, target=target)
            except ValidationFailed:
                return "stale-head-rejected"
            return "rolled-back"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: attempt(), range(2)))

    assert outcomes == ["rolled-back", "stale-head-rejected"]
    with connect(blank_database) as connection:
        assert migrations.status(connection).head == target - 1


def test_partial_upgrade_to_target(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        applied = migrations.upgrade(connection, target=1)
        current = migrations.status(connection)
    available = migrations.discover_migrations()
    assert [result.version for result in applied] == [1]
    assert current.head == 1
    assert [migration.version for migration in current.pending] == [
        migration.version for migration in available[1:]
    ]


def test_checksum_drift_blocks_upgrade(blank_database: DatabaseSettings, tmp_path: Path) -> None:
    source = migrations.discover_migrations()
    for migration in source:
        (tmp_path / migration.path.name).write_text(migration.read_sql(), encoding="utf-8")
        (tmp_path / migration.down_path.name).write_text(
            migration.down_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    with connect(blank_database) as connection:
        migrations.upgrade(connection, tmp_path)
        target = tmp_path / source[0].path.name
        target.write_text(
            target.read_text(encoding="utf-8") + "\n-- sonradan eklendi\ncreate table core.x ();\n",
            encoding="utf-8",
        )
        current = migrations.status(connection, tmp_path)
        assert [finding.kind.value for finding in current.drift] == ["checksum-mismatch"]
        with pytest.raises(ConfigurationError, match="drift"):
            migrations.upgrade(connection, tmp_path)


def test_required_extensions_are_created_by_migration(blank_database: DatabaseSettings) -> None:
    """Temiz kurulumda migration eklentileri kendisi kurar."""
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute("select extname from pg_extension")
            present = {row[0] for row in cursor.fetchall()}
    assert {"vector", "pg_trgm", "btree_gin", "pgcrypto"} <= present


def test_all_declared_schemas_exist_after_upgrade(blank_database: DatabaseSettings) -> None:
    expected = {
        "core",
        "projects",
        "work",
        "runtime",
        "models",
        "research",
        "knowledge",
        "memory",
        "skills",
        "security",
        "ops",
    }
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute("select nspname from pg_namespace")
            present = {row[0] for row in cursor.fetchall()}
    assert expected <= present


def test_application_role_exists_and_is_not_superuser(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select rolsuper, rolbypassrls, rolcanlogin from pg_roles"
                " where rolname = 'zekam_app'"
            )
            row = cursor.fetchone()
    assert row is not None, "zekam_app rolu olusturulmali"
    assert row[0] is False, "Uygulama rolu superuser olmamali"
    assert row[1] is False, "Uygulama rolu RLS'i atlamamali"


def test_ledger_records_checksum_and_duration(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select version, name, checksum, duration_ms, applied_by"
                " from core.schema_migrations order by version"
            )
            rows = cursor.fetchall()
    available = {m.version: m for m in migrations.discover_migrations()}
    for version, name, checksum, duration_ms, applied_by in rows:
        assert checksum == available[version].checksum
        assert name == available[version].name
        assert duration_ms >= 0
        assert applied_by
