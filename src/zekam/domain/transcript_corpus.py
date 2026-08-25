"""Transcript corpus import manifest ve provenance zinciri."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import canonical_bytes, canonical_json, digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import assert_safe_relative

TRANSCRIPT_CORPUS_SCHEMA = "zekam-transcript-corpus-import/v1"
TRANSCRIPT_SOURCE_TYPE = "external-video-transcript"
TRANSCRIPT_TRUST = "untrusted-observed"


@dataclass(frozen=True, slots=True)
class TranscriptCorpusEntry:
    """Arsivdeki tek transcript dosyasinin raw ve normalize kimligi."""

    relative_path: str
    file_digest: str
    content_digest: str
    byte_size: int
    line_count: int
    unit_count: int
    declared_date: str | None = None
    video_id: str | None = None
    title: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        assert_safe_relative(self.relative_path, "transcript entry path")
        parse_digest(self.file_digest)
        parse_digest(self.content_digest)
        if self.byte_size <= 0 or self.line_count <= 0 or self.unit_count <= 0:
            raise ValidationFailed("transcript entry boyut, satir ve unit sayisi pozitif olmali")
        if self.declared_date is not None:
            try:
                dt.date.fromisoformat(self.declared_date)
            except ValueError as exc:
                raise ValidationFailed("transcript declared date ISO-8601 olmali") from exc
        for label, value in (
            ("video kimligi", self.video_id),
            ("baslik", self.title),
            ("dil", self.language),
        ):
            if value is not None and not value.strip():
                raise ValidationFailed(f"transcript {label} bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "file_digest": self.file_digest,
            "content_digest": self.content_digest,
            "byte_size": self.byte_size,
            "line_count": self.line_count,
            "unit_count": self.unit_count,
            "declared_date": self.declared_date,
            "video_id": self.video_id,
            "title": self.title,
            "language": self.language,
        }

    @property
    def entry_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class TranscriptCorpusImportManifest:
    """Immutable archive->entry->content->parser/profile provenance zinciri."""

    corpus_id: str
    archive_name: str
    archive_digest: str
    archive_size: int
    parser_ref: str
    parser_version: str
    parser_profile_canonical: str
    parser_profile_digest: str
    source_policy_digest: str
    imported_by: str
    created_at: dt.datetime
    entries: tuple[TranscriptCorpusEntry, ...]
    schema: str = TRANSCRIPT_CORPUS_SCHEMA
    source_type: str = TRANSCRIPT_SOURCE_TYPE
    trust: str = TRANSCRIPT_TRUST
    instruction_authority: str = "none"
    factual_authority: str = "none-by-default"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        self.validate()

    @property
    def parser_profile(self) -> dict[str, Any]:
        """Her okumada yeni kopya; manifestin ic durumu mutate edilemez."""

        document = json.loads(self.parser_profile_canonical)
        if not isinstance(document, dict):  # pragma: no cover - validate once guarantees this
            raise ValidationFailed("parser profile nesne olmali")
        return document

    def validate(self) -> None:
        """Persist oncesi de tekrar calistirilabilen tam structural gate."""

        if self.schema != TRANSCRIPT_CORPUS_SCHEMA:
            raise ValidationFailed("transcript corpus manifest schema gecersiz")
        for label, value in (
            ("corpus kimligi", self.corpus_id),
            ("archive adi", self.archive_name),
            ("parser ref", self.parser_ref),
            ("parser surumu", self.parser_version),
            ("import eden", self.imported_by),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        assert_safe_relative(self.archive_name, "archive adi")
        parse_digest(self.archive_digest)
        parse_digest(self.parser_profile_digest)
        parse_digest(self.source_policy_digest)
        profile = self.parser_profile
        if canonical_json(profile) != self.parser_profile_canonical:
            raise ValidationFailed("parser profile canonical JSON degil")
        if digest(profile) != self.parser_profile_digest:
            raise ValidationFailed("parser profile digest govde ile uyusmuyor")
        if self.archive_size <= 0 or not self.entries:
            raise ValidationFailed("archive ve entry listesi bos olamaz")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValidationFailed("manifest created_at timezone-aware olmali")
        paths = [entry.relative_path for entry in self.entries]
        if paths != sorted(paths, key=lambda value: (value.casefold(), value)) or len(
            {value.casefold() for value in paths}
        ) != len(paths):
            raise ValidationFailed("manifest entry yollari tekil ve sirali olmali")
        if self.source_type != TRANSCRIPT_SOURCE_TYPE or self.trust != TRANSCRIPT_TRUST:
            raise ValidationFailed("transcript source trust sinifi degistirilemez")
        if self.instruction_authority != "none" or self.factual_authority != "none-by-default":
            raise ValidationFailed("transcript authority tasiyamaz")
        if self.grants_authority:
            raise ValidationFailed("transcript manifest authority veremez")

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def provenance_body(self) -> dict[str, Any]:
        """Import zamani/aktoru disinda yeniden uretilebilir content zinciri."""

        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "archive_name": self.archive_name,
            "archive_digest": self.archive_digest,
            "archive_size": self.archive_size,
            "entry_count": self.entry_count,
            "parser_ref": self.parser_ref,
            "parser_version": self.parser_version,
            "parser_profile": self.parser_profile,
            "parser_profile_digest": self.parser_profile_digest,
            "source_policy_digest": self.source_policy_digest,
            "entries": [
                {
                    **entry.as_dict(),
                    "entry_digest": entry.entry_digest,
                    "source_version_digest": self.source_version_digest(entry),
                }
                for entry in self.entries
            ],
            "source_type": self.source_type,
            "trust": self.trust,
            "instruction_authority": self.instruction_authority,
            "factual_authority": self.factual_authority,
            "grants_authority": False,
        }

    def source_version_digest(self, entry: TranscriptCorpusEntry) -> str:
        """Entry'nin archive ve parser profiline bagli kanonik source revision kimligi."""

        if entry not in self.entries:
            raise ValidationFailed("source version entry manifestte bulunmuyor")
        return digest(
            {
                "schema": "zekam-transcript-source-version/v1",
                "archive_digest": self.archive_digest,
                "entry_digest": entry.entry_digest,
                "parser_ref": self.parser_ref,
                "parser_version": self.parser_version,
                "parser_profile_digest": self.parser_profile_digest,
                "source_policy_digest": self.source_policy_digest,
            }
        )

    @property
    def provenance_digest(self) -> str:
        return digest(self.provenance_body())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.provenance_body(),
            "imported_by": self.imported_by,
            "created_at": self.created_at,
            "provenance_digest": self.provenance_digest,
        }

    @property
    def manifest_digest(self) -> str:
        return digest(self.as_dict())

    def to_bytes(self) -> bytes:
        return canonical_bytes({**self.as_dict(), "manifest_digest": self.manifest_digest})


@dataclass(frozen=True, slots=True)
class StoredTranscriptCorpusImport:
    """CAS'a yazilmis archive, entry ve manifest receipt'i."""

    manifest: TranscriptCorpusImportManifest
    manifest_object_digest: str
    archive_object_digest: str
    entry_object_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        parse_digest(self.manifest_object_digest)
        parse_digest(self.archive_object_digest)
        for value in self.entry_object_digests:
            parse_digest(value)
        if self.archive_object_digest != self.manifest.archive_digest:
            raise ValidationFailed("archive CAS digest manifest ile uyusmuyor")
        expected = tuple(entry.file_digest for entry in self.manifest.entries)
        if self.entry_object_digests != expected:
            raise ValidationFailed("entry CAS digest zinciri manifest ile uyusmuyor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-stored-transcript-corpus-import/v1",
            "manifest_digest": self.manifest.manifest_digest,
            "provenance_digest": self.manifest.provenance_digest,
            "manifest_object_digest": self.manifest_object_digest,
            "archive_object_digest": self.archive_object_digest,
            "entry_object_digests": list(self.entry_object_digests),
            "grants_authority": False,
        }
