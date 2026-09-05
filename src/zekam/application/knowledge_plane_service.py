"""Production lifecycle for recoverable knowledge-file and operational manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zekam.application.knowledge_file_plane import (
    ArtifactPutPlan,
    KnowledgeNoteManifest,
    ProjectProjection,
)
from zekam.application.operational_store import (
    ArtifactRefRecord,
    KnowledgeNoteRecord,
    OperationalStore,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


class KnowledgeFilePort(Protocol):
    def publish_project_projection(self, projection: ProjectProjection) -> Path: ...

    def create_note(self, manifest: KnowledgeNoteManifest, payload: bytes) -> Path: ...

    def archive_note(self, manifest: KnowledgeNoteManifest) -> str: ...

    def put_artifact(self, plan: ArtifactPutPlan, payload: bytes) -> Path: ...


@dataclass(frozen=True, slots=True)
class MaterializedKnowledgeNote:
    record: KnowledgeNoteRecord
    path: Path


class KnowledgePlaneService:
    """Coordinate durable pending manifests with idempotent filesystem materialization."""

    def __init__(self, operational: OperationalStore, files: KnowledgeFilePort) -> None:
        self._operational = operational
        self._files = files

    def publish_project_projection(self, projection: ProjectProjection) -> Path:
        return self._files.publish_project_projection(projection)

    def materialize_note(
        self,
        *,
        realm_id: str,
        project_id: str | None,
        manifest: KnowledgeNoteManifest,
        payload: bytes,
    ) -> MaterializedKnowledgeNote:
        """Create/recover a note; pending DB state remains visible after a file failure."""

        with self._operational.unit_of_work() as uow:
            pending = uow.register_knowledge_note(
                realm_id=realm_id,
                project_id=project_id,
                owner_scope=manifest.owner_scope,
                portable_ref=manifest.portable_ref,
                note_kind=manifest.note_kind,
                authorship=manifest.authorship,
                classification=manifest.classification.value,
                content_digest=manifest.content_digest,
                state=manifest.state,
            )
            uow.commit()
        path = self._files.create_note(manifest, payload)
        evidence = digest(
            {
                "operation": "knowledge-note-materialized",
                "note_id": pending.id,
                "portable_ref": manifest.portable_ref,
                "content_digest": manifest.content_digest,
            }
        )
        with self._operational.unit_of_work() as uow:
            ready = uow.confirm_knowledge_note(
                note_id=pending.id,
                expected_content_digest=manifest.content_digest,
                evidence_digest=evidence,
            )
            uow.commit()
        return MaterializedKnowledgeNote(ready, path)

    def archive_note(
        self, *, record: KnowledgeNoteRecord, manifest: KnowledgeNoteManifest
    ) -> KnowledgeNoteRecord:
        """Create/recover the physical archive before committing the DB transition."""

        if not record.materialized:
            raise PolicyViolation("Pending knowledge note archive edilemez")
        archived_ref = self._files.archive_note(manifest)
        with self._operational.unit_of_work() as uow:
            archived = uow.archive_knowledge_note(
                note_id=record.id,
                expected_content_digest=record.content_digest,
                archived_ref=archived_ref,
            )
            uow.commit()
        return archived

    def put_artifact(self, plan: ArtifactPutPlan, payload: bytes) -> ArtifactRefRecord:
        """Persist the immutable manifest first; idempotent replay repairs a missing CAS file."""

        with self._operational.unit_of_work() as uow:
            record = uow.register_artifact(
                artifact_digest=plan.digest,
                media_type=plan.media_type,
                size_bytes=plan.size_bytes,
                classification=plan.classification.value,
            )
            uow.commit()
        self._files.put_artifact(plan, payload)
        return record
