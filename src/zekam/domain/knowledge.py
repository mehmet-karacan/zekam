"""Knowledge Plane ingestion sozlesmesi.

Orijinal kaynak immutable'dir. Parser dogrudan vector uretmez: once normalize
edilmis, locator tasiyan icerik birimleri olusur. Aktivasyon atomiktir; yarim
kalmis ingestion aktif surum uretmez.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_RATIO = 100
MAX_DOCUMENT_BYTES = 256 * 1024 * 1024

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)",
    re.IGNORECASE,
)

#: Ingestion sirasinda hicbir kosulda okunmayan dosya desenleri.
DENY_PATTERNS = (
    ".env",
    ".env.local",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".pfx",
    ".p12",
    ".keystore",
    "credentials.json",
    ".npmrc",
    ".pypirc",
)


class SourceFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    MARKDOWN = "markdown"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    ARCHIVE = "archive"
    REPOSITORY = "repository"
    DIRECTORY = "directory"
    ORACLE_METADATA = "oracle-metadata"
    POSTGRES_METADATA = "postgres-metadata"


#: OCR gerektiren gorsel formatlar.
IMAGE_FORMATS = frozenset({SourceFormat.PNG, SourceFormat.JPEG, SourceFormat.TIFF})

#: Metadata-only politikasina tabi kaynaklar.
DATABASE_FORMATS = frozenset({SourceFormat.ORACLE_METADATA, SourceFormat.POSTGRES_METADATA})


class UnitKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CODE = "code"
    FORMULA = "formula"
    IMAGE = "image"
    CAPTION = "caption"
    OCR_BLOCK = "ocr-block"
    FILE_HEADER = "file-header"
    SYMBOL = "symbol"
    CONFIGURATION = "configuration"
    DB_OBJECT = "db-object"


class IngestionStage(StrEnum):
    VALIDATED = "validated"
    STORED = "stored"
    PARSED = "parsed"
    NORMALIZED = "normalized"
    INDEXED = "indexed"
    ACTIVATED = "activated"


#: Asamalar sirali ilerler; atlama yoktur.
STAGE_ORDER = (
    IngestionStage.VALIDATED,
    IngestionStage.STORED,
    IngestionStage.PARSED,
    IngestionStage.NORMALIZED,
    IngestionStage.INDEXED,
    IngestionStage.ACTIVATED,
)


class VersionState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


def assert_safe_relative(value: str, label: str = "path") -> str:
    """Portable relative yol; traversal ve absolute reddedilir."""

    if not value.strip():
        raise ValidationFailed(f"{label} bos olamaz")
    if "\\" in value or value.startswith("/") or PureWindowsPath(value).is_absolute():
        raise PolicyViolation(f"{label} absolute path veya ters bolu tasiyamaz")
    if ".." in PurePosixPath(value).parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")
    return value


def is_denied(path: str) -> bool:
    """Secret veya kimlik dosyasi olma ihtimali olan yollari isaretler."""

    lowered = path.lower()
    name = PurePosixPath(lowered).name
    return any(name == pattern or lowered.endswith(pattern) for pattern in DENY_PATTERNS)


@dataclass(frozen=True, slots=True)
class Artifact:
    """Degistirilemez orijinal kaynak. Icerik adresli depoda tutulur."""

    artifact_id: str
    content_digest: str
    byte_size: int
    media_type: str
    original_name: str
    stored_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if not 0 < self.byte_size <= MAX_DOCUMENT_BYTES:
            raise ValidationFailed("artifact boyutu sinir disinda")
        if not self.media_type.strip():
            raise ValidationFailed("media type bos olamaz")
        assert_safe_relative(self.original_name, "artifact adi")
        if is_denied(self.original_name):
            raise PolicyViolation("deny list'teki dosya ingest edilemez")
        if self.stored_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def body(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_digest": self.content_digest,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "original_name": self.original_name,
        }

    @property
    def artifact_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class Locator:
    """Bir icerik biriminin kaynaktaki exact yeri.

    Alanlar kaynak turune gore doldurulur; uydurma deger uretilmez. PDF sayfa,
    DOCX heading path, kod satir araligi, DB ise nesne adi tasir.
    """

    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    heading_path: tuple[str, ...] = ()
    block_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    object_name: str | None = None
    relative_path: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValidationFailed("sayfa numarasi 1'den kucuk olamaz")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValidationFailed("bbox dort deger ister")
            if self.page is None:
                raise ValidationFailed("bbox sayfa bilgisi olmadan anlamsizdir")
            x0, y0, x1, y1 = self.bbox
            if not all(0.0 <= value <= 1.0 for value in self.bbox):
                raise ValidationFailed("bbox normalize 0..1 araliginda olmali")
            if x1 <= x0 or y1 <= y0:
                raise ValidationFailed("bbox sol-ust/sag-alt sirasi gecersiz")
        if (self.line_start is None) != (self.line_end is None):
            raise ValidationFailed("satir araligi eksik")
        if (
            self.line_start is not None
            and self.line_end is not None
            and (self.line_start < 1 or self.line_end < self.line_start)
        ):
            raise ValidationFailed("satir araligi gecersiz")
        if self.relative_path is not None:
            assert_safe_relative(self.relative_path, "locator relative path")

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.page,
                self.bbox,
                self.heading_path,
                self.block_index is not None,
                self.line_start,
                self.symbol,
                self.object_name,
                self.relative_path,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "heading_path": list(self.heading_path),
            "block_index": self.block_index,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol,
            "object_name": self.object_name,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class ContentUnit:
    """Parser ciktisi: normalize edilmis, locator tasiyan icerik birimi.

    Bu birim vector degildir. Chunker ve embedding profili ayri katmandir.
    """

    unit_id: str
    kind: UnitKind
    text: str
    locator: Locator
    order: int
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValidationFailed("icerik birimi bos olamaz")
        if self.order < 0:
            raise ValidationFailed("sira negatif olamaz")
        if self.locator.is_empty:
            raise ValidationFailed("locator'siz icerik birimi kabul edilmez")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed("confidence 0..1 araliginda olmali")
        if self.kind is UnitKind.OCR_BLOCK and self.confidence is None:
            raise ValidationFailed("OCR birimi confidence ister")

    def body(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": str(self.kind),
            "text": self.text,
            "locator": self.locator.as_dict(),
            "order": self.order,
            "confidence": self.confidence,
        }

    @property
    def unit_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """Bir kaynagin normalize edilmis tam icerigi."""

    document_id: str
    artifact_digest: str
    source_format: SourceFormat
    units: tuple[ContentUnit, ...]
    parser_ref: str
    parser_version: str
    parser_profile: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parse_digest(self.artifact_digest)
        if not self.units:
            raise ValidationFailed("normalize edilmis belge en az bir birim ister")
        orders = [unit.order for unit in self.units]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            raise ValidationFailed("birim sirasi tekrarsiz ve artan olmali")
        if not self.parser_version.strip():
            raise ValidationFailed("parser surumu bos olamaz")
        if not isinstance(self.parser_profile, dict):
            raise ValidationFailed("parser profile nesne olmali")

    @property
    def unit_count(self) -> int:
        return len(self.units)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-normalized-document/v1",
            "document_id": self.document_id,
            "artifact_digest": self.artifact_digest,
            "source_format": str(self.source_format),
            "parser_ref": self.parser_ref,
            "parser_version": self.parser_version,
            "parser_profile": self.parser_profile,
            "units": [unit.body() for unit in self.units],
        }

    @property
    def content_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class IngestionJob:
    """Sürümlü ingestion isi. Asamalar sirali ilerler ve kalicilastirilir."""

    job_id: str
    source_id: str
    artifact_digest: str
    idempotency_key: str
    completed_stages: tuple[IngestionStage, ...] = ()
    failure: str | None = None

    def __post_init__(self) -> None:
        parse_digest(self.artifact_digest)
        if not self.idempotency_key.strip():
            raise ValidationFailed("idempotency anahtari bos olamaz")
        expected = STAGE_ORDER[: len(self.completed_stages)]
        if tuple(self.completed_stages) != expected:
            raise ValidationFailed("ingestion asamalari sirali olmali")

    @property
    def next_stage(self) -> IngestionStage | None:
        if self.failure is not None or len(self.completed_stages) == len(STAGE_ORDER):
            return None
        return STAGE_ORDER[len(self.completed_stages)]

    @property
    def is_complete(self) -> bool:
        return self.failure is None and len(self.completed_stages) == len(STAGE_ORDER)

    def advance(self, stage: IngestionStage) -> IngestionJob:
        """Bir sonraki asamayi tamamlar. Atlama ve tekrar reddedilir."""

        if self.failure is not None:
            raise PolicyViolation("basarisiz ingestion sessizce devam edemez")
        if stage is not self.next_stage:
            raise ValidationFailed("ingestion asamasi atlanamaz")
        return IngestionJob(
            job_id=self.job_id,
            source_id=self.source_id,
            artifact_digest=self.artifact_digest,
            idempotency_key=self.idempotency_key,
            completed_stages=(*self.completed_stages, stage),
        )

    def fail(self, reason: str) -> IngestionJob:
        if not reason.strip():
            raise ValidationFailed("basarisizlik gerekce ister")
        return IngestionJob(
            job_id=self.job_id,
            source_id=self.source_id,
            artifact_digest=self.artifact_digest,
            idempotency_key=self.idempotency_key,
            completed_stages=self.completed_stages,
            failure=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_id": self.source_id,
            "artifact_digest": self.artifact_digest,
            "idempotency_key": self.idempotency_key,
            "completed_stages": [str(item) for item in self.completed_stages],
            "failure": self.failure,
            "is_complete": self.is_complete,
        }


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """Bir kaynagin surumu. Aktivasyon yalniz tamamlanmis ingestion ile olur."""

    version_id: str
    source_id: str
    revision: int
    artifact_digest: str
    content_digest: str
    state: VersionState
    created_at: dt.datetime
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        parse_digest(self.artifact_digest)
        parse_digest(self.content_digest)
        if self.revision < 1:
            raise ValidationFailed("revision 1'den kucuk olamaz")
        if self.state is VersionState.SUPERSEDED and self.superseded_by is None:
            raise ValidationFailed("superseded surum halefini bildirmeli")
        if self.state is not VersionState.SUPERSEDED and self.superseded_by is not None:
            raise ValidationFailed("yalniz superseded surum halef tasir")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def activate(self, job: IngestionJob) -> SourceVersion:
        """Atomik aktivasyon: tamamlanmamis ingestion aktif surum uretemez."""

        if not job.is_complete:
            raise PolicyViolation("tamamlanmamis ingestion aktif surum uretemez")
        if job.artifact_digest != self.artifact_digest:
            raise ValidationFailed("ingestion baska bir artifact'a ait")
        if self.state is not VersionState.PENDING:
            raise PolicyViolation("yalniz pending surum aktive edilir")
        return SourceVersion(
            version_id=self.version_id,
            source_id=self.source_id,
            revision=self.revision,
            artifact_digest=self.artifact_digest,
            content_digest=self.content_digest,
            state=VersionState.ACTIVE,
            created_at=self.created_at,
        )

    def supersede(self, successor: str) -> SourceVersion:
        if self.state is not VersionState.ACTIVE:
            raise PolicyViolation("yalniz aktif surum superseded olur")
        return SourceVersion(
            version_id=self.version_id,
            source_id=self.source_id,
            revision=self.revision,
            artifact_digest=self.artifact_digest,
            content_digest=self.content_digest,
            state=VersionState.SUPERSEDED,
            created_at=self.created_at,
            superseded_by=successor,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "source_id": self.source_id,
            "revision": self.revision,
            "artifact_digest": self.artifact_digest,
            "content_digest": self.content_digest,
            "state": str(self.state),
            "superseded_by": self.superseded_by,
        }


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Arsiv ve dizin taramasi icin bounded sinirlar."""

    max_entries: int = MAX_ARCHIVE_ENTRIES
    max_total_bytes: int = MAX_DOCUMENT_BYTES
    max_compression_ratio: int = MAX_ARCHIVE_RATIO

    def __post_init__(self) -> None:
        if min(self.max_entries, self.max_total_bytes, self.max_compression_ratio) <= 0:
            raise ValidationFailed("tarama sinirlari pozitif olmali")

    def assert_within(self, *, entries: int, total_bytes: int, compressed_bytes: int) -> None:
        if entries > self.max_entries:
            raise PolicyViolation("arsiv girdi sayisi sinirini asiyor")
        if total_bytes > self.max_total_bytes:
            raise PolicyViolation("arsiv boyut sinirini asiyor")
        if compressed_bytes > 0:
            ratio = total_bytes / compressed_bytes
            if ratio > self.max_compression_ratio:
                raise PolicyViolation("arsiv sikistirma orani sinirini asiyor (zip bomb)")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_entries": self.max_entries,
            "max_total_bytes": self.max_total_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }


@dataclass(frozen=True, slots=True)
class ScanDecision:
    """Bir dosyanin ingest edilip edilmeyecegi ve nedeni."""

    path: str
    included: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "included": self.included, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class DatabaseObject:
    """Metadata-only DB nesnesi. Satir verisi varsayilan olarak alinmaz."""

    schema_name: str
    object_name: str
    object_kind: str
    revision: str
    columns: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    row_data_included: bool = False

    def __post_init__(self) -> None:
        if self.row_data_included:
            raise PolicyViolation("satir verisi ayri policy ve authorization ister")
        for label, value in (
            ("schema", self.schema_name),
            ("object", self.object_name),
            ("kind", self.object_kind),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        if _SENSITIVE.search(self.object_name):
            raise PolicyViolation("nesne adi secret benzeri deger tasiyamaz")

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.object_name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "object_name": self.object_name,
            "object_kind": self.object_kind,
            "revision": self.revision,
            "columns": list(self.columns),
            "dependencies": list(self.dependencies),
            "row_data_included": False,
        }


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    """Kod kaynagindan cikarilan sembol. Satir araligi zorunludur."""

    name: str
    kind: str
    relative_path: str
    line_start: int
    line_end: int
    revision: str
    dependencies: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        assert_safe_relative(self.relative_path, "sembol yolu")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValidationFailed("sembol satir araligi gecersiz")
        if not self.name.strip():
            raise ValidationFailed("sembol adi bos olamaz")

    def to_locator(self) -> Locator:
        return Locator(
            symbol=self.name,
            line_start=self.line_start,
            line_end=self.line_end,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "revision": self.revision,
            "dependencies": list(self.dependencies),
        }
