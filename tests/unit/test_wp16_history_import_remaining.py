from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from zekam.application import history_import as history
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import ScanLimits
from zekam.domain.session_continuity import DataClassification, DigestReference, TruthClass
from zekam.domain.transcript_corpus import TranscriptCorpusEntry

DIGEST = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    "changes",
    (
        {"exclude": tuple(f"filter-{index:02d}" for index in range(65))},
        {"exclude": ("",)},
        {"exclude": ("DUPLICATE", "duplicate")},
        {"project_ref": "   "},
        {"scope_ref": "windows\\path"},
    ),
)
def test_filter_rejects_bound_normalization_duplicate_and_nonportable_scope(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationFailed):
        history.HistoryImportFilter(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ("", "../history.zip", "history.txt"))
def test_request_rejects_empty_or_non_filename_zip(value: str) -> None:
    values = {
        "corpus_id": "corpus",
        "source_name": "history.zip",
        "classification": DataClassification.LOCAL_ONLY,
        "source_policy_digest": DIGEST,
        "requested_by": "human",
        "filters": history.HistoryImportFilter(),
    }
    key = "corpus_id" if not value else "source_name"
    values[key] = value
    with pytest.raises(ValidationFailed):
        history.HistoryImportRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("reason", "count"), (("", 0), ("valid", -1)))
def test_count_rejects_empty_reason_and_negative_count(reason: str, count: int) -> None:
    with pytest.raises(ValidationFailed):
        history.HistoryImportCount(reason, count)


@pytest.mark.parametrize("part_size", (0, 129))
def test_service_rejects_part_size_outside_closed_bounds(part_size: int) -> None:
    with pytest.raises(ValidationFailed, match=r"1\.\.128"):
        history.HistoryImportService(part_size=part_size)


def _entry(*, date: str | None = None, title: str | None = None) -> TranscriptCorpusEntry:
    values = {
        "relative_path": "video.txt",
        "file_digest": DIGEST,
        "content_digest": DIGEST,
        "byte_size": 10,
        "line_count": 1,
        "unit_count": 1,
        "title": title,
        "video_id": "video-id",
        "declared_date": date,
    }
    return TranscriptCorpusEntry(**values)  # type: ignore[arg-type]


def test_entry_filter_reason_covers_source_date_and_exclusion_matrix() -> None:
    assert history._entry_filter_reason(_entry(), history.HistoryImportFilter(source_types=())) == (
        "source-type-excluded"
    )
    assert (
        history._entry_filter_reason(
            _entry(), history.HistoryImportFilter(date_from=dt.date(2026, 9, 1))
        )
        == "date-missing"
    )
    assert (
        history._entry_filter_reason(
            _entry(date="2026-08-31"), history.HistoryImportFilter(date_from=dt.date(2026, 9, 1))
        )
        == "date-before-range"
    )
    assert (
        history._entry_filter_reason(
            _entry(date="2026-09-05"), history.HistoryImportFilter(date_to=dt.date(2026, 9, 4))
        )
        == "date-after-range"
    )
    assert (
        history._entry_filter_reason(
            _entry(title="Sensitive Topic"), history.HistoryImportFilter(exclude=("sensitive",))
        )
        == "exclude-filter"
    )
    assert history._entry_filter_reason(_entry(), history.HistoryImportFilter()) is None


def test_source_boundary_rejects_relative_missing_directory_hardlink_and_empty(
    tmp_path: Path,
) -> None:
    limits = ScanLimits(max_total_bytes=10)
    with pytest.raises(PolicyViolation, match="absolute"):
        history._assert_explicit_safe_source(Path("relative.zip"), limits)
    with pytest.raises(ValidationFailed, match="okunamadi"):
        history._assert_explicit_safe_source(tmp_path / "missing.zip", limits)
    with pytest.raises(PolicyViolation, match="special"):
        history._assert_explicit_safe_source(tmp_path, limits)
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    with pytest.raises(PolicyViolation, match="byte siniri"):
        history._assert_explicit_safe_source(empty, limits)
    source = tmp_path / "source.zip"
    source.write_bytes(b"safe")
    hardlink = tmp_path / "hardlink.zip"
    os.link(source, hardlink)
    with pytest.raises(PolicyViolation, match="hardlink"):
        history._assert_explicit_safe_source(source, limits)


def test_apply_plan_rejects_cursor_part_and_authority_drift() -> None:
    reference = DigestReference("source", DIGEST, TruthClass.UNKNOWN)
    base = {
        "preview_digest": DIGEST,
        "consent_digest": DIGEST,
        "source_versions": (reference,),
        "cursor_start": 0,
        "cursor_end": 1,
        "total_sources": 1,
        "part_size": 1,
        "source_watermark": DIGEST,
        "idempotency_key": DIGEST,
    }
    for changes in (
        {"cursor_start": -1},
        {"cursor_end": 2},
        {"part_size": 0},
        {"candidate_only": False},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            history.HistoryImportApplyPlan(**(base | changes))  # type: ignore[arg-type]
