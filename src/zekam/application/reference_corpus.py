"""Rebuildable chunks from canonical, terminal project reference publications."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    assert_public_safe_projection,
)
from zekam.application.knowledge_parsers import MarkdownParser
from zekam.application.operational_store import KnowledgeNoteRecord
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.retrieval import Chunk, ChunkProfile, chunk_units
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

REFERENCE_PATH_PREFIX = ".zekam-reference/"
REFERENCE_PARSER = "zekam.canonical-reference-markdown/v1"
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_CHUNKS = 2048
_NAME = re.compile(r"jira-([a-f0-9]{32})-([a-f0-9]{32})\.md$")


@dataclass(frozen=True, slots=True)
class ReferenceCorpus:
    project_id: str
    notes: tuple[KnowledgeNoteRecord, ...]
    chunks: tuple[Chunk, ...] = ()

    @property
    def snapshot_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-reference-corpus/v1",
                "project_id": self.project_id,
                "notes": [
                    {"id": note.id, "ref": note.portable_ref, "digest": note.content_digest}
                    for note in self.notes
                ],
            }
        )

    def source_digest(self, path: str) -> str:
        for note in self.notes:
            if path == REFERENCE_PATH_PREFIX + note.id + ".md":
                return note.content_digest
        raise PolicyViolation("Reference citation current canonical snapshot icinde degil")

    def verify_citation(self, home: Path, path: str, source_digest: str) -> None:
        if self.source_digest(path) != source_digest:
            raise PolicyViolation("Reference citation source digest stale")
        note = next(note for note in self.notes if path == REFERENCE_PATH_PREFIX + note.id + ".md")
        _read(KnowledgeFileStore(home), note)


def _read(files: KnowledgeFileStore, note: KnowledgeNoteRecord) -> bytes:
    payload = files.read_note(
        KnowledgeNoteManifest(
            owner_scope=note.owner_scope,
            project_slug=note.project_slug,
            note_kind=note.note_kind,
            authorship=note.authorship,
            classification=KnowledgeClassification(note.classification),
            portable_ref=note.portable_ref,
            content_digest=note.content_digest,
            state=note.state,
        )
    )
    assert_public_safe_projection(payload, relative_path=note.portable_ref)
    return payload


def collect_reference_corpus(
    home: Path,
    project_id: str,
    project_slug: str,
    *,
    with_chunks: bool = False,
    verify_files: bool = False,
) -> ReferenceCorpus:
    store = SQLiteOperationalStore(home / "state/operational.db")
    prefix = f"projeler/{project_slug}/knowledge/generated/reference/jira-"
    with store.unit_of_work() as uow:
        if uow.resolve_project(project_id).slug != project_slug:
            raise PolicyViolation("Reference corpus project slug/ID drift")
        notes = uow.knowledge_reference_snapshot(project_id=project_id, portable_prefix=prefix)
    seen = set()
    chunks = []
    total_bytes = 0
    files = KnowledgeFileStore(home)
    parser = MarkdownParser()
    profile = ChunkProfile(
        "canonical-reference-markdown-v1",
        max_tokens=384,
        keep_tables_whole=False,
        keep_code_whole=False,
    )
    for note in notes:
        match = _NAME.fullmatch(note.portable_ref.rsplit("/", 1)[-1])
        if match is None or match[1] in seen:
            raise PolicyViolation("Legacy/ambiguous reference corpus reconciliation ister")
        seen.add(match[1])
        if not (with_chunks or verify_files):
            continue
        payload = _read(files, note)
        total_bytes += len(payload)
        if total_bytes > MAX_REFERENCE_BYTES:
            raise PolicyViolation("Reference corpus byte siniri asildi")
        if with_chunks:
            path = REFERENCE_PATH_PREFIX + note.id + ".md"
            units = tuple(
                replace(unit, locator=replace(unit.locator, relative_path=path))
                for unit in parser.parse(payload)
            )
            for chunk in chunk_units(
                units, document_id=f"reference-{note.content_digest[7:]}", profile=profile
            ):
                chunks.append(
                    replace(
                        chunk,
                        chunk_id=f"ref-{note.id}-{chunk.chunk_id}",
                        order=len(chunks),
                        parent_id=None
                        if chunk.parent_id is None
                        else f"ref-{note.id}-{chunk.parent_id}",
                    )
                )
                if len(chunks) > MAX_REFERENCE_CHUNKS:
                    raise PolicyViolation("Reference corpus chunk siniri asildi")
    return ReferenceCorpus(project_id, notes, tuple(chunks))
