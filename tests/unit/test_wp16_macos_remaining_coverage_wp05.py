from __future__ import annotations

import ctypes
import hashlib
import json
import os
import pickle
import platform
import socket
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.application import local_continuity_v4_compaction as compaction
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle
from zekam.infrastructure.clients import codex_macos_0151_precompaction_client as client

pytestmark = pytest.mark.unit
NONCE = "a" * 64


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(prefix="z-wp05-", dir="/private/tmp") as raw:
        yield Path(raw)


def _observation(*, session: bool = False) -> dict[str, object]:
    return {
        "schema": "zekam-codex-macos-0151-command-hook/v1",
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "session_id": "session-1",
        "external_event_type": "SessionStart" if session else "PreCompact",
        "internal_event_type": "SESSION_START" if session else "PRE_COMPACTION",
        "turn_id": None if session else "turn-1",
        "source": "startup" if session else None,
        "trigger": None if session else "manual",
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": None,
        "wire_digest": digest("wire"),
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
    }


def _request(*, raw: bool = False, session: bool = False) -> dict[str, object]:
    created = 10_000_000_000
    body: dict[str, object] = {
        "attempt_nonce": NONCE,
        "client_pid": 123,
        "client_start_token": "start-token",
        "client_uid": 501,
        "created_monotonic_ns": created,
        "deadline_monotonic_ns": created + client.TOTAL_DEADLINE_NS,
        "event_observation": _observation(session=session),
        "event_wire_digest": digest("wire"),
        "external_session_id": "session-1",
        "protocol_digest": client.PROTOCOL_DIGEST,
        "request_key": "",
        "schema": "zekam-precompact-local-request/v1",
    }
    if session:
        body.update(cwd="/private/tmp/project", source="startup")
        body["schema"] = "zekam-session-start-local-raw-request/v1"
        key_body = {
            "schema": "zekam-session-start-local-request-key/v1",
            "cwd": body["cwd"],
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": body["external_session_id"],
            "source": body["source"],
        }
    elif raw:
        body.update(cwd="/private/tmp/project", trigger="manual", turn_id="turn-1")
        body["schema"] = "zekam-precompact-local-raw-request/v1"
        key_body = {
            "schema": "zekam-precompact-local-raw-request-key/v1",
            "cwd": body["cwd"],
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": body["external_session_id"],
            "trigger": body["trigger"],
            "turn_id": body["turn_id"],
        }
    else:
        body.update(
            binding_digest=digest("binding"),
            delivery_id=digest("delivery"),
            trigger="manual",
            turn_id="turn-1",
        )
        key_body = {
            "schema": "zekam-precompact-local-request-key/v1",
            "binding_digest": body["binding_digest"],
            "delivery_id": body["delivery_id"],
            "event_wire_digest": body["event_wire_digest"],
            "external_session_id": body["external_session_id"],
            "trigger": body["trigger"],
            "turn_id": body["turn_id"],
        }
    body["request_key"] = digest(key_body)
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


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"a":1, "b":2}',
        b'{"a":1,"a":2}',
        b'{"v":NaN}',
        b"\xff",
        json.dumps({str(i): i for i in range(65)}, separators=(",", ":")).encode(),
    ],
)
def test_client_strict_json_rejects_all_noncanonical_shapes(raw: bytes) -> None:
    with pytest.raises(ValidationFailed):
        client._strict_json(raw)


@pytest.mark.parametrize("kind", ["normal", "raw", "session"])
def test_client_request_requires_exact_dictionary(kind: str) -> None:
    body = _request(raw=kind == "raw", session=kind == "session")
    changed = dict(body)
    changed["extra"] = None
    with pytest.raises(ValidationFailed):
        client._validate_request(changed)


@pytest.mark.parametrize(
    ("kind", "field", "value", "error"),
    [
        ("normal", "client_start_token", "", ValidationFailed),
        ("normal", "client_pid", 0, ValidationFailed),
        ("normal", "external_session_id", "", ValidationFailed),
        ("raw", "external_session_id", "bad session", ValidationFailed),
        ("raw", "turn_id", "", ValidationFailed),
        ("raw", "protocol_digest", digest("bad"), PolicyViolation),
        ("session", "external_session_id", "bad session", ValidationFailed),
        ("session", "cwd", "relative", ValidationFailed),
        ("session", "source", "other", ValidationFailed),
        ("session", "protocol_digest", digest("bad"), PolicyViolation),
    ],
)
def test_client_request_scalar_and_relation_boundaries(
    kind: str, field: str, value: object, error: type[Exception]
) -> None:
    body = _request(raw=kind == "raw", session=kind == "session")
    body[field] = value
    with pytest.raises(error):
        client._validate_request(body)


def test_client_session_observation_exact_shape_and_literals() -> None:
    body = _request(session=True)
    body["event_observation"] = []
    with pytest.raises(ValidationFailed):
        client._validate_request(body)
    for key, value in (("session_id", "other"), ("contains_prompt", True)):
        body = _request(session=True)
        raw_observation = body["event_observation"]
        assert isinstance(raw_observation, dict)
        observation = dict(raw_observation)
        observation[key] = value
        body["event_observation"] = observation
        with pytest.raises(PolicyViolation):
            client._validate_request(body)


def test_client_frame_codec_rejects_envelope_and_body_shapes() -> None:
    request = _request()
    frame = client.encode_frame(request, response=False)
    envelope = json.loads(frame[4:])
    for changed, error in (
        ({"body": []}, ValidationFailed),
        ({"body_bytes": 0}, ValidationFailed),
        ({"schema": "wrong"}, ValidationFailed),
        ({"body_digest": digest("wrong")}, PolicyViolation),
    ):
        candidate = dict(envelope)
        candidate.update(changed)
        encoded = client._canonical_bytes(candidate)
        with pytest.raises(error):
            client.decode_frame(struct.pack(">I", len(encoded)) + encoded, response=False)
    with pytest.raises(ValidationFailed):
        client.decode_frame(frame + b"x", response=False)


def test_client_response_shape_protocol_and_session_shape_rejections() -> None:
    with pytest.raises(ValidationFailed):
        client._validate_response({})
    response = _failure(_request())
    response["protocol_digest"] = digest("wrong")
    with pytest.raises(PolicyViolation):
        client._validate_response(response)
    session = _request(session=True)
    stdout = "ok"
    session_response: dict[str, object] = {
        "attempt_nonce": NONCE,
        "attachment_revision_digest": digest("revision"),
        "classification": "hydrated",
        "hook_stdout": stdout,
        "hook_stdout_digest": digest_of_bytes(stdout.encode()),
        "hydration_receipt_digest": digest("hydration"),
        "manifest_digest": digest("manifest"),
        "protocol_digest": client.PROTOCOL_DIGEST,
        "replay": False,
        "request_body_digest": digest(canonical_json(session)),
        "request_key": session["request_key"],
        "schema": "zekam-session-start-local-response/v1",
        "service_pid": 321,
        "service_start_token": "service",
        "service_uid": 501,
    }
    changed = dict(session_response, extra=None)
    with pytest.raises(ValidationFailed):
        client._validate_response(changed)


def test_client_observation_shape_and_selector_drift() -> None:
    body = _request()
    body["event_observation"] = []
    with pytest.raises(ValidationFailed):
        client._validate_request(body)
    body = _request()
    raw_observation = body["event_observation"]
    assert isinstance(raw_observation, dict)
    observation = dict(raw_observation)
    observation["session_id"] = "other"
    body["event_observation"] = observation
    with pytest.raises(PolicyViolation):
        client._validate_request(body)


def test_client_decode_rejects_nondict_body_after_valid_envelope_digest() -> None:
    body_bytes = b"[]"
    envelope = client._canonical_bytes(
        {
            "body": [],
            "body_bytes": len(body_bytes),
            "body_digest": client._body_digest(body_bytes),
            "schema": "zekam-precompact-local-supervisor-frame/v1",
        }
    )
    frame = struct.pack(">I", len(envelope)) + envelope
    with pytest.raises(ValidationFailed):
        client.decode_frame(frame, response=False)


def test_client_exchange_reads_fragmented_frame_and_rejects_trailing() -> None:
    request = _request()
    response = _failure(request)
    response_frame = client.encode_frame(response, response=True)
    left, right = socket.socketpair()

    def fragmented() -> None:
        try:
            right.recv(65536)
            for byte in response_frame:
                right.sendall(bytes([byte]))
            right.shutdown(socket.SHUT_WR)
        finally:
            right.close()

    thread = threading.Thread(target=fragmented)
    thread.start()
    try:
        assert (
            client._exchange(
                left,
                client.encode_frame(request, response=False),
                deadline_ns=time.monotonic_ns() + 2_000_000_000,
            )
            == response_frame
        )
    finally:
        left.close()
        thread.join()


def test_client_exchange_receive_timeout_eof_cap_and_delayed_trailing() -> None:
    request = _request()
    request_frame = client.encode_frame(request, response=False)
    response_frame = client.encode_frame(_failure(request), response=True)

    def run_server(action: str, peer: socket.socket) -> None:
        try:
            peer.recv(65536)
            if action == "eof":
                return
            if action == "cap":
                peer.sendall(struct.pack(">I", client.MAX_FRAME_BYTES + 1))
                return
            if action == "timeout":
                threading.Event().wait(0.05)
                return
            peer.sendall(response_frame)
            threading.Event().wait(0.01)
            peer.sendall(b"x")
        finally:
            peer.close()

    for action, error, budget in (
        ("eof", ConnectionError, 1_000_000_000),
        ("cap", ValidationFailed, 1_000_000_000),
        ("timeout", TimeoutError, 5_000_000),
        ("trailing", ValidationFailed, 1_000_000_000),
    ):
        left, right = socket.socketpair()
        thread = threading.Thread(target=run_server, args=(action, right))
        thread.start()
        try:
            with pytest.raises(error):
                client._exchange(
                    left,
                    request_frame,
                    deadline_ns=time.monotonic_ns() + budget,
                )
        finally:
            left.close()
            thread.join()


class _ClientSocket:
    def __enter__(self) -> _ClientSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, _value: float) -> None:
        return None

    def connect(self, _path: str) -> None:
        return None


def test_client_canary_exchange_requires_peer_identity_socket_stability_and_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(raw=True)
    frame = client.encode_frame(_failure(request), response=True)
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: _ClientSocket())
    monkeypatch.setattr(client, "_canary_socket_identity", lambda _path: (1, 2, 3))
    monkeypatch.setattr(client, "_exchange", lambda *_args, **_kwargs: frame)
    with pytest.raises(PolicyViolation, match="peer was not observed"):
        client.canary_exchange(Path("/private/tmp/x"), request, deadline_ns=10**30)

    monkeypatch.setattr(
        supervisor, "_peer_audit_from_socket", lambda _socket: (321, 501, digest("audit"))
    )

    def observed(*_args: object, **kwargs: object) -> bytes:
        cast(Callable[[], None], kwargs["peer_observer"])()
        return frame

    monkeypatch.setattr(client, "_exchange", observed)
    identities = iter(((1, 2, 3), (1, 9, 3)))
    monkeypatch.setattr(client, "_canary_socket_identity", lambda _path: next(identities))
    with pytest.raises(PolicyViolation, match="identity changed"):
        client.canary_exchange(Path("/private/tmp/x"), request, deadline_ns=10**30)

    monkeypatch.setattr(client, "_canary_socket_identity", lambda _path: (1, 2, 3))
    mismatched = dict(_failure(request), service_pid=999)
    mismatch_frame = client.encode_frame(mismatched, response=True)

    def mismatch(*_args: object, **kwargs: object) -> bytes:
        cast(Callable[[], None], kwargs["peer_observer"])()
        return mismatch_frame

    monkeypatch.setattr(client, "_exchange", mismatch)
    with pytest.raises(PolicyViolation, match="selector mismatch"):
        client.canary_exchange(Path("/private/tmp/x"), request, deadline_ns=10**30)


def test_client_socket_path_validation_rejects_relative_long_and_unsafe_ancestor(
    short_tmp: Path,
) -> None:
    with pytest.raises(ValidationFailed):
        client._canary_socket_identity(Path("relative.sock"))
    with pytest.raises(PolicyViolation):
        client._canary_socket_identity(Path("/private/tmp") / ("x" * 104))
    unsafe = short_tmp / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(PolicyViolation):
        client._canary_socket_identity(unsafe / "missing.sock")


def test_canary_socket_identity_positive_and_rejects_leaf_drift(short_tmp: Path) -> None:
    root = short_tmp / "private"
    root.mkdir(mode=0o700)
    path = root / "listener.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    try:
        device, inode, mode = client._canary_socket_identity(path)
        assert device > 0 and inode > 0 and stat.S_IMODE(mode) == 0o600
    finally:
        listener.close()
    path.unlink()
    path.write_text("not socket")
    with pytest.raises(PolicyViolation):
        client._canary_socket_identity(path)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b'{"a":1,"a":2}',
        b'{"v":NaN}',
        b"\xff",
        json.dumps({str(i): i for i in range(65)}).encode(),
        json.dumps([[[[[[[[[[[[[0]]]]]]]]]]]]]).encode(),
        json.dumps({"v": list(range(129))}).encode(),
        json.dumps({"v": "\ud800"}).encode(),
    ],
)
def test_lifecycle_strict_document_rejects_malformed_bounds(payload: bytes) -> None:
    with pytest.raises(ValidationFailed):
        lifecycle._strict_document(payload)


@pytest.mark.parametrize(
    ("values", "error"),
    [
        (("bad-code",), ValidationFailed),
        (("native-not-live", "native-not-live"), ValidationFailed),
        (("native-not-live", "native-pid"), None),
        (("native-pid", "native-not-live"), ValidationFailed),
        ((), ValidationFailed),
    ],
)
def test_lifecycle_live_error_requires_canonical_codes(
    values: tuple[str, ...], error: type[Exception] | None
) -> None:
    if error is None:
        result = lifecycle.LiveProcessVerificationError(values)
        assert result.codes == values
    else:
        with pytest.raises(error):
            lifecycle.LiveProcessVerificationError(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_session_id", "bad session"),
        ("event_type", "Stop"),
        ("source", "startup"),
        ("turn_id", None),
        ("trigger", "other"),
        ("permission_mode", "other"),
        ("wire_digest", 1),
    ],
)
def test_lifecycle_event_constructor_rejects_invalid_relations(field: str, value: object) -> None:
    values: dict[str, object] = {
        "external_session_id": "session-1",
        "event_type": "PreCompact",
        "source": None,
        "turn_id": "turn-1",
        "trigger": "manual",
        "permission_mode": None,
        "wire_digest": digest("wire"),
    }
    values[field] = value
    with pytest.raises((PolicyViolation, ValidationFailed)):
        lifecycle.CodexMacOS0151Event(**values)  # type: ignore[arg-type]


def test_lifecycle_parser_rejects_exact_key_and_selector_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    base: dict[str, object] = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(root),
        "hook_event_name": "PreCompact",
        "turn_id": "turn-1",
        "trigger": "manual",
    }
    for field, value, error in (
        ("hook_event_name", 1, ValidationFailed),
        ("hook_event_name", "Stop", PolicyViolation),
        ("session_id", "bad session", ValidationFailed),
        ("transcript_path", "bad\x7f", ValidationFailed),
        ("cwd", "relative", ValidationFailed),
        ("turn_id", "", ValidationFailed),
        ("trigger", "other", ValidationFailed),
    ):
        body = dict(base)
        body[field] = value
        with pytest.raises(error):
            lifecycle.parse_codex_macos_0151(
                json.dumps(body, separators=(",", ":")).encode(), expected_root=root
            )
    body = dict(base, extra=True)
    with pytest.raises(ValidationFailed):
        lifecycle.parse_codex_macos_0151(
            json.dumps(body, separators=(",", ":")).encode(), expected_root=root
        )


def test_lifecycle_parser_accepts_both_reviewed_event_shapes_and_rejects_root_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    session = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(root),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    event = lifecycle.parse_codex_macos_0151(
        json.dumps(session, separators=(",", ":")).encode(), expected_root=root
    )
    assert event.internal_event_type == "SESSION_START"
    compact = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": str(root),
        "hook_event_name": "PostCompact",
        "turn_id": "turn-2",
        "trigger": "auto",
    }
    assert (
        lifecycle.parse_codex_macos_0151(
            json.dumps(compact, separators=(",", ":")).encode(), expected_root=root
        ).internal_event_type
        == "POST_COMPACTION"
    )
    missing_transcript = dict(compact)
    missing_transcript.pop("transcript_path")
    with pytest.raises(ValidationFailed):
        lifecycle.parse_codex_macos_0151(
            json.dumps(missing_transcript, separators=(",", ":")).encode(), expected_root=root
        )
    compact["cwd"] = str(other)
    with pytest.raises(PolicyViolation):
        lifecycle.parse_codex_macos_0151(
            json.dumps(compact, separators=(",", ":")).encode(), expected_root=root
        )


def _artifact_fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    paths = tuple(root / f"artifact-{index}" for index in range(4))
    pins: list[tuple[str, int, str]] = []
    for index, path in enumerate(paths):
        raw = f"artifact-{index}".encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        pins.append((f"artifact-{index}", len(raw), hashlib.sha256(raw).hexdigest()))
    monkeypatch.setattr(lifecycle, "_ARTIFACT_PINS", tuple(pins))
    return paths  # type: ignore[return-value]


def test_lifecycle_artifact_pin_success_recheck_close_and_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    pins = lifecycle._PinnedArtifactSet(paths)
    assert len(pins.recheck()) == 4
    moved = paths[0].with_suffix(".old")
    paths[0].rename(moved)
    paths[0].write_bytes(b"replacement")
    paths[0].chmod(0o600)
    with pytest.raises(lifecycle.LiveProcessVerificationError):
        pins.recheck()
    pins.close()
    pins.close()
    with pytest.raises(PolicyViolation):
        pins.recheck()


def test_lifecycle_artifact_pin_rejects_shape_mode_digest_and_short_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailed):
        lifecycle._PinnedArtifactSet(paths[:3])  # type: ignore[arg-type]
    paths[0].chmod(0o622)
    with pytest.raises(PolicyViolation):
        lifecycle._PinnedArtifactSet(paths)
    paths[0].chmod(0o600)
    original = lifecycle._ARTIFACT_PINS
    altered = list(original)
    altered[0] = (altered[0][0], altered[0][1], "0" * 64)
    monkeypatch.setattr(lifecycle, "_ARTIFACT_PINS", tuple(altered))
    with pytest.raises(PolicyViolation):
        lifecycle._PinnedArtifactSet(paths)
    monkeypatch.setattr(lifecycle, "_ARTIFACT_PINS", original)
    monkeypatch.setattr(os, "pread", lambda *_args: b"")
    with pytest.raises(PolicyViolation, match="short read"):
        lifecycle._PinnedArtifactSet(paths)


def test_lifecycle_raw_digest_and_process_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact"
    path.write_bytes(b"content")
    assert lifecycle._raw_file_digest(path) == digest_of_bytes(b"content")
    with pytest.raises(ValidationFailed):
        lifecycle._raw_file_digest(path, deadline=object())  # type: ignore[arg-type]
    path.write_bytes(b"")
    with pytest.raises(PolicyViolation):
        lifecycle._raw_file_digest(path)
    monkeypatch.setattr(platform, "system", lambda: "Other")
    with pytest.raises(lifecycle.LiveProcessVerificationError) as raised:
        lifecycle._process_row(1)
    assert raised.value.codes == ("native-not-live",)


def _deadline() -> compaction.SealedPreCompactionDeadline:
    value = object.__new__(compaction.SealedPreCompactionDeadline)
    object.__setattr__(value, "_clock", lambda: 1)
    object.__setattr__(value, "_deadline_ns", 10**15)
    object.__setattr__(value, "_generation_digest", digest("generation"))
    object.__setattr__(value, "_seal", "wp05-deadline")
    compaction._DEADLINES["wp05-deadline"] = value
    compaction._PARITY["wp05-deadline"] = compaction._value_bytes(value)
    return value


def test_lifecycle_real_sealed_deadline_checks_each_artifact_read_stage(tmp_path: Path) -> None:
    path = tmp_path / "artifact"
    path.write_bytes(b"x" * (1_048_576 + 1))
    assert lifecycle._raw_file_digest(path, deadline=_deadline()) == digest_of_bytes(
        path.read_bytes()
    )


def test_lifecycle_pinned_manager_rechecks_deadline_and_peer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    pins = lifecycle._PinnedArtifactSet(paths)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    manager = lifecycle.TrustedCodex0151ProcessManager(pins)
    try:
        assert manager._artifacts(_deadline()) == pins.recheck()
        manager._peer_identity = (123, 501, "expected", digest("artifact"), digest("audit"))
        monkeypatch.setattr(
            lifecycle,
            "_process_row",
            lambda *_args, **_kwargs: (1, 501, "different", paths[3]),
        )
        monkeypatch.setattr(
            lifecycle, "_raw_file_digest", lambda *_args, **_kwargs: digest("artifact")
        )
        with pytest.raises(lifecycle.LiveProcessVerificationError):
            manager._hook_row(None)
    finally:
        pins.close()


def test_lifecycle_unpinned_manager_rejects_native_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(lifecycle, "_artifact_paths", lambda: paths)
    monkeypatch.setattr(lifecycle, "_raw_file_digest", lambda *_args, **_kwargs: digest("wrong"))
    manager = lifecycle.TrustedCodex0151ProcessManager()
    with pytest.raises(lifecycle.LiveProcessVerificationError) as raised:
        manager._artifacts()
    assert raised.value.codes == ("native-artifact",)


def test_lifecycle_peer_manager_rejects_live_tuple_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    pins = lifecycle._PinnedArtifactSet(paths)
    monkeypatch.setattr(
        lifecycle, "_process_row", lambda *_args, **_kwargs: (1, 999, "other", paths[3])
    )
    monkeypatch.setattr(lifecycle, "_artifact_paths", lambda: paths)
    monkeypatch.setattr(lifecycle, "_raw_file_digest", lambda _path: digest("artifact"))
    try:
        with pytest.raises(lifecycle.LiveProcessVerificationError):
            lifecycle._issue_peer_bound_process_manager(
                pins, (123, 501, "start", digest("artifact"), digest("audit"))
            )
    finally:
        pins.close()


def test_lifecycle_trusted_manager_rejects_unsealed_and_wrong_exact_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(ValidationFailed):
        lifecycle.TrustedCodex0151ProcessManager(object())  # type: ignore[arg-type]
    manager = lifecycle.TrustedCodex0151ProcessManager()
    with pytest.raises(ValidationFailed):
        manager.capture_process(object())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        manager.assert_process(object())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        manager.assert_invocation(object())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        manager._capture_invocation(
            cast(Any, object()),
            {},
            digest("spool"),
            "now",
            digest("gen"),
            "now",
            digest("receipt"),
            cast(Any, object()),
            digest("ancestry"),
            event_type="bad",
            deadline=None,
        )
    unsealed = object.__new__(lifecycle.TrustedCodex0151ProcessManager)
    with pytest.raises(PolicyViolation):
        unsealed.capture_process(object())  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        unsealed.assert_process(object())  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        unsealed.assert_invocation(object())  # type: ignore[arg-type]


def test_lifecycle_peer_manager_input_validation_avoids_native_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _artifact_fixture(tmp_path, monkeypatch)
    pins = lifecycle._PinnedArtifactSet(paths)
    try:
        with pytest.raises(ValidationFailed):
            lifecycle._issue_peer_bound_process_manager(pins, (1, 2))  # type: ignore[arg-type]
        with pytest.raises(ValidationFailed):
            lifecycle._issue_peer_bound_process_manager(
                pins, (1, 2, "start", "bad", digest("audit"))
            )
    finally:
        pins.close()


def test_client_raw_request_builders_and_hook_dispatch_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle, "_process_row", lambda *_args, **_kwargs: (1, 501, "s", Path("/x"))
    )
    precompact: dict[str, object] = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/private/tmp/project",
        "hook_event_name": "PreCompact",
        "turn_id": "turn-1",
        "trigger": "manual",
    }
    raw = client._raw_canary_request(precompact, NONCE)
    assert raw["schema"] == "zekam-precompact-local-raw-request/v1"
    session: dict[str, object] = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/private/tmp/project",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": None,
        "permission_mode": None,
    }
    start = client._raw_session_start_request(session, NONCE)
    assert start["schema"] == "zekam-session-start-local-raw-request/v1"
    with pytest.raises(ValidationFailed):
        client._raw_canary_request(dict(precompact, trigger="wrong"), NONCE)
    with pytest.raises(ValidationFailed):
        client._raw_session_start_request(dict(session, source="wrong"), NONCE)

    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_NONCE", NONCE)
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_SOCKET", "/private/tmp/fake.sock")
    monkeypatch.setattr(client, "canary_exchange", lambda *_args, **_kwargs: _failure(raw))
    wire = json.dumps(precompact, separators=(",", ":")).encode()
    assert b"STORAGE_UNAVAILABLE" in client.production_precompaction_hook(wire)
    monkeypatch.setattr(
        client,
        "canary_exchange",
        lambda *_args, **_kwargs: dict(_failure(raw), classification="checkpoint-ready"),
    )
    assert client.production_precompaction_hook(wire) == client.SUCCESS_STDOUT


def test_client_session_hook_missing_environment_and_response_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/private/tmp/project",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": None,
        "permission_mode": None,
    }
    wire = json.dumps(document, separators=(",", ":")).encode()
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_NONCE", raising=False)
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_SOCKET", raising=False)
    failure = client.production_session_start_hook(wire)
    assert b'"continue":false' in failure
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_NONCE", NONCE)
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_SOCKET", "/private/tmp/fake.sock")
    monkeypatch.setattr(
        lifecycle, "_process_row", lambda *_args, **_kwargs: (1, 501, "s", Path("/x"))
    )
    monkeypatch.setattr(client, "canary_exchange", lambda *_args, **_kwargs: {"hook_stdout": 1})
    assert client.production_session_start_hook(wire) == failure
    monkeypatch.setattr(
        client,
        "canary_exchange",
        lambda *_args, **_kwargs: {"hook_stdout": '{"continue":true}\n'},
    )
    assert client.production_session_start_hook(wire) == b'{"continue":true}\n'


def test_client_production_runner_and_true_flag_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supervisor,
        "_production_hook_round_trip",
        lambda _document: {"classification": "SOURCE_DRIFT"},
    )
    runner = client._ProductionPreCompactionClient()
    assert b"SOURCE_DRIFT" in runner.run({})
    monkeypatch.setattr(
        supervisor,
        "_production_hook_round_trip",
        lambda _document: {"classification": "checkpoint-ready"},
    )
    assert runner.run({}) == client.SUCCESS_STDOUT
    monkeypatch.setattr(client, "DARWIN_LAUNCHD_CAPABILITY_OBSERVED", True)
    monkeypatch.setattr(client, "PRODUCTION_GENERATION_ISSUED", True)
    monkeypatch.setattr(client._ProductionPreCompactionClient, "run", lambda _self, _doc: b"ok")
    document = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/private/tmp/project",
        "hook_event_name": "PreCompact",
        "turn_id": "turn-1",
        "trigger": "manual",
    }
    assert (
        client.production_precompaction_hook(json.dumps(document, separators=(",", ":")).encode())
        == b"ok"
    )


class _FakeLaunchFunction:
    def __init__(self, *, status: int = 0, count: int = 0) -> None:
        self.status = status
        self.count = count
        self.argtypes: object = None
        self.restype: object = None
        self._array = (ctypes.c_int * max(1, count))(*range(max(1, count)))

    def __call__(self, _key: object, values: object, count: object) -> int:
        count._obj.value = self.count  # type: ignore[attr-defined]
        pointer = ctypes.cast(self._array, ctypes.POINTER(ctypes.c_int))
        ctypes.cast(cast(Any, values), ctypes.POINTER(ctypes.POINTER(ctypes.c_int)))[0] = pointer
        return self.status


class _FakeFree:
    argtypes: object = None
    restype: object = None

    def __call__(self, _value: object) -> None:
        return None


class _FakeLibSystem:
    def __init__(self, function: _FakeLaunchFunction | None) -> None:
        if function is not None:
            self.launch_activate_socket = function
        self.free = _FakeFree()


@pytest.mark.parametrize(
    ("status", "count", "error"),
    [(1, 0, OSError), (0, 17, PolicyViolation), (0, 0, PolicyViolation)],
)
def test_supervisor_launch_activate_socket_fail_closed_without_live_launchd(
    status: int, count: int, error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeLibSystem(_FakeLaunchFunction(status=status, count=count))
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)
    with pytest.raises(error):
        supervisor._DarwinAuthorityAdapter._launch_activate_socket()


def test_supervisor_launch_activate_socket_requires_exact_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: _FakeLibSystem(None))
    with pytest.raises(PolicyViolation):
        supervisor._DarwinAuthorityAdapter._launch_activate_socket()


def test_supervisor_launch_activate_rejects_negative_descriptor_and_closes_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLibSystem(_FakeLaunchFunction(count=1))
    fake.launch_activate_socket._array[0] = -1
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)
    with pytest.raises(PolicyViolation):
        supervisor._DarwinAuthorityAdapter._launch_activate_socket()


def test_supervisor_peer_identity_relations_with_pure_socket_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair()
    try:
        monkeypatch.setattr(
            supervisor, "_peer_audit_from_socket", lambda _socket: (123, 501, digest("audit"))
        )
        monkeypatch.setattr(
            supervisor,
            "_process_row",
            lambda *_args, **_kwargs: (1, 502, "start", Path("/private/tmp/runtime")),
        )
        with pytest.raises(PolicyViolation, match="UID"):
            supervisor._peer_identity_from_socket(left)
        monkeypatch.setattr(
            supervisor,
            "_process_row",
            lambda *_args, **_kwargs: (1, 501, "start", Path("/private/tmp/runtime")),
        )
        monkeypatch.setattr(supervisor, "_raw_file_digest", lambda _path: digest("runtime"))
        assert supervisor._peer_identity_from_socket(left) == (
            123,
            501,
            "start",
            digest("runtime"),
            digest("audit"),
        )
    finally:
        left.close()
        right.close()


def test_supervisor_canary_activation_selector_and_root_guards(short_tmp: Path) -> None:
    with pytest.raises(ValidationFailed):
        supervisor._issue_canary_activation("z" * 64, "wrong", str(short_tmp / "x"))
    with pytest.raises(PolicyViolation):
        supervisor._issue_canary_activation(NONCE, "wrong", str(short_tmp / "x"))
    unsafe = short_tmp / "unsafe"
    unsafe.mkdir(mode=0o755)
    label = f"io.zekam.precompaction-canary.{NONCE}"
    with pytest.raises(PolicyViolation, match="root identity"):
        supervisor._issue_canary_activation(NONCE, label, str(unsafe / "x.sock"))


def test_supervisor_canary_consumption_generation_type_and_replay() -> None:
    activation = object.__new__(supervisor._CanaryActivation)
    object.__setattr__(activation, "_seal", "seal")
    object.__setattr__(activation, "_nonce", NONCE)
    object.__setattr__(activation, "_generation", object())
    supervisor._CANARY_ACTIVATIONS["seal"] = activation
    with pytest.raises(PolicyViolation, match="generation unavailable"):
        supervisor._consume_canary(activation)
    with pytest.raises(PolicyViolation, match="already consumed"):
        supervisor._consume_canary(activation)


def test_supervisor_response_wrong_result_and_generation_drift() -> None:
    generation = _stale_generation()

    class DriftAdapter:
        def observe_current(self) -> supervisor._DarwinJobObservation:
            return _job(service_pid=124)

    object.__setattr__(generation, "_adapter", DriftAdapter())
    with pytest.raises(PolicyViolation):
        generation._recheck("response")


def test_supervisor_timeout_status_empty_and_true_production_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationFailed):
        supervisor.serve_canary_once(object(), timeout_seconds=0.0)
    monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_STATUS", raising=False)
    supervisor._write_canary_status("ignored")
    monkeypatch.setattr(supervisor, "DARWIN_LAUNCHD_CAPABILITY_OBSERVED", True)
    monkeypatch.setattr(supervisor, "PRODUCTION_GENERATION_ISSUED", True)
    generation = _stale_generation()
    monkeypatch.setattr(
        supervisor._DarwinAuthorityAdapter, "acquire", classmethod(lambda _cls: generation)
    )

    class Service:
        def __init__(self, _generation: object) -> None:
            pass

        def serve_once(self) -> int:
            return 23

    monkeypatch.setattr(supervisor, "_ProductionService", Service)
    assert supervisor.production_service_entry() == 23
    with pytest.raises(PolicyViolation, match="activation unavailable"):
        supervisor._production_hook_round_trip({})
    with pytest.raises(PolicyViolation, match="flags changed"):
        supervisor.assert_synthetic_cannot_promote(
            supervisor.SyntheticSupervisorObservation(digest("r"), None, "codec-rejected", True)
        )


def _stale_generation() -> supervisor._DarwinGenerationOwner:
    generation = object.__new__(supervisor._DarwinGenerationOwner)
    object.__setattr__(generation, "_seal", "stale")
    object.__setattr__(generation, "_digest", digest("generation"))
    object.__setattr__(generation, "_job", _job())
    return generation


def test_supervisor_server_composition_and_response_types_reject_before_mutation(
    tmp_path: Path,
) -> None:
    generation = _stale_generation()
    request = _request(raw=True)
    with pytest.raises(PolicyViolation):
        supervisor._resolved_precompaction(
            generation,
            request,
            (1, 1, "s", digest("a"), digest("b")),
            tmp_path / "bad.db",
            tmp_path,
        )
    with pytest.raises(PolicyViolation):
        supervisor._allocate_and_hydrate_session(
            generation,
            _request(session=True),
            (1, 1, "s", digest("a"), digest("b")),
            tmp_path / "bad.db",
            tmp_path,
            tmp_path / "plan",
        )
    with pytest.raises(PolicyViolation):
        supervisor._session_response_body(generation, _request(session=True), object())


def test_supervisor_copy_and_dormant_assertion_boundaries() -> None:
    safe = supervisor.SyntheticSupervisorObservation(digest("r"), None, "codec-rejected", True)
    supervisor.assert_synthetic_cannot_promote(safe)
    with pytest.raises(PolicyViolation):
        supervisor.assert_synthetic_cannot_promote(object())


def _listener(**changes: object) -> supervisor._DarwinListenerObservation:
    values: dict[str, object] = {
        "path": "/private/tmp/listener.sock",
        "fd": 3,
        "owner_uid": os.geteuid(),
        "mode": stat.S_IFSOCK | 0o600,
        "device": 1,
        "inode": 2,
        "nlink": 1,
        "socket_type": int(socket.SOCK_STREAM),
    }
    values.update(changes)
    return supervisor._DarwinListenerObservation(**values)  # type: ignore[arg-type]


def _job(**changes: object) -> supervisor._DarwinJobObservation:
    values: dict[str, object] = {
        "struct_version": 1,
        "reserved": b"\0" * 16,
        "label": supervisor.JOB_LABEL,
        "listener_key": supervisor.LISTENER_KEY,
        "service_pid": 123,
        "service_uid": os.geteuid(),
        "service_start_token": "start",
        "service_artifact_digest": digest("artifact"),
        "protocol_digest": client.PROTOCOL_DIGEST,
        "listener": _listener(),
    }
    values.update(changes)
    return supervisor._DarwinJobObservation(**values)  # type: ignore[arg-type]


def test_supervisor_listener_observation_uses_live_temporary_socket(short_tmp: Path) -> None:
    root = short_tmp / "secure"
    root.mkdir(mode=0o700)
    path = root / "listener.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    try:
        observed = supervisor._listener_observation_from_fd(
            str(path), listener.fileno(), os.geteuid()
        )
        assert observed.inode == path.lstat().st_ino
        assert os.get_inheritable(listener.fileno()) is False
        assert os.get_blocking(listener.fileno()) is False
    finally:
        listener.close()


def test_supervisor_listener_observation_rejects_path_and_descriptor_drift(
    short_tmp: Path,
) -> None:
    root = short_tmp / "secure"
    root.mkdir(mode=0o700)
    leaf = root / "leaf"
    leaf.write_text("x")
    descriptor = os.open(leaf, os.O_RDONLY)
    try:
        with pytest.raises(PolicyViolation):
            supervisor._listener_observation_from_fd(str(leaf), descriptor, os.geteuid())
        with pytest.raises(PolicyViolation):
            supervisor._listener_observation_from_fd("relative", descriptor, os.geteuid())
    finally:
        os.close(descriptor)


def test_supervisor_listener_observation_rejects_symlink_and_writable_ancestor(
    short_tmp: Path,
) -> None:
    secure = short_tmp / "secure"
    secure.mkdir(mode=0o700)
    target = secure / "target"
    target.mkdir(mode=0o700)
    linked = secure / "linked"
    linked.symlink_to(target, target_is_directory=True)
    writable = secure / "writable"
    writable.mkdir(mode=0o777)
    writable.chmod(0o777)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = target / "listener.sock"
    listener.bind(str(path))
    os.chmod(path, 0o600)
    try:
        with pytest.raises(PolicyViolation, match="ancestor identity"):
            supervisor._listener_observation_from_fd(
                str(linked / "listener.sock"), listener.fileno(), os.geteuid()
            )
        with pytest.raises(PolicyViolation, match="ancestor ownership"):
            supervisor._listener_observation_from_fd(
                str(writable / "listener.sock"), listener.fileno(), os.geteuid()
            )
    finally:
        listener.close()


def test_supervisor_peer_audit_rejects_invalid_types_and_pid_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = struct.pack("=8I", 0, 501, 0, 0, 0, 123, 0, 0)

    class FakeSocket:
        family = socket.AF_UNIX

        def __init__(self, *, invalid_type: bool = False) -> None:
            self.invalid_type = invalid_type

        def getsockopt(self, _level: int, option: int, *_args: int) -> object:
            if option == supervisor.LOCAL_PEERPID:
                return "bad" if self.invalid_type else 124
            return raw

    monkeypatch.setattr(socket, "socket", FakeSocket)
    with pytest.raises(PolicyViolation, match="identity invalid"):
        supervisor._peer_audit_from_socket(FakeSocket(invalid_type=True))  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation, match="PID"):
        supervisor._peer_audit_from_socket(FakeSocket())  # type: ignore[arg-type]


def test_supervisor_generation_and_canary_values_are_unconstructible() -> None:
    with pytest.raises(PolicyViolation):
        supervisor._DarwinGenerationOwner()
    with pytest.raises(PolicyViolation):
        supervisor._DarwinAuthorityAdapter()
    with pytest.raises(PolicyViolation):
        supervisor._CanaryActivation()
    generation = object.__new__(supervisor._DarwinGenerationOwner)
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(generation)
    activation = object.__new__(supervisor._CanaryActivation)
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(activation)
    with pytest.raises(PolicyViolation):
        supervisor._consume_canary(object())


def test_supervisor_generation_validation_rejects_stage_stale_and_observation_drift() -> None:
    generation = object.__new__(supervisor._DarwinGenerationOwner)
    object.__setattr__(generation, "_seal", "missing")
    object.__setattr__(generation, "_digest", digest("generation"))
    object.__setattr__(generation, "_job", _job())
    adapter = object.__new__(supervisor._DarwinAuthorityAdapter)
    object.__setattr__(adapter, "_expected", _job(service_pid=124))
    object.__setattr__(generation, "_adapter", adapter)
    with pytest.raises(ValidationFailed):
        generation._recheck("wrong")
    with pytest.raises(PolicyViolation):
        supervisor._generation_digest_if_current(generation)
    with pytest.raises(PolicyViolation):
        supervisor._generation_digest_if_current(object())
    blank = object.__new__(supervisor._DarwinAuthorityAdapter)
    with pytest.raises(PolicyViolation):
        blank.observe_current()


def test_supervisor_dormant_authority_and_service_paths_fail_closed() -> None:
    with pytest.raises(PolicyViolation):
        supervisor._DarwinAuthorityAdapter.acquire()
    assert supervisor.production_service_entry() == os.EX_UNAVAILABLE
    with pytest.raises(PolicyViolation):
        supervisor._ProductionService(object())  # type: ignore[arg-type]


def test_supervisor_session_plan_rejects_identity_schema_paths_and_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan.json"
    base: dict[str, object] = {
        "device_id": "device",
        "opened_at": "2026-09-04T00:00:00+00:00",
        "plan_digest": digest("plan"),
        "policy_digest": digest("policy"),
        "project_id": "project",
        "realm_id": "realm",
        "run_id": "run",
        "schema": "zekam-session-start-allocation-plan/v1",
        "source_paths": ["a.md"],
        "source_snapshot_id": "snapshot",
        "task_digest": digest("task"),
        "work_item_id": "work",
    }

    def write(value: dict[str, object]) -> None:
        path.write_bytes(client._canonical_bytes(value))
        path.chmod(0o600)

    write(base)
    assert supervisor._session_plan(path)["project_id"] == "project"
    path.chmod(0o644)
    with pytest.raises(PolicyViolation):
        supervisor._session_plan(path)
    for field, value, error in (
        ("schema", "wrong", ValidationFailed),
        ("source_paths", "a.md", ValidationFailed),
        ("source_paths", [], ValidationFailed),
        ("source_paths", ["b", "a"], ValidationFailed),
        ("opened_at", "bad", ValidationFailed),
        ("opened_at", "2026-09-04T00:00:00.1+00:00", ValidationFailed),
    ):
        changed = dict(base)
        changed[field] = value
        write(changed)
        with pytest.raises(error):
            supervisor._session_plan(path)


def test_supervisor_status_writer_is_bounded_and_ignores_unsafe_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "status"
    root.mkdir(mode=0o700)
    target = root / "status.json"
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_STATUS", str(target))
    supervisor._write_canary_status("ok")
    assert json.loads(target.read_text())["status"] == "ok"
    target.unlink()
    root.chmod(0o755)
    supervisor._write_canary_status("ignored")
    assert not target.exists()
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_STATUS", "relative/status.json")
    supervisor._write_canary_status("ignored")


def test_supervisor_synthetic_replay_and_crash_boundaries() -> None:
    frame = client.encode_frame(_request(), response=False)
    model = supervisor.SyntheticCheckpointModel()
    with pytest.raises(supervisor.SyntheticCrash):
        model.execute(frame, crash_stage="before-spool")
    assert model.census(str(_request()["request_key"])).spool_count == 0
    with pytest.raises(supervisor.SyntheticCrash):
        model.execute(frame, crash_stage="after-spool")
    assert model.census(str(_request()["request_key"])).spool_count == 1
    with pytest.raises(supervisor.SyntheticCrash):
        model.execute(frame, crash_stage="after-commit")
    assert model.execute(frame).classification == "replay-graph"


def test_supervisor_synthetic_exchange_rejects_all_bindings() -> None:
    request = _request()
    response = _failure(request)
    request_frame = client.encode_frame(request, response=False)
    response_frame = client.encode_frame(response, response=True)
    observed = supervisor.observe_synthetic_exchange(request_frame, response_frame)
    assert observed.classification == "response-verified"
    for key, value in (
        ("attempt_nonce", "b" * 64),
        ("request_key", digest("other")),
        ("request_body_digest", digest("other")),
        ("classification", "checkpoint-ready"),
    ):
        changed = dict(response)
        changed[key] = value
        if key == "classification":
            changed.update(
                fresh=True,
                stdout_digest=client.SUCCESS_STDOUT_DIGEST,
                decision_body={},
                decision_digest=digest("d"),
                verified_census_digest=digest("c"),
            )
            # The production response validator rejects the forged success first.
            with pytest.raises((PolicyViolation, ValidationFailed)):
                client.encode_frame(changed, response=True)
            continue
        frame = client.encode_frame(changed, response=True)
        with pytest.raises(PolicyViolation):
            supervisor.observe_synthetic_exchange(request_frame, frame)


def test_supervisor_synthetic_listener_round_trip(short_tmp: Path) -> None:
    path = short_tmp / "synthetic.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    request = _request()
    response = _failure(request)
    request_frame = client.encode_frame(request, response=False)
    response_frame = client.encode_frame(response, response=True)
    result: list[supervisor.SyntheticSupervisorObservation] = []

    def server() -> None:
        result.append(supervisor.serve_synthetic_once(listener, response_frame))

    thread = threading.Thread(target=server)
    thread.start()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(path))
        connection.sendall(request_frame)
        connection.shutdown(socket.SHUT_WR)
        assert (
            client.decode_frame(connection.recv(65536), response=True)["request_key"]
            == request["request_key"]
        )
    finally:
        connection.close()
        thread.join()
        listener.close()
    assert result[0].protocol_verified
