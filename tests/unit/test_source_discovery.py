"""Kaynak kesfi: yoksayma, sinirlar, ikili tespiti ve agac digest'i."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zekam.application.source_discovery import (
    DiscoveryPolicy,
    SkipReason,
    assert_within_root,
    compute_tree_digest,
    discover,
)
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.unit


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    root = tmp_path / "ornek"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "paket").mkdir(parents=True)
    (root / "src" / "ana.py").write_text("print('merhaba')\n", encoding="utf-8")
    (root / "tests" / "test_ana.py").write_text("def test_ana(): pass\n", encoding="utf-8")
    (root / "README.md").write_text("# Ornek\n", encoding="utf-8")
    (root / "node_modules" / "paket" / "index.js").write_text(
        "module.exports={}\n", encoding="utf-8"
    )
    (root / "gecici.log").write_text("kayit\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    return root


def _paths(root: Path) -> set[str]:
    return {item.relative_path for item in discover(root).files}


def test_source_files_are_discovered(sample_project: Path) -> None:
    found = _paths(sample_project)
    assert "src/ana.py" in found
    assert "tests/test_ana.py" in found
    assert "README.md" in found


def test_system_denied_directory_is_excluded(sample_project: Path) -> None:
    found = _paths(sample_project)
    assert not any(path.startswith("node_modules") for path in found)


def test_generated_typescript_build_info_is_excluded(sample_project: Path) -> None:
    generated = sample_project / "gpu-ui" / "tsconfig.tsbuildinfo"
    generated.parent.mkdir()
    generated.write_text('{"program":{"fileNames":[]}}', encoding="utf-8")

    report = discover(sample_project)

    assert "gpu-ui/tsconfig.tsbuildinfo" not in {item.relative_path for item in report.files}
    assert "gpu-ui/tsconfig.tsbuildinfo" in {
        item.relative_path for item in report.skipped_by(SkipReason.IGNORED)
    }


def test_gitignore_is_applied(sample_project: Path) -> None:
    assert "gecici.log" not in _paths(sample_project)


def test_zekamignore_extends_gitignore(sample_project: Path) -> None:
    (sample_project / ".zekamignore").write_text("README.md\n", encoding="utf-8")
    assert "README.md" not in _paths(sample_project)


def test_removed_product_ignore_file_is_not_read(sample_project: Path) -> None:
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    (sample_project / f".{removed_slug}ignore").write_text("README.md\n", encoding="utf-8")
    assert "README.md" in _paths(sample_project)


def test_skipped_paths_carry_reason(sample_project: Path) -> None:
    report = discover(sample_project)
    reasons = {item.reason for item in report.skipped}
    assert SkipReason.IGNORED in reasons


def test_discovery_never_writes_to_source(sample_project: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in sorted(sample_project.rglob("*"))}
    discover(sample_project)
    after = {path: path.stat().st_mtime_ns for path in sorted(sample_project.rglob("*"))}
    assert before == after
    assert set(before) == set(after)


def test_tree_digest_is_stable_across_runs(sample_project: Path) -> None:
    assert discover(sample_project).tree_digest == discover(sample_project).tree_digest


def test_tree_digest_changes_with_content(sample_project: Path) -> None:
    before = discover(sample_project).tree_digest
    (sample_project / "src" / "ana.py").write_text("print('degisti')\n", encoding="utf-8")
    assert discover(sample_project).tree_digest != before


def test_tree_digest_changes_when_file_is_added(sample_project: Path) -> None:
    before = discover(sample_project).tree_digest
    (sample_project / "src" / "yeni.py").write_text("x = 1\n", encoding="utf-8")
    assert discover(sample_project).tree_digest != before


def test_tree_digest_of_empty_set_is_defined() -> None:
    assert compute_tree_digest([]).startswith("sha256:")


def test_binary_file_is_recorded_but_not_scanned(sample_project: Path) -> None:
    (sample_project / "veri.bin").write_bytes(b"\x00\x01\x02binary")
    report = discover(sample_project)
    binary = next(item for item in report.files if item.relative_path == "veri.bin")
    assert binary.is_text is False


def test_large_file_is_skipped(sample_project: Path) -> None:
    (sample_project / "buyuk.txt").write_text("x" * 5000, encoding="utf-8")
    report = discover(sample_project, policy=DiscoveryPolicy(max_file_bytes=1000))
    assert "buyuk.txt" in {item.relative_path for item in report.skipped_by(SkipReason.TOO_LARGE)}


def test_file_count_limit_truncates(sample_project: Path) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_files=1))
    assert report.truncated
    assert report.file_count == 1
    assert report.skipped_by(SkipReason.LIMIT_REACHED)


def test_total_byte_limit_truncates(sample_project: Path) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_bytes=10))
    assert report.truncated


def test_file_with_secret_is_excluded_from_index(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "abcdefgh12345678"\n', encoding="utf-8")
    report = discover(sample_project)
    assert "ayarlar.py" not in {item.relative_path for item in report.files}
    assert report.secrets
    assert report.secrets[0].relative_path == "ayarlar.py"


def test_secret_scan_can_be_disabled(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "abcdefgh12345678"\n', encoding="utf-8")
    report = discover(sample_project, policy=DiscoveryPolicy(scan_secrets=False))
    assert "ayarlar.py" in {item.relative_path for item in report.files}
    assert report.secrets == ()


def test_report_never_contains_secret_value(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "SuperGizliDeger123"\n', encoding="utf-8")
    assert "SuperGizliDeger123" not in repr(discover(sample_project).as_dict())


def test_extension_counts_are_reported(sample_project: Path) -> None:
    extensions = discover(sample_project).extensions
    assert extensions["py"] == 2
    assert extensions["md"] == 1


def test_root_must_be_directory(tmp_path: Path) -> None:
    target = tmp_path / "dosya.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises((PolicyViolation, NotADirectoryError, FileNotFoundError)):
        discover(target)


def test_assert_within_root_accepts_child(tmp_path: Path) -> None:
    child = tmp_path / "alt"
    child.mkdir()
    assert assert_within_root(child, tmp_path) == child.resolve()


def test_assert_within_root_rejects_sibling(tmp_path: Path) -> None:
    root = tmp_path / "kok"
    root.mkdir()
    sibling = tmp_path / "kardes"
    sibling.mkdir()
    with pytest.raises(PolicyViolation):
        assert_within_root(sibling, root)


@pytest.mark.skipif(os.name == "nt", reason="Windows'ta symlink olusturmak yonetici yetkisi ister")
def test_symlink_is_not_followed(sample_project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "sizinti.txt").write_text("gizli", encoding="utf-8")
    (sample_project / "baglanti").symlink_to(outside, target_is_directory=True)
    report = discover(sample_project)
    assert "baglanti" in {item.relative_path for item in report.skipped_by(SkipReason.SYMLINK)}
    assert not any("sizinti" in item.relative_path for item in report.files)
