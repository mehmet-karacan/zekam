"""WP-04.05 arsiv guvenlik sinirlari: ZIP/TAR metadata siniri ve fail-closed girdiler.

Gercek arsiv dosyalari kullanilir. Limitler metadata okunurken uygulanir;
buyuk infolist/getmembers bellege alinmaz. Guvenli olmayan her girdi
fail-closed reddedilir: symlink, absolute/drive yolu, dotdot traversal,
backslash ve yinelenen adlar.
"""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from zekam.application.knowledge_ingestion import ArchiveInspector
from zekam.domain.errors import PolicyViolation
from zekam.domain.knowledge import ScanLimits

pytestmark = pytest.mark.unit


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, payload in entries:
            bundle.writestr(name, payload)
    return buffer.getvalue()


def _raw_zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    """Ham ZIP baytlari uretir; entry adlarini **normalize etmeden** korur.

    stdlib yazici backslash ve surucu harfi tasiyan adlari normalize ettigi
    icin fail-closed testleri ham local file header ile yazilir.
    """

    buffer = io.BytesIO()
    offsets: list[int] = []
    for name, payload in entries:
        encoded = name.encode("cp437")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        offsets.append(buffer.tell())
        buffer.write(
            b"PK\x03\x04"
            + struct.pack(
                "<HHHHHIIIHH",
                20,
                0,
                0,
                0,
                0,
                crc,
                len(payload),
                len(payload),
                len(encoded),
                0,
            )
            + encoded
            + payload
        )
    central_offset = buffer.tell()
    central = bytearray()
    for (name, payload), offset in zip(entries, offsets, strict=True):
        encoded = name.encode("cp437")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        central += (
            b"PK\x01\x02"
            + struct.pack(
                "<HHHHHHIIIHHHHHII",
                20,
                20,
                0,
                0,
                0,
                0,
                crc,
                len(payload),
                len(payload),
                len(encoded),
                0,
                0,
                0,
                0,
                0o100644 << 16,
                offset,
            )
            + encoded
        )
    buffer.write(central)
    buffer.write(
        b"PK\x05\x06"
        + struct.pack(
            "<HHHHIIH",
            0,
            0,
            len(entries),
            len(entries),
            len(central),
            central_offset,
            0,
        )
    )
    return buffer.getvalue()


def _zip_link_entry(name: str, target: str) -> tuple[str, bytes]:
    """Unix create_system ile symlink girdisi uretir (icerik = hedef yol)."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        info = zipfile.ZipInfo(name)
        info.create_system = 3  # Unix
        info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
        bundle.writestr(info, target)
    return name, buffer.getvalue()


def test_zip_traversal_ve_backslash_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "paket.zip"
    archive.write_bytes(
        _raw_zip_bytes(
            [
                ("ok.txt", b"iyi"),
                ("../disari.txt", b"kotu"),
                ("alt\\ters.txt", b"kotu"),
                ("/kokte.txt", b"kotu"),
            ]
        )
    )
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "../disari.txt" not in report.included
    assert "alt\\ters.txt" not in report.included
    assert "/kokte.txt" not in report.included
    assert report.reason_for("../disari.txt") == "arsiv traversal girdisi reddedildi"
    assert report.reason_for("alt\\ters.txt") == "arsiv traversal girdisi reddedildi"
    assert report.reason_for("/kokte.txt") == "arsiv traversal girdisi reddedildi"


def test_zip_drive_harfi_absolute_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "paket.zip"
    archive.write_bytes(
        _raw_zip_bytes(
            [
                ("ok.txt", b"iyi"),
                ("C:\\Users\\gizli.txt", b"kotu"),
                ("C:/Windows/system32", b"kotu"),
            ]
        )
    )
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "C:\\Users\\gizli.txt" not in report.included
    assert "C:/Windows/system32" not in report.included
    assert report.reason_for("C:\\Users\\gizli.txt") == "arsiv traversal girdisi reddedildi"


def test_zip_symlink_girdisi_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "kisayol.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("ok.txt", "iyi")
        info = zipfile.ZipInfo("kisayol.txt")
        info.create_system = 3  # Unix
        info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
        bundle.writestr(info, "/etc/passwd")
    archive.write_bytes(buffer.getvalue())
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "kisayol.txt" not in report.included
    assert report.reason_for("kisayol.txt") == "arsiv baglantisi reddedildi"


def test_zip_duplicate_girdi_fail_closed_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "cift.zip"
    archive.write_bytes(
        _zip_bytes([("ok.txt", b"iyi"), ("ayni.txt", b"bir"), ("ayni.txt", b"iki")])
    )
    with pytest.raises(PolicyViolation):
        ArchiveInspector().inspect(archive)


def test_zip_girdi_limiti_metadata_okunurken_uygulanir(tmp_path: Path) -> None:
    archive = tmp_path / "cok-girdi.zip"
    archive.write_bytes(_zip_bytes([(f"dosya-{i}.txt", b"x") for i in range(20)]))
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_entries=10)).inspect(archive)


def test_zip_boyut_limiti_metadata_okunurken_uygulanir(tmp_path: Path) -> None:
    archive = tmp_path / "buyuk.zip"
    archive.write_bytes(_zip_bytes([(f"dosya-{i}.txt", b"a" * 100) for i in range(5)]))
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_total_bytes=100)).inspect(archive)


def test_zip_metadata_arsiv_icerigini_toplu_okumaz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "akis.zip"
    archive.write_bytes(_zip_bytes([("ok.txt", b"iyi")]))

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("arsiv tamamini bellege okuma")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assert ArchiveInspector().inspect(archive).included == ("ok.txt",)


def test_zip_bozuk_merkezi_dizin_fail_closed_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "bozuk.zip"
    raw = bytearray(_zip_bytes([("ok.txt", b"iyi")]))
    struct.pack_into("<I", raw, len(raw) - 22 + 16, len(raw) + 100)
    archive.write_bytes(raw)
    with pytest.raises(PolicyViolation):
        ArchiveInspector().inspect(archive)


def test_tar_girdi_limiti_metadata_okunurken_uygulanir(tmp_path: Path) -> None:
    archive = tmp_path / "cok-girdi.tar"
    with tarfile.open(archive, "w") as bundle:
        for i in range(20):
            data = b"x"
            info = tarfile.TarInfo(f"dosya-{i}.txt")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_entries=10)).inspect(archive)


def test_tar_traversal_ve_symlink_reddedilir(tmp_path: Path) -> None:
    archive = tmp_path / "kotu.tar"
    with tarfile.open(archive, "w") as bundle:
        data = b"iyi"
        info = tarfile.TarInfo("ok.txt")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))

        link = tarfile.TarInfo("kisayol.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        bundle.addfile(link)

        traversal = tarfile.TarInfo("../disari.txt")
        traversal.size = 0
        bundle.addfile(traversal)
    report = ArchiveInspector().inspect(archive)
    assert "ok.txt" in report.included
    assert "kisayol.txt" not in report.included
    assert report.reason_for("kisayol.txt") == "arsiv baglantisi reddedildi"
    assert "../disari.txt" not in report.included
    assert report.reason_for("../disari.txt") == "arsiv traversal girdisi reddedildi"


def test_tar_bomba_getmembers_olmadan_kesilir(tmp_path: Path) -> None:
    """Cok girdili tar, limit asiminda tum metadata bellege alinmadan kesilir."""

    archive = tmp_path / "bomba.tar"
    with tarfile.open(archive, "w") as bundle:
        for i in range(2000):
            info = tarfile.TarInfo(f"dosya-{i}.txt")
            info.size = 0
            bundle.addfile(info)
    with pytest.raises(PolicyViolation):
        ArchiveInspector(ScanLimits(max_entries=10)).inspect(archive)
