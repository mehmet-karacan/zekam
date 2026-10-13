"""ODI Smart Export XML deklarasyon siniri."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.odi11g_smart_export import _validate_smart_xml
from zekam.domain.errors import PolicyViolation, ValidationFailed


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_smart_xml_rejects_dtd_for_supported_encodings(tmp_path: Path, encoding: str) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_bytes(
        ('<!DOCTYPE SunopsisExport [<!ENTITY x "synthetic">]>'
         '<SunopsisExport><SmartExportList />&x;</SunopsisExport>').encode(encoding)
    )
    with pytest.raises(PolicyViolation, match="DTD/entity"):
        _validate_smart_xml(source)


def test_smart_xml_rejects_external_dtd(tmp_path: Path) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_bytes(
        b'<!DOCTYPE SunopsisExport SYSTEM "https://example.invalid/x.dtd">'
        b'<SunopsisExport><SmartExportList /></SunopsisExport>'
    )
    with pytest.raises(PolicyViolation, match="DTD/entity"):
        _validate_smart_xml(source)


def test_smart_xml_accepts_plain_utf16_and_rejects_malformed(tmp_path: Path) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_bytes('<SunopsisExport><SmartExportList /></SunopsisExport>'.encode("utf-16"))
    source_digest, count = _validate_smart_xml(source)
    assert source_digest.startswith("sha256:")
    assert count == 0

    source.write_bytes(b"<SunopsisExport>")
    with pytest.raises(ValidationFailed, match="parse"):
        _validate_smart_xml(source)
