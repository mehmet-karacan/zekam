from __future__ import annotations

import copy
import json
import os
import platform
import socket
import stat
import struct
import time
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle
from zekam.infrastructure.clients import codex_macos_0151_precompaction_client as client

pytestmark = pytest.mark.unit
NONCE = "a" * 64


def _observation() -> dict[str, object]:
    return {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "session_id": "session-1",
        "external_event_type": "PreCompact",
        "internal_event_type": "PRE_COMPACTION",
        "turn_id": "turn-1",
        "source": None,
        "trigger": "manual",
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
        "wire_digest": digest("wire"),
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
    }


def _request() -> dict[str, object]:
    created = 10_000_000_000
    body: dict[str, object] = {
        "attempt_nonce": NONCE,
        "binding_digest": digest("binding"),
        "client_pid": 123,
        "client_start_token": "start-token",
        "client_uid": 501,
        "created_monotonic_ns": created,
        "deadline_monotonic_ns": created + client.TOTAL_DEADLINE_NS,
        "delivery_id": digest("delivery"),
        "event_observation": _observation(),
        "event_wire_digest": digest("wire"),
        "external_session_id": "session-1",
        "protocol_digest": client.PROTOCOL_DIGEST,
        "request_key": "",
        "schema": "zekam-precompact-local-request/v1",
        "trigger": "manual",
        "turn_id": "turn-1",
    }
    body["request_key"] = digest(
        {
            "schema": "zekam-precompact-local-request-key/v1",
            "binding_digest": body["binding_digest"],
            "delivery_id": body["delivery_id"],
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": body["external_session_id"],
            "trigger": body["trigger"],
            "turn_id": body["turn_id"],
        }
    )
    return body


def _failure(request: dict[str, object]) -> dict[str, object]:
    return {
        "attempt_nonce": request["attempt_nonce"],
        "classification": "STORAGE_UNAVAILABLE",
        "decision_body": None,
        "decision_digest": None,
        "fresh": False,
        "protocol_digest": client.PROTOCOL_DIGEST,
        "replay": False,
        "request_body_digest": digest(canonical_json(request)),
        "request_key": request["request_key"],
        "schema": "zekam-precompact-local-response/v1",
        "service_pid": 321,
        "service_start_token": "service-start",
        "service_uid": 501,
        "stdout_digest": client.STORAGE_FAILURE_STDOUT_DIGEST,
        "verified_census_digest": None,
    }


def _raw_request() -> dict[str, object]:
    body = _request()
    raw = {
        key: value for key, value in body.items() if key not in {"binding_digest", "delivery_id"}
    }
    raw["schema"] = "zekam-precompact-local-raw-request/v1"
    raw["cwd"] = "/private/tmp/project"
    raw["request_key"] = digest(
        {
            "schema": "zekam-precompact-local-raw-request-key/v1",
            "cwd": raw["cwd"],
            "event_wire_digest": raw["event_wire_digest"],
            "external_session_id": raw["external_session_id"],
            "trigger": raw["trigger"],
            "turn_id": raw["turn_id"],
        }
    )
    return raw


def _session_request() -> dict[str, object]:
    body = _raw_request()
    body.pop("trigger")
    body.pop("turn_id")
    body["schema"] = "zekam-session-start-local-raw-request/v1"
    body["source"] = "startup"
    raw_observation = body["event_observation"]
    assert isinstance(raw_observation, dict)
    observation = dict(raw_observation)
    observation.update(
        external_event_type="SessionStart",
        internal_event_type="SESSION_START",
        turn_id=None,
        trigger=None,
        source="startup",
    )
    body["event_observation"] = observation
    body["request_key"] = digest(
        {
            "schema": "zekam-session-start-local-request-key/v1",
            "cwd": body["cwd"],
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": body["external_session_id"],
            "source": body["source"],
        }
    )
    return body


@pytest.mark.parametrize(
    ("call", "value"),
    [
        ("token", 1),
        ("token", "bad\x7fvalue"),
        ("token", "\ud800"),
        ("digest", "SHA256:" + "a" * 64),
        ("digest", "sha256:" + "z" * 64),
        ("nonce", "A" * 64),
        ("nonce", "z" * 64),
        ("integer", True),
        ("integer", -1),
    ],
)
def test_client_scalar_validators_reject_ambiguous_values(call: str, value: object) -> None:
    with pytest.raises(ValidationFailed):
        if call == "token":
            client._token(value, "field")
        elif call == "digest":
            client._digest(value, "field")
        elif call == "nonce":
            client._nonce(value)
        else:
            client._int(value, 0, 10)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("protocol_digest", digest("wrong"), PolicyViolation),
        ("client_pid", True, ValidationFailed),
        ("deadline_monotonic_ns", 10_000_000_001, ValidationFailed),
        ("trigger", "scheduled", ValidationFailed),
        ("request_key", digest("wrong"), PolicyViolation),
    ],
)
def test_client_request_relations_fail_closed(
    field: str, value: object, error: type[Exception]
) -> None:
    body = _request()
    body[field] = value
    with pytest.raises(error):
        client._validate_request(body)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("protocol_digest", digest("wrong"), PolicyViolation),
        ("deadline_monotonic_ns", 1, ValidationFailed),
        ("external_session_id", "bad session", ValidationFailed),
        ("cwd", "relative", ValidationFailed),
        ("trigger", "scheduled", ValidationFailed),
        ("request_key", digest("wrong"), PolicyViolation),
    ],
)
def test_raw_request_selector_and_deadline_relations_fail_closed(
    field: str, value: object, error: type[Exception]
) -> None:
    body = _raw_request()
    body[field] = value
    with pytest.raises(error):
        client._validate_request(body)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("protocol_digest", digest("wrong"), PolicyViolation),
        ("deadline_monotonic_ns", 1, ValidationFailed),
        ("cwd", "relative", ValidationFailed),
        ("source", "other", ValidationFailed),
        ("request_key", digest("wrong"), PolicyViolation),
    ],
)
def test_session_request_selector_and_observation_relations_fail_closed(
    field: str, value: object, error: type[Exception]
) -> None:
    body = _session_request()
    body[field] = value
    with pytest.raises(error):
        client._validate_request(body)


def test_session_request_rejects_observation_shape_and_literal_drift() -> None:
    body = _session_request()
    body["event_observation"] = []
    with pytest.raises(ValidationFailed):
        client._validate_request(body)
    body = _session_request()
    raw_observation = body["event_observation"]
    assert isinstance(raw_observation, dict)
    observation = dict(raw_observation)
    observation["grants_authority"] = True
    body["event_observation"] = observation
    with pytest.raises(PolicyViolation):
        client._validate_request(body)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("classification", "unknown", ValidationFailed),
        ("fresh", 1, ValidationFailed),
        ("fresh", True, PolicyViolation),
        ("stdout_digest", digest("wrong"), PolicyViolation),
        ("decision_digest", digest("unexpected"), PolicyViolation),
    ],
)
def test_client_failure_response_cannot_gain_authority(
    field: str, value: object, error: type[Exception]
) -> None:
    body = _failure(_request())
    body[field] = value
    with pytest.raises(error):
        client._validate_response(body)


def test_session_response_requires_exact_classification_flags_and_stdout() -> None:
    request = _session_request()
    stdout = '{"hookSpecificOutput":{"additionalContext":"safe","hookEventName":"SessionStart"}}\n'
    response: dict[str, object] = {
        "attempt_nonce": NONCE,
        "attachment_revision_digest": digest("revision"),
        "classification": "hydrated",
        "hook_stdout": stdout,
        "hook_stdout_digest": digest_of_bytes(stdout.encode()),
        "hydration_receipt_digest": digest("hydration"),
        "manifest_digest": digest("manifest"),
        "protocol_digest": client.PROTOCOL_DIGEST,
        "replay": False,
        "request_body_digest": digest(canonical_json(request)),
        "request_key": request["request_key"],
        "schema": "zekam-session-start-local-response/v1",
        "service_pid": 321,
        "service_start_token": "service-start",
        "service_uid": 501,
    }
    client._validate_response(response)
    for field, value, error in (
        ("classification", "failed", PolicyViolation),
        ("replay", 0, ValidationFailed),
        ("hook_stdout", 1, ValidationFailed),
        ("hook_stdout_digest", digest("wrong"), PolicyViolation),
    ):
        changed = dict(response)
        changed[field] = value
        with pytest.raises(error):
            client._validate_response(changed)


def test_frame_codec_rejects_type_length_envelope_and_digest_drift() -> None:
    request = _request()
    with pytest.raises(ValidationFailed):
        client.encode_frame(request, response=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        client.decode_frame(b"1234", response=False)
    frame = client.encode_frame(request, response=False)
    with pytest.raises(ValidationFailed):
        client.decode_frame(struct.pack(">I", 0) + frame[4:], response=False)
    envelope = json.loads(frame[4:])
    envelope["body_digest"] = digest("wrong")
    encoded = client._canonical_bytes(envelope)
    with pytest.raises(PolicyViolation):
        client.decode_frame(struct.pack(">I", len(encoded)) + encoded, response=False)
    envelope.pop("schema")
    encoded = client._canonical_bytes(envelope)
    with pytest.raises(ValidationFailed):
        client.decode_frame(struct.pack(">I", len(encoded)) + encoded, response=False)


def test_exchange_timeout_and_early_eof_release_transport() -> None:
    left, right = socket.socketpair()
    try:
        with pytest.raises(TimeoutError):
            client._exchange(left, b"frame", deadline_ns=time.monotonic_ns() - 1)
    finally:
        left.close()
        right.close()
    left, right = socket.socketpair()
    right.close()
    try:
        with pytest.raises((BrokenPipeError, ConnectionError)):
            client._exchange(left, b"frame", deadline_ns=time.monotonic_ns() + 1_000_000_000)
    finally:
        left.close()


def _listener(**changes: object) -> supervisor._DarwinListenerObservation:
    values: dict[str, object] = {
        "path": "/private/tmp/socket",
        "fd": 1,
        "owner_uid": 501,
        "mode": stat.S_IFSOCK | 0o600,
        "device": 1,
        "inode": 2,
        "nlink": 1,
        "socket_type": int(socket.SOCK_STREAM),
    }
    values.update(changes)
    return supervisor._DarwinListenerObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"path": "relative"}, PolicyViolation),
        ({"path": "/private//socket"}, PolicyViolation),
        ({"fd": True}, ValidationFailed),
        ({"inode": 0}, ValidationFailed),
        ({"mode": stat.S_IFSOCK | 0o644}, PolicyViolation),
        ({"nlink": 2}, PolicyViolation),
        ({"socket_type": int(socket.SOCK_DGRAM)}, PolicyViolation),
    ],
)
def test_listener_observation_rejects_identity_drift(
    changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _listener(**changes)


def _job(**changes: object) -> supervisor._DarwinJobObservation:
    values: dict[str, object] = {
        "struct_version": 1,
        "reserved": b"\0" * 16,
        "label": supervisor.JOB_LABEL,
        "listener_key": supervisor.LISTENER_KEY,
        "service_pid": 123,
        "service_uid": 501,
        "service_start_token": "start",
        "service_artifact_digest": digest("artifact"),
        "protocol_digest": client.PROTOCOL_DIGEST,
        "listener": _listener(),
    }
    values.update(changes)
    return supervisor._DarwinJobObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"struct_version": 2}, PolicyViolation),
        ({"reserved": b""}, PolicyViolation),
        ({"label": "foreign"}, PolicyViolation),
        ({"listener_key": "foreign"}, PolicyViolation),
        ({"service_pid": 0}, ValidationFailed),
        ({"service_artifact_digest": "bad"}, ValidationFailed),
        ({"listener": object()}, ValidationFailed),
        ({"service_uid": 502}, PolicyViolation),
    ],
)
def test_job_observation_rejects_platform_drift(
    changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _job(**changes)


def test_peer_and_audit_token_validation_is_exact() -> None:
    raw = struct.pack("=8I", 0, 501, 0, 0, 0, 123, 0, 0)
    assert supervisor._DarwinAuditTokenParser.parse(raw)[:2] == (123, 501)
    with pytest.raises(ValidationFailed):
        supervisor._DarwinAuditTokenParser.parse(raw[:-1])
    with pytest.raises(ValidationFailed):
        supervisor._DarwinAuditTokenParser.parse(struct.pack("=8I", 0, 501, 0, 0, 0, 0, 0, 0))
    with pytest.raises(ValidationFailed):
        supervisor._DarwinPeerObservation(0, 501, "start", digest("audit"), digest("artifact"))
    with pytest.raises(ValidationFailed):
        supervisor._DarwinPeerObservation(1, 501, "start", "bad", digest("artifact"))


@pytest.mark.parametrize("field", ["job_absent", "listener_released", "service_exited"])
def test_quiescence_rejects_non_boolean_flags(field: str) -> None:
    values: dict[str, object] = {
        "job_absent": True,
        "listener_released": True,
        "service_exited": True,
        "connections_closed": True,
        "resource_handles_released": True,
        "durable_census": "complete",
    }
    values[field] = 1
    with pytest.raises(ValidationFailed):
        supervisor.LaunchdQuiescence(**values)  # type: ignore[arg-type]


def test_quiescence_requires_exact_census_and_all_resources() -> None:
    complete = supervisor.LaunchdQuiescence(True, True, True, True, True, "baseline")
    assert complete.permits_next_generation
    assert not replace(complete, connections_closed=False).permits_next_generation
    assert not replace(complete, durable_census="other").permits_next_generation
    with pytest.raises(ValidationFailed):
        replace(complete, durable_census="unknown")


def test_synthetic_value_validation_and_restart_census() -> None:
    with pytest.raises(PolicyViolation):
        supervisor.SyntheticSupervisorObservation(digest("r"), None, "codec-rejected", 1)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        supervisor.SyntheticSupervisorObservation(digest("r"), None, "other", True)
    observation = supervisor.SyntheticSupervisorObservation(
        digest("r"), None, "codec-rejected", True
    )
    with pytest.raises(TypeError):
        copy.deepcopy(observation)
    with pytest.raises(ValidationFailed):
        supervisor.SyntheticDurableOutcome(digest("r"), digest("c"), "other", 0, 0)
    with pytest.raises(PolicyViolation):
        supervisor.SyntheticDurableOutcome(digest("r"), digest("c"), "fixed-false", True, 0)
    model = supervisor.SyntheticCheckpointModel()
    with pytest.raises(ValidationFailed):
        model.execute(client.encode_frame(_request(), response=False), crash_stage="unknown")
    with pytest.raises(ValidationFailed):
        model.census(1)  # type: ignore[arg-type]
    initial = model.census(str(_request()["request_key"]))
    assert initial.classification == "fixed-false"


def test_synthetic_receiver_rejects_eof_cap_and_trailing_bytes() -> None:
    left, right = socket.socketpair()
    right.close()
    try:
        with pytest.raises(ConnectionError):
            supervisor._receive_one(left)
    finally:
        left.close()
    for payload, error in (
        (struct.pack(">I", client.MAX_FRAME_BYTES + 1), ValidationFailed),
        (struct.pack(">I", 2) + b"x", ConnectionError),
        (struct.pack(">I", 1) + b"xy", ValidationFailed),
    ):
        left, right = socket.socketpair()
        try:
            right.sendall(payload)
            right.shutdown(socket.SHUT_WR)
            with pytest.raises(error):
                supervisor._receive_one(left)
        finally:
            left.close()
            right.close()


def test_synthetic_listener_and_dormant_entry_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValidationFailed):
            supervisor.serve_synthetic_once(inet, b"frame")
    finally:
        inet.close()
    with pytest.raises(ValidationFailed):
        supervisor.serve_synthetic_once(object(), b"frame")  # type: ignore[arg-type]
    assert supervisor.production_service_entry() == os.EX_UNAVAILABLE
    with pytest.raises(ValidationFailed):
        supervisor._production_hook_round_trip([])  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        supervisor._production_hook_round_trip({})
    monkeypatch.setattr(supervisor, "canary_service_entry", lambda: 17)
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_NONCE", NONCE)
    assert supervisor.main() == 17
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_NONCE")
    assert supervisor.main() == os.EX_UNAVAILABLE


def test_supervisor_scalar_and_audit_socket_type_guards() -> None:
    with pytest.raises(ValidationFailed):
        supervisor._text(1, "field")
    with pytest.raises(ValidationFailed):
        supervisor._text("bad\x7fvalue", "field")
    with pytest.raises(ValidationFailed):
        supervisor._text("\ud800", "field")
    with pytest.raises(ValidationFailed):
        supervisor._exact_int(True, 0, 10, "coordinate")
    inet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValidationFailed):
            supervisor._peer_audit_from_socket(inet)
        with pytest.raises(ValidationFailed):
            supervisor._peer_identity_from_socket(inet)
    finally:
        inet.close()


def test_lifecycle_text_document_and_parser_failure_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationFailed):
        lifecycle._text(1, "field")
    with pytest.raises(ValidationFailed):
        lifecycle._text("bad\x80", "field")
    with pytest.raises(ValidationFailed):
        lifecycle._text("\ud800", "field")
    with pytest.raises(ValidationFailed):
        lifecycle._strict_document(b"[]")
    root = tmp_path / "root"
    root.mkdir()
    body = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(root),
        "hook_event_name": "SessionStart",
        "source": "resume",
    }
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(PolicyViolation, match="startup only"):
        lifecycle.parse_codex_macos_0151(
            json.dumps(body, separators=(",", ":")).encode(), expected_root=root
        )
    body["source"] = "startup"
    body["permission_mode"] = "invalid"
    with pytest.raises(ValidationFailed, match="permission mode"):
        lifecycle.parse_codex_macos_0151(
            json.dumps(body, separators=(",", ":")).encode(), expected_root=root
        )
    body["permission_mode"] = None
    body["cwd"] = str(tmp_path / "missing")
    with pytest.raises(PolicyViolation, match="source root unavailable"):
        lifecycle.parse_codex_macos_0151(
            json.dumps(body, separators=(",", ":")).encode(), expected_root=root
        )


def test_hook_failure_mapping_and_dormant_client_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert b"RECOVERY_REQUIRED" in client._failure_for("not-reviewed")
    assert b"SOURCE_DRIFT" in client._failure_for("SOURCE_DRIFT")
    assert client.production_precompaction_hook(b"not-json") == client.VALIDATION_FAILURE_STDOUT
    document = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/private/tmp/project",
        "hook_event_name": "PreCompact",
        "turn_id": "turn-1",
        "trigger": "manual",
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_NONCE", raising=False)
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_SOCKET", raising=False)
    assert client.production_precompaction_hook(raw) == client.STORAGE_FAILURE_STDOUT
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_NONCE", "bad")
    assert client.production_precompaction_hook(raw) == client.STORAGE_FAILURE_STDOUT
