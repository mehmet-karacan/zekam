"""Operational SQLite reparse/junction guards without platform privileges."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite import operational_schema as schema

pytestmark = pytest.mark.unit


class _ReparseInfo:
    st_mode = stat.S_IFREG
    st_file_attributes = 0x400


def _mark_reparse(
    monkeypatch: pytest.MonkeyPatch, *paths: Path
) -> None:
    original = Path.lstat
    marked = frozenset(paths)

    def fake_lstat(path: Path) -> object:
        if path in marked:
            return _ReparseInfo()
        return original(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)


def test_operational_connect_and_status_reject_reparse_leaf_without_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "operational.db"
    path.write_bytes(b"sqlite")
    _mark_reparse(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="regular file"):
        schema._connect(path)
    result = schema.status(path)
    assert (result.exists, result.schema_version, result.integrity_ok, result.schema_ok) == (
        False,
        None,
        False,
        False,
    )


def test_operational_bootstrap_rejects_reparse_parent_without_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "junction-like"
    parent.mkdir()
    path = parent / "operational.db"
    _mark_reparse(monkeypatch, parent)

    with pytest.raises(ConfigurationError, match="regular file"):
        schema.bootstrap(path)
    with pytest.raises(ConfigurationError, match="empty destination"):
        schema.bootstrap_v4(path)


def test_operational_normal_bootstrap_remains_available(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    result = schema.bootstrap(path)
    assert result.schema_version == schema.SCHEMA_VERSION
    assert schema.status(path).schema_ok is True
