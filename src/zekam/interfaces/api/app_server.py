"""FastAPI WebSocket transport for Zekam App Server protocol v1."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, cast

from zekam.application.app_server import AppServerConnection
from zekam.domain.app_server_protocol import (
    NotificationStore,
    ProtocolErrorCode,
    ProtocolFault,
    ReadProjectionStore,
    protocol_schema_bundle,
    schema_bundle_digest,
)
from zekam.domain.canonical import canonicalize


def install_app_server_routes(
    app: Any,
    *,
    store_factory: Callable[[], AbstractContextManager[NotificationStore]] | None,
    ingress_limit: int = 32,
    outbound_limit: int = 64,
) -> None:
    """Install one versioned read-only transport without creating authority."""
    from fastapi import WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    @app.get("/api/app-server/v1/schema")  # type: ignore[untyped-decorator]
    async def app_server_schema() -> JSONResponse:
        return JSONResponse(
            {
                "schema": protocol_schema_bundle(),
                "schema_bundle_digest": schema_bundle_digest(),
                "grants_authority": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/api/app-server/v1")  # type: ignore[untyped-decorator]
    async def app_server_socket(websocket: WebSocket) -> None:
        if store_factory is None:
            await websocket.close(code=4403, reason="realm-scoped PostgreSQL required")
            return
        requested_protocols = {
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        }
        if "zekam.app-server.v1" not in requested_protocols:
            await websocket.close(code=4406, reason="zekam.app-server.v1 subprotocol required")
            return
        await websocket.accept(subprotocol="zekam.app-server.v1")
        with store_factory() as store:
            connection = AppServerConnection(
                store,
                projections=(
                    cast(ReadProjectionStore, store) if hasattr(store, "read_project") else None
                ),
                ingress_limit=ingress_limit,
                outbound_limit=outbound_limit,
            )
            try:
                while True:
                    document: Any = None
                    try:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            raise WebSocketDisconnect(code=int(message.get("code", 1000)))
                        document = _decode_frame(message, maximum=connection.max_frame_bytes)
                        outbound = connection.handle(document)
                    except ProtocolFault as fault:
                        request_id = document.get("id") if isinstance(document, dict) else None
                        outbound = (fault.error(request_id),)
                    for response in outbound:
                        await websocket.send_json(canonicalize(response))
            except WebSocketDisconnect:
                connection.close()
            except Exception:
                connection.close()
                await websocket.close(code=1011, reason="app-server internal failure")


def _decode_frame(message: Mapping[str, Any], *, maximum: int) -> Any:
    text = message.get("text")
    raw = message.get("bytes")
    if text is None and raw is None:
        raise ProtocolFault(ProtocolErrorCode.PARSE_ERROR, "JSON frame gerekli")
    if raw is not None:
        if not isinstance(raw, bytes) or len(raw) > maximum:
            raise ProtocolFault(ProtocolErrorCode.PARSE_ERROR, "JSON frame byte siniri gecersiz")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolFault(ProtocolErrorCode.PARSE_ERROR, "JSON UTF-8 olmali") from exc
    if not isinstance(text, str) or len(text.encode("utf-8")) > maximum:
        raise ProtocolFault(ProtocolErrorCode.PARSE_ERROR, "JSON frame boyutu gecersiz")
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolFault(ProtocolErrorCode.PARSE_ERROR, "JSON parse edilemedi") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
