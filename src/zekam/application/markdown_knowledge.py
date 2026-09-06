"""Digest-verified read surfaces for canonical Markdown knowledge notes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
)
from zekam.application.operational_store import KnowledgeNoteRecord
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_QUERY_CHARS = 500


class KnowledgeReadStore(Protocol):
    def list_knowledge_notes(
        self,
        *,
        project_id: str | None = None,
        owner_scope: str | None = None,
        note_kind: str | None = None,
        state: str | None = "active",
        limit: int = 100,
    ) -> tuple[KnowledgeNoteRecord, ...]: ...

    def get_knowledge_note(self, reference: str) -> KnowledgeNoteRecord: ...


class KnowledgeFileReader(Protocol):
    def read_note(
        self, manifest: KnowledgeNoteManifest, *, relative_ref: str | None = None
    ) -> bytes: ...


def _metadata(record: KnowledgeNoteRecord) -> dict[str, object]:
    return {
        "note_id": record.id,
        "owner_scope": record.owner_scope,
        "project_id": record.project_id,
        "project_slug": record.project_slug,
        "portable_ref": record.portable_ref,
        "note_kind": record.note_kind,
        "authorship": record.authorship,
        "classification": record.classification,
        "content_digest": record.content_digest,
        "state": record.state,
        "materialized": record.materialized,
    }


def _read_verified(files: KnowledgeFileReader, record: KnowledgeNoteRecord) -> str:
    if not record.materialized:
        raise PolicyViolation("Knowledge note materialized degil")
    try:
        classification = KnowledgeClassification(record.classification)
    except ValueError as exc:
        raise ValidationFailed("Knowledge note classification gecersiz") from exc
    manifest = KnowledgeNoteManifest(
        owner_scope=record.owner_scope,
        project_slug=record.project_slug,
        note_kind=record.note_kind,
        authorship=record.authorship,
        classification=classification,
        portable_ref=record.portable_ref,
        content_digest=record.content_digest,
        state=record.state,
    )
    payload = files.read_note(
        manifest,
        relative_ref=(record.archived_ref if record.state == "archived" else None),
    )
    if not payload or len(payload) > MAX_NOTE_BYTES:
        raise ValidationFailed("Knowledge note bos veya oversized")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Knowledge note strict UTF-8 olmali") from exc


def list_markdown_knowledge(
    store: KnowledgeReadStore,
    *,
    project_id: str | None = None,
    owner_scope: str | None = None,
    note_kind: str | None = None,
    state: str | None = "active",
    limit: int = 100,
) -> dict[str, object]:
    rows = store.list_knowledge_notes(
        project_id=project_id,
        owner_scope=owner_scope,
        note_kind=note_kind,
        state=state,
        limit=limit,
    )
    body: dict[str, object] = {
        "schema": "zekam-markdown-knowledge-list/v1",
        "filters": {
            "project_id": project_id,
            "owner_scope": owner_scope,
            "note_kind": note_kind,
            "state": state,
        },
        "count": len(rows),
        "notes": [_metadata(row) for row in rows],
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


def show_markdown_knowledge(
    store: KnowledgeReadStore,
    files: KnowledgeFileReader,
    reference: str,
    *,
    project_id: str | None = None,
    owner_scope: str | None = None,
) -> dict[str, object]:
    record = store.get_knowledge_note(reference)
    if project_id is not None and record.project_id != project_id:
        raise PolicyViolation("Knowledge note project scope disinda")
    if owner_scope is not None and record.owner_scope != owner_scope:
        raise PolicyViolation("Knowledge note owner scope disinda")
    text = _read_verified(files, record)
    body: dict[str, object] = {
        "schema": "zekam-markdown-knowledge-show/v1",
        **_metadata(record),
        "body": text,
        "verified": True,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    note: dict[str, object]
    score: int
    excerpt: str


def search_markdown_knowledge(
    store: KnowledgeReadStore,
    files: KnowledgeFileReader,
    query: str,
    *,
    project_id: str | None = None,
    owner_scope: str | None = None,
    note_kind: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    normalized = query.strip().casefold()
    if not normalized or len(query) > MAX_QUERY_CHARS:
        raise ValidationFailed("Knowledge search query bos veya oversized")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationFailed("Knowledge search limit 1..100 araliginda olmali")
    terms = tuple(dict.fromkeys(normalized.split()))
    candidates = store.list_knowledge_notes(
        project_id=project_id,
        owner_scope=owner_scope,
        note_kind=note_kind,
        state="active",
        limit=1000,
    )
    hits: list[KnowledgeSearchHit] = []
    for record in candidates:
        text = _read_verified(files, record)
        folded = text.casefold()
        path_folded = record.portable_ref.casefold()
        matched = sum(term in folded or term in path_folded for term in terms)
        if matched != len(terms):
            continue
        phrase_index = folded.find(normalized)
        score = matched * 10 + (100 if phrase_index >= 0 else 0)
        first_index = (
            phrase_index
            if phrase_index >= 0
            else min((folded.find(term) for term in terms if folded.find(term) >= 0), default=0)
        )
        start = max(0, first_index - 120)
        excerpt = " ".join(text[start : start + 500].split())
        hits.append(KnowledgeSearchHit(_metadata(record), score, excerpt))
    hits.sort(key=lambda item: (-item.score, str(item.note["portable_ref"])))
    selected = hits[:limit]
    body: dict[str, object] = {
        "schema": "zekam-markdown-knowledge-search/v1",
        "query_digest": digest({"query": query}),
        "project_id": project_id,
        "owner_scope": owner_scope,
        "note_kind": note_kind,
        "count": len(selected),
        "hits": [asdict(item) for item in selected],
        "searched_count": len(candidates),
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}
