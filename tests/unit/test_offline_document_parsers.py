"""Cevrimdisi belge parser kontratlari."""

from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from zekam.application.knowledge_parsers import default_router
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator, SourceFormat, UnitKind
from zekam.infrastructure.knowledge import document_parsers
from zekam.infrastructure.knowledge.document_parsers import DocxParser, PdfParser

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "knowledge"


def test_docx_real_binary_preserves_heading_path_and_table() -> None:
    units = DocxParser().parse((FIXTURES / "heading-table.docx").read_bytes())
    assert [unit.kind for unit in units].count(UnitKind.HEADING) == 2
    table = next(unit for unit in units if unit.kind is UnitKind.TABLE)
    assert table.locator.heading_path == ("Cevrimdisi Belge", "Tablo Bolumu")
    assert "Alan | Deger" in table.text
    assert "Calisma | Cevrimdisi" in table.text
    assert all(unit.locator.page is None for unit in units)


def test_docx_profile_declares_only_stdlib_permissive_component() -> None:
    profile = DocxParser().parser_profile
    assert profile["license_ids"] == ["PSF-2.0"]
    assert profile["xml_engine"] == "python-stdlib-elementtree"
    assert all(not str(value).startswith("/") for value in profile.values())


def test_docx_rejects_external_relationship() -> None:
    source = FIXTURES / "heading-table.docx"
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
        for info in original.infolist():
            payload = original.read(info.filename)
            if info.filename == "word/_rels/document.xml.rels":
                payload = payload.replace(
                    b"</Relationships>",
                    b'<Relationship Id="external" Type="urn:test" '
                    b'Target="https://example.invalid/x" TargetMode="External"/>'
                    b"</Relationships>",
                )
            changed.writestr(info, payload)
    with pytest.raises(PolicyViolation, match="external relationship"):
        DocxParser().parse(output.getvalue())


def test_docx_rejects_zip_traversal() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("[Content_Types].xml", "<Types/>")
        bundle.writestr("word/document.xml", "<document/>")
        bundle.writestr("../escape", "x")
    with pytest.raises(PolicyViolation, match="traversal"):
        DocxParser().parse(output.getvalue())


def test_normalized_bbox_contract_is_top_left_zero_to_one() -> None:
    assert Locator(page=1, bbox=(0.1, 0.2, 0.8, 0.9)).bbox is not None
    with pytest.raises(ValidationFailed, match=r"0\.\.1"):
        Locator(page=1, bbox=(-0.1, 0.2, 0.8, 0.9))
    with pytest.raises(ValidationFailed, match="sirasi"):
        Locator(page=1, bbox=(0.8, 0.2, 0.1, 0.9))


def test_default_router_declares_every_offline_document_format() -> None:
    supported = set(default_router().supported())
    assert {
        SourceFormat.DOCX,
        SourceFormat.PDF,
        SourceFormat.PNG,
        SourceFormat.JPEG,
        SourceFormat.TIFF,
    } <= supported


def test_pdf_without_optional_permissive_dependency_fails_closed() -> None:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        with pytest.raises(ConfigurationError, match="knowledge-docs"):
            PdfParser().parse(b"%PDF-1.7\n")


@pytest.mark.parametrize(
    ("banner", "expected"),
    (
        ("tesseract v5.4.0.20240606\n leptonica-1.84.1", "5.4.0.20240606"),
        ("tesseract 5.3.4\n leptonica-1.82.0", "5.3.4"),
    ),
)
def test_tesseract_version_accepts_optional_portable_v_prefix(
    monkeypatch: pytest.MonkeyPatch, banner: str, expected: str
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=banner),
    )

    assert document_parsers._tesseract_version("tesseract") == expected


def test_tesseract_requested_turkish_language_has_no_english_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "eng.traineddata").write_bytes(b"english-only")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=f'List of available languages in "{tmp_path}" (1):\neng\n'
        ),
    )

    with pytest.raises(ConfigurationError, match="Tesseract dili bulunamadi: tur"):
        document_parsers._tessdata_digests("tesseract", ("tur", "eng"))
