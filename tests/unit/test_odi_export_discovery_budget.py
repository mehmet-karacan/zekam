from pathlib import Path

import pytest

from zekam.application import odi11g_export as export
from zekam.domain.errors import ValidationFailed


def test_empty_directory_chain_cannot_evade_discovery_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_MAX_FILES", 3)
    parent = tmp_path
    for name in ("a", "b", "c", "d"):
        parent = parent / name
        parent.mkdir()
    with pytest.raises(ValidationFailed, match="dosya/dizin"):
        export._bundle_files(tmp_path)


def test_directories_and_files_share_one_discovery_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_MAX_FILES", 3)
    directory = tmp_path / "design"
    directory.mkdir()
    for name in ("a.xml", "b.xml", "c.xml"):
        (directory / name).write_text("<root/>", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="dosya/dizin"):
        export._bundle_files(tmp_path)


def test_exact_budget_keeps_all_sorted_regular_files(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_MAX_FILES", 3)
    directory = tmp_path / "design"
    directory.mkdir()
    for name in ("b.xml", "a.xml"):
        (directory / name).write_text("<root/>", encoding="utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in export._bundle_files(tmp_path)] == [
        "design/a.xml",
        "design/b.xml",
    ]
