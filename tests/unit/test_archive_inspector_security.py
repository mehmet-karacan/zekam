"""Arsiv metadata taramasinin fail-closed sinirlari."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from zekam.application.knowledge_ingestion import ArchiveInspector
from zekam.domain.errors import PolicyViolation
from zekam.domain.knowledge import ScanLimits


def test_zip_symlink_drive_and_traversal_are_excluded(tmp_path: Path) -> None:
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "target.txt")
        bundle.writestr("C:/outside.txt", "fixture")
        bundle.writestr("../outside.txt", "fixture")
        bundle.writestr("folder/../outside.txt", "fixture")
        bundle.writestr("safe.txt", "fixture")
    # Windows zipfile yazicisi backslash'i slash'a cevirir; central ve local
    # header adlarini ayni uzunlukta degistirerek dis kaynak girdisini taklit et.
    archive.write_bytes(
        archive.read_bytes().replace(b"folder/../outside.txt", b"folder\\..\\outside.txt")
    )

    report = ArchiveInspector().inspect(archive)

    assert report.included == ("safe.txt",)
    assert report.reason_for("link.txt") == "arsiv baglantisi reddedildi"


def test_duplicate_zip_name_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("same.txt", "first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            bundle.writestr("same.txt", "second")
    with pytest.raises(PolicyViolation):
        ArchiveInspector().inspect(archive)


def test_zip_entry_limits_are_enforced(tmp_path: Path) -> None:
    archive = tmp_path / "entries.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one.txt", "1")
        bundle.writestr("two.txt", "2")
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_entries=1)).inspect(archive)


def test_zip_entry_ratio_is_enforced_even_with_archive_padding(tmp_path: Path) -> None:
    archive = tmp_path / "padding.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("big.txt", "x" * 100_000)
    archive.write_bytes(b"0" * 100_000 + archive.read_bytes())
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_compression_ratio=10)).inspect(archive)
