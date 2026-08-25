"""Versioned, authority-free Zekam App Server protocol contracts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.app_server_schema_models import pydantic_protocol_schema
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed

PROTOCOL_VERSION = "1.0"
PROTOCOL_SCHEMA = "zekam-app-server-protocol/v1"
KNOWN_CAPABILITIES = frozenset({"notifications", "replay", "read-status", "prepare"})
_METHOD = re.compile(r"^[a-z][a-z0-9_.\-/]{0,127}$")
_CLIENT = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class ConnectionPhase(StrEnum):
    NEW = "new"
    INITIALIZE_ACKED = "initialize-acked"
    READY = "ready"
    CLOSED = "closed"


class ProtocolErrorCode(StrEnum):
    PARSE_ERROR = "parse-error"
    INVALID_REQUEST = "invalid-request"
    METHOD_NOT_FOUND = "method-not-found"
    NOT_INITIALIZED = "not-initialized"
    ALREADY_INITIALIZED = "already-initialized"
    SCHEMA_MISMATCH = "schema-mismatch"
    OVERLOADED = "overloaded"
    POLICY_DENIED = "policy-denied"
    EXPERIMENTAL_NOT_ENABLED = "experimental-not-enabled"
    CURSOR_EXPIRED = "cursor-expired"


_NUMERIC_ERRORS: dict[ProtocolErrorCode, int] = {
    ProtocolErrorCode.PARSE_ERROR: -32700,
    ProtocolErrorCode.INVALID_REQUEST: -32600,
    ProtocolErrorCode.METHOD_NOT_FOUND: -32601,
    ProtocolErrorCode.NOT_INITIALIZED: -32001,
    ProtocolErrorCode.ALREADY_INITIALIZED: -32002,
    ProtocolErrorCode.SCHEMA_MISMATCH: -32003,
    ProtocolErrorCode.OVERLOADED: -32010,
    ProtocolErrorCode.POLICY_DENIED: -32020,
    ProtocolErrorCode.EXPERIMENTAL_NOT_ENABLED: -32021,
    ProtocolErrorCode.CURSOR_EXPIRED: -32030,
}


@dataclass(frozen=True, slots=True)
class ProtocolFault(Exception):
    code: ProtocolErrorCode
    message: str
    retryable: bool = False
    data: dict[str, Any] | None = None

    def error(self, request_id: str | int | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": _NUMERIC_ERRORS[self.code],
            "message": self.message,
            "data": {
                "category": self.code.value,
                "retryable": self.retryable,
                "grants_authority": False,
            },
        }
        if self.data:
            body["data"].update(self.data)
        return {"jsonrpc": "2.0", "id": request_id, "error": body}


@dataclass(frozen=True, slots=True)
class InitializeRequest:
    client_id: str
    client_version: str
    protocol_version: str
    schema_bundle_digest: str
    capabilities: tuple[str, ...]
    experimental_methods: tuple[str, ...]
    replay_cursor: int | None

    @classmethod
    def parse(cls, value: Any) -> InitializeRequest:
        if not isinstance(value, dict) or set(value) != {
            "client_id",
            "client_version",
            "protocol_version",
            "schema_bundle_digest",
            "capabilities",
            "experimental_methods",
            "replay_cursor",
        }:
            raise ProtocolFault(
                ProtocolErrorCode.INVALID_REQUEST, "initialize params exact schema ister"
            )
        capabilities = _string_array(value["capabilities"], "capabilities")
        experimental = _string_array(value["experimental_methods"], "experimental_methods")
        if not isinstance(value["client_id"], str) or not _CLIENT.fullmatch(value["client_id"]):
            raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "client_id gecersiz")
        if not isinstance(value["client_version"], str) or not _CLIENT.fullmatch(
            value["client_version"]
        ):
            raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "client_version gecersiz")
        if not isinstance(value["protocol_version"], str) or not isinstance(
            value["schema_bundle_digest"], str
        ):
            raise ProtocolFault(
                ProtocolErrorCode.INVALID_REQUEST, "initialize version/digest gecersiz"
            )
        cursor = value["replay_cursor"]
        if cursor is not None and (
            not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0
        ):
            raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, "replay_cursor gecersiz")
        return cls(
            client_id=str(value["client_id"]),
            client_version=str(value["client_version"]),
            protocol_version=str(value["protocol_version"]),
            schema_bundle_digest=str(value["schema_bundle_digest"]),
            capabilities=capabilities,
            experimental_methods=experimental,
            replay_cursor=cursor,
        )


@dataclass(frozen=True, slots=True)
class AppNotification:
    event_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: str
    previous_digest: str | None
    event_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 1 or not _METHOD.fullmatch(self.event_type):
            raise ValidationFailed("App notification sequence/type gecersiz")
        try:
            occurred_at = dt.datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailed("App notification occurred_at gecersiz") from exc
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None or self.grants_authority:
            raise ValidationFailed("App notification timezone ister ve authority tasiyamaz")
        if not isinstance(self.payload, dict):
            raise ValidationFailed("App notification payload object olmali")
        parse_digest(self.event_digest)
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)
        if (self.sequence == 1) != (self.previous_digest is None):
            raise ValidationFailed("App notification sequence/previous_digest zinciri gecersiz")
        if self.event_digest != digest(self.body()):
            raise ValidationFailed("App notification digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-app-notification/v1",
            "event_id": str(self.event_id),
            "sequence": self.sequence,
            "previous_digest": self.previous_digest,
            "event_type": self.event_type,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "grants_authority": False,
        }

    def notification(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "server/notification",
            "params": self.body() | {"event_digest": self.event_digest},
        }


class NotificationStore(Protocol):
    def replay(self, *, after_sequence: int, limit: int) -> tuple[AppNotification, ...]: ...

    def head_sequence(self) -> int: ...

    def cursor_exists(self, sequence: int) -> bool: ...


class ReadProjectionStore(Protocol):
    """Realm-scoped canonical read surface; it cannot create authority."""

    def read_project(self, project_id: UUID) -> dict[str, Any] | None: ...

    def read_work(self, work_item_id: UUID) -> dict[str, Any] | None: ...

    def read_session(self, session_id: str, *, limit: int) -> dict[str, Any] | None: ...


def protocol_schema_bundle() -> dict[str, Any]:
    """Return JSON Schema generated from the exact Pydantic wire models."""

    return pydantic_protocol_schema()


def schema_bundle_digest() -> str:
    return digest(protocol_schema_bundle())


def valid_protocol_method(value: str) -> bool:
    return _METHOD.fullmatch(value) is not None


def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(item, str) or not _METHOD.fullmatch(item) for item in value)
        or len(value) != len(set(value))
    ):
        raise ProtocolFault(ProtocolErrorCode.INVALID_REQUEST, f"{label} gecersiz")
    return tuple(value)
