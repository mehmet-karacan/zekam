"""Provider document/OCR contract regressions; no live provider or OCR execution."""

import pytest

from zekam.application import knowledge_parsers as subject
from zekam.domain.canonical import canonical_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import ContentUnit, Locator, SourceFormat, UnitKind


def ocr_block(**changes):
    return {
        "text": "Kaynak metni",
        "page": 1,
        "bbox": (0.0, 0.0, 1.0, 1.0),
        "confidence": 0.9,
    } | changes


@pytest.mark.parametrize("page", [True, "2", 1.9, 0, -1, float("nan"), float("inf")])
def test_ocr_does_not_invent_integer_page_by_conversion(page):
    parser = subject.OcrParser(lambda _payload: (ocr_block(page=page),))
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


@pytest.mark.parametrize(
    "confidence", [True, "0.9", -0.1, 1.1, float("nan"), float("inf"), 10**1000]
)
def test_ocr_validates_confidence_before_threshold_filter(confidence):
    parser = subject.OcrParser(
        lambda _payload: (ocr_block(), ocr_block(confidence=confidence)), minimum_confidence=0.5
    )
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


@pytest.mark.parametrize(
    "bbox",
    ["0,0,1,1", ("0", 0, 1, 1), (False, 0, 1, 1), (float("nan"), 0, 1, 1), (0, 0, 1), (1, 0, 0, 1)],
)
def test_ocr_does_not_coerce_invalid_coordinates(bbox):
    parser = subject.OcrParser(lambda _payload: (ocr_block(bbox=bbox),))
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


@pytest.mark.parametrize(
    "threshold", [True, "0.5", -0.1, 1.1, float("nan"), float("inf"), 10**1000]
)
def test_ocr_rejects_invalid_threshold_before_provider_call(threshold):
    calls = []
    with pytest.raises(ValidationFailed):
        subject.OcrParser(lambda payload: calls.append(payload), minimum_confidence=threshold)
    assert not calls


@pytest.mark.parametrize("text", [None, True, 123, {"not": "text"}])
@pytest.mark.parametrize("structured", [False, True])
def test_provider_values_cannot_become_source_text_through_str(text, structured):
    if structured:
        parser = subject.StructuredDocumentParser(
            lambda _: ({"text": text, "kind": "paragraph", "page": 1},), SourceFormat.PDF
        )
    else:
        parser = subject.OcrParser(lambda _: (ocr_block(text=text),))
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


@pytest.mark.parametrize("blocks", [None, "text", b"bytes", {"text": "mapping"}, [None]])
def test_provider_requires_a_block_sequence_of_objects(blocks):
    parser = subject.OcrParser(lambda _: blocks)
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


def test_structured_pdf_preserves_exact_bbox_and_table_locator():
    parser = subject.StructuredDocumentParser(
        lambda _: (
            {
                "text": "A | B",
                "kind": "table",
                "page": 3,
                "block_index": 7,
                "bbox": [0.1, 0.2, 0.8, 0.9],
            },
        ),
        SourceFormat.PDF,
    )
    unit = parser.parse(b"fixture")[0]
    assert unit.kind is UnitKind.TABLE
    assert unit.locator.page == 3 and unit.locator.block_index == 7
    assert unit.locator.bbox == (0.1, 0.2, 0.8, 0.9)


@pytest.mark.parametrize("heading", ["Baslik", [None], [""], ["Baslik"] * 65, ["a" * 513]])
def test_structured_docx_rejects_invalid_heading_path(heading):
    parser = subject.StructuredDocumentParser(
        lambda _: ({"text": "Metin", "kind": "paragraph", "heading_path": heading},),
        SourceFormat.DOCX,
    )
    with pytest.raises(ValidationFailed):
        parser.parse(b"fixture")


@pytest.mark.parametrize("format", ["pdf", SourceFormat.TXT, None, []])
def test_structured_format_cannot_bypass_pdf_page_contract(format):
    with pytest.raises(ValidationFailed):
        subject.StructuredDocumentParser(lambda _: (), format)


def test_unknown_structured_kind_is_not_echoed_as_source_or_error():
    private_value = "untrusted-provider-value-do-not-echo"
    parser = subject.StructuredDocumentParser(
        lambda _: ({"text": "Metin", "kind": private_value, "page": 1},), SourceFormat.PDF
    )
    with pytest.raises(ValidationFailed) as captured:
        parser.parse(b"fixture")
    assert private_value not in str(captured.value)


@pytest.mark.parametrize("structured", [False, True])
def test_provider_block_stream_stops_on_cap_plus_one_without_partial_output(
    monkeypatch, structured
):
    monkeypatch.setattr(subject, "MAX_PROVIDER_BLOCKS", 2)
    produced = []

    def blocks(_payload):
        for index in range(100):
            produced.append(index)
            yield {"text": "Metin", "kind": "paragraph", "page": 1} if structured else ocr_block()

    parser = (
        subject.StructuredDocumentParser(blocks, SourceFormat.PDF)
        if structured
        else subject.OcrParser(blocks)
    )
    with pytest.raises(PolicyViolation, match="blok sayisi"):
        parser.parse(b"fixture")
    assert produced == [0, 1, 2]


def test_output_budget_counts_filtered_low_confidence_blocks(monkeypatch):
    block = ocr_block()
    sample = subject.OcrParser(lambda _: (block,)).parse(b"fixture")[0]
    monkeypatch.setattr(
        subject, "MAX_PROVIDER_OUTPUT_BYTES", len(canonical_bytes(sample.body())) + 10
    )
    parser = subject.OcrParser(lambda _: (block, ocr_block(confidence=0.1)), minimum_confidence=0.5)
    with pytest.raises(PolicyViolation, match="normalize cikti"):
        parser.parse(b"fixture")


@pytest.mark.parametrize(
    "fields",
    [
        {"page": True},
        {"block_index": -1},
        {"block_index": False},
        {"line_start": 1.5, "line_end": 2},
        {"timestamp_start_ms": True},
        {"heading_path": "Baslik"},
    ],
)
def test_domain_locator_rejects_false_numeric_and_heading_evidence(fields):
    with pytest.raises(ValidationFailed):
        Locator(**fields)


def test_valid_domain_body_keeps_existing_locator_serialization():
    unit = ContentUnit("unit-1", UnitKind.PARAGRAPH, "Metin", Locator(page=2, block_index=0), 0)
    assert unit.body()["locator"]["page"] == 2
    assert unit.body()["locator"]["block_index"] == 0
    assert unit.body()["text"] == "Metin"
