from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from zekam.application.history_import import (
    HistoryImportConsent,
    HistoryImportFilter,
    HistoryImportPreview,
    HistoryImportRequest,
    HistoryImportService,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 8, 26, 11, 0, tzinfo=dt.UTC)
POLICY = digest("private-history-policy")


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in entries.items():
            archive.writestr(path, payload)
    return output.getvalue()


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "history.zip"
    path.write_bytes(
        _zip(
            {
                "include.txt": b"Date: 2026-08-20\n[00:01-00:02] keep\n",
                "skip-me.txt": b"Date: 2026-08-21\n[00:01-00:02] skip\n",
            }
        )
    )
    return path


def _request(filters: HistoryImportFilter | None = None) -> HistoryImportRequest:
    return HistoryImportRequest(
        corpus_id="private-history-1",
        source_name="history.zip",
        classification=DataClassification.LOCAL_ONLY,
        source_policy_digest=POLICY,
        requested_by="human-user",
        filters=filters or HistoryImportFilter(),
    )


def _consent(preview: HistoryImportPreview) -> HistoryImportConsent:
    return HistoryImportConsent(
        preview_digest=preview.preview_digest,
        archive_digest=preview.archive_digest,
        filter_digest=preview.filter_digest,
        classification=preview.classification,
        approved_by="human-user",
        approved_at=NOW + dt.timedelta(minutes=1),
        explicit=True,
    )


@dataclass
class _Stored:
    digest: str


class _SpyStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: list[dict[str, str]] = []

    def put(
        self,
        payload: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> _Stored:
        object_digest = digest_of_bytes(payload)
        self.objects[object_digest] = payload
        self.metadata.append(dict(metadata or {}))
        return _Stored(object_digest)

    def exists(self, object_digest: str) -> bool:
        return object_digest in self.objects

    def get(self, object_digest: str) -> bytes:
        return self.objects[object_digest]


def test_preview_is_read_only_filtered_deterministic_and_secret_free(tmp_path: Path) -> None:
    source = _source(tmp_path)
    filters = HistoryImportFilter(
        date_from=dt.date(2026, 8, 19),
        date_to=dt.date(2026, 8, 21),
        exclude=("skip",),
        project_ref="project:zekam",
        scope_ref="work:memory",
    )
    service = HistoryImportService()
    first, _ = service.preview_path(_request(filters), source, scanned_at=NOW)
    second, _ = service.preview_path(_request(filters), source, scanned_at=NOW)

    assert first.preview_digest == second.preview_digest
    assert first.durable_writes == 0
    assert first.source_mutations == 0
    assert first.estimated_provider_calls == 0
    assert len(first.included_sources) == 1
    assert {item.reason_code: item.count for item in first.excluded_counts} == {"exclude-filter": 1}
    assert "skip" not in str(first.body())
    assert "[00:01" not in str(first.body())


def test_preview_requires_private_classification_before_source_read(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(PolicyViolation, match="ilk okumadan once"):
        HistoryImportRequest(
            corpus_id="x",
            source_name="history.zip",
            classification=DataClassification.PUBLIC,
            source_policy_digest=POLICY,
            requested_by="human-user",
            filters=HistoryImportFilter(),
        )
    assert source.exists()


def test_apply_requires_separate_exact_consent_and_rejects_drift(tmp_path: Path) -> None:
    service = HistoryImportService()
    preview, _ = service.preview_path(_request(), _source(tmp_path), scanned_at=NOW)
    with pytest.raises(PolicyViolation, match="explicit consent"):
        HistoryImportConsent(
            preview_digest=preview.preview_digest,
            archive_digest=preview.archive_digest,
            filter_digest=preview.filter_digest,
            classification=preview.classification,
            approved_by="human-user",
            approved_at=NOW,
            explicit=False,
        )
    drifted = HistoryImportConsent(
        preview_digest=digest("different-preview"),
        archive_digest=preview.archive_digest,
        filter_digest=preview.filter_digest,
        classification=preview.classification,
        approved_by="human-user",
        approved_at=NOW,
        explicit=True,
    )
    with pytest.raises(PolicyViolation, match="drift"):
        service.prepare_apply(preview, drifted)


def test_apply_delegates_to_existing_importer_with_private_metadata(tmp_path: Path) -> None:
    service = HistoryImportService()
    preview, scan = service.preview_path(_request(), _source(tmp_path), scanned_at=NOW)
    store = _SpyStore()
    receipt = service.apply_scan(preview, _consent(preview), scan, store=store)

    assert receipt.cursor == len(preview.included_sources)
    assert receipt.provider_calls == 0
    assert receipt.plan.candidate_only is True
    assert receipt.grants_authority is False
    assert store.metadata
    assert all(item["classification"] == "local-only" for item in store.metadata)
    assert all(item["projection_eligible"] == "false" for item in store.metadata)
    assert all(item["default_hydration_eligible"] == "false" for item in store.metadata)


def test_collision_blocks_overwrite_and_exact_replay_returns_receipt(tmp_path: Path) -> None:
    service = HistoryImportService()
    preview, scan = service.preview_path(_request(), _source(tmp_path), scanned_at=NOW)
    consent = _consent(preview)
    store = _SpyStore()
    receipt = service.apply_scan(preview, consent, scan, store=store)

    replay = service.apply_scan(
        preview,
        consent,
        scan,
        store=store,
        existing_receipt=receipt,
    )
    assert replay is receipt

    with pytest.raises(PolicyViolation, match="no-overwrite"):
        service.apply_scan(
            preview,
            consent,
            scan,
            store=store,
            existing_source_versions=frozenset(
                item.digest_value for item in preview.included_sources
            ),
        )


def test_source_change_after_preview_is_rejected_before_store_effect(tmp_path: Path) -> None:
    service = HistoryImportService()
    source = _source(tmp_path)
    request = _request()
    preview, _ = service.preview_path(request, source, scanned_at=NOW)
    source.write_bytes(_zip({"changed.txt": b"[00:01-00:02] changed\n"}))
    store = _SpyStore()
    with pytest.raises(PolicyViolation, match="drift"):
        service.apply_path(
            request,
            preview,
            _consent(preview),
            source,
            store=store,
        )
    assert store.metadata == []


def test_bounded_parts_resume_with_exact_cursor_and_watermark(tmp_path: Path) -> None:
    service = HistoryImportService(part_size=1)
    preview, scan = service.preview_path(_request(), _source(tmp_path), scanned_at=NOW)
    consent = _consent(preview)
    store = _SpyStore()

    first = service.apply_scan(preview, consent, scan, store=store, cursor_start=0)
    second = service.apply_scan(
        preview,
        consent,
        scan,
        store=store,
        cursor_start=first.cursor,
        existing_source_versions=frozenset(
            item.digest_value for item in first.completed_source_versions
        ),
    )

    assert first.cursor == 1
    assert second.plan.cursor_start == 1
    assert second.cursor == 2
    assert first.source_watermark != second.source_watermark
