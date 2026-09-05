"""PostgreSQL durable notification replay store for App Server v1."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.app_server_protocol import AppNotification
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation


@dataclass(frozen=True, slots=True)
class PostgresAppNotificationRepository:
    connection: Any
    realm_id: UUID

    def append(
        self,
        *,
        event_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: dt.datetime,
    ) -> tuple[AppNotification, bool]:
        if occurred_at.tzinfo is None:
            raise PolicyViolation("App notification timezone-aware olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            try:
                cursor.execute(
                    "select app_server.publish_notification(%s,%s,%s,%s::jsonb,%s)",
                    (self.realm_id, event_id, event_type, canonical_json(payload), occurred_at),
                )
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "40001":
                    raise ConcurrencyConflict(
                        "App notification event_id replay payload drift"
                    ) from exc
                raise
            created = bool(cursor.fetchone()[0])
            cursor.execute(
                "select sequence,previous_digest,event_type,payload,event_body,event_digest,"
                "occurred_at from app_server.notification_event where realm_id=%s and id=%s",
                (self.realm_id, event_id),
            )
            row = cursor.fetchone()
        if row is None:  # pragma: no cover - SQL function contract guard
            raise ConcurrencyConflict("App notification publish sonucu bulunamadi")
        return self._row(row), created

    def replay(self, *, after_sequence: int, limit: int) -> tuple[AppNotification, ...]:
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise PolicyViolation("App notification replay cursor/limit gecersiz")
        with self.connection.cursor() as cursor:
            expected_previous: str | None = None
            if after_sequence:
                cursor.execute(
                    "select event_digest from app_server.notification_event"
                    " where realm_id=%s and sequence=%s",
                    (self.realm_id, after_sequence),
                )
                previous = cursor.fetchone()
                if previous is None:
                    raise ConcurrencyConflict("App notification replay cursor zincirde yok")
                expected_previous = str(previous[0])
            cursor.execute(
                "select sequence,previous_digest,event_type,payload,event_body,event_digest,"
                "occurred_at"
                " from app_server.notification_event where realm_id=%s and sequence>%s"
                " order by sequence,id limit %s",
                (self.realm_id, after_sequence, limit),
            )
            events = tuple(self._row(row) for row in cursor.fetchall())
        expected_sequence = after_sequence + 1
        for event in events:
            if event.sequence != expected_sequence or event.previous_digest != expected_previous:
                raise ConcurrencyConflict("App notification replay digest zinciri bozuk")
            expected_sequence += 1
            expected_previous = event.event_digest
        return events

    def head_sequence(self) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select head_sequence from app_server.notification_stream where realm_id=%s",
                (self.realm_id,),
            )
            row = cursor.fetchone()
        return 0 if row is None else int(row[0])

    def cursor_exists(self, sequence: int) -> bool:
        if sequence == 0:
            return True
        if sequence < 0:
            return False
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select exists(select 1 from app_server.notification_event"
                " where realm_id=%s and sequence=%s)",
                (self.realm_id, sequence),
            )
            return bool(cursor.fetchone()[0])

    def read_project(self, project_id: UUID) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,slug,display_name,status,revision,created_at,updated_at"
                " from projects.project where realm_id=%s and id=%s",
                (self.realm_id, project_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "slug": row[1],
            "display_name": row[2],
            "status": row[3],
            "revision": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }

    def read_work(self, work_item_id: UUID) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,project_id,external_number,type,state,title,summary,revision,"
                "acceptance_criteria,acceptance_evidence,record_digest,created_at,updated_at"
                " from work.work_item where realm_id=%s and id=%s",
                (self.realm_id, work_item_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "project_id": str(row[1]),
            "external_number": row[2],
            "type": row[3],
            "state": row[4],
            "title": row[5],
            "summary": row[6],
            "revision": row[7],
            "acceptance_criteria": row[8],
            "acceptance_evidence": row[9],
            "record_digest": row[10],
            "created_at": row[11],
            "updated_at": row[12],
        }

    def read_session(self, session_id: str, *, limit: int) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,project_id,work_item_id,client_id,session_id,source_revision,state,"
                "created_at,started_at,terminal_at,run_digest"
                " from runtime.execution_run where realm_id=%s and session_id=%s"
                " order by created_at desc,id desc limit %s",
                (self.realm_id, session_id, limit),
            )
            rows = cursor.fetchall()
        if not rows:
            return None
        return {
            "session_id": session_id,
            "runs": [
                {
                    "id": str(row[0]),
                    "project_id": str(row[1]),
                    "work_item_id": str(row[2]),
                    "client_id": row[3],
                    "session_id": row[4],
                    "source_revision": row[5],
                    "state": row[6],
                    "created_at": row[7],
                    "started_at": row[8],
                    "terminal_at": row[9],
                    "run_digest": row[10],
                    "grants_authority": False,
                }
                for row in rows
            ],
            "bounded_limit": limit,
            "grants_authority": False,
        }

    @classmethod
    def _row(cls, row: Any) -> AppNotification:
        body = dict(row[4])
        return AppNotification(
            event_id=UUID(str(body["event_id"])),
            sequence=int(row[0]),
            previous_digest=None if row[1] is None else str(row[1]),
            event_type=str(row[2]),
            payload=dict(row[3]),
            occurred_at=str(body["occurred_at"]),
            event_digest=str(row[5]),
        )
