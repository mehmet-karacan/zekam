from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import Any, cast

from fastapi import FastAPI
from fastapi.routing import APIWebSocketRoute

from zekam.application.app_server import InMemoryNotificationStore
from zekam.domain.app_server_protocol import schema_bundle_digest
from zekam.interfaces.api.app_server import install_app_server_routes


class FakeWebSocket:
    def __init__(self, frames: list[Any], protocols: str) -> None:
        self.frames = frames
        self.headers = {"sec-websocket-protocol": protocols}
        self.accepted: str | None = None
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict[str, Any]] = []

    async def accept(self, *, subprotocol: str) -> None:
        self.accepted = subprotocol

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def receive(self) -> dict[str, Any]:
        if not self.frames:
            return {"type": "websocket.disconnect", "code": 1000}
        frame = self.frames.pop(0)
        if isinstance(frame, bytes):
            return {"type": "websocket.receive", "bytes": frame}
        return {
            "type": "websocket.receive",
            "text": frame if isinstance(frame, str) else json.dumps(frame),
        }

    async def send_json(self, document: dict[str, Any]) -> None:
        json.dumps(document)
        self.sent.append(document)


def test_versioned_schema_and_websocket_routes_are_installed() -> None:
    app = FastAPI()
    store = InMemoryNotificationStore()
    install_app_server_routes(app, store_factory=lambda: nullcontext(store))

    routes = {(getattr(route, "path", ""), type(route).__name__) for route in app.routes}
    assert ("/api/app-server/v1/schema", "APIRoute") in routes
    assert ("/api/app-server/v1", "APIWebSocketRoute") in routes


def test_websocket_transport_enforces_subprotocol_and_runs_handshake() -> None:
    app = FastAPI()
    store = InMemoryNotificationStore()
    install_app_server_routes(app, store_factory=lambda: nullcontext(store))
    route = cast(
        APIWebSocketRoute,
        next(route for route in app.routes if getattr(route, "path", "") == "/api/app-server/v1"),
    )

    rejected = FakeWebSocket([], "future.protocol")
    asyncio.run(route.endpoint(rejected))
    assert rejected.closed == (4406, "zekam.app-server.v1 subprotocol required")

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "client_id": "codex",
            "client_version": "1.0.0",
            "protocol_version": "1.0",
            "schema_bundle_digest": schema_bundle_digest(),
            "capabilities": ["read-status"],
            "experimental_methods": [],
            "replay_cursor": None,
        },
    }
    accepted = FakeWebSocket(
        [
            initialize,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "server/status", "params": {}},
        ],
        "zekam.app-server.v1",
    )
    asyncio.run(route.endpoint(accepted))
    assert accepted.accepted == "zekam.app-server.v1"
    assert [item["id"] for item in accepted.sent] == [1, 2]
    assert accepted.sent[1]["result"]["read_only"] is True
    assert accepted.sent[1]["result"]["grants_authority"] is False


def test_websocket_transport_returns_parse_error_and_keeps_connection_bounded() -> None:
    app = FastAPI()
    install_app_server_routes(app, store_factory=lambda: nullcontext(InMemoryNotificationStore()))
    route = cast(
        APIWebSocketRoute,
        next(route for route in app.routes if getattr(route, "path", "") == "/api/app-server/v1"),
    )
    socket = FakeWebSocket(
        ['{"jsonrpc":"2.0",', '{"duplicate":1,"duplicate":2}', b"\xff"],
        "zekam.app-server.v1",
    )
    asyncio.run(route.endpoint(socket))
    assert [item["error"]["data"]["category"] for item in socket.sent] == [
        "parse-error",
        "parse-error",
        "parse-error",
    ]
    assert all(item["id"] is None for item in socket.sent)


def test_websocket_transport_reports_typed_cursor_expired_without_generic_close() -> None:
    app = FastAPI()
    install_app_server_routes(app, store_factory=lambda: nullcontext(InMemoryNotificationStore()))
    route = cast(
        APIWebSocketRoute,
        next(route for route in app.routes if getattr(route, "path", "") == "/api/app-server/v1"),
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "client_id": "codex",
            "client_version": "1.0.0",
            "protocol_version": "1.0",
            "schema_bundle_digest": schema_bundle_digest(),
            "capabilities": ["notifications", "replay"],
            "experimental_methods": [],
            "replay_cursor": 1,
        },
    }
    socket = FakeWebSocket(
        [request, {"jsonrpc": "2.0", "method": "initialized", "params": {}}],
        "zekam.app-server.v1",
    )
    asyncio.run(route.endpoint(socket))
    assert socket.sent[1]["error"]["data"]["category"] == "cursor-expired"
    assert socket.closed is None
