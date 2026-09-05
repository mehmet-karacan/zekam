"""ZEKAM_HOME yerlesimi ve core/user-data ayrimi testleri."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.home import (
    HOME_ENTRIES,
    LAYOUT_FILE,
    LAYOUT_SCHEMA,
    PROJECT_ENTRIES,
    HomeLayout,
    assert_separated_from_core,
    default_home,
    resolve_home,
)
from zekam.domain.errors import ConfigurationError, LayoutError
from zekam.domain.identity import PRODUCT

pytestmark = pytest.mark.unit


def test_ensure_creates_every_declared_directory(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    for entry in HOME_ENTRIES:
        assert (layout.root / entry.relative).is_dir(), entry.relative


def test_ensure_is_idempotent(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    marker = layout.root / "global" / "raporlar" / "korunmali.txt"
    marker.write_text("kanit", encoding="utf-8")
    layout.ensure()
    assert marker.read_text(encoding="utf-8") == "kanit"


def test_layout_file_declares_schema_and_ownership(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    document = json.loads((layout.root / LAYOUT_FILE).read_text(encoding="utf-8"))
    assert document["schema"] == LAYOUT_SCHEMA
    assert document["data_root_env"] == PRODUCT.data_root_env
    assert {entry["path"] for entry in document["entries"]} == {
        entry.relative for entry in HOME_ENTRIES
    }
    assert all(entry["ownership"] for entry in document["entries"])


def test_verify_reports_missing_directory(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    (layout.root / "runtime" / "locks").rmdir()
    issues = layout.verify()
    assert [issue.kind for issue in issues] == ["missing-directory"]
    assert issues[0].relative == "runtime/locks"


def test_verify_reports_unsupported_layout_schema(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    (layout.root / LAYOUT_FILE).write_text(
        json.dumps({"schema": "zekam-home-layout/v0"}), encoding="utf-8"
    )
    kinds = {issue.kind for issue in layout.verify()}
    assert "unsupported-layout-schema" in kinds


def test_ensure_does_not_overwrite_existing_unsupported_layout(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    layout.layout_file.write_text(json.dumps({"schema": "zekam-home-layout/v1"}), encoding="utf-8")
    before = layout.layout_file.read_bytes()

    with pytest.raises(LayoutError, match="overwrite"):
        layout.ensure()

    assert layout.layout_file.read_bytes() == before


def test_ensure_rejects_nonempty_unowned_home(home_root: Path) -> None:
    home_root.mkdir()
    marker = home_root / "user.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(LayoutError, match="authority"):
        HomeLayout(home_root).ensure()

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_removed_product_layout_schema_is_rejected(home_root: Path) -> None:
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    layout = HomeLayout(home_root).ensure()
    (layout.root / LAYOUT_FILE).write_text(
        json.dumps({"schema": f"{removed_slug}-home-layout/v1"}), encoding="utf-8"
    )
    assert "unsupported-layout-schema" in {issue.kind for issue in layout.verify()}


def test_verify_on_missing_root_reports_single_issue(tmp_path: Path) -> None:
    issues = HomeLayout(tmp_path / "yok").verify()
    assert len(issues) == 1
    assert issues[0].kind == "missing-root"


def test_ensure_project_creates_project_tree(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    root = layout.ensure_project("ornek-proje")
    for entry in PROJECT_ENTRIES:
        assert (root / entry.relative).is_dir(), entry.relative


@pytest.mark.parametrize("project_id", ["", ".", "..", "a/b", "a\\b"])
def test_project_id_traversal_is_rejected(home_root: Path, project_id: str) -> None:
    layout = HomeLayout(home_root).ensure()
    with pytest.raises(LayoutError):
        layout.project_root(project_id)


def test_path_outside_home_is_rejected(home_root: Path) -> None:
    layout = HomeLayout(home_root).ensure()
    with pytest.raises(LayoutError):
        layout.path("../disari")


def test_resolve_home_prefers_explicit_value(tmp_path: Path) -> None:
    assert resolve_home(tmp_path / "acik") == tmp_path / "acik"


def test_resolve_home_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRODUCT.data_root_env, raising=False)
    assert resolve_home() == default_home()


def test_removed_product_home_locator_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    removed_prefix = "".join(chr(item) for item in (69, 78, 65, 73))
    monkeypatch.delenv(PRODUCT.data_root_env, raising=False)
    monkeypatch.setenv(f"{removed_prefix}_HOME", str(tmp_path))
    assert resolve_home() == default_home()


def test_resolve_home_reads_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(PRODUCT.data_root_env, str(tmp_path / "ortam"))
    assert resolve_home() == tmp_path / "ortam"


def test_home_cannot_equal_core_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        assert_separated_from_core(tmp_path, tmp_path)


def test_home_cannot_live_inside_core_root(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    with pytest.raises(ConfigurationError):
        assert_separated_from_core(core / "veri", core)


def test_core_root_cannot_live_inside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ConfigurationError):
        assert_separated_from_core(home, home / "core")


def test_separate_trees_are_accepted(tmp_path: Path) -> None:
    assert_separated_from_core(tmp_path / "home", tmp_path / "core")
