"""Operational v9 immutable pins and admitted migration fixtures (not yet run)."""

import sqlite3

import pytest

from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite.operational_backup import _logical_rows_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


def test_v9_is_additive_and_never_bootstraps_over_existing_database(tmp_path):
    path = tmp_path / "operational.db"
    schema.bootstrap_v8(path)
    with pytest.raises(ConfigurationError, match="empty destination"):
        schema.bootstrap_v9(path)
    assert schema.status(path).schema_version == 8
    assert schema.V9_MIGRATION_LEDGER[:-1] == schema.V8_MIGRATION_LEDGER
    assert (
        schema.V9_MIGRATION_DIGEST
        == "sha256:9d6f8ffe66d6893f696b0f3f2d6ec8ef9a882aef84693025b31651dcdd6490a9"
    )
    assert (
        schema.V9_SCHEMA_DIGEST
        == "sha256:6191ee9991632220a7587486ae97ad24707233bde01629e60e266b164ae06a21"
    )


def test_v8_to_v9_core_migration_preserves_original_rows_and_adds_no_grant(tmp_path):
    path = tmp_path / "operational.db"
    schema.bootstrap_v8(path)
    with SQLiteOperationalStore(path).unit_of_work() as uow:
        uow.create_project(slug="preserved", display_name="Preserved")
        uow.commit()
    with sqlite3.connect(path) as connection:
        logical = _logical_rows_digest(connection)
        original = schema._original_rows_digest(connection, 8)
        connection.execute("begin immediate")
        migration._migrate_connection(
            connection,
            source_logical_digest=logical,
            source_original_digest=original,
            source_version=8,
            target_version=9,
        )
        connection.commit()
        assert schema._original_rows_digest(connection, 8) == original
        for table in (
            "git_publication_evidence",
            "git_publication_authorization",
            "git_publication_consumption",
            "git_publication_revocation",
        ):
            assert connection.execute(f"select count(*) from {table}").fetchone()[0] == 0
    observed = schema.status(path)
    assert observed.schema_version == 9 and observed.schema_ok and observed.integrity_ok
