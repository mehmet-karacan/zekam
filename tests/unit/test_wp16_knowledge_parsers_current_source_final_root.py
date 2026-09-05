"""Current-source adversarial branch closure for knowledge parser boundaries."""

from __future__ import annotations

from typing import Any

import pytest

from zekam.application.knowledge_parsers import (
    MarkdownParser,
    OcrParser,
    PlainTextParser,
    StructuredDocumentParser,
    TimestampTranscriptParser,
    _timestamp_ms,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import SourceFormat, UnitKind


def test_timestamp_conversion_rejects_shape_and_component_overflow() -> None:
    with pytest.raises(ValidationFailed, match="bicimi"):
        _timestamp_ms("1")
    with pytest.raises(ValidationFailed, match="saniyesi"):
        _timestamp_ms("00:60")
    with pytest.raises(ValidationFailed, match="dakikasi"):
        _timestamp_ms("01:60:00")
    assert _timestamp_ms("01:02.3") == 62_300
    assert _timestamp_ms("01:02:03,004") == 3_723_004


def test_plain_and_markdown_empty_or_malformed_inputs_fail_closed() -> None:
    with pytest.raises(ValidationFailed, match="metin kaynagi bos"):
        PlainTextParser().parse(b"\n\n")
    with pytest.raises(ValidationFailed, match="markdown kaynagi bos"):
        MarkdownParser().parse(b"")
    with pytest.raises(ValidationFailed, match="UTF-8"):
        PlainTextParser().parse(b"\xff")


def test_transcript_constructor_and_timestamp_like_lines_preserve_bad_data() -> None:
    with pytest.raises(ValidationFailed, match="video kimligi"):
        TimestampTranscriptParser("entry.md", video_id=" ")
    with pytest.raises(ValidationFailed, match="pozitif"):
        TimestampTranscriptParser("entry.md", max_merged_lines=0)
    with pytest.raises(ValidationFailed, match="pozitif"):
        TimestampTranscriptParser("entry.md", max_merged_chars=0)
    with pytest.raises(ValidationFailed, match="transcript kaynagi bos"):
        TimestampTranscriptParser("entry.md").parse(b"\n")

    parser = TimestampTranscriptParser("entry.md", max_merged_lines=1, max_merged_chars=8)
    units = parser.parse(
        b"[00:60 --> 00:61] invalid range\n"
        b"[00:02 --> 00:01] reversed\n"
        b"[00:60] invalid single\n"
        b"plain overflow\n"
        b"Title: Example\n"
        b"# Heading\n"
    )
    assert [unit.kind for unit in units][-2:] == [UnitKind.METADATA, UnitKind.TRANSCRIPT_HEADING]
    assert any("invalid range" in unit.text for unit in units)
    assert any("reversed" in unit.text for unit in units)
    assert any("invalid single" in unit.text for unit in units)


def test_ocr_missing_bbox_threshold_and_empty_results_are_rejected() -> None:
    with pytest.raises(ValidationFailed, match="eksik alan"):
        OcrParser(lambda _: ({"text": "x"},)).parse(b"x")
    with pytest.raises(ValidationFailed, match="dort deger"):
        OcrParser(lambda _: ({"text": "x", "page": 1, "bbox": (0, 1), "confidence": 1},)).parse(
            b"x"
        )
    with pytest.raises(ValidationFailed, match="OCR sonucu bos"):
        OcrParser(
            lambda _: ({"text": "x", "page": 1, "bbox": (0, 0, 1, 1), "confidence": 0.2},),
            minimum_confidence=0.5,
        ).parse(b"x")


@pytest.mark.parametrize(
    "blocks,error",
    (
        (({"kind": "paragraph"},), "text ve kind"),
        (({"text": "x"},), "text ve kind"),
        (({"text": "x", "kind": "paragraph"},), "locator ister"),
        ((), "yapili belge bos"),
    ),
)
def test_structured_document_requires_exact_kind_text_and_locator(
    blocks: tuple[dict[str, Any], ...], error: str
) -> None:
    parser = StructuredDocumentParser(lambda _: blocks, SourceFormat.PDF)
    with pytest.raises(ValidationFailed, match=error):
        parser.parse(b"payload")


def test_docx_page_number_is_never_fabricated() -> None:
    parser = StructuredDocumentParser(
        lambda _: ({"text": "x", "kind": "paragraph", "page": 1},), SourceFormat.DOCX
    )
    with pytest.raises(PolicyViolation, match="sayfa numarasi"):
        parser.parse(b"payload")
