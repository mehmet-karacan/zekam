"""P11 ingestion guvenlik sinirlari.

Gercek dizin ve gercek arsiv dosyalari kullanilir. Ingestion sirasinda hicbir
build, hook veya kod calistirilmaz.
"""

from __future__ import annotations

import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from zekam.application.knowledge_ingestion import (
    ArchiveInspector,
    DirectoryScanner,
    PythonSymbolExtractor,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.knowledge import ScanLimits

pytestmark = pytest.mark.security


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    root = tmp_path / "kaynak"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules" / "paket").mkdir(parents=True)
    (root / "src" / "modul.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# baslik\n", encoding="utf-8")
    (root / ".env").write_text("ZEKAM_DATABASE_PASSWORD=gizli\n", encoding="utf-8")
    (root / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (root / "araclar.exe").write_bytes(b"MZ\x00")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "node_modules" / "paket" / "index.js").write_text("x", encoding="utf-8")
    return root


def test_secret_dosyalari_taranmaz(sample_tree: Path) -> None:
    report = DirectoryScanner(sample_tree).scan()
    assert "src/modul.py" in report.included
    assert "README.md" in report.included
    assert ".env" not in report.included
    assert "id_rsa" not in report.included
    assert "deny list" in report.reason_for(".env")


def test_git_ve_bagimlilik_dizinleri_atlanir(sample_tree: Path) -> None:
    report = DirectoryScanner(sample_tree).scan()
    assert not any(path.startswith(".git/") for path in report.included)
    assert not any(path.startswith("node_modules/") for path in report.included)


def test_ikili_dosya_icerik_olarak_alinmaz(sample_tree: Path) -> None:
    report = DirectoryScanner(sample_tree).scan()
    assert "araclar.exe" not in report.included
    assert "ikili" in report.reason_for("araclar.exe")


def test_ignore_kurali_uygulanir(sample_tree: Path) -> None:
    report = DirectoryScanner(sample_tree, ignore_names=frozenset({"README.md"})).scan()
    assert "README.md" not in report.included
    assert report.reason_for("README.md") == "ignore kurali"


def test_izinli_kok_disi_reddedilir(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation):
        DirectoryScanner(tmp_path / "olmayan").scan()


def test_symlink_izlenmez(sample_tree: Path, tmp_path: Path) -> None:
    disari = tmp_path / "disari"
    disari.mkdir()
    (disari / "gizli.txt").write_text("gizli", encoding="utf-8")
    link = sample_tree / "src" / "kisayol.txt"
    try:
        link.symlink_to(disari / "gizli.txt")
    except OSError:
        pytest.skip("Windows'ta symlink olusturmak yonetici yetkisi ister")
    report = DirectoryScanner(sample_tree).scan()
    assert "src/kisayol.txt" not in report.included
    assert report.reason_for("src/kisayol.txt") == "symlink izlenmez"


def test_reparse_directory_izlenmez_without_windows_privilege(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Junction metadata is rejected even when CreateSymbolicLink is unavailable."""

    linked = sample_tree / "src" / "junction-like"
    linked.mkdir()
    (linked / "disari.txt").write_text("disari", encoding="utf-8")
    original_scandir = os.scandir
    source_directory = (sample_tree / "src").resolve()

    class ReparseEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_symlink(self) -> bool:
            return False

        def stat(self, *, follow_symlinks: bool = True) -> object:
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            return type(
                "ReparseStat",
                (),
                {"st_mode": stat.S_IFDIR, "st_size": info.st_size, "st_file_attributes": 0x400},
            )()

    def scandir_with_reparse(directory: str | os.PathLike[str]):
        entries = list(original_scandir(directory))
        if Path(directory).resolve() == source_directory:
            return [ReparseEntry(entry) if entry.name == linked.name else entry for entry in entries]
        return entries

    monkeypatch.setattr(os, "scandir", scandir_with_reparse)
    report = DirectoryScanner(sample_tree).scan()
    assert "src/junction-like" not in report.included
    assert report.reason_for("src/junction-like") == "symlink/junction izlenmez"
    assert not any(item.path.endswith("disari.txt") for item in report.decisions)


def test_reparse_root_rejected_without_windows_privilege(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_lstat = Path.lstat

    def mocked_lstat(path: Path) -> object:
        if path == sample_tree:
            return type(
                "ReparseRootStat",
                (),
                {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400},
            )()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", mocked_lstat)
    with pytest.raises(PolicyViolation, match="symlink/junction"):
        DirectoryScanner(sample_tree).scan()


def test_girdi_sayisi_siniri_uygulanir(sample_tree: Path) -> None:
    with pytest.raises(PolicyViolation):
        DirectoryScanner(sample_tree, limits=ScanLimits(max_entries=1)).scan()


def test_zip_traversal_girdisi_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "paket.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("ok.txt", "iyi")
        bundle.writestr("../disari.txt", "kotu")
        bundle.writestr(".env", "gizli")
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "../disari.txt" not in report.included
    assert ".env" not in report.included


def test_zip_bomb_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "bomba.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("buyuk.txt", "a" * 5_000_000)
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_compression_ratio=50)).inspect(archive)


def test_tar_arsivi_incelenir(tmp_path: Path) -> None:
    payload = tmp_path / "ok.txt"
    payload.write_text("iyi", encoding="utf-8")
    archive = tmp_path / "paket.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname="ok.txt")
        bundle.add(payload, arcname="../disari.txt")
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "../disari.txt" not in report.included


def test_desteklenmeyen_arsiv_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "paket.rar"
    archive.write_bytes(b"Rar!")
    with pytest.raises(PolicyViolation):
        ArchiveInspector().inspect(archive)


def test_kod_ingestion_yan_etki_uretmez(tmp_path: Path) -> None:
    """AST ayristirmasi modulu import etmez; dosya yazan kod calismaz."""

    marker = tmp_path / "yan-etki.txt"
    source = (
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('calisti')\n\n"
        f"def guvenli() -> int:\n    return 1\n"
    )
    symbols = PythonSymbolExtractor().extract(source, relative_path="a.py", revision="r")
    assert [item.name for item in symbols] == ["guvenli"]
    assert marker.exists() is False, "ingestion sirasinda kod calistirilmamali"


def test_sembol_yolu_traversal_reddeder() -> None:
    with pytest.raises(PolicyViolation):
        PythonSymbolExtractor().extract("x = 1\n", relative_path="../disari.py", revision="r")
