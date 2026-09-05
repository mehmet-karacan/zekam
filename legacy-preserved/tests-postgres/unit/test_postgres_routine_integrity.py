"""Migration-bound PostgreSQL routine inventory unit tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.postgres import migrations, routine_integrity

pytestmark = pytest.mark.unit


def test_splitter_preserves_semicolons_inside_dollar_body_and_quotes() -> None:
    sql = """
    create function core.sample() returns text language plpgsql as $$
    begin
      return 'a;b';
    end;
    $$;
    select 'x;y';
    """

    statements = routine_integrity.split_sql_statements(sql)

    assert len(statements) == 2
    assert "return 'a;b';" in statements[0]
    assert statements[1] == "select 'x;y';"


def test_expected_inventory_uses_last_canonical_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "0001_first.sql"
    first.write_text(
        """
        -- first routine

        create function core.one() returns integer language sql as $$ select 1; $$;
        create function core.two(value text) returns text language sql as $$ select value; $$;
        revoke all on function core.two(text) from public;
        grant execute on function core.two(text) to zekam_app;
        """,
        encoding="utf-8",
    )
    second = tmp_path / "0002_second.sql"
    second.write_text(
        """
        create or replace function core.one() returns integer
        language sql as $$ select 2; $$;
        """,
        encoding="utf-8",
    )
    available = migrations.discover_migrations(tmp_path)
    applied = tuple(
        migrations.AppliedMigration(item.version, item.name, item.checksum) for item in available
    )
    monkeypatch.setattr(routine_integrity.migrations, "read_applied", lambda _connection: applied)

    expected = routine_integrity.expected_routines(SimpleNamespace(), tmp_path)

    assert [item.key.label for item in expected] == [
        "core.one:function",
        "core.two:function",
    ]
    assert expected[0].migration_version == 2
    assert "select 2" in expected[0].statement
    assert len(expected[1].post_statements) == 2
    assert expected[1].as_dict()["post_statement_digests"]


def test_overloaded_name_is_rejected_instead_of_replaying_ambiguous_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "0001_overload.sql"
    path.write_text(
        """
        create function core.same(value text) returns text language sql as $$ select value; $$;
        create function core.same(value integer) returns integer
        language sql as $$ select value; $$;
        """,
        encoding="utf-8",
    )
    migration = migrations.discover_migrations(tmp_path)[0]
    applied = (migrations.AppliedMigration(migration.version, migration.name, migration.checksum),)
    monkeypatch.setattr(routine_integrity.migrations, "read_applied", lambda _connection: applied)

    with pytest.raises(ConfigurationError, match="Overloaded routine"):
        routine_integrity.expected_routines(SimpleNamespace(), tmp_path)


def test_repair_plan_digest_never_contains_sql_text() -> None:
    key = routine_integrity.RoutineKey("core", "missing", routine_integrity.RoutineKind.FUNCTION)
    spec = routine_integrity.RoutineSpec(
        key=key,
        migration_version=1,
        migration_label="0001_test",
        migration_checksum="a" * 64,
        statement="create function core.missing() returns integer language sql as $$ select 1; $$;",
    )
    status = routine_integrity.RoutineIntegrityStatus(
        migration_head=1,
        expected=(spec,),
        present=(),
        missing=(spec,),
        unexpected=(),
        migration_pending=(),
        migration_drift=(),
    )

    document = status.as_dict()

    assert document["missing"][0]["statement_digest"].startswith("sha256:")
    assert "select 1" not in repr(document)
