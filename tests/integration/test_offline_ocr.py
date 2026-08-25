"""Gercek yerel Tesseract ile OCR binary fixture testleri."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from zekam.domain.knowledge import SourceFormat, UnitKind
from zekam.infrastructure.knowledge.document_parsers import (
    TesseractOcrParser,
    _tesseract_version,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Gercek OCR entegrasyonu icin optional Tesseract binary gerekli",
    ),
]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "knowledge"


def test_real_tesseract_optional_v_banner_parses_to_numeric_version() -> None:
    executable = shutil.which("tesseract")
    assert executable is not None

    version = _tesseract_version(executable)

    assert version[0].isdigit()
    assert not version.casefold().startswith("v")


@pytest.mark.parametrize(
    ("filename", "source_format"),
    (("ocr.png", SourceFormat.PNG), ("ocr.jpg", SourceFormat.JPEG)),
)
def test_real_ocr_image_has_normalized_locator_and_confidence(
    filename: str, source_format: SourceFormat
) -> None:
    parser = TesseractOcrParser(source_format)
    units = parser.parse((FIXTURES / filename).read_bytes())
    text = " ".join(unit.text for unit in units).upper()
    assert {"CEVRIMDISI", "BIRINCI", "SAYFA"} <= set(text.split())
    assert all(unit.kind is UnitKind.OCR_BLOCK for unit in units)
    assert all(unit.locator.page == 1 and unit.locator.bbox for unit in units)
    assert all(unit.confidence is not None and 0 <= unit.confidence <= 1 for unit in units)


def test_real_ocr_multipage_tiff_preserves_page_numbers() -> None:
    parser = TesseractOcrParser(SourceFormat.TIFF)
    units = parser.parse((FIXTURES / "ocr-multipage.tiff").read_bytes())
    assert {unit.locator.page for unit in units} == {1, 2}
    second = " ".join(unit.text for unit in units if unit.locator.page == 2).upper()
    assert {"YEREL", "TESSERACT", "IKINCI", "SAYFA"} <= set(second.split())


def test_tesseract_profile_has_versions_hashes_and_no_absolute_paths() -> None:
    profile = TesseractOcrParser(SourceFormat.PNG).parser_profile
    assert profile["engine_license"] == "Apache-2.0"
    assert profile["image_library_license"] == "BSD-2-Clause"
    assert set(profile["tessdata_digests"]) == {"tur", "eng"}
    assert "/opt/" not in str(profile)
