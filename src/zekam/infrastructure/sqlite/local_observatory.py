"""Read-only SQLite projections for the local observatory and App Server."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from zekam.application.composition import ApplicationContext
from zekam.application.observatory import RuntimeProjection, unavailable_runtime_projection
from zekam.domain.app_server_protocol import AppNotification
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.domain.observability import REQUIRED_TILES, ProjectionTile
from zekam.infrastructure.local_core_services import LocalCoreServices


def _read_only(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys=on")
    connection.execute("pragma query_only=on")
    return connection


class SQLiteLocalProjectionStore:
    """One bounded read transaction over the canonical operational store."""

    def __init__(self, services: LocalCoreServices, realm_id: UUID) -> None:
        self._services = services
        self._realm_id = str(realm_id)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        if not self._services.status()["all_ready"]:
            raise RuntimeError("Local core projection is not ready")
        self._connection = _read_only(self._services.operational_path)
        self._connection.execute("begin")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._connection is not None:
            self._connection.rollback()
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Local projection transaction is not open")
        return self._connection

    @staticmethod
    def _notification(row: sqlite3.Row, sequence: int, previous: str | None) -> AppNotification:
        payload = {
            "outbox_id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "event_kind": str(row["event_kind"]),
            "payload_digest": str(row["payload_digest"]),
        }
        body = {
            "schema": "zekam-app-notification/v1",
            "event_id": str(row["id"]),
            "sequence": sequence,
            "previous_digest": previous,
            "event_type": str(row["event_kind"]),
            "payload": payload,
            "occurred_at": str(row["created_at"]),
            "grants_authority": False,
        }
        return AppNotification(
            event_id=UUID(str(row["id"])),
            sequence=sequence,
            event_type=str(row["event_kind"]),
            payload=payload,
            occurred_at=str(row["created_at"]),
            previous_digest=previous,
            event_digest=digest(body),
        )

    def replay(self, *, after_sequence: int, limit: int) -> tuple[AppNotification, ...]:
        if type(after_sequence) is not int or type(limit) is not int or after_sequence < 0:
            raise ValueError("Local replay cursor invalid")
        if not 1 <= limit <= 1000:
            raise ValueError("Local replay limit invalid")
        if after_sequence + limit > 100_000:
            raise ValueError("Local replay bounded sequence invalid")
        rows = self._db.execute(
            "select id,job_id,event_kind,payload_digest,created_at from local_outbox "
            "order by rowid limit ?",
            (after_sequence + limit,),
        ).fetchall()
        notifications: list[AppNotification] = []
        previous: str | None = None
        for sequence, row in enumerate(rows, start=1):
            notification = self._notification(row, sequence, previous)
            previous = notification.event_digest
            if sequence > after_sequence:
                notifications.append(notification)
        return tuple(notifications)

    def head_sequence(self) -> int:
        return int(self._db.execute("select count(*) from local_outbox").fetchone()[0])

    def cursor_exists(self, sequence: int) -> bool:
        return type(sequence) is int and 0 <= sequence <= self.head_sequence()

    def read_project(self, project_id: UUID) -> dict[str, Any] | None:
        row = self._db.execute(
            "select p.id,p.slug,p.display_name,p.status,p.revision from project p "
            "join project_knowledge_realm r on r.project_id=p.id "
            "where p.id=? and r.realm_id=?",
            (str(project_id), self._realm_id),
        ).fetchone()
        return None if row is None else dict(row)

    def read_work(self, work_item_id: UUID) -> dict[str, Any] | None:
        row = self._db.execute(
            "select w.id,w.project_id,w.kind,w.title,w.state,w.revision,w.evidence_digest "
            "from work_item w join project_knowledge_realm r on r.project_id=w.project_id "
            "where w.id=? and r.realm_id=?",
            (str(work_item_id), self._realm_id),
        ).fetchone()
        return None if row is None else dict(row)

    def read_session(self, session_id: str, *, limit: int) -> dict[str, Any] | None:
        if type(session_id) is not str or not session_id or not 1 <= limit <= 100:
            raise ValueError("Local session projection request invalid")
        row = self._db.execute(
            "select s.id,s.client_id,s.device_id,s.project_id,s.work_item_id,s.status,"
            "s.opened_at,s.closed_at,s.close_receipt_digest from session s "
            "join project_knowledge_realm r on r.project_id=s.project_id "
            "where s.id=? and r.realm_id=?",
            (session_id, self._realm_id),
        ).fetchone()
        if row is None:
            return None
        events = self._db.execute(
            "select id,event_kind,event_digest,created_at from session_event "
            "where session_id=? order by created_at desc,id desc limit ?",
            (session_id, limit),
        ).fetchall()
        return {**dict(row), "events": [dict(item) for item in events]}


class SQLiteRuntimeProjectionReader:
    """Sanitized counts from all composed local stores."""

    def __init__(self, context: ApplicationContext, realm_id: UUID) -> None:
        self._context = context
        self._realm_id = str(realm_id)

    def read(self) -> RuntimeProjection:
        try:
            return self._read_validated()
        except (OSError, sqlite3.Error, ZekamError):
            return unavailable_runtime_projection("local-core-unavailable")

    def _read_validated(self) -> RuntimeProjection:
        services = LocalCoreServices.from_context(self._context)
        status = services.status()
        if not status["all_ready"]:
            return unavailable_runtime_projection("local-core-unavailable")
        with _read_only(services.operational_path) as operational:
            values = {
                "work": int(
                    operational.execute(
                        "select count(*) from work_item w join project_knowledge_realm r "
                        "on r.project_id=w.project_id where r.realm_id=? "
                        "and w.state not in ('completed','cancelled','archived')",
                        (self._realm_id,),
                    ).fetchone()[0]
                ),
                "run": int(
                    operational.execute(
                        "select count(*) from run n join work_item w on w.id=n.work_item_id "
                        "join project_knowledge_realm r on r.project_id=w.project_id "
                        "where r.realm_id=? and n.status not in ('succeeded','failed','cancelled')",
                        (self._realm_id,),
                    ).fetchone()[0]
                ),
                "knowledge": int(
                    operational.execute(
                        "select count(*) from knowledge_note where realm_id=? and state='active'",
                        (self._realm_id,),
                    ).fetchone()[0]
                ),
                "scheduler": int(
                    operational.execute("select count(*) from local_scheduler_slot").fetchone()[0]
                ),
            }
        with _read_only(services.registry.path) as registry:
            values["model"] = int(
                registry.execute("select count(*) from discovery_observation").fetchone()[0]
            )
        with _read_only(services.learning.path) as learning:
            values["memory"] = int(
                learning.execute("select count(*) from memory_head").fetchone()[0]
            )
        generated_at = dt.datetime.now(dt.UTC)
        tiles = tuple(
            ProjectionTile(
                key=key,
                title=key.title(),
                value=values[key],
                drill_down=f"local:{key}",
                detail="canonical local SQLite projection",
            )
            for key in REQUIRED_TILES
        )
        return RuntimeProjection(
            generated_at=generated_at,
            tiles=tiles,
            source_digest=digest({"realm_id": self._realm_id, "values": values}),
            available=True,
            detail="local-core-sqlite",
        )
