"""Operational authority port and transport-neutral records.

Application services depend on this module, never on SQLite/PostgreSQL adapters.
Every mutating workflow receives an explicit unit of work and must call
``commit``; leaving the context without a commit rolls the transaction back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self


@dataclass(frozen=True, slots=True)
class ConfigRevisionRecord:
    id: str
    config_digest: str
    task_digest: str


@dataclass(frozen=True, slots=True)
class OperationalProjectRecord:
    id: str
    slug: str
    display_name: str
    status: str = "active"
    revision: int = 1


@dataclass(frozen=True, slots=True)
class SourceBindingRecord:
    id: str
    project_id: str
    portable_ref: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class SourceSnapshotRecord:
    id: str
    source_binding_id: str
    revision_ref: str
    tree_digest: str
    content_digest: str
    config_digest: str


@dataclass(frozen=True, slots=True)
class OperationalWorkRecord:
    id: str
    project_id: str
    kind: str
    title: str
    state: str
    revision: int
    evidence_digest: str | None
    external_number: str | None = None
    summary: str = ""
    acceptance_criteria: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    work_item_id: str
    status: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class RunStepRecord:
    id: str
    run_id: str
    step_key: str
    status: str
    input_digest: str
    evidence_digest: str | None


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    id: str
    run_id: str
    sequence: int
    checkpoint_digest: str


@dataclass(frozen=True, slots=True)
class OperationalBackupReceipt:
    source_schema_version: int
    source_schema_digest: str
    logical_digest: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    client_id: str
    device_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ModelIdentityRecord:
    id: str
    canonical_id: str
    access_name: str
    modality: str


@dataclass(frozen=True, slots=True)
class ModelRevisionRecord:
    id: str
    model_identity_id: str
    provider_fingerprint_digest: str
    observed_revision: str


@dataclass(frozen=True, slots=True)
class ArtifactRefRecord:
    digest: str
    media_type: str
    size_bytes: int
    classification: str


@dataclass(frozen=True, slots=True)
class KnowledgeNoteRecord:
    id: str
    owner_scope: str
    portable_ref: str
    note_kind: str
    authorship: str
    classification: str
    content_digest: str
    state: str
    realm_id: str
    project_id: str | None
    project_slug: str | None
    materialized: bool
    archived_ref: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeRelationRecord:
    id: str
    from_note_id: str
    to_note_id: str
    relation_kind: str
    source_digest: str
    verified: bool


class OperationalUnitOfWork(Protocol):
    """One fail-closed operational transaction."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]: ...

    def activate_config(
        self,
        *,
        config_digest: str,
        task_digest: str,
        sanitized_config: dict[str, Any],
    ) -> ConfigRevisionRecord: ...

    def create_project(self, *, slug: str, display_name: str) -> OperationalProjectRecord: ...

    def resolve_project(
        self, reference: str, *, include_archived: bool = False
    ) -> OperationalProjectRecord: ...

    def list_projects(
        self, *, include_archived: bool = False
    ) -> tuple[OperationalProjectRecord, ...]: ...

    def list_project_aliases(self, project_id: str) -> tuple[str, ...]: ...

    def add_project_alias(self, *, project_id: str, alias: str) -> None: ...

    def bind_source(
        self, *, project_id: str, portable_ref: str, source_kind: str
    ) -> SourceBindingRecord: ...

    def capture_source_snapshot(
        self,
        *,
        source_binding_id: str,
        revision_ref: str,
        tree_digest: str,
        content_digest: str,
        config_digest: str,
    ) -> SourceSnapshotRecord: ...

    def create_work(
        self,
        *,
        project_id: str,
        kind: str,
        title: str,
        state: str,
        payload_digest: str | None = None,
        payload: dict[str, Any] | None = None,
        external_number: str | None = None,
        evidence_digest: str | None = None,
    ) -> OperationalWorkRecord: ...

    def transition_work(
        self,
        *,
        work_item_id: str,
        expected_revision: int,
        to_state: str,
        payload_digest: str,
        event_digest: str,
        evidence_digest: str | None = None,
    ) -> OperationalWorkRecord: ...

    def create_run(
        self,
        *,
        work_item_id: str,
        config_revision_id: str,
        plan_digest: str,
        budget: dict[str, Any],
        source_snapshot_id: str | None = None,
    ) -> RunRecord: ...

    def add_run_step(
        self,
        *,
        run_id: str,
        step_key: str,
        input_digest: str,
        dependencies: tuple[str, ...] = (),
    ) -> RunStepRecord: ...

    def record_checkpoint(
        self,
        *,
        run_id: str,
        sequence: int,
        checkpoint_digest: str,
        payload: dict[str, Any],
        source_snapshot_id: str | None = None,
    ) -> CheckpointRecord: ...

    def get_work(self, work_item_id: str) -> OperationalWorkRecord: ...

    def list_work(self, *, project_id: str | None = None) -> tuple[OperationalWorkRecord, ...]: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def list_checkpoints(self, run_id: str) -> tuple[CheckpointRecord, ...]: ...
    def record_bootstrap_receipt(
        self,
        *,
        receipt_digest: str,
        plan_digest: str,
        task_digest: str,
        status: str,
    ) -> None: ...

    def open_session(
        self,
        *,
        client_id: str,
        device_id: str,
        project_id: str | None = None,
        work_item_id: str | None = None,
    ) -> SessionRecord: ...

    def record_session_event(
        self, *, session_id: str, event_kind: str, event_digest: str
    ) -> None: ...

    def register_model(
        self, *, canonical_id: str, access_name: str, modality: str
    ) -> ModelIdentityRecord: ...

    def observe_model_revision(
        self,
        *,
        model_identity_id: str,
        provider_fingerprint_digest: str,
        observed_revision: str,
    ) -> ModelRevisionRecord: ...

    def record_model_availability(
        self,
        *,
        model_revision_id: str,
        device_scope: str,
        client_scope: str,
        provider_scope: str,
        available: bool,
    ) -> None: ...

    def record_model_health(
        self,
        *,
        model_revision_id: str,
        status: str,
        evidence_digest: str,
        latency_ms: int | None,
    ) -> None: ...

    def register_artifact(
        self,
        *,
        artifact_digest: str,
        media_type: str,
        size_bytes: int,
        classification: str,
    ) -> ArtifactRefRecord: ...

    def register_knowledge_note(
        self,
        *,
        realm_id: str,
        project_id: str | None,
        owner_scope: str,
        portable_ref: str,
        note_kind: str,
        authorship: str,
        classification: str,
        content_digest: str,
        state: str = "active",
    ) -> KnowledgeNoteRecord: ...

    def relate_knowledge_notes(
        self,
        *,
        from_note_id: str,
        to_note_id: str,
        relation_kind: str,
        source_digest: str,
        verified: bool,
    ) -> KnowledgeRelationRecord: ...

    def confirm_knowledge_note(
        self, *, note_id: str, expected_content_digest: str, evidence_digest: str
    ) -> KnowledgeNoteRecord: ...

    def archive_knowledge_note(
        self, *, note_id: str, expected_content_digest: str, archived_ref: str
    ) -> KnowledgeNoteRecord: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class OperationalStore(Protocol):
    """Factory port for operational units of work."""

    def unit_of_work(self) -> OperationalUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class OperationalSchemaStatus:
    exists: bool
    schema_version: int | None
    integrity_ok: bool
    schema_ok: bool


class OperationalSchemaPort(Protocol):
    """Fresh operational schema bootstrap/integrity boundary."""

    @property
    def schema_version(self) -> int: ...

    @property
    def schema_digest(self) -> str: ...

    def bootstrap(self, path: Path) -> OperationalSchemaStatus: ...

    def status(self, path: Path) -> OperationalSchemaStatus: ...


class OperationalBackupPort(Protocol):
    """Online snapshot/restore adapter with logical parity evidence."""

    def create_backup(self, destination: str) -> OperationalBackupReceipt: ...

    def restore_backup(self, backup_path: str, destination: str) -> OperationalBackupReceipt: ...
