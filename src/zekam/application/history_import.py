"""Preview/second-consent gate around the existing transcript corpus importer.

This is not a second parser or importer.  It composes the existing ZIP scanner,
provenance manifest and CAS importer, adds classification before file reading,
and binds an exact preview to a separate apply consent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zekam.application.transcript_corpus_import import (
    ContentAddressedStore,
    StoredObject,
    TranscriptArchiveScan,
    TranscriptCorpusImporter,
    scan_transcript_archive,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import ScanLimits
from zekam.domain.session_continuity import DataClassification, DigestReference, TruthClass
from zekam.domain.transcript_corpus import (
    StoredTranscriptCorpusImport,
    TranscriptCorpusEntry,
)

_ALLOWED_HISTORY_CLASSIFICATIONS = frozenset(
    {DataClassification.RESTRICTED, DataClassification.LOCAL_ONLY}
)
_SOURCE_TYPE = "external-video-transcript"
MAX_EXCLUDE_FILTERS = 64
MAX_FILTER_LENGTH = 256


@dataclass(frozen=True, slots=True)
class HistoryImportFilter:
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    source_types: tuple[str, ...] = (_SOURCE_TYPE,)
    project_ref: str | None = None
    scope_ref: str | None = None
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValidationFailed("history import tarih araligi gecersiz")
        if tuple(sorted(set(self.source_types))) != self.source_types:
            raise ValidationFailed("history import source types tekil ve sirali olmali")
        if len(self.exclude) > MAX_EXCLUDE_FILTERS:
            raise ValidationFailed("history import exclude filter sinirini asiyor")
        normalized = tuple(value.strip().casefold() for value in self.exclude)
        if (
            any(not value or len(value) > MAX_FILTER_LENGTH for value in normalized)
            or tuple(sorted(set(normalized))) != normalized
        ):
            raise ValidationFailed(
                "history import exclude filtreleri normalize, tekil ve sirali olmali"
            )
        for value in (self.project_ref, self.scope_ref):
            if value is not None and (not value.strip() or "\\" in value):
                raise ValidationFailed("history import scope ref portable olmali")

    def body(self) -> dict[str, Any]:
        """Secret-free shape; raw exclude strings never enter receipts/telemetry."""

        return {
            "date_from": self.date_from,
            "date_to": self.date_to,
            "source_types": list(self.source_types),
            "project_ref": self.project_ref,
            "scope_ref": self.scope_ref,
            "exclude_digests": [digest(value.casefold()) for value in self.exclude],
        }

    @property
    def filter_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class HistoryImportRequest:
    corpus_id: str
    source_name: str
    classification: DataClassification
    source_policy_digest: str
    requested_by: str
    filters: HistoryImportFilter

    def __post_init__(self) -> None:
        if self.classification not in _ALLOWED_HISTORY_CLASSIFICATIONS:
            raise PolicyViolation("history import ilk okumadan once restricted/local-only olmali")
        parse_digest(self.source_policy_digest)
        for value in (self.corpus_id, self.source_name, self.requested_by):
            if not value.strip():
                raise ValidationFailed("history import request kimligi bos olamaz")
        if Path(self.source_name).name != self.source_name or not self.source_name.endswith(".zip"):
            raise ValidationFailed("history import source name yalniz dosya adi olmali")


@dataclass(frozen=True, slots=True)
class HistoryImportCount:
    reason_code: str
    count: int

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or self.count < 0:
            raise ValidationFailed("history import count gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {"reason_code": self.reason_code, "count": self.count}


@dataclass(frozen=True, slots=True)
class HistoryImportPreview:
    corpus_id: str
    archive_digest: str
    archive_size: int
    provenance_digest: str
    parser_profile_digest: str
    source_policy_digest: str
    filter_digest: str
    classification: DataClassification
    date_min: dt.date | None
    date_max: dt.date | None
    total_entries: int
    included_sources: tuple[DigestReference, ...]
    excluded_counts: tuple[HistoryImportCount, ...]
    invalid_count: int
    empty_count: int
    estimated_provider_calls: int
    disclosure_required: bool
    scanned_at: dt.datetime
    durable_writes: int = 0
    source_mutations: int = 0
    grants_authority: bool = False
    source_format: str = "zip-transcript-corpus"

    def __post_init__(self) -> None:
        for value in (
            self.archive_digest,
            self.provenance_digest,
            self.parser_profile_digest,
            self.source_policy_digest,
            self.filter_digest,
        ):
            parse_digest(value)
        if self.archive_size <= 0 or self.total_entries <= 0:
            raise ValidationFailed("history import preview source sayimlari gecersiz")
        if self.source_format != "zip-transcript-corpus":
            raise ValidationFailed("history import preview source formati gecersiz")
        if (self.date_min is None) != (self.date_max is None) or (
            self.date_min is not None
            and self.date_max is not None
            and self.date_min > self.date_max
        ):
            raise ValidationFailed("history import preview tarih ozeti gecersiz")
        if self.classification not in _ALLOWED_HISTORY_CLASSIFICATIONS:
            raise PolicyViolation("history import preview private classification ister")
        if self.invalid_count < 0 or self.empty_count < 0 or self.estimated_provider_calls < 0:
            raise ValidationFailed("history import preview negatif sayim tasiyamaz")
        if self.durable_writes or self.source_mutations or self.grants_authority:
            raise PolicyViolation("history import preview byte/state duzeyinde no-op olmali")
        if self.estimated_provider_calls or self.disclosure_required:
            raise PolicyViolation("default history preview provider/disclosure uretemez")
        if self.scanned_at.tzinfo is None or self.scanned_at.utcoffset() is None:
            raise ValidationFailed("history import preview zamani timezone-aware olmali")
        refs = tuple(item.ref for item in self.included_sources)
        if refs != tuple(sorted(set(refs))):
            raise ValidationFailed("history import included sources tekil ve sirali olmali")
        if sum(item.count for item in self.excluded_counts) + len(refs) != self.total_entries:
            raise ValidationFailed("history import preview included/excluded sayimi uyusmuyor")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-history-import-preview/v1",
            "source_format": self.source_format,
            "corpus_id": self.corpus_id,
            "archive_digest": self.archive_digest,
            "archive_size": self.archive_size,
            "provenance_digest": self.provenance_digest,
            "parser_profile_digest": self.parser_profile_digest,
            "source_policy_digest": self.source_policy_digest,
            "filter_digest": self.filter_digest,
            "classification": self.classification.value,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "total_entries": self.total_entries,
            "included_sources": [item.as_dict() for item in self.included_sources],
            "excluded_counts": [item.as_dict() for item in self.excluded_counts],
            "invalid_count": self.invalid_count,
            "empty_count": self.empty_count,
            "estimated_provider_calls": 0,
            "disclosure_required": False,
            "scanned_at": self.scanned_at,
            "durable_writes": 0,
            "source_mutations": 0,
            "grants_authority": False,
        }

    @property
    def preview_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class HistoryImportConsent:
    preview_digest: str
    archive_digest: str
    filter_digest: str
    classification: DataClassification
    approved_by: str
    approved_at: dt.datetime
    explicit: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (self.preview_digest, self.archive_digest, self.filter_digest):
            parse_digest(value)
        if not self.approved_by.strip():
            raise ValidationFailed("history import consent actor bos olamaz")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValidationFailed("history import consent zamani timezone-aware olmali")
        if not self.explicit or self.grants_authority:
            raise PolicyViolation("history import apply ayri explicit consent ister")

    @property
    def consent_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-history-import-consent/v1",
                "preview_digest": self.preview_digest,
                "archive_digest": self.archive_digest,
                "filter_digest": self.filter_digest,
                "classification": self.classification.value,
                "approved_by": self.approved_by,
                "approved_at": self.approved_at,
                "explicit": True,
                "grants_authority": False,
            }
        )


@dataclass(frozen=True, slots=True)
class HistoryImportApplyPlan:
    preview_digest: str
    consent_digest: str
    source_versions: tuple[DigestReference, ...]
    cursor_start: int
    cursor_end: int
    total_sources: int
    part_size: int
    source_watermark: str
    idempotency_key: str
    candidate_only: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.preview_digest)
        parse_digest(self.consent_digest)
        parse_digest(self.source_watermark)
        parse_digest(self.idempotency_key)
        if (
            self.cursor_start < 0
            or self.cursor_end <= self.cursor_start
            or self.cursor_end > self.total_sources
            or self.cursor_end - self.cursor_start != len(self.source_versions)
        ):
            raise ValidationFailed("history import cursor exact source partina uymali")
        if self.part_size < 1 or len(self.source_versions) > self.part_size:
            raise ValidationFailed("history import bounded part siniri gecersiz")
        if not self.candidate_only or self.grants_authority:
            raise PolicyViolation("history import output candidate-only ve authority-free olmali")


@dataclass(frozen=True, slots=True)
class HistoryImportApplyReceipt:
    plan: HistoryImportApplyPlan
    stored_import: StoredTranscriptCorpusImport
    completed_source_versions: tuple[DigestReference, ...]
    cursor: int
    source_watermark: str
    provider_calls: int = 0
    projection_writes: int = 0
    telemetry_content_writes: int = 0
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.completed_source_versions != self.plan.source_versions:
            raise ValidationFailed("history import receipt source seti planla uyusmuyor")
        if (
            self.cursor != self.plan.cursor_end
            or self.source_watermark != self.plan.source_watermark
        ):
            raise ValidationFailed("history import receipt cursor/watermark drift")
        if (
            self.provider_calls
            or self.projection_writes
            or self.telemetry_content_writes
            or self.grants_authority
        ):
            raise PolicyViolation("history import default apply dis sistem/public leak uretemez")

    @property
    def receipt_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-history-import-apply-receipt/v1",
                "idempotency_key": self.plan.idempotency_key,
                "stored_import": self.stored_import.as_dict(),
                "completed_source_versions": [
                    item.as_dict() for item in self.completed_source_versions
                ],
                "cursor": self.cursor,
                "source_watermark": self.source_watermark,
                "candidate_only": True,
                "provider_calls": 0,
                "projection_writes": 0,
                "telemetry_content_writes": 0,
                "grants_authority": False,
            }
        )


@dataclass(frozen=True, slots=True)
class _ClassifiedPrivateStore:
    delegate: ContentAddressedStore
    classification: DataClassification

    def put(
        self,
        payload: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        protected = dict(metadata or {})
        protected.update(
            {
                "classification": self.classification.value,
                "projection_eligible": "false",
                "telemetry_content_eligible": "false",
                "embedding_eligible": "false",
                "default_hydration_eligible": "false",
            }
        )
        return self.delegate.put(payload, media_type=media_type, metadata=protected)

    def exists(self, object_digest: str) -> bool:
        return self.delegate.exists(object_digest)

    def get(self, object_digest: str) -> bytes:
        return self.delegate.get(object_digest)


def _assert_explicit_safe_source(source_path: Path, limits: ScanLimits) -> None:
    if not source_path.is_absolute():
        raise PolicyViolation("history import source path explicit absolute olmali")
    try:
        stat_result = source_path.lstat()
    except OSError as exc:
        raise ValidationFailed("history import source okunamadi") from exc
    if source_path.is_symlink() or not source_path.is_file():
        raise PolicyViolation("history import source symlink veya special file olamaz")
    if stat_result.st_nlink != 1:
        raise PolicyViolation("history import source hardlink olamaz")
    if stat_result.st_size <= 0 or stat_result.st_size > limits.max_total_bytes:
        raise PolicyViolation("history import source byte siniri disinda")


def _read_exact_source(source_path: Path, limits: ScanLimits) -> bytes:
    _assert_explicit_safe_source(source_path, limits)
    try:
        with source_path.open("rb") as stream:
            payload = stream.read(limits.max_total_bytes + 1)
    except OSError as exc:
        raise ValidationFailed("history import source okunamadi") from exc
    if len(payload) > limits.max_total_bytes:
        raise PolicyViolation("history import source byte sinirini asiyor")
    return payload


def _entry_filter_reason(entry: TranscriptCorpusEntry, filters: HistoryImportFilter) -> str | None:
    if _SOURCE_TYPE not in filters.source_types:
        return "source-type-excluded"
    if filters.date_from is not None or filters.date_to is not None:
        if entry.declared_date is None:
            return "date-missing"
        declared = dt.date.fromisoformat(entry.declared_date)
        if filters.date_from is not None and declared < filters.date_from:
            return "date-before-range"
        if filters.date_to is not None and declared > filters.date_to:
            return "date-after-range"
    haystack = "\n".join(
        value.casefold() for value in (entry.relative_path, entry.title or "", entry.video_id or "")
    )
    if any(pattern.casefold() in haystack for pattern in filters.exclude):
        return "exclude-filter"
    return None


@dataclass(frozen=True, slots=True)
class HistoryImportService:
    limits: ScanLimits = field(default_factory=ScanLimits)
    part_size: int = 128

    def __post_init__(self) -> None:
        if self.part_size < 1 or self.part_size > 128:
            raise ValidationFailed("history import part size 1..128 olmali")

    def preview_path(
        self,
        request: HistoryImportRequest,
        source_path: Path,
        *,
        scanned_at: dt.datetime,
    ) -> tuple[HistoryImportPreview, TranscriptArchiveScan]:
        """Read-only preview; request classification is validated before file read."""

        request.__post_init__()
        if source_path.name != request.source_name:
            raise ValidationFailed("history import explicit source name path ile uyusmuyor")
        payload = _read_exact_source(source_path, self.limits)
        scan = scan_transcript_archive(
            payload,
            archive_name=request.source_name,
            corpus_id=request.corpus_id,
            source_policy_digest=request.source_policy_digest,
            imported_by=request.requested_by,
            created_at=scanned_at,
            limits=self.limits,
        )
        return self.preview_scan(request, scan, scanned_at=scanned_at), scan

    def preview_scan(
        self,
        request: HistoryImportRequest,
        scan: TranscriptArchiveScan,
        *,
        scanned_at: dt.datetime,
    ) -> HistoryImportPreview:
        request.__post_init__()
        scan.validate()
        reasons: dict[str, int] = {}
        included: list[DigestReference] = []
        dates: list[dt.date] = []
        for entry in scan.manifest.entries:
            if entry.declared_date is not None:
                dates.append(dt.date.fromisoformat(entry.declared_date))
            reason = _entry_filter_reason(entry, request.filters)
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            included.append(
                DigestReference(
                    ref=f"transcript-entry:{entry.entry_digest.removeprefix('sha256:')}",
                    digest_value=scan.manifest.source_version_digest(entry),
                    truth_class=TruthClass.UNKNOWN,
                )
            )
        included_refs = tuple(sorted(included, key=lambda item: item.ref))
        excluded_counts = tuple(
            HistoryImportCount(reason, count) for reason, count in sorted(reasons.items())
        )
        return HistoryImportPreview(
            corpus_id=request.corpus_id,
            archive_digest=scan.manifest.archive_digest,
            archive_size=scan.manifest.archive_size,
            provenance_digest=scan.manifest.provenance_digest,
            parser_profile_digest=scan.manifest.parser_profile_digest,
            source_policy_digest=request.source_policy_digest,
            filter_digest=request.filters.filter_digest,
            classification=request.classification,
            date_min=min(dates) if dates else None,
            date_max=max(dates) if dates else None,
            total_entries=scan.manifest.entry_count,
            included_sources=included_refs,
            excluded_counts=excluded_counts,
            invalid_count=0,
            empty_count=0,
            estimated_provider_calls=0,
            disclosure_required=False,
            scanned_at=scanned_at,
        )

    def prepare_apply(
        self,
        preview: HistoryImportPreview,
        consent: HistoryImportConsent,
        *,
        cursor_start: int = 0,
    ) -> HistoryImportApplyPlan:
        if (
            consent.preview_digest != preview.preview_digest
            or consent.archive_digest != preview.archive_digest
            or consent.filter_digest != preview.filter_digest
            or consent.classification is not preview.classification
        ):
            raise PolicyViolation("history import consent preview/filter/source drift")
        if not preview.included_sources:
            raise ValidationFailed("history import apply secili source ister")
        if not 0 <= cursor_start < len(preview.included_sources):
            raise ValidationFailed("history import resume cursor source seti disinda")
        cursor_end = min(cursor_start + self.part_size, len(preview.included_sources))
        source_part = preview.included_sources[cursor_start:cursor_end]
        watermark = digest(
            {
                "preview_digest": preview.preview_digest,
                "cursor_end": cursor_end,
                "source_part": [item.as_dict() for item in source_part],
            }
        )
        idempotency_key = digest(
            {
                "preview_digest": preview.preview_digest,
                "consent_digest": consent.consent_digest,
                "source_watermark": watermark,
                "part_size": self.part_size,
                "cursor_start": cursor_start,
                "cursor_end": cursor_end,
            }
        )
        return HistoryImportApplyPlan(
            preview_digest=preview.preview_digest,
            consent_digest=consent.consent_digest,
            source_versions=source_part,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            total_sources=len(preview.included_sources),
            part_size=self.part_size,
            source_watermark=watermark,
            idempotency_key=idempotency_key,
        )

    def apply_scan(
        self,
        preview: HistoryImportPreview,
        consent: HistoryImportConsent,
        scan: TranscriptArchiveScan,
        *,
        store: ContentAddressedStore,
        cursor_start: int = 0,
        existing_source_versions: frozenset[str] = frozenset(),
        existing_receipt: HistoryImportApplyReceipt | None = None,
    ) -> HistoryImportApplyReceipt:
        """Apply through the existing importer after exact consent/drift checks."""

        scan.validate()
        if (
            scan.manifest.archive_digest != preview.archive_digest
            or scan.manifest.provenance_digest != preview.provenance_digest
            or scan.manifest.parser_profile_digest != preview.parser_profile_digest
            or scan.manifest.source_policy_digest != preview.source_policy_digest
        ):
            raise PolicyViolation("history import source preview sonrasinda degisti")
        plan = self.prepare_apply(preview, consent, cursor_start=cursor_start)
        if existing_receipt is not None:
            if existing_receipt.plan.idempotency_key != plan.idempotency_key:
                raise PolicyViolation("history import replay receipt planla uyusmuyor")
            return existing_receipt
        collisions = {item.digest_value for item in plan.source_versions} & set(
            existing_source_versions
        )
        if collisions:
            raise PolicyViolation("history-import-collision:no-overwrite")
        protected_store = _ClassifiedPrivateStore(store, preview.classification)
        stored = TranscriptCorpusImporter(protected_store).persist(scan)
        return HistoryImportApplyReceipt(
            plan=plan,
            stored_import=stored,
            completed_source_versions=plan.source_versions,
            cursor=plan.cursor_end,
            source_watermark=plan.source_watermark,
        )

    def apply_path(
        self,
        request: HistoryImportRequest,
        preview: HistoryImportPreview,
        consent: HistoryImportConsent,
        source_path: Path,
        *,
        store: ContentAddressedStore,
        cursor_start: int = 0,
        existing_source_versions: frozenset[str] = frozenset(),
        existing_receipt: HistoryImportApplyReceipt | None = None,
    ) -> HistoryImportApplyReceipt:
        """Re-read and re-preview the explicit path before any durable apply."""

        current_preview, scan = self.preview_path(
            request,
            source_path,
            scanned_at=preview.scanned_at,
        )
        if current_preview.preview_digest != preview.preview_digest:
            raise PolicyViolation("history import preview/filter/source drift")
        return self.apply_scan(
            current_preview,
            consent,
            scan,
            store=store,
            cursor_start=cursor_start,
            existing_source_versions=existing_source_versions,
            existing_receipt=existing_receipt,
        )
