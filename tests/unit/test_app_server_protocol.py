from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from zekam.application.app_server import AppServerConnection, InMemoryNotificationStore
from zekam.domain.app_server_protocol import (
    AppNotification,
    ConnectionPhase,
    ProtocolErrorCode,
    ProtocolFault,
    protocol_schema_bundle,
    schema_bundle_digest,
)
from zekam.domain.canonical import digest

NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)


class ProjectionStore:
    def __init__(self) -> None:
        self.revision = 1

    def read_project(self, project_id: object) -> dict[str, Any] | None:
        return {"id": str(project_id), "revision": self.revision}

    def read_work(self, work_item_id: object) -> dict[str, Any] | None:
        return {"id": str(work_item_id), "state": "active"}

    def read_session(self, session_id: str, *, limit: int) -> dict[str, Any] | None:
        return {"session_id": session_id, "runs": [], "bounded_limit": limit}


def frame(method: str, params: dict[str, object], request_id: int = 1) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def initialize(
    *,
    request_id: int = 1,
    capabilities: list[str] | None = None,
    experimental: list[str] | None = None,
    cursor: int | None = None,
    schema_digest: str | None = None,
) -> dict[str, object]:
    return frame(
        "initialize",
        {
            "client_id": "codex",
            "client_version": "1.2.3",
            "protocol_version": "1.0",
            "schema_bundle_digest": schema_digest or schema_bundle_digest(),
            "capabilities": capabilities or ["read-status", "notifications", "replay"],
            "experimental_methods": experimental or [],
            "replay_cursor": cursor,
        },
        request_id,
    )


def initialized() -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": "initialized", "params": {}}


def notification(sequence: int) -> AppNotification:
    event_id = uuid4()
    occurred_at = NOW + dt.timedelta(seconds=sequence)
    values = {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": "work.updated",
        "payload": {"work_ref": f"work-{sequence}"},
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "previous_digest": None if sequence == 1 else digest(f"previous-{sequence}"),
    }
    event_digest = digest(
        {
            "schema": "zekam-app-notification/v1",
            "event_id": str(event_id),
            "sequence": sequence,
            "event_type": "work.updated",
            "payload": values["payload"],
            "occurred_at": values["occurred_at"],
            "previous_digest": values["previous_digest"],
            "grants_authority": False,
        }
    )
    return AppNotification(event_digest=event_digest, **values)


def test_generated_schema_is_exactly_tracked() -> None:
    tracked = Path("schemas/app-server-protocol-v1.schema.json")
    assert json.loads(tracked.read_text(encoding="utf-8")) == protocol_schema_bundle()


def test_generated_schema_validates_exact_method_contracts_and_rejects_drift() -> None:
    validator = Draft202012Validator(protocol_schema_bundle(), format_checker=FormatChecker())
    validator.validate(initialize())
    validator.validate({"jsonrpc": "2.0", "method": "future/event", "params": {"v": 1}})
    with pytest.raises(ValidationError):
        validator.validate(frame("initialize", {}))
    with pytest.raises(ValidationError):
        validator.validate(frame("notifications/replay", {"after_sequence": 0, "limit": 1001}))
    with pytest.raises(ValidationError):
        validator.validate(frame("future/request", {}))
    invalid = initialize()
    invalid["params"] = dict(invalid["params"], unexpected=True)
    with pytest.raises(ValidationError):
        validator.validate(invalid)

    connection = AppServerConnection(InMemoryNotificationStore())
    validator.validate(connection.handle(initialize())[0])
    connection.handle(initialized())
    validator.validate(connection.handle(frame("server/status", {}, 2))[0])
    validator.validate(connection.handle(frame("future/request", {}, 3))[0])
    validator.validate(notification(1).notification())

    event_store = InMemoryNotificationStore([notification(1)])
    read_connection = AppServerConnection(event_store, projections=ProjectionStore())
    validator.validate(
        read_connection.handle(initialize(experimental=["experimental/session-fork.prepare"]))[0]
    )
    for replayed in read_connection.handle(initialized()):
        validator.validate(replayed)
    validator.validate(
        read_connection.handle(
            frame("notifications/replay", {"after_sequence": 0, "limit": 10}, 4)
        )[0]
    )
    validator.validate(
        read_connection.handle(frame("project/read", {"project_id": str(uuid4())}, 5))[0]
    )
    validator.validate(read_connection.handle(frame("experimental/session-fork.prepare", {}, 6))[0])


@pytest.mark.parametrize(
    "document",
    [
        initialize(capabilities=["BAD CAPABILITY"]),
        frame("session/read", {"session_id": " ", "limit": 1}),
        frame(
            "initialize",
            {
                **initialize()["params"],
                "client_id": 42,
            },
        ),
    ],
)
def test_schema_and_runtime_reject_the_same_constraint_drift(
    document: dict[str, object],
) -> None:
    validator = Draft202012Validator(protocol_schema_bundle(), format_checker=FormatChecker())
    assert not validator.is_valid(document)
    connection = AppServerConnection(InMemoryNotificationStore())
    if document["method"] == "session/read":
        connection.handle(initialize())
        connection.handle(initialized())
    response = connection.handle(document)[0]
    assert response["error"]["data"]["category"] == ProtocolErrorCode.INVALID_REQUEST.value


def test_initialize_initialized_handshake_is_mandatory_and_immutable() -> None:
    connection = AppServerConnection(InMemoryNotificationStore())

    before = connection.handle(frame("server/status", {}, 9))[0]
    assert before["error"]["data"]["category"] == ProtocolErrorCode.NOT_INITIALIZED.value
    response = connection.handle(initialize())[0]
    assert response["result"]["schema_bundle_digest"] == schema_bundle_digest()
    assert response["result"]["grants_authority"] is False
    assert connection.phase is ConnectionPhase.INITIALIZE_ACKED
    second = connection.handle(initialize(request_id=2))[0]
    assert second["error"]["data"]["category"] == ProtocolErrorCode.ALREADY_INITIALIZED.value
    connection.handle(initialized())
    assert connection.phase is ConnectionPhase.READY
    status = connection.handle(frame("server/status", {}, 3))[0]["result"]
    assert status["read_only"] is True
    assert status["grants_authority"] is False
    assert status["capabilities"] == ["read-status", "notifications", "replay"]


def test_schema_mismatch_and_experimental_opt_in_fail_closed() -> None:
    mismatch = AppServerConnection(InMemoryNotificationStore())
    response = mismatch.handle(initialize(schema_digest=digest("wrong")))[0]
    assert response["error"]["data"]["category"] == ProtocolErrorCode.SCHEMA_MISMATCH.value
    assert mismatch.phase is ConnectionPhase.NEW

    connection = AppServerConnection(InMemoryNotificationStore())
    connection.handle(initialize())
    connection.handle(initialized())
    denied = connection.handle(frame("experimental/session-fork.prepare", {}, 4))[0]
    assert denied["error"]["data"]["category"] == (ProtocolErrorCode.EXPERIMENTAL_NOT_ENABLED.value)
    opted = AppServerConnection(InMemoryNotificationStore())
    opted.handle(initialize(experimental=["experimental/session-fork.prepare"]))
    opted.handle(initialized())
    prepared = opted.handle(frame("experimental/session-fork.prepare", {}, 5))[0]["result"]
    assert prepared["carries_authority"] is False
    assert prepared["carries_lease"] is False
    assert prepared["carries_receipt"] is False


def test_bounded_ingress_reports_retryable_overload_without_dropping_frames() -> None:
    connection = AppServerConnection(InMemoryNotificationStore(), ingress_limit=2)
    connection.enqueue(initialize(request_id=1))
    connection.enqueue(initialize(request_id=2))
    with pytest.raises(ProtocolFault) as raised:
        connection.enqueue(initialize(request_id=3))
    assert raised.value.code is ProtocolErrorCode.OVERLOADED
    assert raised.value.retryable
    connection.process_next()
    connection.process_next()
    responses = connection.drain_outbound()
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[1]["error"]["data"]["category"] == "already-initialized"


def test_bounded_outbound_backpressure_does_not_consume_next_ingress_frame() -> None:
    connection = AppServerConnection(InMemoryNotificationStore(), outbound_limit=1)
    connection.enqueue(initialize(request_id=1))
    connection.process_next()
    connection.enqueue(initialize(request_id=2))
    with pytest.raises(ProtocolFault) as raised:
        connection.process_next()
    assert raised.value.code is ProtocolErrorCode.OVERLOADED
    assert connection.drain_outbound()[0]["id"] == 1
    connection.process_next()
    assert connection.drain_outbound()[0]["id"] == 2


def test_oversized_frame_is_rejected_before_queue_admission() -> None:
    connection = AppServerConnection(InMemoryNotificationStore(), max_frame_bytes=1024)
    with pytest.raises(ProtocolFault, match="cok buyuk"):
        connection.enqueue(frame("server/status", {"padding": "x" * 2000}))
    connection.enqueue(initialize())
    connection.process_next()
    assert connection.drain_outbound()[0]["id"] == 1


def test_noncanonical_frame_and_mutation_surface_fail_closed_with_distinct_errors() -> None:
    connection = AppServerConnection(InMemoryNotificationStore())
    with pytest.raises(ProtocolFault) as invalid:
        connection.enqueue(frame("server/status", {"value": {1, 2, 3}}))
    assert invalid.value.code is ProtocolErrorCode.INVALID_REQUEST

    connection.handle(initialize())
    connection.handle(initialized())
    denied = connection.handle(frame("work/mutate", {"state": "completed"}, 8))[0]
    assert denied["error"]["data"]["category"] == ProtocolErrorCode.POLICY_DENIED.value
    unknown = connection.handle(frame("future/request", {}, 9))[0]
    assert unknown["error"]["data"]["category"] == ProtocolErrorCode.METHOD_NOT_FOUND.value


def test_project_work_and_session_reads_are_bounded_authority_free_canonical_rereads() -> None:
    project_id, work_id = uuid4(), uuid4()
    projections = ProjectionStore()
    first = AppServerConnection(InMemoryNotificationStore(), projections=projections)
    first.handle(initialize())
    first.handle(initialized())
    project = first.handle(frame("project/read", {"project_id": str(project_id)}, 10))[0]["result"]
    assert project["canonical_reread"] is True
    assert project["grants_authority"] is False
    assert project["value"]["revision"] == 1
    work = first.handle(frame("work/read", {"work_item_id": str(work_id)}, 11))[0]["result"]
    assert work["value"]["state"] == "active"
    session = first.handle(frame("session/read", {"session_id": "ses-1", "limit": 25}, 12))[0][
        "result"
    ]
    assert session["value"]["bounded_limit"] == 25

    projections.revision = 2
    reconnected = AppServerConnection(InMemoryNotificationStore(), projections=projections)
    reconnected.handle(initialize())
    reconnected.handle(initialized())
    reread = reconnected.handle(frame("project/read", {"project_id": str(project_id)}, 13))[0]
    assert reread["result"]["value"]["revision"] == 2


def test_reconnect_cursor_replays_at_least_once_with_event_id_dedupe_surface() -> None:
    events = [notification(sequence) for sequence in range(1, 7)]
    store = InMemoryNotificationStore(events)
    connection = AppServerConnection(store, outbound_limit=3, replay_limit=5)
    connection.handle(initialize(cursor=2))
    replayed = connection.handle(initialized())
    assert [item["params"]["sequence"] for item in replayed] == [3, 4, 5]
    assert len({item["params"]["event_id"] for item in replayed}) == 3
    assert connection.replay_cursor == 5
    explicit = connection.handle(
        frame("notifications/replay", {"after_sequence": 5, "limit": 5}, 10)
    )[0]["result"]
    assert [item["sequence"] for item in explicit["events"]] == [6]
    assert explicit["next_cursor"] == 6
    assert explicit["connection_cursor"] == 6


def test_explicit_replay_empty_page_and_rewind_have_separate_monotonic_cursors() -> None:
    store = InMemoryNotificationStore([notification(sequence) for sequence in range(1, 4)])
    replay_only = AppServerConnection(store)
    replay_only.handle(initialize(capabilities=["replay"], cursor=0))
    replay_only.handle(initialized())
    empty = replay_only.handle(
        frame("notifications/replay", {"after_sequence": 3, "limit": 10}, 20)
    )[0]["result"]
    assert empty["events"] == []
    assert empty["next_cursor"] == 3
    assert empty["connection_cursor"] == 3
    assert replay_only.replay_cursor == 3

    rewind = replay_only.handle(
        frame("notifications/replay", {"after_sequence": 0, "limit": 1}, 21)
    )[0]["result"]
    assert [event["sequence"] for event in rewind["events"]] == [1]
    assert rewind["next_cursor"] == 1
    assert rewind["connection_cursor"] == 3
    assert replay_only.replay_cursor == 3


def test_reconnect_cursor_validation_is_typed_and_phase_atomic() -> None:
    store = InMemoryNotificationStore([notification(1)])
    exact = AppServerConnection(store)
    exact.handle(initialize(cursor=1))
    assert exact.handle(initialized()) == ()
    assert exact.phase is ConnectionPhase.READY

    ahead = AppServerConnection(store)
    ahead.handle(initialize(cursor=2))
    with pytest.raises(ProtocolFault) as raised:
        ahead.handle(initialized())
    assert raised.value.code is ProtocolErrorCode.CURSOR_EXPIRED
    assert ahead.phase is ConnectionPhase.INITIALIZE_ACKED
    assert ahead.replay_cursor == 2

    gap_store = InMemoryNotificationStore([notification(1), notification(3)])
    gap = AppServerConnection(gap_store)
    gap.handle(initialize(cursor=2))
    with pytest.raises(ProtocolFault) as missing:
        gap.handle(initialized())
    assert missing.value.code is ProtocolErrorCode.CURSOR_EXPIRED
    assert gap.phase is ConnectionPhase.INITIALIZE_ACKED


def test_unknown_notification_is_ignored_only_after_ready() -> None:
    connection = AppServerConnection(InMemoryNotificationStore())
    connection.handle(initialize())
    connection.handle(initialized())
    assert connection.handle({"jsonrpc": "2.0", "method": "future/event", "params": {}}) == ()
