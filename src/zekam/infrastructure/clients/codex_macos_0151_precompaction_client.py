"""Bounded canonical IPC for the dormant Codex 0.151 PreCompact client."""

# ruff: noqa: SIM905 -- compact exact vocabularies keep the fixed file cap.

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import selectors
import socket
import stat
import struct
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, final

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_FRAME_BYTES: Final = 16_384
TOTAL_DEADLINE_NS: Final = 8_000_000_000
LISTENER_KEY: Final = "PreCompactionListener"
DARWIN_LAUNCHD_CAPABILITY_OBSERVED: Final = False
PRODUCTION_GENERATION_ISSUED: Final = False
_DOMAIN: Final = b"zekam/precompact/local-supervisor-ipc/v1"
PROTOCOL_DIGEST: Final = digest(
    {
        "schema": "zekam-precompact-local-supervisor-protocol/v1",
        "frame_cap": MAX_FRAME_BYTES,
        "deadline_ns": TOTAL_DEADLINE_NS,
        "transport": "unix-stream-one-request-one-response",
    }
)
VALIDATION_FAILURE_STDOUT: Final = (
    b'{"continue":false,"stopReason":"ZEKAM_PRECOMPACT_VALIDATION","suppressOutput":true}\n'
)
STORAGE_FAILURE_STDOUT: Final = (
    b'{"continue":false,"stopReason":"ZEKAM_PRECOMPACT_STORAGE_UNAVAILABLE",'
    b'"suppressOutput":true}\n'
)
SUCCESS_STDOUT: Final = b'{"continue":true,"suppressOutput":true}\n'
SUCCESS_STDOUT_DIGEST: Final = (
    "sha256:83b0c2d644685886e897a47420a509055cd62bdc37be550ee96b839cdb1028be"
)
VALIDATION_FAILURE_STDOUT_DIGEST: Final = (
    "sha256:4f457656b5dfc945f1e6b4833972769b2d8dd9e61618250f35a508b699ecf10a"
)
STORAGE_FAILURE_STDOUT_DIGEST: Final = (
    "sha256:3b01faabf2a42ad37043f33139185f7861b9d44f074ad96955ff7e49ffefc249"
)
_STDOUT_DIGESTS: Final = {
    "checkpoint-ready": SUCCESS_STDOUT_DIGEST,
    "VALIDATION": VALIDATION_FAILURE_STDOUT_DIGEST,
    "PENDING_WORK": "sha256:7da104c915c9941c5d5f2c11eb62709bf90acc4918cba5c981b913de952d73f4",
    "UNPERSISTED_DELTA": "sha256:e56dd61ea0bd60e4463a2a8f6120b6dd85b28cd7ccc81da2a288cd6d3db03d52",
    "SOURCE_DRIFT": "sha256:3e0b9f4a0facf15c7ad972883e740b69b94689094005841b6b3dc4568b40ac36",
    "PROCESS_DRIFT": "sha256:be2f21f1fc18b648c1f988641403bcdb1404778bef960e93d0f24a07ca29dc9b",
    "STORAGE_UNAVAILABLE": STORAGE_FAILURE_STDOUT_DIGEST,
    "RECOVERY_REQUIRED": "sha256:25bcb1e379b1feaf8c9053f1290f94610e7d9f0f16174c09f7b02dda3ef3ec59",
    "DEADLINE": "sha256:b3976fb81a91cce9150352d3c8a40cdbca166ea7420afc7e8407bbf32646c7bb",
}
_REQUEST_KEYS: Final = frozenset(
    (
        "attempt_nonce binding_digest client_pid client_start_token client_uid "
        "created_monotonic_ns deadline_monotonic_ns delivery_id event_observation "
        "event_wire_digest external_session_id protocol_digest request_key schema trigger turn_id"
    ).split()
)
_RAW_REQUEST_KEYS: Final = frozenset(
    (
        "attempt_nonce client_pid client_start_token client_uid created_monotonic_ns cwd "
        "deadline_monotonic_ns event_observation event_wire_digest external_session_id "
        "protocol_digest request_key schema trigger turn_id"
    ).split()
)
_SESSION_REQUEST_KEYS: Final = frozenset(
    (
        "attempt_nonce client_pid client_start_token client_uid created_monotonic_ns cwd "
        "deadline_monotonic_ns event_observation event_wire_digest external_session_id "
        "protocol_digest request_key schema source"
    ).split()
)
_SESSION: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RESPONSE_KEYS: Final = frozenset(
    (
        "attempt_nonce classification decision_body decision_digest fresh protocol_digest replay "
        "request_body_digest request_key schema service_pid service_start_token service_uid "
        "stdout_digest verified_census_digest"
    ).split()
)
_SESSION_RESPONSE_KEYS: Final = frozenset(
    (
        "attempt_nonce attachment_revision_digest classification hook_stdout "
        "hook_stdout_digest hydration_receipt_digest manifest_digest protocol_digest replay "
        "request_body_digest request_key schema service_pid service_start_token service_uid"
    ).split()
)
_OBSERVATION_KEYS: Final = frozenset(
    (
        "schema client_id client_kind client_version session_id external_event_type "
        "internal_event_type turn_id source trigger reason stop_hook_active permission_mode "
        "wire_digest contains_prompt contains_response contains_transcript grants_authority"
    ).split()
)
_FAILURES: Final = frozenset(_STDOUT_DIGESTS) - {"checkpoint-ready"}
_ACK_KEYS: Final = frozenset(
    (
        "active_hydration_receipt_digest active_manifest_digest ancestry_receipt_digest "
        "approval_inherited attachment_id binding_digest checkpoint_digest "
        "checkpoint_requested_event_digest client_id delivery_id device_id "
        "durable_reopen_verified external_session_id full_spool_tuple_digest grants_authority "
        "hydrated_predecessor_revision_digest internal_receipt_digest native_ack_observed "
        "native_receipt_digest pre_compact_committed_revision_digest pre_compaction_event_digest "
        "process_generation_digest schema session_id source_revision source_snapshot_digest "
        "source_snapshot_id spool_entry_digest success_stdout_digest"
    ).split()
)


def _strict_json(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_FRAME_BYTES:
        raise ValidationFailed("PreCompact IPC bounded bytes required")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        if len(values) > 64:
            raise ValidationFailed("PreCompact IPC object member bound exceeded")
        for key, value in values:
            if type(key) is not str or key in result:
                raise ValidationFailed("PreCompact IPC duplicate/nontext key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationFailed("PreCompact IPC invalid JSON") from exc
    if type(value) is not dict or _canonical_bytes(value) != raw:
        raise ValidationFailed("PreCompact IPC noncanonical object")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _body_digest(body_bytes: bytes) -> str:
    value = hashlib.sha256(_DOMAIN + b"\0" + struct.pack(">Q", len(body_bytes)) + body_bytes)
    return f"sha256:{value.hexdigest()}"


def _token(value: object, name: str, maximum: int = 512) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"PreCompact {name} string required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed(f"PreCompact {name} UTF-8 required") from exc
    if not 1 <= len(encoded) <= maximum or any(
        ord(char) < 32 or ord(char) == 127 or 128 <= ord(char) <= 159 for char in value
    ):
        raise ValidationFailed(f"PreCompact {name} outside bound")
    return value


def _digest(value: object, name: str) -> str:
    text = _token(value, name, 71)
    if len(text) != 71 or not text.startswith("sha256:") or text != text.lower():
        raise ValidationFailed(f"PreCompact {name} digest required")
    try:
        bytes.fromhex(text[7:])
    except ValueError as exc:
        raise ValidationFailed(f"PreCompact {name} digest required") from exc
    return text


def _nonce(value: object) -> str:
    text = _token(value, "nonce", 64)
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise ValidationFailed("PreCompact exact nonce required") from exc
    if len(decoded) != 32 or text != text.lower():
        raise ValidationFailed("PreCompact exact nonce required")
    return text


def _int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationFailed("PreCompact exact integer coordinate required")
    return value


def _validate_observation(value: object, body: Mapping[str, object]) -> None:
    if type(value) is not dict or set(value) != _OBSERVATION_KEYS:
        raise ValidationFailed("PreCompact exact sanitized observation required")
    exact = {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "external_event_type": "PreCompact",
        "internal_event_type": "PRE_COMPACTION",
        "source": None,
        "reason": None,
        "permission_mode": None,
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
        "stop_hook_active": False,
    }
    if any(
        value[name] != expected or type(value[name]) is not type(expected)
        for name, expected in exact.items()
    ):
        raise PolicyViolation("PreCompact sanitized observation literal drift")
    for left, right in (
        ("session_id", "external_session_id"),
        ("turn_id", "turn_id"),
        ("trigger", "trigger"),
        ("wire_digest", "event_wire_digest"),
    ):
        if value[left] != body[right]:
            raise PolicyViolation("PreCompact sanitized observation selector drift")


def _validate_request(body: Mapping[str, object]) -> None:
    if body.get("schema") == "zekam-session-start-local-raw-request/v1":
        _validate_session_request(body)
        return
    if body.get("schema") == "zekam-precompact-local-raw-request/v1":
        _validate_raw_request(body)
        return
    if (
        type(body) is not dict
        or set(body) != _REQUEST_KEYS
        or body.get("schema") != "zekam-precompact-local-request/v1"
    ):
        raise ValidationFailed("PreCompact exact request body required")
    _nonce(body["attempt_nonce"])
    for name in (
        "binding_digest",
        "delivery_id",
        "event_wire_digest",
        "protocol_digest",
        "request_key",
    ):
        _digest(body[name], name)
    if body["protocol_digest"] != PROTOCOL_DIGEST:
        raise PolicyViolation("PreCompact protocol digest mismatch")
    _int(body["client_pid"], 1, 2_147_483_647)
    _int(body["client_uid"], 0, 2_147_483_647)
    created = _int(body["created_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    deadline = _int(body["deadline_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    if deadline != created + TOTAL_DEADLINE_NS:
        raise ValidationFailed("PreCompact exact deadline relation required")
    _token(body["client_start_token"], "client start")
    _token(body["external_session_id"], "session", 128)
    _token(body["turn_id"], "turn")
    if body["trigger"] not in {"manual", "auto"}:
        raise ValidationFailed("PreCompact exact trigger required")
    _validate_observation(body["event_observation"], body)
    expected = digest(
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
    if body["request_key"] != expected:
        raise PolicyViolation("PreCompact request key relation invalid")


def _validate_raw_request(body: Mapping[str, object]) -> None:
    if type(body) is not dict or set(body) != _RAW_REQUEST_KEYS:
        raise ValidationFailed("PreCompact exact raw request body required")
    _nonce(body["attempt_nonce"])
    for name in ("event_wire_digest", "protocol_digest", "request_key"):
        _digest(body[name], name)
    if body["protocol_digest"] != PROTOCOL_DIGEST:
        raise PolicyViolation("PreCompact raw protocol digest mismatch")
    _int(body["client_pid"], 1, 2_147_483_647)
    _int(body["client_uid"], 0, 2_147_483_647)
    created = _int(body["created_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    deadline = _int(body["deadline_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    if deadline != created + TOTAL_DEADLINE_NS:
        raise ValidationFailed("PreCompact exact raw deadline relation required")
    _token(body["client_start_token"], "client start")
    session = _token(body["external_session_id"], "session", 128)
    if _SESSION.fullmatch(session) is None:
        raise ValidationFailed("PreCompact raw session selector invalid")
    cwd = _token(body["cwd"], "cwd", 4096)
    if not Path(cwd).is_absolute():
        raise ValidationFailed("PreCompact raw cwd must be absolute")
    _token(body["turn_id"], "turn")
    if body["trigger"] not in {"manual", "auto"}:
        raise ValidationFailed("PreCompact exact raw trigger required")
    _validate_observation(body["event_observation"], body)
    expected = digest(
        {
            "schema": "zekam-precompact-local-raw-request-key/v1",
            "cwd": cwd,
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": session,
            "trigger": body["trigger"],
            "turn_id": body["turn_id"],
        }
    )
    if body["request_key"] != expected:
        raise PolicyViolation("PreCompact raw request key relation invalid")


def _validate_session_request(body: Mapping[str, object]) -> None:
    if type(body) is not dict or set(body) != _SESSION_REQUEST_KEYS:
        raise ValidationFailed("SessionStart exact raw request required")
    _nonce(body["attempt_nonce"])
    for name in ("event_wire_digest", "protocol_digest", "request_key"):
        _digest(body[name], name)
    if body["protocol_digest"] != PROTOCOL_DIGEST:
        raise PolicyViolation("SessionStart protocol digest mismatch")
    _int(body["client_pid"], 1, 2_147_483_647)
    _int(body["client_uid"], 0, 2_147_483_647)
    created = _int(body["created_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    deadline = _int(body["deadline_monotonic_ns"], 1, 9_223_372_036_854_775_807)
    if deadline != created + TOTAL_DEADLINE_NS:
        raise ValidationFailed("SessionStart exact deadline relation required")
    _token(body["client_start_token"], "client start")
    session = _token(body["external_session_id"], "session", 128)
    cwd = _token(body["cwd"], "cwd", 4096)
    source = _token(body["source"], "source", 32)
    if _SESSION.fullmatch(session) is None or not Path(cwd).is_absolute():
        raise ValidationFailed("SessionStart selector invalid")
    if source not in {"startup", "resume", "compact"}:
        raise ValidationFailed("SessionStart source invalid")
    observation = body["event_observation"]
    if type(observation) is not dict or set(observation) != _OBSERVATION_KEYS:
        raise ValidationFailed("SessionStart exact observation required")
    exact = {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "external_event_type": "SessionStart",
        "internal_event_type": "SESSION_START",
        "turn_id": None,
        "source": source,
        "trigger": None,
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
        "session_id": session,
        "wire_digest": body["event_wire_digest"],
    }
    if any(
        observation[name] != value or type(observation[name]) is not type(value)
        for name, value in exact.items()
    ):
        raise PolicyViolation("SessionStart observation relation invalid")
    expected = digest(
        {
            "schema": "zekam-session-start-local-request-key/v1",
            "cwd": cwd,
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": session,
            "source": source,
        }
    )
    if body["request_key"] != expected:
        raise PolicyViolation("SessionStart request key relation invalid")


def _validate_decision(value: object) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != _ACK_KEYS
        or value.get("schema") != "zekam-precompaction-ack-decision/v1"
    ):
        raise ValidationFailed("PreCompact exact ACK decision body required")
    for name, expected in (
        ("durable_reopen_verified", True),
        ("native_ack_observed", False),
        ("grants_authority", False),
        ("approval_inherited", False),
    ):
        if value[name] is not expected:
            raise PolicyViolation("PreCompact ACK authority relation invalid")
    if value["success_stdout_digest"] != SUCCESS_STDOUT_DIGEST:
        raise PolicyViolation("PreCompact ACK stdout relation invalid")
    nondigests = {
        "schema",
        "durable_reopen_verified",
        "native_ack_observed",
        "grants_authority",
        "approval_inherited",
        "session_id",
        "external_session_id",
        "client_id",
        "device_id",
        "attachment_id",
        "source_snapshot_id",
        "source_revision",
    }
    for name in _ACK_KEYS - nondigests:
        _digest(value[name], name)
    return value


def _validate_response(body: Mapping[str, object]) -> None:
    if body.get("schema") == "zekam-session-start-local-response/v1":
        _validate_session_response(body)
        return
    if (
        type(body) is not dict
        or set(body) != _RESPONSE_KEYS
        or body.get("schema") != "zekam-precompact-local-response/v1"
    ):
        raise ValidationFailed("PreCompact exact response body required")
    _nonce(body["attempt_nonce"])
    for name in ("protocol_digest", "request_body_digest", "request_key", "stdout_digest"):
        _digest(body[name], name)
    if body["protocol_digest"] != PROTOCOL_DIGEST:
        raise PolicyViolation("PreCompact response protocol mismatch")
    _int(body["service_pid"], 1, 2_147_483_647)
    _int(body["service_uid"], 0, 2_147_483_647)
    _token(body["service_start_token"], "service start")
    if type(body["fresh"]) is not bool or type(body["replay"]) is not bool:
        raise ValidationFailed("PreCompact exact response flags required")
    success = body["classification"] == "checkpoint-ready"
    if not success and body["classification"] not in _FAILURES:
        raise ValidationFailed("PreCompact response classification invalid")
    if success != (bool(body["fresh"]) ^ bool(body["replay"])):
        raise PolicyViolation("PreCompact fresh XOR replay relation invalid")
    if body["stdout_digest"] != _STDOUT_DIGESTS[body["classification"]]:
        raise PolicyViolation("PreCompact stdout/classification drift")
    if not success:
        if any(
            body[name] is not None
            for name in ("decision_body", "decision_digest", "verified_census_digest")
        ):
            raise PolicyViolation("PreCompact failure response carries authority")
        return
    decision = _validate_decision(body["decision_body"])
    _digest(body["decision_digest"], "decision digest")
    _digest(body["verified_census_digest"], "verified census")
    if digest(decision) != body["decision_digest"]:
        raise PolicyViolation("PreCompact decision digest relation invalid")
    expected = digest(
        {
            "schema": "zekam-precompact-verified-census/v1",
            "decision_digest": body["decision_digest"],
            "checkpoint_digest": decision["checkpoint_digest"],
            "attachment_revision_digest": decision["pre_compact_committed_revision_digest"],
        }
    )
    if body["verified_census_digest"] != expected:
        raise PolicyViolation("PreCompact verified census relation invalid")


def _validate_session_response(body: Mapping[str, object]) -> None:
    if type(body) is not dict or set(body) != _SESSION_RESPONSE_KEYS:
        raise ValidationFailed("SessionStart exact response required")
    _nonce(body["attempt_nonce"])
    for name in (
        "protocol_digest",
        "request_body_digest",
        "request_key",
        "hook_stdout_digest",
        "attachment_revision_digest",
        "hydration_receipt_digest",
        "manifest_digest",
    ):
        _digest(body[name], name)
    if body["protocol_digest"] != PROTOCOL_DIGEST or body["classification"] != "hydrated":
        raise PolicyViolation("SessionStart response classification invalid")
    _int(body["service_pid"], 1, 2_147_483_647)
    _int(body["service_uid"], 0, 2_147_483_647)
    _token(body["service_start_token"], "service start")
    if type(body["replay"]) is not bool or type(body["hook_stdout"]) is not str:
        raise ValidationFailed("SessionStart response payload invalid")
    encoded = body["hook_stdout"].encode("utf-8")
    if not 1 <= len(encoded) <= 32_847 or digest_of_bytes(encoded) != body["hook_stdout_digest"]:
        raise PolicyViolation("SessionStart response stdout relation invalid")


def encode_frame(body: dict[str, object], *, response: bool) -> bytes:
    if type(body) is not dict or type(response) is not bool:
        raise ValidationFailed("PreCompact exact frame inputs required")
    (_validate_response if response else _validate_request)(body)
    body_bytes = _canonical_bytes(body)
    envelope = _canonical_bytes(
        {
            "body": body,
            "body_digest": _body_digest(body_bytes),
            "schema": "zekam-precompact-local-supervisor-envelope/v1",
        }
    )
    if not 1 <= len(envelope) <= MAX_FRAME_BYTES:
        raise ValidationFailed("PreCompact frame outside bound")
    return struct.pack(">I", len(envelope)) + envelope


def decode_frame(frame: bytes, *, response: bool) -> dict[str, object]:
    if type(frame) is not bytes or len(frame) < 5:
        raise ValidationFailed("PreCompact framed bytes required")
    size = struct.unpack(">I", frame[:4])[0]
    if not 1 <= size <= MAX_FRAME_BYTES or len(frame) != size + 4:
        raise ValidationFailed("PreCompact frame length mismatch")
    envelope = _strict_json(frame[4:])
    if (
        set(envelope) != {"body", "body_digest", "schema"}
        or envelope["schema"] != "zekam-precompact-local-supervisor-envelope/v1"
    ):
        raise ValidationFailed("PreCompact exact envelope required")
    body = envelope["body"]
    if type(body) is not dict:
        raise ValidationFailed("PreCompact exact body required")
    expected = _body_digest(_canonical_bytes(body))
    if type(envelope["body_digest"]) is not str or not hmac.compare_digest(
        envelope["body_digest"], expected
    ):
        raise PolicyViolation("PreCompact envelope digest mismatch")
    (_validate_response if response else _validate_request)(body)
    return body


def _exchange(
    sock: socket.socket,
    frame: bytes,
    *,
    deadline_ns: int,
    peer_observer: Callable[[], None] | None = None,
) -> bytes:
    if type(sock) is not socket.socket or type(deadline_ns) is not int:
        raise ValidationFailed("PreCompact exact transport required")
    selector = selectors.DefaultSelector()
    received = bytearray()
    try:
        sock.setblocking(False)
        selector.register(sock, selectors.EVENT_WRITE)
        sent = 0
        while sent < len(frame):
            remaining = deadline_ns - time.monotonic_ns()
            if remaining <= 0 or not selector.select(remaining / 1e9):
                raise TimeoutError
            sent += sock.send(frame[sent:])
        sock.shutdown(socket.SHUT_WR)
        selector.modify(sock, selectors.EVENT_READ)
        needed: int | None = None
        peer_observed = False
        while needed is None or len(received) < needed + 4:
            remaining = deadline_ns - time.monotonic_ns()
            if remaining <= 0 or not selector.select(remaining / 1e9):
                raise TimeoutError
            if peer_observer is not None and not peer_observed:
                peer_observer()
                peer_observed = True
            chunk = sock.recv(min(4096, MAX_FRAME_BYTES + 4 - len(received)))
            if not chunk:
                raise ConnectionError
            received.extend(chunk)
            if needed is None and len(received) >= 4:
                needed = struct.unpack(">I", received[:4])[0]
                if not 1 <= needed <= MAX_FRAME_BYTES:
                    raise ValidationFailed("PreCompact response cap exceeded")
        if needed is None or len(received) != needed + 4:
            raise ValidationFailed("PreCompact trailing response frame")
        while True:
            remaining = deadline_ns - time.monotonic_ns()
            if remaining <= 0 or not selector.select(remaining / 1e9):
                raise TimeoutError
            trailing = sock.recv(1)
            if trailing:
                raise ValidationFailed("PreCompact trailing response frame")
            return bytes(received)
    finally:
        selector.close()


def _canary_socket_identity(path: Path) -> tuple[int, int, int]:
    if type(path) is not type(Path()) or not path.is_absolute():
        raise ValidationFailed("PreCompact exact canary socket path required")
    encoded = os.fsencode(path)
    if len(encoded) > 103 or b".." in encoded.split(b"/"):
        raise PolicyViolation("PreCompact canary socket path invalid")
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        item = current.lstat()
        if (
            not stat.S_ISDIR(item.st_mode)
            or stat.S_ISLNK(item.st_mode)
            or item.st_uid not in {0, os.geteuid()}
            or (item.st_mode & 0o022 and not (item.st_uid == 0 and item.st_mode & stat.S_ISVTX))
        ):
            raise PolicyViolation("PreCompact canary socket ancestor invalid")
    item = path.lstat()
    if (
        not stat.S_ISSOCK(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
    ):
        raise PolicyViolation("PreCompact canary socket identity invalid")
    return (item.st_dev, item.st_ino, item.st_mode)


def canary_exchange(
    socket_path: Path,
    request_body: dict[str, object],
    *,
    deadline_ns: int,
) -> dict[str, object]:
    """Perform one exact canary exchange; this cannot enable the production hook."""
    _validate_request(request_body)
    before = _canary_socket_identity(socket_path)
    frame = encode_frame(request_body, response=False)
    service: tuple[int, int, str] | None = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(0.001, (deadline_ns - time.monotonic_ns()) / 1e9))
        connection.connect(str(socket_path))
        from zekam.infrastructure.macos_precompaction_supervisor import (
            _peer_audit_from_socket,
        )

        def observe_peer() -> None:
            nonlocal service
            service = _peer_audit_from_socket(connection)

        response = decode_frame(
            _exchange(
                connection,
                frame,
                deadline_ns=deadline_ns,
                peer_observer=observe_peer,
            ),
            response=True,
        )
    if service is None:
        raise PolicyViolation("PreCompact canary service peer was not observed")
    if _canary_socket_identity(socket_path) != before:
        raise PolicyViolation("PreCompact canary socket identity changed")
    if (
        response["attempt_nonce"] != request_body["attempt_nonce"]
        or response["request_key"] != request_body["request_key"]
        or response["request_body_digest"] != digest(canonical_json(request_body))
        or response["service_pid"] != service[0]
        or response["service_uid"] != service[1]
    ):
        raise PolicyViolation("PreCompact canary response selector mismatch")
    return response


def _raw_canary_request(document: dict[str, object], nonce: str) -> dict[str, object]:
    required = {"session_id", "transcript_path", "cwd", "hook_event_name", "turn_id", "trigger"}
    if (
        type(document) is not dict
        or not required.issubset(document)
        or not set(document).issubset(required | {"model"})
        or document.get("hook_event_name") != "PreCompact"
        or type(document.get("session_id")) is not str
        or _SESSION.fullmatch(str(document["session_id"])) is None
        or (
            document.get("transcript_path") is not None
            and type(document["transcript_path"]) is not str
        )
        or type(document.get("cwd")) is not str
        or not Path(str(document["cwd"])).is_absolute()
        or type(document.get("turn_id")) is not str
        or document.get("trigger") not in {"manual", "auto"}
        or (document.get("model") is not None and type(document["model"]) is not str)
    ):
        raise ValidationFailed("PreCompact exact raw hook selector required")
    _nonce(nonce)
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import _process_row

    _parent, uid, start, _executable = _process_row(os.getpid(), timeout=1.0)
    wire = {
        "session_id": document["session_id"],
        "hook_event_name": "PreCompact",
        "turn_id": document["turn_id"],
        "source": None,
        "trigger": document["trigger"],
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
    }
    wire_digest = digest(wire)
    observation = {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "session_id": document["session_id"],
        "external_event_type": "PreCompact",
        "internal_event_type": "PRE_COMPACTION",
        "turn_id": document["turn_id"],
        "source": None,
        "trigger": document["trigger"],
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
        "wire_digest": wire_digest,
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
    }
    created = time.monotonic_ns()
    request: dict[str, object] = {
        "attempt_nonce": nonce,
        "client_pid": os.getpid(),
        "client_start_token": start,
        "client_uid": uid,
        "created_monotonic_ns": created,
        "cwd": document["cwd"],
        "deadline_monotonic_ns": created + TOTAL_DEADLINE_NS,
        "event_observation": observation,
        "event_wire_digest": wire_digest,
        "external_session_id": document["session_id"],
        "protocol_digest": PROTOCOL_DIGEST,
        "request_key": "",
        "schema": "zekam-precompact-local-raw-request/v1",
        "trigger": document["trigger"],
        "turn_id": document["turn_id"],
    }
    request["request_key"] = digest(
        {
            "schema": "zekam-precompact-local-raw-request-key/v1",
            "cwd": request["cwd"],
            "event_wire_digest": wire_digest,
            "external_session_id": request["external_session_id"],
            "trigger": request["trigger"],
            "turn_id": request["turn_id"],
        }
    )
    _validate_raw_request(request)
    return request


def _raw_session_start_request(document: dict[str, object], nonce: str) -> dict[str, object]:
    required = {
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "source",
        "model",
        "permission_mode",
    }
    if (
        type(document) is not dict
        or not required.issubset(document)
        or set(document) != required
        or document.get("hook_event_name") != "SessionStart"
        or type(document.get("session_id")) is not str
        or type(document.get("cwd")) is not str
        or document.get("source") not in {"startup", "resume", "compact"}
        or (
            document.get("transcript_path") is not None
            and type(document["transcript_path"]) is not str
        )
        or (document.get("model") is not None and type(document["model"]) is not str)
        or (
            document.get("permission_mode") is not None
            and type(document["permission_mode"]) is not str
        )
    ):
        raise ValidationFailed("SessionStart exact raw hook selector required")
    _nonce(nonce)
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import _process_row

    _parent, uid, start, _executable = _process_row(os.getpid(), timeout=1.0)
    wire = {
        "session_id": document["session_id"],
        "hook_event_name": "SessionStart",
        "turn_id": None,
        "source": document["source"],
        "trigger": None,
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": document["permission_mode"],
    }
    wire_digest = digest(wire)
    observation = {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "session_id": document["session_id"],
        "external_event_type": "SessionStart",
        "internal_event_type": "SESSION_START",
        "turn_id": None,
        "source": document["source"],
        "trigger": None,
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
        "wire_digest": wire_digest,
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
    }
    created = time.monotonic_ns()
    request: dict[str, object] = {
        "attempt_nonce": nonce,
        "client_pid": os.getpid(),
        "client_start_token": start,
        "client_uid": uid,
        "created_monotonic_ns": created,
        "cwd": document["cwd"],
        "deadline_monotonic_ns": created + TOTAL_DEADLINE_NS,
        "event_observation": observation,
        "event_wire_digest": wire_digest,
        "external_session_id": document["session_id"],
        "protocol_digest": PROTOCOL_DIGEST,
        "request_key": "",
        "schema": "zekam-session-start-local-raw-request/v1",
        "source": document["source"],
    }
    request["request_key"] = digest(
        {
            "schema": "zekam-session-start-local-request-key/v1",
            "cwd": request["cwd"],
            "event_wire_digest": wire_digest,
            "external_session_id": request["external_session_id"],
            "source": request["source"],
        }
    )
    _validate_session_request(request)
    return request


def production_session_start_hook(raw_payload: bytes) -> bytes:
    """Canary-only SessionStart dispatcher; no allocator authority is accepted client-side."""
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
        _strict_document,
        handled_failure_output,
    )

    failure = handled_failure_output(recovery_required=False)
    try:
        document = _strict_json(_canonical_bytes(_strict_document(raw_payload)))
        nonce = os.environ.get("ZEKAM_PRECOMPACT_CANARY_NONCE", "")
        socket_path = os.environ.get("ZEKAM_PRECOMPACT_CANARY_SOCKET", "")
        if not nonce or not socket_path:
            return failure
        request = _raw_session_start_request(document, nonce)
        deadline = request["deadline_monotonic_ns"]
        if type(deadline) is not int:
            return failure
        response = canary_exchange(Path(socket_path), request, deadline_ns=deadline)
        output = response["hook_stdout"]
        if type(output) is not str:
            return failure
        return output.encode("utf-8")
    except Exception:
        return failure


@final
class _ProductionPreCompactionClient:
    __slots__ = ()

    def run(self, document: dict[str, object]) -> bytes:
        from zekam.infrastructure.macos_precompaction_supervisor import _production_hook_round_trip

        response = _production_hook_round_trip(document)
        if response["classification"] != "checkpoint-ready":
            return _failure_for(str(response["classification"]))
        return SUCCESS_STDOUT


def _failure_for(category: str) -> bytes:
    if category not in _FAILURES:
        category = "RECOVERY_REQUIRED"
    return (
        _canonical_bytes(
            {
                "continue": False,
                "stopReason": f"ZEKAM_PRECOMPACT_{category}",
                "suppressOutput": True,
            }
        )
        + b"\n"
    )


def production_precompaction_hook(raw_payload: bytes) -> bytes:
    """Strict hook entry; normal failures are fixed while process-control escapes."""
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import _strict_document

    try:
        document = _strict_json(_canonical_bytes(_strict_document(raw_payload)))
        required = {"session_id", "transcript_path", "cwd", "hook_event_name", "turn_id", "trigger"}
        if (
            not required.issubset(document)
            or not set(document).issubset(required | {"model"})
            or document["hook_event_name"] != "PreCompact"
            or type(document["session_id"]) is not str
            or document["transcript_path"] is not None
            or type(document["cwd"]) is not str
            or type(document["turn_id"]) is not str
            or document["trigger"] not in {"manual", "auto"}
            or (document.get("model") is not None and type(document["model"]) is not str)
        ):
            raise ValidationFailed("PreCompact exact hook document required")
    except Exception:
        return VALIDATION_FAILURE_STDOUT
    nonce = os.environ.get("ZEKAM_PRECOMPACT_CANARY_NONCE", "")
    socket_path = os.environ.get("ZEKAM_PRECOMPACT_CANARY_SOCKET", "")
    if nonce or socket_path:
        try:
            request = _raw_canary_request(document, nonce)
            deadline = request["deadline_monotonic_ns"]
            if type(deadline) is not int:
                raise ValidationFailed("PreCompact exact raw deadline required")
            response = canary_exchange(
                Path(socket_path),
                request,
                deadline_ns=deadline,
            )
            if response["classification"] == "checkpoint-ready":
                return SUCCESS_STDOUT
            return _failure_for(str(response["classification"]))
        except Exception:
            return STORAGE_FAILURE_STDOUT
    if not DARWIN_LAUNCHD_CAPABILITY_OBSERVED or not PRODUCTION_GENERATION_ISSUED:
        return STORAGE_FAILURE_STDOUT
    try:
        return _ProductionPreCompactionClient().run(document)
    except Exception:
        return STORAGE_FAILURE_STDOUT
