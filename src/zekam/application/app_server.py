"""Bounded App Server v1 connection state machine."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zekam.domain.app_server_protocol import (
    KNOWN_CAPABILITIES,
    PROTOCOL_SCHEMA,
    PROTOCOL_VERSION,
    AppNotification,
    ConnectionPhase,
    InitializeRequest,
    NotificationStore,
    ProtocolErrorCode,
    ProtocolFault,
    ReadProjectionStore,
    schema_bundle_digest,
    valid_protocol_method,
)
from zekam.domain.canonical import canonical_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7

_MUTATION_METHOD_PREFIXES = (
    "authorization/",
    "effect/",
    "git/",
    "project/mutate",
    "provider/",
    "work/mutate",
)


@dataclass(slots=True)
class InMemoryNotificationStore:
    events: list[AppNotification] = field(default_factory=list)

    def replay(self, *, after_sequence: int, limit: int) -> tuple[AppNotification, ...]:
        return tuple(item for item in self.events if item.sequence > after_sequence)[:limit]

    def head_sequence(self) -> int:
        return 0 if not self.events else self.events[-1].sequence

    def cursor_exists(self, sequence: int) -> bool:
        return sequence == 0 or any(item.sequence == sequence for item in self.events)


@dataclass(slots=True)
class AppServerConnection:
    notifications: NotificationStore
    projections: ReadProjectionStore | None = None
    ingress_limit: int = 32
    outbound_limit: int = 64
    replay_limit: int = 256
    max_frame_bytes: int = 65_536
    connection_id: UUID = field(default_factory=new_uuid7)
    phase: ConnectionPhase = ConnectionPhase.NEW
    _ingress: deque[dict[str, Any]] = field(default_factory=deque)
    _outbound: deque[dict[str, Any]] = field(default_factory=deque)
    _client_id: str | None = None
    _capabilities: tuple[str, ...] = ()
    _experimental_methods: frozenset[str] = frozenset()
    _replay_cursor: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.ingress_limit <= 1024:
            raise ValueError("ingress_limit 1..1024 olmali")
        if not 1 <= self.outbound_limit <= 4096:
            raise ValueError("outbound_limit 1..4096 olmali")
        if not 1 <= self.replay_limit <= 1000:
            raise ValueError("replay_limit 1..1000 olmali")
        if not 1024 <= self.max_frame_bytes <= 1_048_576:
            raise ValueError("max_frame_bytes 1024..1048576 olmali")

    @property
    def negotiated_capabilities(self) -> tuple[str, ...]:
        return self._capabilities

    @property
    def replay_cursor(self) -> int:
        return self._replay_cursor

    def enqueue(self, document: Any) -> None:
        """Admit one inbound frame or reject explicitly without dropping older work."""
        try:
            frame_size = len(canonical_bytes(document))
        except (TypeError, ValueError, ValidationFailed) as exc:
            raise ProtocolFault(
                ProtocolErrorCode.INVALID_REQUEST, "JSON-RPC frame kanonik degil"
            ) from exc
        if frame_size > self.max_frame_bytes:
            raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "JSON-RPC frame cok buyuk")
        if len(self._ingress) >= self.ingress_limit:
            raise ProtocolFault(
                ProtocolErrorCode.OVERLOADED,
                "ingress queue dolu",
                retryable=True,
                data={"retry_after_ms": 100},
            )
        self._ingress.append(_parse_frame(document))

    def process_next(self) -> None:
        if not self._ingress:
            return
        if len(self._outbound) >= self.outbound_limit:
            raise ProtocolFault(
                ProtocolErrorCode.OVERLOADED,
                "outbound queue dolu",
                retryable=True,
                data={"retry_after_ms": 100},
            )
        frame = self._ingress.popleft()
        request_id = frame.get("id")
        try:
            response = self._dispatch(frame)
        except ProtocolFault as fault:
            if request_id is not None:
                self._emit(fault.error(request_id))
            elif frame.get("method") == "initialized" or (
                request_id is None and self.phase is not ConnectionPhase.READY
            ):
                raise
            return
        if response is not None:
            self._emit(response)

    def handle(self, document: Any) -> tuple[dict[str, Any], ...]:
        self.enqueue(document)
        self.process_next()
        return self.drain_outbound()

    def drain_outbound(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        count = len(self._outbound) if limit is None else min(limit, len(self._outbound))
        return tuple(self._outbound.popleft() for _ in range(count))

    def close(self) -> None:
        self.phase = ConnectionPhase.CLOSED
        self._ingress.clear()
        self._outbound.clear()

    def _dispatch(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        if self.phase is ConnectionPhase.CLOSED:
            raise ProtocolFault(ProtocolErrorCode.POLICY_DENIED, "connection kapali")
        method = str(frame["method"])
        request_id = frame.get("id")
        if method == "initialize":
            if request_id is None:
                raise ProtocolFault(
                    ProtocolErrorCode.INVALID_REQUEST, "initialize request id ister"
                )
            return self._initialize(request_id, frame["params"])
        if method == "initialized":
            if request_id is not None or frame["params"] != {}:
                raise ProtocolFault(
                    ProtocolErrorCode.INVALID_REQUEST, "initialized bos notification olmali"
                )
            self._initialized()
            return None
        if self.phase is not ConnectionPhase.READY:
            raise ProtocolFault(
                ProtocolErrorCode.NOT_INITIALIZED, "initialize/initialized handshake gerekli"
            )
        if request_id is None:
            # Unknown notifications are intentionally ignored after readiness.
            return None
        if method.startswith("experimental/") and method not in self._experimental_methods:
            raise ProtocolFault(
                ProtocolErrorCode.EXPERIMENTAL_NOT_ENABLED,
                "experimental method initialize opt-in ister",
            )
        if method.startswith(_MUTATION_METHOD_PREFIXES):
            raise ProtocolFault(
                ProtocolErrorCode.POLICY_DENIED,
                "App Server v1 mutation acmaz; kanonik authorization akisi gerekli",
            )
        if method == "server/status":
            return _result(
                request_id,
                {
                    "schema": "zekam-app-server-status/v1",
                    "connection_id": str(self.connection_id),
                    "phase": self.phase.value,
                    "client_id": self._client_id,
                    "capabilities": list(self._capabilities),
                    "replay_cursor": self._replay_cursor,
                    "notification_head": self.notifications.head_sequence(),
                    "read_only": True,
                    "grants_authority": False,
                },
            )
        if method == "notifications/replay":
            if "replay" not in self._capabilities:
                raise ProtocolFault(ProtocolErrorCode.POLICY_DENIED, "replay negotiated degil")
            cursor, limit = _replay_params(frame["params"], self.replay_limit)
            if not self.notifications.cursor_exists(cursor):
                raise ProtocolFault(
                    ProtocolErrorCode.CURSOR_EXPIRED,
                    "replay cursor canonical stream head ilerisinde",
                )
            events = self.notifications.replay(after_sequence=cursor, limit=limit)
            page_cursor = events[-1].sequence if events else cursor
            self._replay_cursor = max(self._replay_cursor, page_cursor)
            return _result(
                request_id,
                {
                    "schema": "zekam-notification-replay/v1",
                    "events": [item.notification()["params"] for item in events],
                    "next_cursor": page_cursor,
                    "connection_cursor": self._replay_cursor,
                    "head_cursor": self.notifications.head_sequence(),
                    "read_only": True,
                    "grants_authority": False,
                },
            )
        if method in {"project/read", "work/read", "session/read"}:
            if "read-status" not in self._capabilities:
                raise ProtocolFault(
                    ProtocolErrorCode.POLICY_DENIED, "kanonik read projection kullanilamiyor"
                )
            return _result(request_id, self._read_projection(method, frame["params"]))
        if method == "experimental/session-fork.prepare":
            return _result(
                request_id,
                {
                    "schema": "zekam-session-fork-prepare/v1",
                    "disposition": "prepare-only",
                    "carries_authority": False,
                    "carries_lease": False,
                    "carries_receipt": False,
                    "read_only": True,
                    "grants_authority": False,
                },
            )
        raise ProtocolFault(ProtocolErrorCode.METHOD_NOT_FOUND, "method desteklenmiyor")

    def _read_projection(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/read":
            if set(params) != {"session_id", "limit"}:
                raise ProtocolFault(
                    ProtocolErrorCode.INVALID_REQUEST, "session/read params exact degil"
                )
            session_id, limit = params["session_id"], params["limit"]
            if (
                not isinstance(session_id, str)
                or not session_id.strip()
                or len(session_id) > 160
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 100
            ):
                raise ProtocolFault(
                    ProtocolErrorCode.INVALID_REQUEST, "session/read params gecersiz"
                )
            reference = session_id
        else:
            key = "project_id" if method == "project/read" else "work_item_id"
            if set(params) != {key}:
                raise ProtocolFault(
                    ProtocolErrorCode.INVALID_REQUEST, f"{method} params exact degil"
                )
            try:
                reference_uuid = UUID(str(params[key]))
            except (TypeError, ValueError) as exc:
                raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, f"{key} gecersiz") from exc
            reference = str(reference_uuid)
        if self.projections is None:
            raise ProtocolFault(
                ProtocolErrorCode.POLICY_DENIED, "kanonik read projection kullanilamiyor"
            )
        if method == "session/read":
            value = self.projections.read_session(session_id, limit=limit)
        else:
            value = (
                self.projections.read_project(reference_uuid)
                if method == "project/read"
                else self.projections.read_work(reference_uuid)
            )
        return {
            "schema": "zekam-app-read-projection/v1",
            "kind": method.removesuffix("/read"),
            "reference": reference,
            "found": value is not None,
            "value": value,
            "canonical_reread": True,
            "read_only": True,
            "grants_authority": False,
        }

    def _initialize(self, request_id: str | int, params: Any) -> dict[str, Any]:
        if self.phase is not ConnectionPhase.NEW:
            raise ProtocolFault(ProtocolErrorCode.ALREADY_INITIALIZED, "initialize tek seferliktir")
        request = InitializeRequest.parse(params)
        expected = schema_bundle_digest()
        if request.protocol_version != PROTOCOL_VERSION:
            raise ProtocolFault(
                ProtocolErrorCode.SCHEMA_MISMATCH,
                "protocol version uyusmuyor",
                data={"expected_protocol_version": PROTOCOL_VERSION},
            )
        if request.schema_bundle_digest != expected:
            raise ProtocolFault(
                ProtocolErrorCode.SCHEMA_MISMATCH,
                "schema bundle digest uyusmuyor",
                data={"expected_schema_bundle_digest": expected},
            )
        self._client_id = request.client_id
        self._capabilities = tuple(
            capability for capability in request.capabilities if capability in KNOWN_CAPABILITIES
        )
        self._experimental_methods = frozenset(request.experimental_methods)
        self._replay_cursor = request.replay_cursor or 0
        self.phase = ConnectionPhase.INITIALIZE_ACKED
        return _result(
            request_id,
            {
                "schema": PROTOCOL_SCHEMA,
                "connection_id": str(self.connection_id),
                "protocol_version": PROTOCOL_VERSION,
                "schema_bundle_digest": expected,
                "capabilities": list(self._capabilities),
                "experimental_methods": sorted(self._experimental_methods),
                "ingress_limit": self.ingress_limit,
                "outbound_limit": self.outbound_limit,
                "replay_limit": self.replay_limit,
                "max_frame_bytes": self.max_frame_bytes,
                "read_only": True,
                "grants_authority": False,
            },
        )

    def _initialized(self) -> None:
        if self.phase is ConnectionPhase.NEW:
            raise ProtocolFault(ProtocolErrorCode.NOT_INITIALIZED, "initialize once gerekli")
        if self.phase is not ConnectionPhase.INITIALIZE_ACKED:
            raise ProtocolFault(
                ProtocolErrorCode.ALREADY_INITIALIZED, "initialized tek seferliktir"
            )
        if "notifications" not in self._capabilities or "replay" not in self._capabilities:
            self.phase = ConnectionPhase.READY
            return
        if not self.notifications.cursor_exists(self._replay_cursor):
            raise ProtocolFault(
                ProtocolErrorCode.CURSOR_EXPIRED,
                "replay cursor canonical stream zincirinde yok",
            )
        events = self.notifications.replay(
            after_sequence=self._replay_cursor,
            limit=min(self.replay_limit, self.outbound_limit - len(self._outbound)),
        )
        self.phase = ConnectionPhase.READY
        for event in events:
            self._emit(event.notification())
            self._replay_cursor = event.sequence

    def _emit(self, document: dict[str, Any]) -> None:
        if len(self._outbound) >= self.outbound_limit:
            raise ProtocolFault(
                ProtocolErrorCode.OVERLOADED,
                "outbound queue dolu",
                retryable=True,
                data={"retry_after_ms": 100},
            )
        self._outbound.append(document)


def _parse_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in (
        {"jsonrpc", "id", "method", "params"},
        {"jsonrpc", "method", "params"},
    ):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "JSON-RPC frame exact degil")
    if (
        value.get("jsonrpc") != "2.0"
        or not isinstance(value.get("method"), str)
        or not valid_protocol_method(str(value.get("method")))
    ):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "JSON-RPC version/method gecersiz")
    if not isinstance(value.get("params"), dict):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "params object olmali")
    request_id = value.get("id")
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int))
    ):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "request id gecersiz")
    return dict(value)


def _result(request_id: str | int, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _replay_params(params: dict[str, Any], maximum: int) -> tuple[int, int]:
    if set(params) != {"after_sequence", "limit"}:
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "replay params exact degil")
    cursor, limit = params["after_sequence"], params["limit"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (cursor, limit)):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "replay cursor/limit gecersiz")
    if cursor < 0 or limit < 1 or limit > maximum:
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "replay cursor/limit sinir disi")
    return cursor, limit
