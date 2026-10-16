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
        (
            '<!DOCTYPE SunopsisExport [<!ENTITY x "synthetic">]>'
            "<SunopsisExport><SmartExportList />&x;</SunopsisExport>"
        ).encode(encoding)
    )
    with pytest.raises(PolicyViolation, match="DTD/entity"):
        _validate_smart_xml(source)


def test_smart_xml_rejects_external_dtd(tmp_path: Path) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_bytes(
        b'<!DOCTYPE SunopsisExport SYSTEM "https://example.invalid/x.dtd">'
        b"<SunopsisExport><SmartExportList /></SunopsisExport>"
    )
    with pytest.raises(PolicyViolation, match="DTD/entity"):
        _validate_smart_xml(source)


def test_smart_xml_accepts_plain_utf16_and_rejects_malformed(tmp_path: Path) -> None:
    source = tmp_path / "SmartExport.xml"
    source.write_bytes("<SunopsisExport><SmartExportList /></SunopsisExport>".encode("utf-16"))
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
    original_scan = smart_export._scan_smart_export

    def mutate_after_parse(path: Path, *, collect):
        parsed = original_scan(path, collect=collect)
        path.write_text(
            path.read_text(encoding="utf-8").replace("MODEL_A", "MODEL_B"),
            encoding="utf-8",
        )
        return parsed

    monkeypatch.setattr(smart_export, "_scan_smart_export", mutate_after_parse)

    with pytest.raises(PolicyViolation, match="degisti"):
        smart_export.build_sanitized_odi_plan(
            project_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_slug="gpu",
            source=source,
        )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_guard_rejects_declaration_split_across_single_byte_reads(tmp_path, monkeypatch, encoding):
    monkeypatch.setattr(smart_export, "_XML_READ_BYTES", 1)
    source = tmp_path / "split.xml"
    source.write_bytes(
        (
            '<!DOCTYPE SunopsisExport [<!ENTITY value "must-not-expand">]>'
            "<SunopsisExport><SmartExportList/>&value;</SunopsisExport>"
        ).encode(encoding)
    )
    with pytest.raises(PolicyViolation, match="DTD/entity"):
        smart_export._scan_smart_export(source, collect=True)


def test_guard_parse_hash_share_one_file_descriptor(tmp_path, monkeypatch):
    from zekam.domain.canonical import digest_of_bytes

    source = tmp_path / "single.xml"
    raw = b'<SunopsisExport><SmartExportList/><Object class="SnpModel"><Field name="IMod">1</Field><Other name="IMod">not-a-field</Other></Object></SunopsisExport>'
    source.write_bytes(raw)
    opened = []
    original = smart_export.os.open

    def record(path, flags, *args, **kwargs):
        descriptor = original(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(smart_export.os, "open", record)
    monkeypatch.setattr(
        smart_export, "_sha256_file", lambda _path: pytest.fail("scan re-opened file for hash")
    )
    key, count, objects, excluded = smart_export._scan_smart_export(source, collect=True)
    assert key == digest_of_bytes(raw) and count == 1
    assert objects["SnpModel"] == [{"IMod": "1"}] and not excluded
    assert len(opened) == 1
    with pytest.raises(OSError):
        smart_export.os.fstat(opened[0])


@pytest.mark.parametrize(
    "limit,value,xml",
    [
        (
            "_MAX_XML_DEPTH",
            2,
            "<SunopsisExport><SmartExportList><nested/></SmartExportList></SunopsisExport>",
        ),
        ("_MAX_XML_ELEMENTS", 2, "<SunopsisExport><SmartExportList/><extra/></SunopsisExport>"),
        (
            "_MAX_SMART_OBJECTS",
            1,
            "<SunopsisExport><SmartExportList/><Object/><Object/></SunopsisExport>",
        ),
        ("_MAX_EXPORT_BYTES", 20, "<SunopsisExport><SmartExportList/></SunopsisExport>"),
    ],
)
def test_stream_budgets_never_return_partial_objects(tmp_path, monkeypatch, limit, value, xml):
    source = tmp_path / "bounded.xml"
    source.write_text(xml, encoding="utf-8")
    monkeypatch.setattr(smart_export, limit, value)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        smart_export._scan_smart_export(source, collect=True)


def test_many_objects_are_extracted_without_losing_allowed_fields(tmp_path, monkeypatch):
    source = tmp_path / "objects.xml"
    raw = (
        "<SunopsisExport><SmartExportList/>"
        + "".join(
            f'<Object class="SnpModel"><Field name="IMod">{number}</Field><Field name="SecretField">excluded</Field></Object>'
            for number in range(1000)
        )
        + "</SunopsisExport>"
    ).encode()
    source.write_bytes(raw)
    monkeypatch.setattr(smart_export, "_XML_READ_BYTES", 113)
    key, count, objects, _excluded = smart_export._scan_smart_export(source, collect=True)
    from zekam.domain.canonical import digest_of_bytes

    assert key == digest_of_bytes(raw) and count == 1000
    assert objects["SnpModel"] == [{"IMod": str(number)} for number in range(1000)]


@pytest.mark.parametrize("values", [("one", "two"), ("two", "one")])
def test_conflicting_field_values_reject_in_either_order(tmp_path, values):
    source = tmp_path / "fields.xml"
    first, second = values
    source.write_text(
        '<SunopsisExport><SmartExportList/><Object class="SnpModel">'
        f'<Field name="IMod">{first}</Field><Field name="IMod">{second}</Field>'
        "</Object></SunopsisExport>",
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="conflicting field"):
        smart_export._scan_smart_export(source, collect=True)


@pytest.mark.parametrize("class_name,field", list(smart_export._INDEX_IDENTITIES.items()))
def test_conflicting_index_identity_rejects_without_order_dependent_lineage(class_name, field):
    first = {field: "1"}
    second = {field: "1", "synthetic_design_field": "different"}
    for rows in ([first, second], [second, first]):
        with pytest.raises(PolicyViolation, match="conflicting object identity"):
            smart_export._validate_index_identities({class_name: rows})


def test_identical_identity_and_composite_parent_repetitions_are_allowed():
    model = {"IMod": "1", "ModName": "same"}
    smart_export._validate_index_identities(
        {
            "SnpModel": [model, dict(model)],
            "SnpKeyCol": [{"IKey": "1", "ICol": "1"}, {"IKey": "1", "ICol": "2"}],
        }
    )


def test_sanitizer_rejects_conflicting_models_before_lineage(tmp_path):
    source = tmp_path / "models.xml"
    source.write_text(
        "<SunopsisExport><SmartExportList/>"
        '<Object class="SnpModel"><Field name="IMod">1</Field><Field name="ModName">first</Field></Object>'
        '<Object class="SnpModel"><Field name="IMod">1</Field><Field name="ModName">second</Field></Object>'
        "</SunopsisExport>",
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="conflicting object identity"):
        smart_export.build_sanitized_odi_plan(
            project_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_slug="gpu",
            source=source,
        )
