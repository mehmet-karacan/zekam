"""Izin verici exact wheel gate'i gecen PDFium ile gercek PDF fixture testleri."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.domain.knowledge import UnitKind
from zekam.infrastructure.knowledge.document_parsers import PdfParser

pytest.importorskip("pypdfium2")

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures" / "knowledge"


def test_digital_pdf_preserves_real_page_numbers() -> None:
    parser = PdfParser()
    units = parser.parse((FIXTURES / "digital-2page.pdf").read_bytes())
    assert [unit.locator.page for unit in units] == [1, 2]
    assert all(unit.kind is UnitKind.PARAGRAPH for unit in units)
    assert parser.parser_profile["license_gate"] == ("exact-wheel-build-licenses-allowlisted")


def test_scanned_pdf_uses_page_scoped_ocr_fallback() -> None:
    units = PdfParser().parse((FIXTURES / "scanned-2page.pdf").read_bytes())
    assert {unit.locator.page for unit in units} == {1, 2}
    assert all(unit.kind is UnitKind.OCR_BLOCK for unit in units)
    assert all(unit.locator.bbox and unit.confidence is not None for unit in units)
