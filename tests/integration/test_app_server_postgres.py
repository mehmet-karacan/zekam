from __future__ import annotations

import asyncio
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

from zekam.application.app_server import AppServerConnection
from zekam.domain.app_server_protocol import (
    AppNotification,
    ConnectionPhase,
    ProtocolErrorCode,
    ProtocolFault,
    schema_bundle_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientKind, ClientLifecycleEvent
from zekam.domain.errors import ConcurrencyConflict
from zekam.infrastructure.postgres.app_server_repository import (
    PostgresAppNotificationRepository,
)
from zekam.infrastructure.postgres.client_lifecycle_repository import ClientLifecycleRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.interfaces.api.app_server import install_app_server_routes

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 25, 8, 0, tzinfo=dt.UTC)


class TransportSocket:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.headers = {"sec-websocket-protocol": "zekam.app-server.v1"}
        self.sent: list[dict[str, Any]] = []

    async def accept(self, *, subprotocol: str) -> None:
        assert subprotocol == "zekam.app-server.v1"

    async def close(self, *, code: int, reason: str) -> None:
        assert code in {1000, 1011}

    async def receive(self) -> dict[str, Any]:
        if not self.frames:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "text": json.dumps(self.frames.pop(0))}

    async def send_json(self, document: dict[str, Any]) -> None:
        json.dumps(document)
        self.sent.append(document)


def _initialize(cursor: int | None) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "client_id": "opencode",
            "client_version": "1.0.0",
            "protocol_version": "1.0",
            "schema_bundle_digest": schema_bundle_digest(),
            "capabilities": ["notifications", "replay", "read-status"],
            "experimental_methods": [],
            "replay_cursor": cursor,
        },
    }


def test_durable_notification_replay_dedupe_and_connection_resume(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = PostgresAppNotificationRepository(connection, realm.id)
    first_id, second_id = uuid4(), uuid4()
    first, created = repository.append(
        event_id=first_id,
        event_type="work.updated",
        payload={"work_ref": "ZK-P1-013", "state": "active"},
        occurred_at=NOW,
    )
    assert created and first.sequence == 1
    replay, replay_created = repository.append(
        event_id=first_id,
        event_type="work.updated",
        payload={"work_ref": "ZK-P1-013", "state": "active"},
        occurred_at=NOW + dt.timedelta(seconds=1),
    )
    assert not replay_created and replay.event_digest == first.event_digest
    with pytest.raises(ConcurrencyConflict, match="payload drift"):
        repository.append(
            event_id=first_id,
            event_type="work.updated",
            payload={"work_ref": "forged"},
            occurred_at=NOW,
        )
    second, created = repository.append(
        event_id=second_id,
        event_type="work.updated",
        payload={"work_ref": "ZK-P1-013", "state": "completed"},
        occurred_at=NOW + dt.timedelta(seconds=2),
    )
    assert created and second.sequence == 2
    assert second.previous_digest == first.event_digest
    assert repository.head_sequence() == 2
    assert repository.cursor_exists(0)
    assert repository.cursor_exists(2)
    assert not repository.cursor_exists(3)
    stale = AppServerConnection(repository)
    stale.handle(_initialize(cursor=3))
    with pytest.raises(ProtocolFault) as cursor_fault:
        stale.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    assert cursor_fault.value.code is ProtocolErrorCode.CURSOR_EXPIRED
    assert stale.phase is ConnectionPhase.INITIALIZE_ACKED
    assert [event.event_id for event in repository.replay(after_sequence=0, limit=10)] == [
        first_id,
        second_id,
    ]

    app = AppServerConnection(repository)
    app.handle(_initialize(cursor=1))
    notifications = app.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    assert [item["params"]["event_id"] for item in notifications] == [str(second_id)]
    assert app.replay_cursor == 2

    project_id, work_item_id = uuid4(), uuid4()
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into projects.project"
            "(id,realm_id,slug,display_name,status,revision,created_at,updated_at)"
            " values(%s,%s,'app-server-test','App Server Test','active',1,%s,%s)",
            (project_id, realm.id, NOW, NOW),
        )
        cursor.execute(
            "insert into work.work_item"
            "(id,realm_id,project_id,external_number,type,state,title,summary,revision,"
            "acceptance_criteria,acceptance_evidence,record_digest,created_at,updated_at)"
            " values(%s,%s,%s,'P1-013','task','active','App Server v1','',1,"
            "'[]'::jsonb,'[]'::jsonb,%s,%s,%s)",
            (work_item_id, realm.id, project_id, digest("app-server-work"), NOW, NOW),
        )
    assert repository.read_project(project_id)["slug"] == "app-server-test"
    assert repository.read_work(work_item_id)["external_number"] == "P1-013"
    assert repository.read_project(uuid4()) is None
    assert repository.read_session("missing-session", limit=10) is None
    published = repository.replay(after_sequence=2, limit=10)
    assert len(published) == 1
    assert published[0].event_type == "work.item.created"
    assert published[0].payload["work_item_id"] == str(work_item_id)
    assert published[0].previous_digest == second.event_digest
    assert isinstance(published[0].occurred_at, str)

    lifecycle = ClientLifecycleEvent(
        client_id="internal-app-server",
        client_kind=ClientKind.INTERNAL,
        session_id="session-app-server",
        sequence=1,
        previous_digest=None,
        event_type="turn.item.completed",
        payload_digest=digest({"item": "bounded"}),
        occurred_at=NOW + dt.timedelta(seconds=4),
    )
    ClientLifecycleRepository(connection, realm.id).ingest(
        lifecycle.as_dict(),
        client_instance_id="internal-app-server",
        now=NOW + dt.timedelta(seconds=5),
    )
    session_event = repository.replay(after_sequence=3, limit=10)
    assert len(session_event) == 1
    assert session_event[0].event_type == "session.item.observed"
    assert session_event[0].payload["event_type"] == "turn.item.completed"
    assert session_event[0].previous_digest == published[0].event_digest

    resumed = AppServerConnection(repository)
    resumed.handle(_initialize(cursor=3))
    produced = resumed.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    assert produced[0]["params"]["event_type"] == "session.item.observed"
    assert produced[0]["params"]["previous_digest"] == published[0].event_digest

    transport_app = FastAPI()
    install_app_server_routes(transport_app, store_factory=lambda: nullcontext(repository))
    socket_route = next(
        route
        for route in transport_app.routes
        if getattr(route, "path", "") == "/api/app-server/v1"
    )
    socket = TransportSocket(
        [
            _initialize(cursor=2),
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "project/read",
                "params": {"project_id": str(project_id)},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "work/read",
                "params": {"work_item_id": str(work_item_id)},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/read",
                "params": {"session_id": "missing-session", "limit": 10},
            },
        ]
    )
    asyncio.run(socket_route.endpoint(socket))
    assert [item["params"]["event_type"] for item in socket.sent[1:3]] == [
        "work.item.created",
        "session.item.observed",
    ]
    assert [item["result"]["kind"] for item in socket.sent[3:]] == [
        "project",
        "work",
        "session",
    ]

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("savepoint forged_notification")
        with pytest.raises(Exception, match=r"head mismatch|canonical body mismatch"):
            cursor.execute(
                "insert into app_server.notification_event"
                "(id,realm_id,sequence,previous_digest,event_type,payload,payload_digest,"
                "event_body,event_digest,occurred_at,grants_authority)"
                " select %s,realm_id,3,event_digest,'work.updated',payload,%s,event_body,"
                "%s,%s,false"
                " from app_server.notification_event where realm_id=%s and id=%s",
                (
                    uuid4(),
                    digest("wrong-payload"),
                    digest("wrong-event"),
                    NOW + dt.timedelta(seconds=3),
                    realm.id,
                    second_id,
                ),
            )
        cursor.execute("rollback to savepoint forged_notification")


@pytest.mark.concurrency
def test_concurrent_notification_replay_is_singleton(
    realm_session: tuple[Any, Any], migrated_database: Any
) -> None:
    realm, _ = realm_session
    event_id = uuid4()

    def publish() -> tuple[AppNotification, bool]:
        with connect(migrated_database) as worker:
            configure_session(worker, realm_id=realm.id)
            return PostgresAppNotificationRepository(worker, realm.id).append(
                event_id=event_id,
                event_type="work.updated",
                payload={"work_ref": "P1-013", "state": "verification"},
                occurred_at=NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: publish(), range(2)))
    assert sorted(created for _, created in outcomes) == [False, True]
    first, second = outcomes[0][0], outcomes[1][0]
    assert first.event_digest == second.event_digest
    assert first.sequence == second.sequence == 1
