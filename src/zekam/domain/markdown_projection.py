"""Authority tasimayan, deterministik Markdown projection domain modeli."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")
_UNSAFE_LITERAL = re.compile(r"[\x00-\x1f\x7f`\[\]|#^]")
RENDERER_PROFILE = "zekam-obsidian-projection-renderer/v1"


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
