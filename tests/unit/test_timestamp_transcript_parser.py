"""ZK-P2-001 timestamp transcript parser kabul testleri."""

from __future__ import annotations

import pytest

from zekam.application.knowledge_parsers import TimestampTranscriptParser
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.retrieval import ChunkProfile, chunk_units
from zekam.infrastructure.postgres.retrieval_repository import _locator_from


def test_timestamp_range_citation_round_trip() -> None:
    parser = TimestampTranscriptParser(
        entry_path="avenoxai/video-1.txt",
        video_id="1hHOgnmAHlE",
    )
    units = parser.parse(b"[05:12 - 07:08] Baglam dikkatle sinirlanir.\n")

    assert len(units) == 1
    assert units[0].kind is UnitKind.TRANSCRIPT_SEGMENT
    expected = {
        "entry_path": "avenoxai/video-1.txt",
        "line_start": 1,
        "line_end": 1,
        "timestamp_start_ms": 312_000,
        "timestamp_end_ms": 428_000,
        "video_id": "1hHOgnmAHlE",
    }
    encoded = units[0].locator.as_dict()
    assert {key: encoded[key] for key in expected} == expected
    assert _locator_from(encoded) == units[0].locator


def test_single_timestamp_preserves_start_without_inventing_end_or_speaker() -> None:
    parser = TimestampTranscriptParser(entry_path="devdan/tek.txt")
    unit = parser.parse(b"01:02:03.450 Konusmaci belirtilmeyen metin\n")[0]

    assert unit.kind is UnitKind.TRANSCRIPT_SEGMENT
    assert unit.text == "Konusmaci belirtilmeyen metin"
    assert unit.locator.timestamp_start_ms == 3_723_450
    assert unit.locator.timestamp_end_ms is None
    assert "speaker" not in unit.body()
    assert "speaker" not in unit.locator.as_dict()


def test_unparseable_timestamp_keeps_line_and_does_not_invent_timestamp() -> None:
    parser = TimestampTranscriptParser(entry_path="devdan/bozuk.txt")
    unit = parser.parse(b"00:99 bu timestamp gecersizdir\n")[0]

    assert unit.kind is UnitKind.PARAGRAPH
    assert unit.locator.line_start == unit.locator.line_end == 1
    assert unit.locator.timestamp_start_ms is None
    assert unit.locator.timestamp_end_ms is None


def test_metadata_heading_and_transcript_are_separate_units() -> None:
    payload = b"""Title: Ornek video
Video ID: abc123
# Bolum
[00:10 --> 00:15] Ilk segment
"""
    units = TimestampTranscriptParser(entry_path="corpus/video.txt", video_id="abc123").parse(
        payload
    )

    assert [unit.kind for unit in units] == [
        UnitKind.METADATA,
        UnitKind.METADATA,
        UnitKind.TRANSCRIPT_HEADING,
        UnitKind.TRANSCRIPT_SEGMENT,
    ]
    assert [unit.locator.line_start for unit in units] == [1, 2, 3, 4]


def test_plain_lines_are_merged_only_within_configured_bounds() -> None:
    parser = TimestampTranscriptParser(entry_path="corpus/video.txt", max_merged_lines=2)
    units = parser.parse(b"bir\niki\nuc\ndort\n")

    assert [unit.text for unit in units] == ["bir\niki", "uc\ndort"]
    assert [(unit.locator.line_start, unit.locator.line_end) for unit in units] == [
        (1, 2),
        (3, 4),
    ]


def test_plain_merge_budget_counts_inserted_newlines_at_exact_boundary() -> None:
    units = TimestampTranscriptParser(entry_path="corpus/video.txt", max_merged_chars=4).parse(
        b"aa\naa\n"
    )

    assert [unit.text for unit in units] == ["aa", "aa"]
    assert all(len(unit.text) <= 4 for unit in units)


@pytest.mark.parametrize(
    "source",
    [
        b"[00:10-00:05] ters\n",
        b"[00:10-00:10] sifir aralik\n",
        b"[00:10-00:99] gecersiz bitis\n",
    ],
)
def test_invalid_ranges_preserve_source_line_without_timestamp(source: bytes) -> None:
    unit = TimestampTranscriptParser(entry_path="corpus/video.txt").parse(source)[0]

    assert unit.kind is UnitKind.PARAGRAPH
    assert unit.locator.line_start == unit.locator.line_end == 1
    assert unit.locator.timestamp_start_ms is None
    assert unit.locator.timestamp_end_ms is None


def test_transcript_segments_do_not_merge_in_chunker_and_keep_exact_ranges() -> None:
    parser = TimestampTranscriptParser(entry_path="corpus/video.txt")
    units = parser.parse(b"[00:01-00:02] bir\n[00:03-00:04] iki\n")
    chunks = chunk_units(
        units,
        document_id="transcript-1",
        profile=ChunkProfile(name="transcript", max_tokens=512),
    )

    assert len(chunks) == 2
    assert [chunk.locator.timestamp_start_ms for chunk in chunks] == [1000, 3000]
    assert [chunk.locator.timestamp_end_ms for chunk in chunks] == [2000, 4000]


def test_locator_and_parser_reject_invalid_or_unsafe_values() -> None:
    with pytest.raises(PolicyViolation):
        TimestampTranscriptParser(entry_path="../disari.txt")
    with pytest.raises(ValidationFailed):
        Locator(entry_path="video.txt", timestamp_start_ms=2000, timestamp_end_ms=1000)
    unit = TimestampTranscriptParser(entry_path="video.txt").parse(b"[00:61-00:62] bozuk\n")[0]
    assert unit.locator.timestamp_start_ms is None


def test_parser_profile_is_stable_and_explicitly_disables_speaker_inference() -> None:
    profile = TimestampTranscriptParser(entry_path="video.txt").parser_profile

    assert profile["adapter"] == "zekam.parser.timestamp-transcript"
    assert profile["adapter_version"] == "1"
    assert profile["speaker_inference"] is False
