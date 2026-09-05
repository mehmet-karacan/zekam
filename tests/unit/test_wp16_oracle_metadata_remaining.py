from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zekam.application import oracle_metadata_index as oracle
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed

DIGEST = "sha256:" + "a" * 64


def _datasource() -> oracle.OracleDatasource:
    return oracle.OracleDatasource("APP", DIGEST, "application.yaml", "dsn", "user", "password")


def test_secure_config_rejects_directory_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    with pytest.raises(ConfigurationError, match="dosyasi gecersiz"):
        oracle._secure_config_path(tmp_path, "config")
    source = tmp_path / "config.yaml"
    source.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(oracle, "MAX_CONFIG_BYTES", 1)
    with pytest.raises(ConfigurationError, match="dosyasi gecersiz"):
        oracle._secure_config_path(tmp_path, "config.yaml")


@pytest.mark.parametrize(
    "value",
    ("wrong", "jdbc:oracle:thin:@", "jdbc:oracle:thin:@//host\nservice"),
)
def test_thin_dsn_rejects_prefix_empty_and_control_character(value: str) -> None:
    with pytest.raises(ConfigurationError):
        oracle._thin_dsn(value)
    assert oracle._thin_dsn("jdbc:oracle:thin:@//host/service") == "host/service"


def test_ddl_units_flushes_buffer_and_splits_long_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle, "MAX_DDL_CHUNK_CHARACTERS", 5)
    item = oracle.OracleDdlObject(
        "APP",
        "TABLE_A",
        "TABLE",
        "VALID",
        "2026-09-04T12:00:00",
        digest_of_bytes(b"aa\n123456789"),
        "aa\n123456789",
    )
    units = oracle._ddl_units(item, 3)
    assert [unit.text for unit in units] == ["aa", "12345", "6789"]
    assert [unit.order for unit in units] == [3, 4, 5]


class _Cursor:
    def __init__(
        self, *, one: list[Any], many: list[list[Any]], execute_error: Exception | None = None
    ) -> None:
        self.one = one
        self.many = many
        self.execute_error = execute_error

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, *_args: object, **_kwargs: object) -> None:
        if self.execute_error is not None:
            raise self.execute_error

    def fetchone(self) -> Any:
        return self.one.pop(0)

    def fetchall(self) -> list[Any]:
        return self.many.pop(0)


class _Connection:
    call_timeout = 0

    def __init__(self, cursor: _Cursor) -> None:
        self.value = cursor
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.value

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, connection: _Connection | Exception) -> None:
    def connect(**_kwargs: object) -> _Connection:
        if isinstance(connection, Exception):
            raise connection
        return connection

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=connect))


def test_oracle_collect_wraps_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, OSError("offline"))
    with pytest.raises(ConfigurationError, match="OSError"):
        oracle.OracleMetadataClient().collect(_datasource())


@pytest.mark.parametrize(
    ("ones", "many", "message"),
    (
        ([None], [], "session identity"),
        ([("DB", "PDB", "APP"), None], [], "schema gorunur"),
        (
            [("DB", "PDB", "APP"), ("APP",), None],
            [[["TABLE_A", "TABLE", "VALID", "2026-09-04T12:00:00"]]],
            "bos DDL",
        ),
        (
            [("DB", "PDB", "APP"), ("APP",), ("   ",)],
            [[["TABLE_A", "TABLE", "VALID", "2026-09-04T12:00:00"]]],
            "bos DDL",
        ),
    ),
)
def test_oracle_collect_rejects_missing_identity_schema_and_ddl(
    monkeypatch: pytest.MonkeyPatch,
    ones: list[Any],
    many: list[list[Any]],
    message: str,
) -> None:
    connection = _Connection(_Cursor(one=ones, many=many))
    _install(monkeypatch, connection)
    with pytest.raises((ConfigurationError, ValidationFailed), match=message):
        oracle.OracleMetadataClient().collect(_datasource())
    assert connection.closed


def test_oracle_collect_enforces_object_and_total_byte_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = ["TABLE_A", "TABLE", "VALID", "2026-09-04T12:00:00"]
    connection = _Connection(_Cursor(one=[("DB", "PDB", "APP"), ("APP",)], many=[[row]]))
    _install(monkeypatch, connection)
    monkeypatch.setattr(oracle, "MAX_OBJECTS", 0)
    with pytest.raises(PolicyViolation, match="nesne sayisi"):
        oracle.OracleMetadataClient().collect(_datasource())

    connection = _Connection(
        _Cursor(one=[("DB", "PDB", "APP"), ("APP",), ("CREATE TABLE A",)], many=[[row]])
    )
    _install(monkeypatch, connection)
    monkeypatch.setattr(oracle, "MAX_OBJECTS", 20_000)
    monkeypatch.setattr(oracle, "MAX_TOTAL_DDL_BYTES", 1)
    with pytest.raises(PolicyViolation, match="DDL boyut"):
        oracle.OracleMetadataClient().collect(_datasource())


def test_oracle_collect_excludes_secret_and_requires_remaining_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = ["TABLE_A", "TABLE", "VALID", "2026-09-04T12:00:00"]
    connection = _Connection(
        _Cursor(one=[("DB", "PDB", "APP"), ("APP",), ("CREATE TABLE A",)], many=[[row]])
    )
    _install(monkeypatch, connection)
    monkeypatch.setattr(oracle, "scan_text", lambda *_args, **_kwargs: (object(),))
    with pytest.raises(PolicyViolation, match="secret-safe"):
        oracle.OracleMetadataClient().collect(_datasource())


def test_oracle_collect_sanitizes_unexpected_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError(SimpleNamespace(full_code="ORA-99999"))
    connection = _Connection(_Cursor(one=[], many=[], execute_error=error))
    _install(monkeypatch, connection)
    with pytest.raises(ConfigurationError, match="RuntimeError:ORA-99999"):
        oracle.OracleMetadataClient().collect(_datasource())
