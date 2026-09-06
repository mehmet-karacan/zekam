"""SQLite adapter for the operational authority port."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self
from uuid import UUID

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    validate_note_ownership_path,
    validate_owner_scope,
    validate_portable_relative,
)
from zekam.application.operational_store import (
    ArtifactRefRecord,
    CheckpointRecord,
    ConfigRevisionRecord,
    KnowledgeNoteRecord,
    KnowledgeRelationRecord,
    ModelIdentityRecord,
    ModelRevisionRecord,
    OperationalProjectRecord,
    OperationalWorkRecord,
    RunRecord,
    RunStepRecord,
    SessionRecord,
    SourceBindingRecord,
    SourceSnapshotRecord,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.identifiers import assert_portable, new_uuid7, validate_slug
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, status

_WORK_STATES: Final = frozenset(
    {"proposed", "ready", "active", "blocked", "verification", "completed", "cancelled", "archived"}
)
_TRANSITIONS: Final = {
    "proposed": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"active", "blocked", "cancelled"}),
    "active": frozenset({"blocked", "verification", "cancelled"}),
    "blocked": frozenset({"ready", "active", "cancelled"}),
    "verification": frozenset({"active", "completed", "blocked"}),
    "completed": frozenset(),
    "cancelled": frozenset({"ready"}),
    "archived": frozenset(),
}
_SOURCE_KINDS: Final = frozenset({"git", "directory", "artifact"})
_MODEL_MODALITIES: Final = frozenset(
    {"chat", "code", "embedding", "reranker", "audio", "vision", "guardrail"}
)
_MODEL_HEALTH: Final = frozenset({"passed", "failed", "timeout", "unavailable"})
_KNOWLEDGE_NOTE_KINDS: Final = frozenset(
    {
        "report",
        "research",
        "idea",
        "decision",
        "reference",
        "note",
        "daylog",
        "concept",
        "connection",
        "failure",
        "lesson",
        "skill",
        "handoff",
    }
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationFailed(f"{label} bos veya gecersiz")
    return value


def _exact_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationFailed(f"{label} pozitif integer olmali")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} digest metin olmali")
    parse_digest(value)
    return value


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} canonical UUID olmali")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValidationFailed(f"{label} canonical UUID olmali") from exc
    if str(parsed) != value:
        raise ValidationFailed(f"{label} canonical UUID olmali")
    return value


def _work_payload(value: object) -> dict[str, Any]:
    if value is None:
        return {"summary": "", "acceptance_criteria": []}
    if not isinstance(value, dict) or set(value) - {"summary", "acceptance_criteria"}:
        raise ValidationFailed("Work payload exact nesne olmali")
    summary = value.get("summary", "")
    criteria = value.get("acceptance_criteria", [])
    if not isinstance(summary, str) or not isinstance(criteria, list):
        raise ValidationFailed("Work payload alan tipleri gecersiz")
    normalized_criteria: list[str] = []
    for criterion in criteria:
        normalized_criteria.append(_required_text(criterion, "Work acceptance criterion"))
    return {"summary": summary, "acceptance_criteria": normalized_criteria}


def _row_work(row: Any) -> OperationalWorkRecord:
    try:
        payload = json.loads(row["payload_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Operational Work payload JSON bozuk") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Operational Work payload nesne olmali")
    summary = payload.get("summary", "")
    criteria = payload.get("acceptance_criteria", [])
    if (
        not isinstance(summary, str)
        or not isinstance(criteria, list)
        or not all(isinstance(item, str) for item in criteria)
    ):
        raise ConfigurationError("Operational Work payload projection bozuk")
    return OperationalWorkRecord(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        title=row["title"],
        state=row["state"],
        revision=row["revision"],
        evidence_digest=row["evidence_digest"],
        external_number=row["external_number"],
        summary=summary,
        acceptance_criteria=tuple(criteria),
    )


def _row_knowledge_note(row: Any) -> KnowledgeNoteRecord:
    return KnowledgeNoteRecord(
        str(row["id"]),
        str(row["owner_scope"]),
        str(row["portable_ref"]),
        str(row["note_kind"]),
        str(row["authorship"]),
        str(row["classification"]),
        str(row["content_digest"]),
        str(row["state"]),
        str(row["realm_id"]),
        str(row["project_id"]) if row["project_id"] is not None else None,
        str(row["project_slug"]) if row["project_slug"] is not None else None,
        bool(row["materialized"]),
        str(row["archived_ref"]) if row["archived_ref"] is not None else None,
    )


class SQLiteOperationalStore:
    """Create transaction-scoped SQLite units of work."""

    def __init__(self, path: Path) -> None:
        current = status(path)
        if (
            not current.integrity_ok
            or not current.schema_ok
            or current.schema_version != SCHEMA_VERSION
        ):
            raise ConfigurationError("Operational SQLite store current schema gerektiriyor")
        self._path = path
        self._local = threading.local()

    def unit_of_work(self) -> SQLiteOperationalUnitOfWork:
        return SQLiteOperationalUnitOfWork(self._path, self._local)


class SQLiteOperationalUnitOfWork:
    """One explicit BEGIN IMMEDIATE transaction; default outcome is rollback."""

    def __init__(self, path: Path, local: threading.local) -> None:
        self._path = path
        self._local = local
        self._connection: sqlite3.Connection | None = None
        self._committed = False

    def __enter__(self) -> Self:
        if bool(getattr(self._local, "active", False)):
            raise ConfigurationError("Nested operational unit-of-work yasak")
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        if connection.execute("pragma foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise ConfigurationError("SQLite foreign key enforcement acilamadi")
        connection.execute("pragma busy_timeout = 5000")
        connection.execute("begin immediate")
        self._connection = connection
        self._local.active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        connection = self._connection
        try:
            if connection is not None and not self._committed:
                connection.rollback()
        finally:
            if connection is not None:
                connection.close()
            self._connection = None
            self._local.active = False
        return False

    def _db(self) -> sqlite3.Connection:
        if self._connection is None or self._committed:
            raise ConfigurationError("Operational unit-of-work aktif degil")
        return self._connection

    def commit(self) -> None:
        connection = self._db()
        connection.commit()
        self._committed = True

    def rollback(self) -> None:
        connection = self._db()
        connection.rollback()
        self._committed = True

    def activate_config(
        self,
        *,
        config_digest: str,
        task_digest: str,
        sanitized_config: dict[str, Any],
    ) -> ConfigRevisionRecord:
        _validate_digest(config_digest, "Config")
        _validate_digest(task_digest, "Task")
        body = canonical_json(sanitized_config)
        if digest(sanitized_config) != config_digest:
            raise ValidationFailed("Config digest sanitized payload ile eslesmiyor")
        connection = self._db()
        existing = connection.execute(
            "select id, task_digest, sanitized_json from config_revision where config_digest = ?",
            (config_digest,),
        ).fetchone()
        if existing is not None:
            if existing["task_digest"] != task_digest or existing["sanitized_json"] != body:
                raise ValidationFailed("Config revision replay payload drift")
            config_id = existing["id"]
        else:
            config_id = str(new_uuid7())
            connection.execute("update config_revision set active = 0 where active = 1")
            connection.execute(
                "insert into config_revision(id, config_digest, task_digest, sanitized_json,"
                " active, activated_at) values (?, ?, ?, ?, 1, ?)",
                (config_id, config_digest, task_digest, body, _now()),
            )
        connection.execute("update config_revision set active = (id = ?)", (config_id,))
        return ConfigRevisionRecord(config_id, config_digest, task_digest)

    def create_project(self, *, slug: str, display_name: str) -> OperationalProjectRecord:
        validate_slug(slug)
        display_name = _required_text(display_name, "Project display name")
        connection = self._db()
        alias_collision = connection.execute(
            "select project_id from project_alias where alias = ?", (slug,)
        ).fetchone()
        if alias_collision is not None:
            raise ValidationFailed("Project slug mevcut alias ile cakismiyor olmali")
        existing = connection.execute(
            "select id, display_name, status, revision from project where slug = ?", (slug,)
        ).fetchone()
        if existing is not None:
            if existing["display_name"] != display_name:
                raise ValidationFailed("Project slug replay payload drift")
            return OperationalProjectRecord(
                existing["id"], slug, display_name, existing["status"], existing["revision"]
            )
        project_id = str(new_uuid7())
        connection.execute(
            "insert into project(id, slug, display_name, source_ref, created_at, status, revision)"
            " values (?, ?, ?, null, ?, 'active', 1)",
            (project_id, slug, display_name, _now()),
        )
        return OperationalProjectRecord(project_id, slug, display_name)

    def resolve_project(
        self, reference: str, *, include_archived: bool = False
    ) -> OperationalProjectRecord:
        reference = _required_text(reference, "Project reference")
        connection = self._db()
        row = connection.execute(
            "select distinct project.id, project.slug, project.display_name, project.status,"
            " project.revision from project left join project_alias"
            " on project_alias.project_id = project.id"
            " where (project.id = ? or project.slug = ? or project_alias.alias = ?)"
            + ("" if include_archived else " and project.status = 'active'"),
            (reference, reference, reference),
        ).fetchone()
        if row is None:
            raise ValidationFailed("Project bulunamadi")
        return OperationalProjectRecord(
            row["id"], row["slug"], row["display_name"], row["status"], row["revision"]
        )

    def list_projects(
        self, *, include_archived: bool = False
    ) -> tuple[OperationalProjectRecord, ...]:
        rows = (
            self._db()
            .execute(
                "select id, slug, display_name, status, revision from project"
                + ("" if include_archived else " where status = 'active'")
                + " order by slug, id"
            )
            .fetchall()
        )
        return tuple(
            OperationalProjectRecord(
                row["id"], row["slug"], row["display_name"], row["status"], row["revision"]
            )
            for row in rows
        )

    def list_project_aliases(self, project_id: str) -> tuple[str, ...]:
        rows = (
            self._db()
            .execute(
                "select alias from project_alias where project_id = ? order by alias",
                (project_id,),
            )
            .fetchall()
        )
        return tuple(str(row["alias"]) for row in rows)

    def add_project_alias(self, *, project_id: str, alias: str) -> None:
        validate_slug(alias)
        connection = self._db()
        slug_collision = connection.execute(
            "select id from project where slug = ?", (alias,)
        ).fetchone()
        if slug_collision is not None:
            raise ValidationFailed("Project alias mevcut slug ile cakismiyor olmali")
        existing = connection.execute(
            "select project_id from project_alias where alias = ?", (alias,)
        ).fetchone()
        if existing is not None:
            if existing["project_id"] != project_id:
                raise ValidationFailed("Project alias baska project'e bagli")
            return
        try:
            connection.execute(
                "insert into project_alias(alias, project_id, created_at) values (?, ?, ?)",
                (alias, project_id, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Project alias constraint ihlali") from exc

    def remove_project_alias(self, *, project_id: str, alias: str) -> None:
        validate_slug(alias)
        connection = self._db()
        existing = connection.execute(
            "select project_id from project_alias where alias = ?", (alias,)
        ).fetchone()
        if existing is None or existing["project_id"] != project_id:
            raise ValidationFailed("Project alias bu project'e bagli degil")
        connection.execute(
            "delete from project_alias where alias = ? and project_id = ?",
            (alias, project_id),
        )

    def bind_source(
        self, *, project_id: str, portable_ref: str, source_kind: str
    ) -> SourceBindingRecord:
        portable_ref = assert_portable(portable_ref)
        if source_kind not in _SOURCE_KINDS:
            raise ValidationFailed("Source kind gecersiz")
        connection = self._db()
        existing = connection.execute(
            "select id, source_kind from source_binding where project_id = ? and portable_ref = ?",
            (project_id, portable_ref),
        ).fetchone()
        if existing is not None:
            if existing["source_kind"] != source_kind:
                raise ValidationFailed("Source binding replay payload drift")
            return SourceBindingRecord(existing["id"], project_id, portable_ref, source_kind)
        binding_id = str(new_uuid7())
        try:
            connection.execute(
                "insert into source_binding(id, project_id, portable_ref, source_kind, active,"
                " created_at) values (?, ?, ?, ?, 1, ?)",
                (binding_id, project_id, portable_ref, source_kind, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Source binding constraint ihlali") from exc
        return SourceBindingRecord(binding_id, project_id, portable_ref, source_kind)

    def capture_source_snapshot(
        self,
        *,
        source_binding_id: str,
        revision_ref: str,
        tree_digest: str,
        content_digest: str,
        config_digest: str,
    ) -> SourceSnapshotRecord:
        revision_ref = assert_portable(revision_ref)
        for value, label in (
            (tree_digest, "Tree"),
            (content_digest, "Content"),
            (config_digest, "Config"),
        ):
            _validate_digest(value, label)
        connection = self._db()
        existing = connection.execute(
            "select id from source_snapshot where source_binding_id = ? and revision_ref = ?"
            " and tree_digest = ? and content_digest = ? and config_digest = ?",
            (source_binding_id, revision_ref, tree_digest, content_digest, config_digest),
        ).fetchone()
        if existing is not None:
            snapshot_id = existing["id"]
        else:
            snapshot_id = str(new_uuid7())
            try:
                connection.execute(
                    "insert into source_snapshot(id, source_binding_id, revision_ref, tree_digest,"
                    " content_digest, config_digest, captured_at) values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        source_binding_id,
                        revision_ref,
                        tree_digest,
                        content_digest,
                        config_digest,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationFailed("Source snapshot constraint ihlali") from exc
        return SourceSnapshotRecord(
            snapshot_id,
            source_binding_id,
            revision_ref,
            tree_digest,
            content_digest,
            config_digest,
        )

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
    ) -> OperationalWorkRecord:
        kind = _required_text(kind, "Work kind")
        title = _required_text(title, "Work title")
        if state not in _WORK_STATES:
            raise ValidationFailed("Work state gecersiz")
        payload_document = _work_payload(payload)
        payload_json = canonical_json(payload_document)
        actual_payload_digest = digest(payload_document)
        if payload_digest is None:
            payload_digest = actual_payload_digest
        else:
            _validate_digest(payload_digest, "Work payload")
            if payload is not None and payload_digest != actual_payload_digest:
                raise ValidationFailed("Work payload digest drift")
        if external_number is not None:
            external_number = _required_text(external_number, "Work external number")
        if evidence_digest is not None:
            _validate_digest(evidence_digest, "Work evidence")
        if state == "completed" and evidence_digest is None:
            raise ValidationFailed("Completed Work evidence gerektirir")
        work_id = str(new_uuid7())
        created_at = _now()
        connection = self._db()
        try:
            connection.execute(
                "insert into work_item(id, project_id, kind, title, state, revision,"
                " evidence_digest, created_at, external_number)"
                " values (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    work_id,
                    project_id,
                    kind,
                    title,
                    state,
                    evidence_digest,
                    created_at,
                    external_number,
                ),
            )
            connection.execute(
                "insert into work_revision(id, work_item_id, revision, state, payload_digest,"
                " evidence_digest, created_at, payload_json) values (?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    str(new_uuid7()),
                    work_id,
                    state,
                    payload_digest,
                    evidence_digest,
                    created_at,
                    payload_json,
                ),
            )
            event_digest = digest(
                {
                    "work_item_id": work_id,
                    "revision": 1,
                    "to_state": state,
                    "payload": payload_digest,
                }
            )
            connection.execute(
                "insert into work_event(id, work_item_id, revision, event_kind, from_state,"
                " to_state, event_digest, created_at) values (?, ?, 1, 'created', null, ?, ?, ?)",
                (str(new_uuid7()), work_id, state, event_digest, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Operational Work create constraint ihlali") from exc
        return OperationalWorkRecord(
            work_id,
            project_id,
            kind,
            title,
            state,
            1,
            evidence_digest,
            external_number,
            str(payload_document.get("summary", "")),
            tuple(payload_document.get("acceptance_criteria", ())),
        )

    def transition_work(
        self,
        *,
        work_item_id: str,
        expected_revision: int,
        to_state: str,
        payload_digest: str,
        event_digest: str,
        evidence_digest: str | None = None,
    ) -> OperationalWorkRecord:
        expected_revision = _exact_positive_int(expected_revision, "Expected revision")
        if to_state not in _WORK_STATES:
            raise ValidationFailed("Work target state gecersiz")
        for value, label in ((payload_digest, "Payload"), (event_digest, "Event")):
            _validate_digest(value, label)
        if evidence_digest is not None:
            _validate_digest(evidence_digest, "Evidence")
        if to_state == "completed" and evidence_digest is None:
            raise ValidationFailed("Completed Work evidence gerektirir")
        connection = self._db()
        row = connection.execute(
            "select work_item.id, work_item.project_id, work_item.kind, work_item.title,"
            " work_item.state, work_item.revision, work_item.evidence_digest,"
            " work_item.external_number, work_revision.payload_json"
            " from work_item join work_revision on work_revision.work_item_id = work_item.id"
            " and work_revision.revision = work_item.revision where work_item.id = ?",
            (work_item_id,),
        ).fetchone()
        if row is None:
            raise ValidationFailed("Work bulunamadi")
        if row["revision"] != expected_revision:
            raise ValidationFailed("Work optimistic revision drift")
        if to_state not in _TRANSITIONS[row["state"]]:
            raise ValidationFailed("Work state transition yasak")
        revision = expected_revision + 1
        updated = connection.execute(
            "update work_item set state = ?, revision = ?, evidence_digest = ?"
            " where id = ? and revision = ?",
            (to_state, revision, evidence_digest, work_item_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise ValidationFailed("Work concurrent revision conflict")
        created_at = _now()
        try:
            connection.execute(
                "insert into work_revision(id, work_item_id, revision, state, payload_digest,"
                " evidence_digest, created_at, payload_json) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(new_uuid7()),
                    work_item_id,
                    revision,
                    to_state,
                    payload_digest,
                    evidence_digest,
                    created_at,
                    row["payload_json"],
                ),
            )
            connection.execute(
                "insert into work_event(id, work_item_id, revision, event_kind, from_state,"
                " to_state, event_digest, created_at) values (?, ?, ?, 'transitioned', ?, ?, ?, ?)",
                (
                    str(new_uuid7()),
                    work_item_id,
                    revision,
                    row["state"],
                    to_state,
                    event_digest,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Work revision/event constraint ihlali") from exc
        updated_row = dict(row)
        updated_row["state"] = to_state
        updated_row["revision"] = revision
        updated_row["evidence_digest"] = evidence_digest
        return _row_work(updated_row)

    def create_run(
        self,
        *,
        work_item_id: str,
        config_revision_id: str,
        plan_digest: str,
        budget: dict[str, Any],
        source_snapshot_id: str | None = None,
    ) -> RunRecord:
        _validate_digest(plan_digest, "Run plan")
        budget_json = canonical_json(budget)
        run_id = str(new_uuid7())
        now = _now()
        try:
            self._db().execute(
                "insert into run(id, work_item_id, source_snapshot_id, config_revision_id,"
                " status, budget_json, plan_digest, created_at, updated_at)"
                " values (?, ?, ?, ?, 'planned', ?, ?, ?, ?)",
                (
                    run_id,
                    work_item_id,
                    source_snapshot_id,
                    config_revision_id,
                    budget_json,
                    plan_digest,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Run constraint ihlali") from exc
        return RunRecord(run_id, work_item_id, "planned", plan_digest)

    def add_run_step(
        self,
        *,
        run_id: str,
        step_key: str,
        input_digest: str,
        dependencies: tuple[str, ...] = (),
    ) -> RunStepRecord:
        step_key = assert_portable(step_key)
        _validate_digest(input_digest, "Step input")
        if len(set(dependencies)) != len(dependencies):
            raise ValidationFailed("Run step duplicate dependency")
        step_id = str(new_uuid7())
        now = _now()
        connection = self._db()
        try:
            connection.execute(
                "insert into run_step(id, run_id, step_key, status, input_digest, created_at,"
                " updated_at) values (?, ?, ?, 'pending', ?, ?, ?)",
                (step_id, run_id, step_key, input_digest, now, now),
            )
            for dependency in dependencies:
                connection.execute(
                    "insert into run_step_dependency(run_step_id, depends_on_step_id)"
                    " values (?, ?)",
                    (step_id, dependency),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Run step/dependency constraint ihlali") from exc
        return RunStepRecord(step_id, run_id, step_key, "pending", input_digest, None)

    def record_checkpoint(
        self,
        *,
        run_id: str,
        sequence: int,
        checkpoint_digest: str,
        payload: dict[str, Any],
        source_snapshot_id: str | None = None,
    ) -> CheckpointRecord:
        sequence = _exact_positive_int(sequence, "Checkpoint sequence")
        _validate_digest(checkpoint_digest, "Checkpoint")
        if digest(payload) != checkpoint_digest:
            raise ValidationFailed("Checkpoint digest payload ile eslesmiyor")
        checkpoint_id = str(new_uuid7())
        try:
            self._db().execute(
                "insert into checkpoint(id, run_id, sequence, source_snapshot_id,"
                " checkpoint_digest, payload_json, created_at) values (?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint_id,
                    run_id,
                    sequence,
                    source_snapshot_id,
                    checkpoint_digest,
                    canonical_json(payload),
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Checkpoint constraint ihlali") from exc
        return CheckpointRecord(checkpoint_id, run_id, sequence, checkpoint_digest)

    def get_work(self, work_item_id: str) -> OperationalWorkRecord:
        row = (
            self._db()
            .execute(
                "select work_item.id, work_item.project_id, work_item.kind, work_item.title,"
                " work_item.state, work_item.revision, work_item.evidence_digest,"
                " work_item.external_number, work_revision.payload_json"
                " from work_item join work_revision on work_revision.work_item_id = work_item.id"
                " and work_revision.revision = work_item.revision where work_item.id = ?",
                (work_item_id,),
            )
            .fetchone()
        )
        if row is None:
            raise ValidationFailed("Work bulunamadi")
        return _row_work(row)

    def list_work(self, *, project_id: str | None = None) -> tuple[OperationalWorkRecord, ...]:
        parameters: tuple[str, ...] = () if project_id is None else (project_id,)
        rows = (
            self._db()
            .execute(
                "select work_item.id, work_item.project_id, work_item.kind, work_item.title,"
                " work_item.state, work_item.revision, work_item.evidence_digest,"
                " work_item.external_number, work_revision.payload_json"
                " from work_item join work_revision on work_revision.work_item_id = work_item.id"
                " and work_revision.revision = work_item.revision"
                + ("" if project_id is None else " where work_item.project_id = ?")
                + " order by work_item.created_at, work_item.id",
                parameters,
            )
            .fetchall()
        )
        return tuple(_row_work(row) for row in rows)

    def get_run(self, run_id: str) -> RunRecord:
        row = (
            self._db()
            .execute(
                "select id, work_item_id, status, plan_digest from run where id = ?", (run_id,)
            )
            .fetchone()
        )
        if row is None:
            raise ValidationFailed("Run bulunamadi")
        return RunRecord(row["id"], row["work_item_id"], row["status"], row["plan_digest"])

    def list_checkpoints(self, run_id: str) -> tuple[CheckpointRecord, ...]:
        rows = (
            self._db()
            .execute(
                "select id, run_id, sequence, checkpoint_digest from checkpoint"
                " where run_id = ? order by sequence",
                (run_id,),
            )
            .fetchall()
        )
        return tuple(
            CheckpointRecord(row["id"], row["run_id"], row["sequence"], row["checkpoint_digest"])
            for row in rows
        )

    def record_bootstrap_receipt(
        self,
        *,
        receipt_digest: str,
        plan_digest: str,
        task_digest: str,
        status: str,
    ) -> None:
        for value, label in (
            (receipt_digest, "Receipt"),
            (plan_digest, "Plan"),
            (task_digest, "Task"),
        ):
            _validate_digest(value, label)
        if status not in {"completed", "failed"}:
            raise ValidationFailed("Bootstrap receipt status gecersiz")
        existing = (
            self._db()
            .execute(
                "select plan_digest, task_digest, status from bootstrap_receipt"
                " where receipt_digest = ?",
                (receipt_digest,),
            )
            .fetchone()
        )
        if existing is not None:
            if tuple(existing) != (plan_digest, task_digest, status):
                raise ValidationFailed("Bootstrap receipt replay payload drift")
            return
        self._db().execute(
            "insert into bootstrap_receipt(receipt_digest, plan_digest, task_digest, status,"
            " created_at) values (?, ?, ?, ?, ?)",
            (receipt_digest, plan_digest, task_digest, status, _now()),
        )

    def open_session(
        self,
        *,
        client_id: str,
        device_id: str,
        project_id: str | None = None,
        work_item_id: str | None = None,
    ) -> SessionRecord:
        client_id = assert_portable(client_id)
        device_id = assert_portable(device_id)
        session_id = str(new_uuid7())
        try:
            self._db().execute(
                "insert into session(id, client_id, device_id, project_id, work_item_id, status,"
                " opened_at) values (?, ?, ?, ?, ?, 'open', ?)",
                (session_id, client_id, device_id, project_id, work_item_id, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Session constraint ihlali") from exc
        return SessionRecord(session_id, client_id, device_id, "open")

    def record_session_event(self, *, session_id: str, event_kind: str, event_digest: str) -> None:
        event_kind = _required_text(event_kind, "Session event kind")
        _validate_digest(event_digest, "Session event")
        try:
            self._db().execute(
                "insert into session_event(id, session_id, event_kind, event_digest, created_at)"
                " values (?, ?, ?, ?, ?)",
                (str(new_uuid7()), session_id, event_kind, event_digest, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Session event constraint ihlali") from exc

    def register_model(
        self, *, canonical_id: str, access_name: str, modality: str
    ) -> ModelIdentityRecord:
        canonical_id = assert_portable(canonical_id)
        access_name = _required_text(access_name, "Model access name")
        if modality not in _MODEL_MODALITIES:
            raise ValidationFailed("Model modality gecersiz")
        existing = (
            self._db()
            .execute(
                "select id, access_name, modality from model_identity where canonical_id = ?",
                (canonical_id,),
            )
            .fetchone()
        )
        if existing is not None:
            if existing["access_name"] != access_name or existing["modality"] != modality:
                raise ValidationFailed("Model identity replay payload drift")
            return ModelIdentityRecord(existing["id"], canonical_id, access_name, modality)
        model_id = str(new_uuid7())
        self._db().execute(
            "insert into model_identity(id, canonical_id, access_name, modality, created_at)"
            " values (?, ?, ?, ?, ?)",
            (model_id, canonical_id, access_name, modality, _now()),
        )
        return ModelIdentityRecord(model_id, canonical_id, access_name, modality)

    def observe_model_revision(
        self,
        *,
        model_identity_id: str,
        provider_fingerprint_digest: str,
        observed_revision: str,
    ) -> ModelRevisionRecord:
        _validate_digest(provider_fingerprint_digest, "Provider fingerprint")
        observed_revision = assert_portable(observed_revision)
        existing = (
            self._db()
            .execute(
                "select id from model_revision where model_identity_id = ?"
                " and provider_fingerprint_digest = ? and observed_revision = ?",
                (model_identity_id, provider_fingerprint_digest, observed_revision),
            )
            .fetchone()
        )
        if existing is not None:
            revision_id = existing["id"]
        else:
            revision_id = str(new_uuid7())
            try:
                self._db().execute(
                    "insert into model_revision(id, model_identity_id,"
                    " provider_fingerprint_digest, observed_revision, observed_at)"
                    " values (?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        model_identity_id,
                        provider_fingerprint_digest,
                        observed_revision,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationFailed("Model revision constraint ihlali") from exc
        return ModelRevisionRecord(
            revision_id,
            model_identity_id,
            provider_fingerprint_digest,
            observed_revision,
        )

    def record_model_availability(
        self,
        *,
        model_revision_id: str,
        device_scope: str,
        client_scope: str,
        provider_scope: str,
        available: bool,
    ) -> None:
        if type(available) is not bool:
            raise ValidationFailed("Model availability bool olmali")
        scopes = tuple(
            assert_portable(value) for value in (device_scope, client_scope, provider_scope)
        )
        try:
            self._db().execute(
                "insert into model_availability(id, model_revision_id, device_scope,"
                " client_scope, provider_scope, available, observed_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(new_uuid7()),
                    model_revision_id,
                    *scopes,
                    int(available),
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Model availability constraint ihlali") from exc

    def record_model_health(
        self,
        *,
        model_revision_id: str,
        status: str,
        evidence_digest: str,
        latency_ms: int | None,
    ) -> None:
        if status not in _MODEL_HEALTH:
            raise ValidationFailed("Model health status gecersiz")
        _validate_digest(evidence_digest, "Model health evidence")
        if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
            raise ValidationFailed("Model health latency non-negative integer olmali")
        try:
            self._db().execute(
                "insert into model_health_observation(id, model_revision_id, status,"
                " evidence_digest, latency_ms, observed_at) values (?, ?, ?, ?, ?, ?)",
                (
                    str(new_uuid7()),
                    model_revision_id,
                    status,
                    evidence_digest,
                    latency_ms,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("Model health constraint ihlali") from exc

    def register_artifact(
        self,
        *,
        artifact_digest: str,
        media_type: str,
        size_bytes: int,
        classification: str,
    ) -> ArtifactRefRecord:
        _validate_digest(artifact_digest, "Artifact")
        media_type = _required_text(media_type, "Artifact media type").lower()
        if "/" not in media_type:
            raise ValidationFailed("Artifact media type gecersiz")
        if type(size_bytes) is not int or size_bytes < 0 or size_bytes > 64 * 1024 * 1024:
            raise ValidationFailed("Artifact size 0..64 MiB olmali")
        try:
            typed_classification = KnowledgeClassification(classification)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Artifact classification gecersiz") from exc
        if typed_classification is KnowledgeClassification.SECRET:
            raise ValidationFailed("Secret artifact normal CAS ref olamaz")
        connection = self._db()
        existing = connection.execute(
            "select media_type,size_bytes,classification from artifact_ref where digest=?",
            (artifact_digest,),
        ).fetchone()
        expected = (media_type, size_bytes, typed_classification.value)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValidationFailed("Artifact ref replay payload drift")
        else:
            try:
                connection.execute(
                    "insert into artifact_ref(digest,media_type,size_bytes,classification,"
                    "created_at) values(?,?,?,?,?)",
                    (artifact_digest, *expected, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationFailed("Artifact ref constraint ihlali") from exc
        return ArtifactRefRecord(artifact_digest, *expected)

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
    ) -> KnowledgeNoteRecord:
        realm_id = _canonical_uuid(realm_id, "Knowledge realm")
        owner_scope = validate_owner_scope(owner_scope)
        connection = self._db()
        project_slug: str | None = None
        realm_binding_missing = False
        if owner_scope == "global-user":
            if project_id is not None:
                raise ValidationFailed("Global knowledge note project id tasiyamaz")
        else:
            project_id = _canonical_uuid(project_id, "Knowledge project")
            project = connection.execute(
                "select slug from project where id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise ValidationFailed("Knowledge project bulunamadi")
            project_slug = str(project["slug"])
            owner_kind, owner_id = owner_scope.split(":", 1)
            owner_matches_project = False
            if owner_kind == "project":
                owner_matches_project = owner_id == project_id
            elif owner_kind == "work":
                owner_matches_project = (
                    connection.execute(
                        "select 1 from work_item where id=? and project_id=?",
                        (owner_id, project_id),
                    ).fetchone()
                    is not None
                )
            elif owner_kind == "run":
                owner_matches_project = (
                    connection.execute(
                        "select 1 from run r join work_item w on w.id=r.work_item_id"
                        " where r.id=? and w.project_id=?",
                        (owner_id, project_id),
                    ).fetchone()
                    is not None
                )
            elif owner_kind == "session":
                owner_matches_project = (
                    connection.execute(
                        "select 1 from session s left join work_item w on w.id=s.work_item_id"
                        " where s.id=? and (s.project_id is not null or w.project_id is not null)"
                        " and (s.project_id is null or s.project_id=?)"
                        " and (w.project_id is null or w.project_id=?)",
                        (owner_id, project_id, project_id),
                    ).fetchone()
                    is not None
                )
            if not owner_matches_project:
                raise ValidationFailed("Knowledge owner scope exact project binding ister")
            binding = connection.execute(
                "select realm_id from project_knowledge_realm where project_id=?",
                (project_id,),
            ).fetchone()
            if binding is None:
                realm_binding_missing = True
            elif binding["realm_id"] != realm_id:
                raise ValidationFailed("Knowledge project realm binding drift")
        portable_ref = validate_note_ownership_path(
            owner_scope, portable_ref, authorship, project_slug=project_slug
        )
        if note_kind not in _KNOWLEDGE_NOTE_KINDS:
            raise ValidationFailed("Knowledge note kind gecersiz")
        if authorship not in {"user", "generated"}:
            raise ValidationFailed("Knowledge authorship gecersiz")
        try:
            typed_classification = KnowledgeClassification(classification)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Knowledge classification gecersiz") from exc
        if typed_classification is KnowledgeClassification.SECRET:
            raise ValidationFailed("Secret note normal knowledge file ref olamaz")
        _validate_digest(content_digest, "Knowledge content")
        if state not in {"inbox", "active"}:
            raise ValidationFailed("Knowledge note ilk state inbox/active olmali")
        rows = connection.execute(
            "select * from knowledge_note where portable_ref=?"
            " or (realm_id=? and owner_scope=? and content_digest=?) order by id",
            (portable_ref, realm_id, owner_scope, content_digest),
        ).fetchall()
        expected = (
            realm_id,
            project_id,
            project_slug,
            owner_scope,
            portable_ref,
            note_kind,
            authorship,
            typed_classification.value,
            content_digest,
            state,
        )
        if rows:
            if (
                len(rows) != 1
                or tuple(
                    rows[0][key]
                    for key in (
                        "realm_id",
                        "project_id",
                        "project_slug",
                        "owner_scope",
                        "portable_ref",
                        "note_kind",
                        "authorship",
                        "classification",
                        "content_digest",
                        "state",
                    )
                )
                != expected
            ):
                raise ValidationFailed("Knowledge note replay/authority drift")
            note_id = str(rows[0]["id"])
        else:
            note_id = str(new_uuid7())
            now = _now()
            connection.execute("savepoint register_knowledge_note")
            try:
                if realm_binding_missing:
                    connection.execute(
                        "insert into project_knowledge_realm(project_id,realm_id,created_at)"
                        " values(?,?,?)",
                        (project_id, realm_id, now),
                    )
                connection.execute(
                    "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                    "portable_ref,note_kind,authorship,classification,content_digest,state,"
                    "materialized,materialization_evidence_digest,archived_ref,created_at,"
                    "updated_at) values(?,?,?,?,?,?,?,?,?,?,?,0,null,null,?,?)",
                    (note_id, *expected, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.execute("rollback to register_knowledge_note")
                connection.execute("release register_knowledge_note")
                raise ValidationFailed("Knowledge note constraint ihlali") from exc
            except BaseException:
                connection.execute("rollback to register_knowledge_note")
                connection.execute("release register_knowledge_note")
                raise
            else:
                connection.execute("release register_knowledge_note")
        return KnowledgeNoteRecord(
            note_id,
            owner_scope,
            portable_ref,
            note_kind,
            authorship,
            typed_classification.value,
            content_digest,
            state,
            realm_id,
            project_id,
            project_slug,
            False,
        )

    def confirm_knowledge_note(
        self,
        *,
        note_id: str,
        expected_content_digest: str,
        evidence_digest: str,
    ) -> KnowledgeNoteRecord:
        _validate_digest(expected_content_digest, "Knowledge materialization content")
        _validate_digest(evidence_digest, "Knowledge materialization evidence")
        connection = self._db()
        row = connection.execute("select * from knowledge_note where id=?", (note_id,)).fetchone()
        if row is None:
            raise ValidationFailed("Knowledge note bulunamadi")
        if row["content_digest"] != expected_content_digest:
            raise ValidationFailed("Knowledge materialization content drift")
        if bool(row["materialized"]):
            if row["materialization_evidence_digest"] != evidence_digest:
                raise ValidationFailed("Knowledge materialization evidence replay drift")
        else:
            connection.execute(
                "update knowledge_note set materialized=1,materialization_evidence_digest=?,"
                "updated_at=? where id=?",
                (evidence_digest, _now(), note_id),
            )
        return KnowledgeNoteRecord(
            str(row["id"]),
            str(row["owner_scope"]),
            str(row["portable_ref"]),
            str(row["note_kind"]),
            str(row["authorship"]),
            str(row["classification"]),
            str(row["content_digest"]),
            str(row["state"]),
            str(row["realm_id"]),
            str(row["project_id"]) if row["project_id"] is not None else None,
            str(row["project_slug"]) if row["project_slug"] is not None else None,
            True,
            str(row["archived_ref"]) if row["archived_ref"] is not None else None,
        )

    def relate_knowledge_notes(
        self,
        *,
        from_note_id: str,
        to_note_id: str,
        relation_kind: str,
        source_digest: str,
        verified: bool,
    ) -> KnowledgeRelationRecord:
        if from_note_id == to_note_id:
            raise ValidationFailed("Knowledge relation self olamaz")
        relation_kind = validate_portable_relative(relation_kind, "Relation kind")
        _validate_digest(source_digest, "Knowledge relation source")
        if type(verified) is not bool or not verified:
            raise ValidationFailed("Canonical knowledge relation verified source ister")
        connection = self._db()
        endpoints = connection.execute(
            "select id,state,realm_id,materialized from knowledge_note"
            " where id in (?,?) order by id",
            (from_note_id, to_note_id),
        ).fetchall()
        if (
            len(endpoints) != 2
            or any(row["state"] == "archived" for row in endpoints)
            or any(not bool(row["materialized"]) for row in endpoints)
            or len({row["realm_id"] for row in endpoints}) != 1
        ):
            raise ValidationFailed("Knowledge relation iki active same-realm note ister")
        existing = connection.execute(
            "select id,source_digest,verified from knowledge_relation"
            " where from_note_id=? and to_note_id=? and relation_kind=?",
            (from_note_id, to_note_id, relation_kind),
        ).fetchone()
        if existing is not None:
            if (existing["source_digest"], bool(existing["verified"])) != (
                source_digest,
                True,
            ):
                raise ValidationFailed("Knowledge relation replay payload drift")
            relation_id = str(existing["id"])
        else:
            relation_id = str(new_uuid7())
            try:
                connection.execute(
                    "insert into knowledge_relation(id,from_note_id,to_note_id,relation_kind,"
                    "source_digest,verified,created_at) values(?,?,?,?,?,1,?)",
                    (
                        relation_id,
                        from_note_id,
                        to_note_id,
                        relation_kind,
                        source_digest,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationFailed("Knowledge relation constraint ihlali") from exc
        return KnowledgeRelationRecord(
            relation_id,
            from_note_id,
            to_note_id,
            relation_kind,
            source_digest,
            True,
        )

    def archive_knowledge_note(
        self,
        *,
        note_id: str,
        expected_content_digest: str,
        archived_ref: str,
    ) -> KnowledgeNoteRecord:
        _validate_digest(expected_content_digest, "Knowledge archive content")
        archived_ref = validate_portable_relative(archived_ref, "Knowledge archive ref")
        connection = self._db()
        row = connection.execute("select * from knowledge_note where id=?", (note_id,)).fetchone()
        if row is None:
            raise ValidationFailed("Knowledge note bulunamadi")
        if row["content_digest"] != expected_content_digest:
            raise ValidationFailed("Knowledge archive content drift")
        if not bool(row["materialized"]):
            raise ValidationFailed("Knowledge archive materialized note ister")
        expected_prefix = f"archive/{str(row['owner_scope']).replace(':', '/')}/"
        if not archived_ref.startswith(expected_prefix):
            raise ValidationFailed("Knowledge archive owner scope ref drift")
        if row["state"] == "archived":
            if row["archived_ref"] != archived_ref:
                raise ValidationFailed("Knowledge archive replay ref drift")
        elif row["state"] in {"inbox", "active"}:
            connection.execute(
                "update knowledge_note set state='archived',archived_ref=?,updated_at=? where id=?",
                (archived_ref, _now(), note_id),
            )
        else:
            raise ValidationFailed("Knowledge note archive state gecersiz")
        return KnowledgeNoteRecord(
            str(row["id"]),
            str(row["owner_scope"]),
            str(row["portable_ref"]),
            str(row["note_kind"]),
            str(row["authorship"]),
            str(row["classification"]),
            str(row["content_digest"]),
            "archived",
            str(row["realm_id"]),
            str(row["project_id"]) if row["project_id"] is not None else None,
            str(row["project_slug"]) if row["project_slug"] is not None else None,
            True,
            archived_ref,
        )

    def list_knowledge_notes(
        self,
        *,
        project_id: str | None = None,
        owner_scope: str | None = None,
        note_kind: str | None = None,
        state: str | None = "active",
        limit: int = 100,
    ) -> tuple[KnowledgeNoteRecord, ...]:
        if project_id is not None:
            project_id = _canonical_uuid(project_id, "Knowledge project")
        if owner_scope is not None:
            owner_scope = validate_owner_scope(owner_scope)
        if note_kind is not None and note_kind not in _KNOWLEDGE_NOTE_KINDS:
            raise ValidationFailed("Knowledge note kind gecersiz")
        if state is not None and state not in {"inbox", "active", "archived"}:
            raise ValidationFailed("Knowledge note state gecersiz")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationFailed("Knowledge list limit 1..1000 araliginda olmali")
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("project_id", project_id),
            ("owner_scope", owner_scope),
            ("note_kind", note_kind),
            ("state", state),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = "" if not clauses else " where " + " and ".join(clauses)
        rows = (
            self._db()
            .execute(
                "select * from knowledge_note"
                + where
                + " order by updated_at desc,id desc limit ?",
                (*values, limit),
            )
            .fetchall()
        )
        return tuple(_row_knowledge_note(row) for row in rows)

    def get_knowledge_note(self, reference: str) -> KnowledgeNoteRecord:
        reference = _required_text(reference, "Knowledge note reference")
        if len(reference.encode("utf-8")) > 1024:
            raise ValidationFailed("Knowledge note reference bounded sinirini asiyor")
        rows = (
            self._db()
            .execute(
                "select * from knowledge_note where id=? or portable_ref=? order by id",
                (reference, reference),
            )
            .fetchall()
        )
        if len(rows) != 1:
            raise ValidationFailed("Knowledge note bulunamadi veya belirsiz")
        return _row_knowledge_note(rows[0])
