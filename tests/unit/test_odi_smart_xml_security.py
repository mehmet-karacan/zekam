"""ODI Smart Export XML deklarasyon siniri."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from zekam.application import odi11g_smart_export as smart_export
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


def test_smart_export_rejects_source_mutation_after_object_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<SunopsisExport><SmartExportList />
<Object class="com.sunopsis.dwg.dbobj.SnpModel">
  <Field name="IMod">1</Field>
  <Field name="ModName">MODEL_A</Field>
  <Field name="LschemaName">LOG.GPU</Field>
</Object>
</SunopsisExport>""",
        encoding="utf-8",
    )
    original_objects = smart_export._objects

    def mutate_after_parse(path: Path):
        parsed = original_objects(path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("MODEL_A", "MODEL_B"),
            encoding="utf-8",
        )
        return parsed

    monkeypatch.setattr(smart_export, "_objects", mutate_after_parse)

    with pytest.raises(PolicyViolation, match="degisti"):
        smart_export.build_sanitized_odi_plan(
            project_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_slug="gpu",
            source=source,
        )
