"""PostgreSQL 18 + pgvector baseline kabul testi.

Gercek sunucu yoksa test atlanir; taklit edilmez.
"""

from __future__ import annotations

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckStatus
from zekam.infrastructure.doctor import postgres_checks
from zekam.infrastructure.postgres.connection import connect, read_server_info

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_server_is_postgresql_18_or_newer(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection:
        info = read_server_info(connection)
    assert info.major_version >= 18


def test_required_extensions_are_installed(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection:
        info = read_server_info(connection)
    for extension in ("vector", "pg_trgm", "btree_gin", "pgcrypto"):
        assert extension in info.extensions, extension


def test_pgvector_version_meets_minimum(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection:
        info = read_server_info(connection)
    major, minor, *_ = (int(part) for part in info.extensions["vector"].split("."))
    assert (major, minor) >= (0, 8)


def test_vector_roundtrip_uses_cosine_distance(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute("select ('[1,0,0]'::vector(3)) <=> ('[1,0,0]'::vector(3))")
        identical = cursor.fetchone()[0]
        cursor.execute("select ('[1,0,0]'::vector(3)) <=> ('[0,1,0]'::vector(3))")
        orthogonal = cursor.fetchone()[0]
    assert identical == pytest.approx(0.0, abs=1e-9)
    assert orthogonal == pytest.approx(1.0, abs=1e-9)


def test_full_text_search_is_available(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select to_tsvector('simple', %s) @@ plainto_tsquery('simple', %s)",
            ("zekam kanit tabanli platform", "kanit"),
        )
        assert cursor.fetchone()[0] is True


def test_trigram_similarity_is_available(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute("select similarity(%s, %s) > 0.4", ("gpu-projesi", "gpu projesi"))
        assert cursor.fetchone()[0] is True


def test_server_timezone_is_utc(postgres_settings: DatabaseSettings) -> None:
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute("show timezone")
        assert cursor.fetchone()[0].upper() == "UTC"


def test_doctor_connection_check_passes(postgres_settings: DatabaseSettings) -> None:
    result = postgres_checks.ConnectionCheck(settings=postgres_settings).run()
    assert result.status is CheckStatus.PASSED
    assert result.evidence["server_major_version"] >= 18
    assert "vector" in result.evidence["extensions"]


def test_doctor_connection_check_fails_closed_on_bad_port(
    postgres_settings: DatabaseSettings,
) -> None:
    broken = DatabaseSettings(
        host=postgres_settings.host,
        port=1,
        name=postgres_settings.name,
        user=postgres_settings.user,
        connect_timeout_seconds=2,
    )
    result = postgres_checks.ConnectionCheck(settings=broken).run()
    assert result.status is CheckStatus.FAILED
    assert result.findings[0].code == "postgres.connection-failed"
    # Hata ayrintisi ham surucu metnini sizdirmamalidir.
    assert "password" not in repr(result).lower()
