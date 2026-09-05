"""Storage-neutral port and immutable records for the hybrid knowledge index."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from zekam.application.retrieval_service import ChunkView
from zekam.domain.canonical import digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import ScoredHit

KNOWLEDGE_VECTOR_DIMENSION = 1024
MAX_KNOWLEDGE_TEXT_BYTES = 512 * 1024


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in value
    ):
        raise ValidationFailed("Knowledge source path portable relative olmali")
    return value


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRecord:
    chunk_id: str
    project_id: str
    source_revision: str
    source_path: str
    source_digest: str
    locator: Locator
    text: str
    content_digest: str
    chunk_order: int
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            not self.chunk_id.strip()
            or not self.project_id.strip()
            or not self.source_revision.strip()
        ):
            raise ValidationFailed("Knowledge record exact kimlik/revision ister")
        _safe_relative(self.source_path)
        parse_digest(self.source_digest)
        parse_digest(self.content_digest)
        if self.content_digest != digest_of_bytes(self.text.encode("utf-8")):
            raise ValidationFailed("Knowledge record text/content digest drift")
        if self.locator.relative_path != self.source_path or self.locator.is_empty:
            raise ValidationFailed("Knowledge record locator/source path drift")
        if not self.text.strip() or len(self.text.encode("utf-8")) > MAX_KNOWLEDGE_TEXT_BYTES:
            raise ValidationFailed("Knowledge record text bounded non-empty olmali")
        if type(self.chunk_order) is not int or self.chunk_order < 0:
            raise ValidationFailed("Knowledge chunk order non-negative integer olmali")
        if len(self.vector) != KNOWLEDGE_VECTOR_DIMENSION or any(
            type(value) is not float or not math.isfinite(value) for value in self.vector
        ):
            raise ValidationFailed("Knowledge vector exact finite float1024 olmali")
        norm = math.sqrt(sum(value * value for value in self.vector))
        if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise ValidationFailed("Knowledge vector normalized olmali")


@dataclass(frozen=True, slots=True)
class KnowledgeGeneration:
    generation_digest: str
    project_id: str
    source_revision: str
    tree_digest: str
    source_manifest_digest: str
    embedding_profile_digest: str
    provider_profile_digest: str
    chunk_count: int
    state: str

    def __post_init__(self) -> None:
        for value in (
            self.generation_digest,
            self.tree_digest,
            self.source_manifest_digest,
            self.embedding_profile_digest,
            self.provider_profile_digest,
        ):
            parse_digest(value)
        if self.state not in {"building", "ready", "superseded"}:
            raise ValidationFailed("Knowledge generation state gecersiz")


class KnowledgeIndexPort(Protocol):
    """Application-facing hybrid index contract; storage choice is external."""

    def build_generation(
        self,
        records: tuple[KnowledgeIndexRecord, ...],
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        embedding_profile_digest: str,
        provider_profile_digest: str,
        created_at: str,
    ) -> KnowledgeGeneration: ...

    def generation(self, project_id: str) -> KnowledgeGeneration: ...

    def exact(
        self,
        project_id: str,
        identifiers: tuple[str, ...],
        *,
        limit: int,
        generation_digest: str,
    ) -> tuple[ScoredHit, ...]: ...

    def lexical(
        self,
        project_id: str,
        query: str,
        *,
        limit: int,
        generation_digest: str,
    ) -> tuple[ScoredHit, ...]: ...

    def dense(
        self,
        project_id: str,
        vector: tuple[float, ...],
        *,
        limit: int,
        generation_digest: str,
    ) -> tuple[ScoredHit, ...]: ...

    def views(
        self,
        project_id: str,
        chunk_refs: tuple[str, ...],
        *,
        generation_digest: str,
    ) -> dict[str, ChunkView]: ...

    def source_identity(
        self, project_id: str, chunk_id: str, *, generation_digest: str
    ) -> dict[str, str]: ...

    def integrity(self) -> dict[str, object]: ...
