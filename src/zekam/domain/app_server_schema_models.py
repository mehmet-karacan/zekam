"""Pydantic source-of-truth for the exact App Server wire protocol."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints

PROTOCOL_VERSION = "1.0"
PROTOCOL_SCHEMA = "zekam-app-server-protocol/v1"
METHOD_PATTERN = r"^[a-z][a-z0-9_.\-/]{0,127}$"
CLIENT_PATTERN = r"^[A-Za-z0-9_.-]{1,80}$"
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

MethodText = Annotated[str, StringConstraints(pattern=METHOD_PATTERN)]
ClientText = Annotated[str, StringConstraints(pattern=CLIENT_PATTERN)]
DigestText = Annotated[str, StringConstraints(pattern=DIGEST_PATTERN)]
type JsonRpcId = str | int


class StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyParams(StrictWireModel):
    pass


class InitializeParams(StrictWireModel):
    client_id: ClientText
    client_version: ClientText
    protocol_version: Literal["1.0"]
    schema_bundle_digest: DigestText
    capabilities: Annotated[frozenset[MethodText], Field(max_length=32)]
    experimental_methods: Annotated[frozenset[MethodText], Field(max_length=32)]
    replay_cursor: Annotated[int, Field(ge=0)] | None


class ReplayParams(StrictWireModel):
    after_sequence: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=1000)]


class ProjectReadParams(StrictWireModel):
    project_id: UUID


class WorkReadParams(StrictWireModel):
    work_item_id: UUID


class SessionReadParams(StrictWireModel):
    session_id: Annotated[str, StringConstraints(min_length=1, max_length=160, pattern=r".*\S.*")]
    limit: Annotated[int, Field(ge=1, le=100)]


class InitializeRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["initialize"]
    params: InitializeParams


class StatusRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["server/status"]
    params: EmptyParams


class ReplayRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["notifications/replay"]
    params: ReplayParams


class ProjectReadRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["project/read"]
    params: ProjectReadParams


class WorkReadRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["work/read"]
    params: WorkReadParams


class SessionReadRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["session/read"]
    params: SessionReadParams


class ForkPrepareRequest(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: Literal["experimental/session-fork.prepare"]
    params: EmptyParams


class InitializedNotification(StrictWireModel):
    jsonrpc: Literal["2.0"]
    method: Literal["initialized"]
    params: EmptyParams


class AppNotificationPayload(StrictWireModel):
    schema_: Literal["zekam-app-notification/v1"] = Field(alias="schema")
    event_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    previous_digest: DigestText | None
    event_type: MethodText
    payload: dict[str, Any]
    occurred_at: Annotated[str, Field(json_schema_extra={"format": "date-time"})]
    grants_authority: Literal[False]
    event_digest: DigestText


class ServerNotification(StrictWireModel):
    jsonrpc: Literal["2.0"]
    method: Literal["server/notification"]
    params: AppNotificationPayload


class FutureNotification(StrictWireModel):
    jsonrpc: Literal["2.0"]
    method: Annotated[
        MethodText,
        Field(json_schema_extra={"not": {"enum": ["initialized", "server/notification"]}}),
    ]
    params: dict[str, Any]


class InitializeResult(StrictWireModel):
    schema_: Literal["zekam-app-server-protocol/v1"] = Field(alias="schema")
    connection_id: UUID
    protocol_version: Literal["1.0"]
    schema_bundle_digest: DigestText
    capabilities: list[str]
    experimental_methods: list[str]
    ingress_limit: int
    outbound_limit: int
    replay_limit: int
    max_frame_bytes: int
    read_only: Literal[True]
    grants_authority: Literal[False]


class StatusResult(StrictWireModel):
    schema_: Literal["zekam-app-server-status/v1"] = Field(alias="schema")
    connection_id: UUID
    phase: Literal["ready"]
    client_id: str
    capabilities: list[str]
    replay_cursor: int
    notification_head: int
    read_only: Literal[True]
    grants_authority: Literal[False]


class ReplayResult(StrictWireModel):
    schema_: Literal["zekam-notification-replay/v1"] = Field(alias="schema")
    events: list[AppNotificationPayload]
    next_cursor: int
    connection_cursor: int
    head_cursor: int
    read_only: Literal[True]
    grants_authority: Literal[False]


class ReadResult(StrictWireModel):
    schema_: Literal["zekam-app-read-projection/v1"] = Field(alias="schema")
    kind: Literal["project", "work", "session"]
    reference: str
    found: bool
    value: dict[str, Any] | None
    canonical_reread: Literal[True]
    read_only: Literal[True]
    grants_authority: Literal[False]


class ForkPrepareResult(StrictWireModel):
    schema_: Literal["zekam-session-fork-prepare/v1"] = Field(alias="schema")
    disposition: Literal["prepare-only"]
    carries_authority: Literal[False]
    carries_lease: Literal[False]
    carries_receipt: Literal[False]
    read_only: Literal[True]
    grants_authority: Literal[False]


class ResultResponse(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    result: InitializeResult | StatusResult | ReplayResult | ReadResult | ForkPrepareResult


class ProtocolErrorData(BaseModel):
    model_config = ConfigDict(extra="allow")
    category: Literal[
        "parse-error",
        "invalid-request",
        "method-not-found",
        "not-initialized",
        "already-initialized",
        "schema-mismatch",
        "overloaded",
        "policy-denied",
        "experimental-not-enabled",
        "cursor-expired",
    ]
    retryable: bool
    grants_authority: Literal[False]


class ProtocolError(StrictWireModel):
    code: int
    message: Annotated[str, StringConstraints(min_length=1)]
    data: ProtocolErrorData


class ErrorResponse(StrictWireModel):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId | None
    error: ProtocolError


type ProtocolRequest = (
    InitializeRequest
    | StatusRequest
    | ReplayRequest
    | ProjectReadRequest
    | WorkReadRequest
    | SessionReadRequest
    | ForkPrepareRequest
)
type ProtocolNotification = InitializedNotification | ServerNotification | FutureNotification
type ProtocolResponse = ResultResponse | ErrorResponse


class ProtocolDocument(RootModel[ProtocolRequest | ProtocolNotification | ProtocolResponse]):
    pass


def pydantic_protocol_schema() -> dict[str, Any]:
    """Return deterministic draft 2020-12 JSON Schema from exact wire models."""

    schema = ProtocolDocument.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
        union_format="any_of",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://zekam.local/schemas/app-server-protocol-v1.schema.json"
    schema["title"] = "Zekam App Server Protocol v1"
    return schema
