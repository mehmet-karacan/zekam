"""Authority tasimayan, deterministik Markdown projection domain modeli."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.session_continuity import DataClassification, TruthClass

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")
_UNSAFE_LITERAL = re.compile(r"[\x00-\x1f\x7f`\[\]|#^]")
RENDERER_PROFILE = "zekam-obsidian-projection-renderer/v1"
OBSIDIAN_RENDERER_PROFILE = "zekam-obsidian-vault-renderer/v1"


def _literal(value: str, field: str) -> str:
    if not value.strip() or _UNSAFE_LITERAL.search(value):
        raise ValidationFailed(f"{field} guvenli tek satirli literal olmali")
    return value


def _markdown_text(value: str) -> str:
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValidationFailed("projection metni control karakteri tasiyamaz")
    escaped = value.replace("\\", "\\\\")
    for char in "`*_{}[]<>#+-.!|":
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _yaml(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
        + '"'
    )


def projection_filename(entity_type: str, entity_id: str) -> str:
    stem = _SAFE_NAME.sub("-", f"{entity_type}-{entity_id}").strip("-.").lower()
    if not stem or stem in {".", ".."}:
        raise ValidationFailed("projection dosya adi uretilemedi")
    return f"notes/{stem}.md"


@dataclass(frozen=True, slots=True)
class ProjectionSourceRef:
    source_type: str
    source_id: str
    source_revision: str
    record_digest: str

    def __post_init__(self) -> None:
        _literal(self.source_type, "source type")
        _literal(self.source_id, "source id")
        _literal(self.source_revision, "source revision")
        parse_digest(self.record_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "record_digest": self.record_digest,
        }


@dataclass(frozen=True, slots=True)
class ProjectionRelationRef:
    relation_id: str
    direction: str
    kind: str
    other_entity_id: str
    relation_digest: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.relation_id, "relation id"),
            (self.kind, "relation kind"),
            (self.other_entity_id, "relation other entity"),
        ):
            _literal(value, field)
        if self.direction not in {"outgoing", "incoming"}:
            raise ValidationFailed("projection relation direction gecersiz")
        parse_digest(self.relation_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "relation_id": self.relation_id,
            "direction": self.direction,
            "kind": self.kind,
            "other_entity_id": self.other_entity_id,
            "relation_digest": self.relation_digest,
        }


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    entity_type: str
    entity_id: str
    title: str
    status: str
    summary: str
    source_refs: tuple[ProjectionSourceRef, ...]
    related_entity_ids: tuple[str, ...] = ()
    relation_refs: tuple[ProjectionRelationRef, ...] = ()

    def __post_init__(self) -> None:
        for value, field in (
            (self.entity_type, "entity type"),
            (self.entity_id, "entity id"),
            (self.title, "title"),
            (self.status, "status"),
        ):
            _literal(value, field)
        if not self.summary.strip():
            raise ValidationFailed("projection summary bos olamaz")
        for item in self.source_refs:
            item.__post_init__()
        expected = tuple(
            sorted(set(self.source_refs), key=lambda item: tuple(item.as_dict().values()))
        )
        if expected != self.source_refs or not self.source_refs:
            raise ValidationFailed("projection source refs tekil, sirali ve dolu olmali")
        if tuple(sorted(set(self.related_entity_ids))) != self.related_entity_ids:
            raise ValidationFailed("projection iliskileri tekil ve sirali olmali")
        for value in self.related_entity_ids:
            _literal(value, "related entity id")
        for relation in self.relation_refs:
            relation.__post_init__()
        expected_relations = tuple(
            sorted(
                set(self.relation_refs),
                key=lambda item: tuple(item.as_dict().values()),
            )
        )
        if expected_relations != self.relation_refs:
            raise ValidationFailed("projection relation refs tekil ve sirali olmali")
        if self.relation_refs:
            expected_related = tuple(sorted({item.other_entity_id for item in self.relation_refs}))
            if self.related_entity_ids != expected_related:
                raise ValidationFailed("projection relation refs related entity setiyle uyusmuyor")

    @property
    def record_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "source_refs": [item.as_dict() for item in self.source_refs],
            "related_entity_ids": list(self.related_entity_ids),
            "relation_refs": [item.as_dict() for item in self.relation_refs],
        }


def render_projection_record(
    record: ProjectionRecord, *, project_id: str, snapshot_digest: str, generation_digest: str
) -> bytes:
    record.__post_init__()
    refs = "\n".join(
        f"- `{r.source_type}:{r.source_id}@{r.source_revision}` - `{r.record_digest}`"
        for r in record.source_refs
    )
    if record.relation_refs:
        related = "\n".join(
            f"- `{item.direction}` `{item.kind}` [[{item.other_entity_id}]] "
            f"- `{item.relation_id}` `{item.relation_digest}`"
            for item in record.relation_refs
        )
    else:
        related = "\n".join(f"- [[{value}]]" for value in record.related_entity_ids) or "- Yok"
    text = f"""---
title: {_yaml(record.title)}
tags:
  - zekam/projection
  - {_yaml("zekam/" + record.entity_type)}
status: {_yaml(record.status)}
project_id: {_yaml(project_id)}
entity_id: {_yaml(record.entity_id)}
source_snapshot_digest: {_yaml(snapshot_digest)}
source_record_digest: {_yaml(record.record_digest)}
last_generated_digest: {_yaml(generation_digest)}
read_only_projection: true
grants_authority: false
---

# {_markdown_text(record.title)}

> [!warning] Salt okunur projeksiyon
> Bu not kanonik kayit degildir; plan, receipt, yetki veya aktif bellek durumunu degistirmez.

## Ozet

{_markdown_text(record.summary)}

## Iliskiler

{related}

## Kaynaklar

{refs}
"""
    return text.encode("utf-8")


@dataclass(frozen=True, slots=True)
class MarkdownProjectionFile:
    relative_path: str
    payload: bytes
    source_record_digest: str

    def __post_init__(self) -> None:
        posix, windows = PurePosixPath(self.relative_path), PureWindowsPath(self.relative_path)
        if (
            "\\" in self.relative_path
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or ".." in posix.parts
        ):
            raise ValidationFailed("projection relative path guvenli degil")
        if not self.relative_path.endswith(".md") or not self.payload:
            raise ValidationFailed("projection yalniz dolu Markdown dosyasi kabul eder")
        parse_digest(self.source_record_digest)

    @property
    def content_digest(self) -> str:
        return digest_of_bytes(self.payload)


@dataclass(frozen=True, slots=True)
class MarkdownProjectionBundle:
    project_id: str
    source_snapshot_digest: str
    generation_digest: str
    records: tuple[ProjectionRecord, ...]
    files: tuple[MarkdownProjectionFile, ...]
    schema: str = "zekam-markdown-projection/v1"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _literal(self.project_id, "project id")
        if self.schema != "zekam-markdown-projection/v1":
            raise ValidationFailed("projection bundle kimligi/semasi gecersiz")
        for record in self.records:
            record.__post_init__()
        expected_snapshot = digest([item.as_dict() for item in self.records])
        if self.source_snapshot_digest != expected_snapshot:
            raise ValidationFailed("projection source snapshot drift")
        expected_generation = digest(
            {
                "schema": self.schema,
                "project_id": self.project_id,
                "source_snapshot_digest": self.source_snapshot_digest,
                "renderer_profile": RENDERER_PROFILE,
            }
        )
        if self.generation_digest != expected_generation:
            raise ValidationFailed("projection generation digest drift")
        for projection_file in self.files:
            projection_file.__post_init__()
        paths = tuple(item.relative_path for item in self.files)
        if not paths or paths != tuple(sorted(set(paths))) or self.grants_authority:
            raise ValidationFailed("projection dosya sirasi veya authority gecersiz")
        expected_files = tuple(
            MarkdownProjectionFile(
                projection_filename(item.entity_type, item.entity_id),
                render_projection_record(
                    item,
                    project_id=self.project_id,
                    snapshot_digest=self.source_snapshot_digest,
                    generation_digest=self.generation_digest,
                ),
                item.record_digest,
            )
            for item in self.records
        )
        if self.files != expected_files:
            raise ValidationFailed("projection payload canonical render ile uyusmuyor")

    @property
    def projection_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "source_snapshot_digest": self.source_snapshot_digest,
            "generation_digest": self.generation_digest,
            "renderer_profile": RENDERER_PROFILE,
            "files": [
                {
                    "relative_path": f.relative_path,
                    "content_digest": f.content_digest,
                    "source_record_digest": f.source_record_digest,
                }
                for f in self.files
            ],
            "grants_authority": False,
        }

    def manifest_bytes(self) -> bytes:
        return canonical_bytes({**self.as_dict(), "projection_digest": self.projection_digest})


class ObsidianProfile(StrEnum):
    """Birbirine karistirilamayan fiziksel projection profilleri."""

    PRIVATE_LOCAL = "private-local"
    PUBLIC_SAFE = "public-safe"

    @property
    def allowed_classifications(self) -> frozenset[DataClassification]:
        if self is ObsidianProfile.PUBLIC_SAFE:
            return frozenset({DataClassification.PUBLIC})
        return frozenset({DataClassification.PUBLIC, DataClassification.INTERNAL})


class ObsidianNoteKind(StrEnum):
    WORK = "work"
    DECISION = "decision"
    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    FAILURE = "failure"
    DAYLOG = "daylog"


_NOTE_ROOT = {
    ObsidianNoteKind.WORK: "01_ACTIVE/CALISMA_OGELERI",
    ObsidianNoteKind.DECISION: "02_DECISIONS",
    ObsidianNoteKind.KNOWLEDGE: "03_KNOWLEDGE/KAVRAMLAR",
    ObsidianNoteKind.SKILL: "04_SKILLS",
    ObsidianNoteKind.FAILURE: "05_FAILURES",
    ObsidianNoteKind.DAYLOG: "06_DAYLOGS",
}
_OBSIDIAN_EXCLUSION_REASONS = {
    "classification-prohibited",
    "classification-excluded",
    "record-oversized",
    "secret-pattern",
    "pii-email",
    "absolute-path",
    "connection-string",
    "raw-content-marker",
}


def _aware(value: dt.datetime, field: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{field} timezone-aware olmali")


def _portable_path(value: str, field: str, *, suffixes: tuple[str, ...] = ()) -> None:
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
    ):
        raise ValidationFailed(f"{field} portable relative path olmali")
    if suffixes and not value.endswith(suffixes):
        raise ValidationFailed(f"{field} izinli dosya turunde olmali")


@dataclass(frozen=True, slots=True)
class ObsidianProjectionRecord:
    """Kanonik kaydin profile girmeden onceki typed projection gorunumu."""

    record: ProjectionRecord
    note_kind: ObsidianNoteKind
    realm_slug: str
    project_id: UUID
    truth_class: TruthClass
    classification: DataClassification
    observed_at: dt.datetime
    memory_class: str | None = None
    confidence: float | None = None
    valid_from: dt.datetime | None = None
    valid_until: dt.datetime | None = None
    supersedes: tuple[str, ...] = ()
    superseded_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.record.__post_init__()
        _literal(self.realm_slug, "projection realm slug")
        if not isinstance(self.project_id, UUID):
            raise ValidationFailed("projection record exact project UUID ister")
        if not isinstance(self.note_kind, ObsidianNoteKind):
            raise ValidationFailed("projection note kind registry disinda")
        if not isinstance(self.truth_class, TruthClass):
            raise ValidationFailed("projection truth class registry disinda")
        if not isinstance(self.classification, DataClassification):
            raise ValidationFailed("projection classification registry disinda")
        _aware(self.observed_at, "projection observed_at")
        for moment in (self.valid_from, self.valid_until):
            if moment is not None:
                _aware(moment, "projection validity")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValidationFailed("projection validity araligi gecersiz")
        if self.memory_class is not None:
            _literal(self.memory_class, "projection memory class")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed("projection confidence 0..1 araliginda olmali")
        for values, field in (
            (self.supersedes, "projection supersedes"),
            (self.superseded_by, "projection superseded_by"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValidationFailed(f"{field} tekil ve sirali olmali")
            for value in values:
                _literal(value, field)

    @property
    def identity(self) -> str:
        return f"{self.record.entity_type}:{self.record.entity_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.as_dict(),
            "note_kind": self.note_kind.value,
            "realm_slug": self.realm_slug,
            "project_id": str(self.project_id),
            "truth_class": self.truth_class.value,
            "classification": self.classification.value,
            "memory_class": self.memory_class,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "observed_at": self.observed_at,
            "supersedes": list(self.supersedes),
            "superseded_by": list(self.superseded_by),
        }


def obsidian_note_path(record: ObsidianProjectionRecord) -> str:
    record.__post_init__()
    stem = (
        _SAFE_NAME.sub("-", f"{record.record.entity_type}-{record.record.entity_id}")
        .strip("-.")
        .lower()
    )
    if not stem:
        raise ValidationFailed("Obsidian note dosya adi uretilemedi")
    if len(stem) > 120:
        suffix = parse_digest(digest(record.identity))[:32]
        stem = f"{stem[:80].rstrip('-.')}-{suffix}"
    root = _NOTE_ROOT[record.note_kind]
    if record.note_kind in {ObsidianNoteKind.DECISION, ObsidianNoteKind.DAYLOG}:
        root = f"{root}/{record.observed_at.astimezone(dt.UTC).year:04d}"
    if record.record.status in {
        "completed",
        "cancelled",
        "superseded",
        "revoked",
        "archived",
        "deprecated",
        "retired",
        "rejected",
        "quarantined",
    }:
        root = "90_ARCHIVE"
    return f"{root}/{stem}.md"


@dataclass(frozen=True, slots=True)
class ObsidianProjectionFile:
    relative_path: str
    payload: bytes
    media_type: str

    def __post_init__(self) -> None:
        _portable_path(
            self.relative_path,
            "Obsidian projection file",
            suffixes=(".md", ".json", "schema-version"),
        )
        if not self.payload or len(self.payload) > 1024 * 1024:
            raise ValidationFailed("Obsidian projection file bos veya bounded disi")
        if self.media_type not in {
            "text/markdown; charset=utf-8",
            "application/json",
            "text/plain; charset=utf-8",
        }:
            raise ValidationFailed("Obsidian projection media type allowlist disinda")

    @property
    def content_digest(self) -> str:
        return digest_of_bytes(self.payload)

    def as_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "media_type": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class ProjectionExclusion:
    record_digest: str
    reason_code: str

    def __post_init__(self) -> None:
        parse_digest(self.record_digest)
        _literal(self.reason_code, "projection exclusion reason")
        if self.reason_code not in _OBSIDIAN_EXCLUSION_REASONS:
            raise ValidationFailed("projection exclusion reason registry disinda")

    def as_dict(self) -> dict[str, str]:
        return {"record_digest": self.record_digest, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class ObsidianProjectionBundle:
    """Immutable generation; filesystem konumu ve authority tasimaz."""

    realm_slug: str
    project_id: UUID
    profile: ObsidianProfile
    source_snapshot_digest: str
    policy_digest: str
    projection_digest: str
    generated_at: dt.datetime
    files: tuple[ObsidianProjectionFile, ...]
    privacy_scan_digest: str
    link_check_digest: str
    exclusions: tuple[ProjectionExclusion, ...] = ()
    grants_authority: bool = False
    schema: str = "zekam-obsidian-projection/v1"

    def __post_init__(self) -> None:
        _literal(self.realm_slug, "Obsidian bundle realm")
        if not isinstance(self.project_id, UUID):
            raise ValidationFailed("Obsidian bundle exact project UUID ister")
        if not isinstance(self.profile, ObsidianProfile):
            raise ValidationFailed("Obsidian profile registry disinda")
        for value in (
            self.source_snapshot_digest,
            self.policy_digest,
            self.projection_digest,
            self.privacy_scan_digest,
            self.link_check_digest,
        ):
            parse_digest(value)
        _aware(self.generated_at, "Obsidian generated_at")
        if self.schema != "zekam-obsidian-projection/v1" or self.grants_authority:
            raise PolicyViolation("Obsidian bundle authority-free exact schema ister")
        paths = tuple(item.relative_path for item in self.files)
        if paths != tuple(sorted(set(paths))) or not self.files or len(self.files) > 4096:
            raise ValidationFailed("Obsidian bundle files tekil, sirali ve dolu olmali")
        for item in self.files:
            item.__post_init__()
        if sum(len(item.payload) for item in self.files) > 64 * 1024 * 1024:
            raise ValidationFailed("Obsidian bundle toplam payload bounded disi")
        expected_exclusions = tuple(
            sorted(self.exclusions, key=lambda item: (item.record_digest, item.reason_code))
        )
        if len(self.exclusions) > 1000:
            raise ValidationFailed("Obsidian exclusions bounded limiti asiyor")
        for exclusion in self.exclusions:
            exclusion.__post_init__()
        if expected_exclusions != self.exclusions:
            raise ValidationFailed("Obsidian exclusions deterministik sirada olmali")
        expected_projection = digest(
            {
                "schema": self.schema,
                "realm_slug": self.realm_slug,
                "project_id": str(self.project_id),
                "profile": self.profile.value,
                "source_snapshot_digest": self.source_snapshot_digest,
                "policy_digest": self.policy_digest,
                "renderer_profile": OBSIDIAN_RENDERER_PROFILE,
            }
        )
        if self.projection_digest != expected_projection:
            raise ValidationFailed(
                "Obsidian projection digest source/project/profile ile uyusmuyor"
            )

    def manifest_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-obsidian-manifest/v1",
            "realm": self.realm_slug,
            "project_id": str(self.project_id),
            "profile": self.profile.value,
            "source_snapshot_digest": self.source_snapshot_digest,
            "policy_digest": self.policy_digest,
            "projection_digest": self.projection_digest,
            "renderer_profile": OBSIDIAN_RENDERER_PROFILE,
            "generated_at": self.generated_at,
            "files": [item.as_dict() for item in self.files],
            "privacy_scan_digest": self.privacy_scan_digest,
            "link_check_digest": self.link_check_digest,
            "exclusions": [item.as_dict() for item in self.exclusions],
            "grants_authority": False,
        }

    @property
    def manifest_digest(self) -> str:
        return digest(self.manifest_body())

    def manifest_bytes(self) -> bytes:
        return canonical_bytes(self.manifest_body() | {"manifest_digest": self.manifest_digest})

    def receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-obsidian-projection-receipt/v1",
            "realm": self.realm_slug,
            "project_id": str(self.project_id),
            "profile": self.profile.value,
            "source_snapshot_digest": self.source_snapshot_digest,
            "manifest_digest": self.manifest_digest,
            "file_count": len(self.files),
            "privacy_scan_digest": self.privacy_scan_digest,
            "link_check_digest": self.link_check_digest,
            "projection_digest": self.projection_digest,
            "status": "completed",
            "generated_at": self.generated_at,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.receipt_body())

    def receipt_bytes(self) -> bytes:
        return canonical_bytes(self.receipt_body() | {"receipt_digest": self.receipt_digest})
