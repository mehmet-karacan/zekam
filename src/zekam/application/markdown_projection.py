"""Deterministik Markdown/Obsidian projection builder ve CAS persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.application.transcript_corpus_import import ContentAddressedStore
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.markdown_projection import (
    RENDERER_PROFILE,
    MarkdownProjectionBundle,
    MarkdownProjectionFile,
    ProjectionRecord,
    projection_filename,
    render_projection_record,
)


def build_markdown_projection(
    project_id: str, records: tuple[ProjectionRecord, ...]
) -> MarkdownProjectionBundle:
    if not records:
        raise ValidationFailed("projection project ve record ister")
    for item in records:
        item.__post_init__()
    ordered = tuple(sorted(records, key=lambda item: (item.entity_type, item.entity_id)))
    identities = tuple((item.entity_type, item.entity_id) for item in ordered)
    if len(set(identities)) != len(identities):
        raise ValidationFailed("projection entity kimlikleri tekil olmali")
    snapshot = digest([item.as_dict() for item in ordered])
    generation = digest(
        {
            "schema": "zekam-markdown-projection/v1",
            "project_id": project_id,
            "source_snapshot_digest": snapshot,
            "renderer_profile": RENDERER_PROFILE,
        }
    )
    files = tuple(
        MarkdownProjectionFile(
            projection_filename(item.entity_type, item.entity_id),
            render_projection_record(
                item, project_id=project_id, snapshot_digest=snapshot, generation_digest=generation
            ),
            item.record_digest,
        )
        for item in ordered
    )
    return MarkdownProjectionBundle(project_id, snapshot, generation, ordered, files)


class MarkdownProjectionRecordSource(Protocol):
    def load_project_records(
        self, project_id: UUID, *, limit: int = 500
    ) -> tuple[ProjectionRecord, ...]: ...


def rebuild_markdown_projection_from_database(
    source: MarkdownProjectionRecordSource,
    project_id: UUID,
    *,
    limit: int = 500,
) -> MarkdownProjectionBundle:
    """Rebuild a read-only projection from one canonical DB snapshot."""

    records = source.load_project_records(project_id, limit=limit)
    return build_markdown_projection(str(project_id), records)


@dataclass(frozen=True, slots=True)
class StoredMarkdownProjection:
    projection_digest: str
    manifest_object_digest: str
    file_object_digests: tuple[str, ...]


def persist_markdown_projection(
    bundle: MarkdownProjectionBundle, store: ContentAddressedStore
) -> StoredMarkdownProjection:
    bundle.__post_init__()
    stored: list[str] = []
    for item in bundle.files:
        info = store.put(
            item.payload,
            media_type="text/markdown; charset=utf-8",
            metadata={"relative_path": item.relative_path, "projection": "read-only"},
        )
        if (
            info.digest != item.content_digest
            or not store.exists(info.digest)
            or store.get(info.digest) != item.payload
        ):
            raise ValidationFailed("Markdown projection CAS dogrulamasi basarisiz")
        stored.append(info.digest)
    manifest = bundle.manifest_bytes()
    info = store.put(
        manifest,
        media_type="application/vnd.zekam.markdown-projection+json",
        metadata={"project_id": bundle.project_id, "projection": "read-only"},
    )
    if (
        info.digest != digest_of_bytes(manifest)
        or not store.exists(info.digest)
        or store.get(info.digest) != manifest
    ):
        raise ValidationFailed("Markdown projection manifest CAS dogrulamasi basarisiz")
    return StoredMarkdownProjection(bundle.projection_digest, info.digest, tuple(stored))
