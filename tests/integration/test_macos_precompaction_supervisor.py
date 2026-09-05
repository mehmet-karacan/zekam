from __future__ import annotations

import copy
import ctypes
import fcntl
import inspect
import os
import pickle
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle
from zekam.infrastructure.clients import codex_macos_0151_precompaction_client as client


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
        "attempt_nonce": "a" * 64,
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
        "request_key": digest("pending"),
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


def _live_request(nonce: str) -> dict[str, object]:
    body = _request()
    _parent, uid, start, _executable = lifecycle._process_row(os.getpid(), timeout=1.0)
    created = time.monotonic_ns()
    body.update(
        attempt_nonce=nonce,
        client_pid=os.getpid(),
        client_start_token=start,
        client_uid=uid,
        created_monotonic_ns=created,
        deadline_monotonic_ns=created + client.TOTAL_DEADLINE_NS,
    )
    return body


def _canary(
    root: Path, listener: socket.socket, nonce: str, monkeypatch: pytest.MonkeyPatch
) -> supervisor._CanaryActivation:
    socket_path = root / "canary.sock"
    listener.bind(str(socket_path))
    listener.listen(1)
    socket_path.chmod(0o600)
    monkeypatch.setattr(
        supervisor._DarwinAuthorityAdapter,
        "_launch_activate_socket",
        staticmethod(lambda _key=supervisor.LISTENER_KEY: (os.dup(listener.fileno()),)),
    )
    return supervisor._issue_canary_activation(
        nonce, f"io.zekam.precompaction-canary.{nonce}", str(socket_path)
    )


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


def _ack() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-precompaction-ack-decision/v1",
        "session_id": "session-internal",
        "external_session_id": "session-1",
        "client_id": "codex",
        "device_id": "device-1",
        "attachment_id": "attachment-1",
        "source_snapshot_id": "snapshot-1",
        "source_revision": "a" * 40,
        "durable_reopen_verified": True,
        "native_ack_observed": False,
        "grants_authority": False,
        "approval_inherited": False,
    }
    for name in [
        "binding_digest",
        "process_generation_digest",
        "hydrated_predecessor_revision_digest",
        "delivery_id",
        "spool_entry_digest",
        "full_spool_tuple_digest",
        "ancestry_receipt_digest",
        "native_receipt_digest",
        "internal_receipt_digest",
        "checkpoint_requested_event_digest",
        "pre_compaction_event_digest",
        "checkpoint_digest",
        "pre_compact_committed_revision_digest",
        "source_snapshot_digest",
        "active_manifest_digest",
        "active_hydration_receipt_digest",
        "success_stdout_digest",
    ]:
        body[name] = (
            client.SUCCESS_STDOUT_DIGEST if name == "success_stdout_digest" else digest(name)
        )
    return body


def _success(request: dict[str, object]) -> dict[str, object]:
    decision = _ack()
    decision_digest = digest(decision)
    return {
        "attempt_nonce": request["attempt_nonce"],
        "classification": "checkpoint-ready",
        "decision_body": decision,
        "decision_digest": decision_digest,
        "fresh": True,
        "protocol_digest": client.PROTOCOL_DIGEST,
        "replay": False,
        "request_body_digest": digest(canonical_json(request)),
        "request_key": request["request_key"],
        "schema": "zekam-precompact-local-response/v1",
        "service_pid": 321,
        "service_start_token": "service-start",
        "service_uid": 501,
        "stdout_digest": client.SUCCESS_STDOUT_DIGEST,
        "verified_census_digest": digest(
            {
                "schema": "zekam-precompact-verified-census/v1",
                "decision_digest": decision_digest,
                "checkpoint_digest": decision["checkpoint_digest"],
                "attachment_revision_digest": decision["pre_compact_committed_revision_digest"],
            }
        ),
    }


def test_codec_round_trip_and_synthetic_is_disjoint() -> None:
    request = _request()
    request_frame = client.encode_frame(request, response=False)
    response_frame = client.encode_frame(_failure(request), response=True)
    assert client.decode_frame(request_frame, response=False) == request
    observed = supervisor.observe_synthetic_exchange(request_frame, response_frame)
    assert observed.protocol_verified and observed.grants_authority is False
    with pytest.raises(TypeError):
        copy.copy(observed)
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(observed)
    with pytest.raises(PolicyViolation):
        supervisor.observe_synthetic_exchange(
            request_frame, client.encode_frame(_success(request), response=True)
        )


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"\xff",
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":"\\ud800"}',
        b'{"x":1 }',
        b"{}",
        b"x" * 16_385,
    ),
)
def test_parser_duplicate_key_noncanonical_invalid_utf8_surrogate_is_fixed_validation(
    raw: bytes,
) -> None:
    assert client.production_precompaction_hook(raw) == client.VALIDATION_FAILURE_STDOUT


def test_valid_hook_fails_before_clock_socket_or_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = canonical_json(
        {
            "cwd": "/tmp",
            "hook_event_name": "PreCompact",
            "session_id": "session-1",
            "transcript_path": None,
            "trigger": "manual",
            "turn_id": "turn-1",
        }
    ).encode()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dormant boundary crossed")

    monkeypatch.setattr(time, "monotonic_ns", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert client.production_precompaction_hook(raw) == client.STORAGE_FAILURE_STDOUT


def test_canary_hook_sends_only_raw_selectors_and_timeout_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = canonical_json(
        {
            "cwd": "/private/tmp/source",
            "hook_event_name": "PreCompact",
            "session_id": "session-1",
            "transcript_path": None,
            "trigger": "manual",
            "turn_id": "turn-1",
        }
    ).encode()
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_NONCE", "cd" * 32)
    monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_SOCKET", "/private/tmp/canary.sock")
    monkeypatch.setattr(
        lifecycle,
        "_process_row",
        lambda _pid, timeout=1.0: (321, os.geteuid(), "hook-start", Path("/python")),
    )
    captured: list[dict[str, object]] = []

    def exchange(_path: Path, request: dict[str, object], *, deadline_ns: int) -> dict[str, object]:
        assert deadline_ns == request["deadline_monotonic_ns"]
        captured.append(request)
        return {"classification": "checkpoint-ready"}

    monkeypatch.setattr(client, "canary_exchange", exchange)
    assert client.production_precompaction_hook(raw) == client.SUCCESS_STDOUT
    assert len(captured) == 1
    assert captured[0]["schema"] == "zekam-precompact-local-raw-request/v1"
    assert "binding_digest" not in captured[0] and "delivery_id" not in captured[0]
    monkeypatch.setattr(
        client,
        "canary_exchange",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    assert client.production_precompaction_hook(raw) == client.STORAGE_FAILURE_STDOUT


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("client_pid", True),
        ("client_pid", 2**31),
        ("client_uid", -1),
        ("client_uid", 2**31),
        ("created_monotonic_ns", 0),
        ("created_monotonic_ns", 2**63),
    ),
)
def test_request_integer_bounds(name: str, value: object) -> None:
    body = _request()
    body[name] = value
    if name == "created_monotonic_ns" and type(value) is int:
        body["deadline_monotonic_ns"] = value + client.TOTAL_DEADLINE_NS
    with pytest.raises(ValidationFailed):
        client.encode_frame(body, response=False)


@pytest.mark.parametrize("name", ("attempt_nonce", "request_key", "request_body_digest"))
def test_response_is_bound_to_request_selectors(name: str) -> None:
    request = _request()
    response = _failure(request)
    response[name] = "b" * 64 if name == "attempt_nonce" else digest("wrong")
    response_frame = client.encode_frame(response, response=True)
    with pytest.raises(PolicyViolation):
        supervisor.observe_synthetic_exchange(
            client.encode_frame(request, response=False), response_frame
        )


def test_response_success_requires_xor_decision_stdout_and_census() -> None:
    request = _request()
    client.encode_frame(_success(request), response=True)
    for name, value in (
        ("fresh", False),
        ("stdout_digest", digest("wrong")),
        ("verified_census_digest", digest("wrong")),
    ):
        body = _success(request)
        body[name] = value
        with pytest.raises(PolicyViolation):
            client.encode_frame(body, response=True)


@pytest.mark.parametrize(
    "mutator",
    (
        lambda frame: frame[:-1],
        lambda frame: frame + b"x",
        lambda frame: b"\x00\x00\x40\x01" + frame[4:],
        lambda frame: frame[:4] + frame[4:].replace(b'"schema"', b'"schema" ', 1),
    ),
)
def test_framing_early_eof_second_frame_trailing_byte_and_noncanonical_reject(
    mutator: Callable[[bytes], bytes],
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        client.decode_frame(
            mutator(client.encode_frame(_request(), response=False)), response=False
        )


def test_exchange_requires_half_close_eof_and_rejects_delayed_second_frame() -> None:
    request = _request()
    sent = client.encode_frame(request, response=False)
    response = client.encode_frame(_failure(request), response=True)
    left, right = socket.socketpair()

    def peer() -> None:
        while right.recv(4096):
            pass
        right.sendall(response + response)
        right.close()

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        with pytest.raises(ValidationFailed):
            client._exchange(left, sent, deadline_ns=time.monotonic_ns() + 1_000_000_000)
    finally:
        left.close()
        thread.join(1)


def test_exchange_timeout_releases_socket_selector() -> None:
    left, right = socket.socketpair()
    try:
        with pytest.raises(TimeoutError):
            client._exchange(
                left,
                client.encode_frame(_request(), response=False),
                deadline_ns=time.monotonic_ns() - 1,
            )
    finally:
        left.close()
        right.close()


def test_exchange_epipe_closes_selector_and_never_returns_response() -> None:
    left, right = socket.socketpair()
    right.close()
    try:
        with pytest.raises((BrokenPipeError, ConnectionError)):
            client._exchange(
                left,
                client.encode_frame(_request(), response=False),
                deadline_ns=time.monotonic_ns() + 1_000_000_000,
            )
    finally:
        left.close()


def test_peer_audit_token_rejects_uid_only_identity_and_wrong_layout() -> None:
    raw = struct.pack("=8I", 0, 501, 0, 0, 0, 123, 0, 0)
    assert supervisor._DarwinAuditTokenParser.parse(raw)[:2] == (123, 501)
    for invalid in (b"", raw[:-1], raw + b"x"):
        with pytest.raises(ValidationFailed):
            supervisor._DarwinAuditTokenParser.parse(invalid)


def _listener(**changes: object) -> supervisor._DarwinListenerObservation:
    values: dict[str, object] = {
        "path": "/private/tmp/zekam.sock",
        "fd": 7,
        "owner_uid": 501,
        "mode": 0o600,
        "device": 1,
        "inode": 2,
        "nlink": 1,
        "socket_type": socket.SOCK_STREAM,
    }
    values.update(changes)
    return supervisor._DarwinListenerObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    (
        {"nlink": 2},
        {"mode": 0o666},
        {"inode": 0},
        {"socket_type": socket.SOCK_DGRAM},
        {"path": "relative"},
    ),
)
def test_listener_metadata_rejects_wrong_identity(changes: dict[str, object]) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _listener(**changes)


def test_listener_inode_symlink_hardlink_link_count_and_fd_identity() -> None:
    with TemporaryDirectory(prefix="zpc-", dir="/private/tmp") as directory:
        root = Path(directory)
        listener_path = root / "listener.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(listener_path))
            listener.listen(1)
            listener_path.chmod(0o600)
            observed = supervisor._listener_observation_from_fd(
                str(listener_path), listener.fileno(), os.geteuid()
            )
            assert (observed.device, observed.inode) == (
                listener_path.stat().st_dev,
                listener_path.stat().st_ino,
            )
            assert fcntl.fcntl(listener.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            assert fcntl.fcntl(listener.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK
            symlink = root / "listener-link.sock"
            symlink.symlink_to(listener_path)
            with pytest.raises(PolicyViolation):
                supervisor._listener_observation_from_fd(
                    str(symlink), listener.fileno(), os.geteuid()
                )
            hardlink = root / "listener-hard.sock"
            os.link(listener_path, hardlink)
            with pytest.raises(PolicyViolation):
                supervisor._listener_observation_from_fd(
                    str(listener_path), listener.fileno(), os.geteuid()
                )


def test_launchd_generation_drift_cannot_be_constructed_copied_or_enter_writer() -> None:
    with pytest.raises(PolicyViolation):
        supervisor._DarwinGenerationOwner()
    forged = object.__new__(supervisor._DarwinGenerationOwner)
    with pytest.raises((PolicyViolation, AttributeError)):
        supervisor._generation_digest_if_current(forged)
    with pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(forged)


def test_bootout_quiescence_stale_daemon_and_pid_reuse_require_every_coordinate() -> None:
    assert supervisor.LaunchdQuiescence(
        True, True, True, True, True, "complete"
    ).permits_next_generation
    for index in range(5):
        flags = [True] * 5
        flags[index] = False
        assert not supervisor.LaunchdQuiescence(
            flags[0], flags[1], flags[2], flags[3], flags[4], "complete"
        ).permits_next_generation
    assert not supervisor.LaunchdQuiescence(
        True, True, True, True, True, "other"
    ).permits_next_generation


def test_production_entry_is_runnable_dormant_noarg_boundary() -> None:
    assert inspect.signature(supervisor.production_service_entry).parameters == {}
    assert supervisor.production_service_entry() == os.EX_UNAVAILABLE
    assert supervisor.main() == os.EX_UNAVAILABLE
    assert supervisor.DARWIN_LAUNCHD_CAPABILITY_OBSERVED is False
    assert supervisor.PRODUCTION_GENERATION_ISSUED is False
    assert supervisor.NATIVE_HOOK_ACTIVATED is False
    assert supervisor.NATIVE_ACK_OBSERVED is False


def test_plist_is_disabled_no_shell_socket_template() -> None:
    text = Path("packaging/macos/io.zekam.precompaction-supervisor.plist.in").read_text()
    assert "<key>Disabled</key>\n  <true/>" in text
    assert "@ZEKAM_PYTHON@" in text and "@ZEKAM_PRECOMPACTION_SOCKET@" in text
    assert "/bin/sh" not in text and "/bin/zsh" not in text and "launchctl" not in text


@pytest.mark.parametrize("stage", ("before-spool", "after-spool", "after-commit"))
def test_synthetic_crash_boundaries_preserve_exact_census(stage: str) -> None:
    request = _request()
    frame = client.encode_frame(request, response=False)
    model = supervisor.SyntheticCheckpointModel()
    with pytest.raises(supervisor.SyntheticCrash):
        model.execute(frame, crash_stage=stage)
    observed = model.census(str(request["request_key"]))
    expected = {
        "before-spool": (0, 0, "fixed-false"),
        "after-spool": (1, 0, "fixed-false"),
        "after-commit": (1, 1, "replay-graph"),
    }[stage]
    assert (observed.spool_count, observed.graph_count, observed.classification) == expected
    assert observed.grants_authority is False


def test_synthetic_spool_ahead_retry_completes_once_then_replays() -> None:
    request = _request()
    frame = client.encode_frame(request, response=False)
    model = supervisor.SyntheticCheckpointModel()
    with pytest.raises(supervisor.SyntheticCrash):
        model.execute(frame, crash_stage="after-spool")
    fresh = model.execute(frame)
    replay = model.execute(frame)
    assert (fresh.classification, replay.classification) == ("fresh-graph", "replay-graph")
    assert fresh.census_digest == replay.census_digest
    assert fresh.spool_count == fresh.graph_count == 1


def test_synthetic_same_key_concurrency_is_one_fresh_rest_replay() -> None:
    frame = client.encode_frame(_request(), response=False)
    model = supervisor.SyntheticCheckpointModel()
    outcomes: list[supervisor.SyntheticDurableOutcome] = []
    threads = [
        threading.Thread(target=lambda: outcomes.append(model.execute(frame))) for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert sum(item.classification == "fresh-graph" for item in outcomes) == 1
    assert sum(item.classification == "replay-graph" for item in outcomes) == 11
    assert len({item.census_digest for item in outcomes}) == 1
    assert all(item.grants_authority is False for item in outcomes)


def test_synthetic_different_request_keys_do_not_collide() -> None:
    model = supervisor.SyntheticCheckpointModel()
    first = _request()
    second = _request()
    second["attempt_nonce"] = "b" * 64
    second["delivery_id"] = digest("delivery-2")
    second["request_key"] = digest(
        {
            "schema": "zekam-precompact-local-request-key/v1",
            "binding_digest": second["binding_digest"],
            "delivery_id": second["delivery_id"],
            "event_wire_digest": second["event_wire_digest"],
            "external_session_id": second["external_session_id"],
            "trigger": second["trigger"],
            "turn_id": second["turn_id"],
        }
    )
    values = [model.execute(client.encode_frame(item, response=False)) for item in (first, second)]
    assert [item.classification for item in values] == ["fresh-graph", "fresh-graph"]
    assert values[0].request_key != values[1].request_key


def test_synthetic_model_never_returns_production_result_or_stdout() -> None:
    from zekam.application.local_continuity_v4_compaction import PreCompactionResult

    outcome = supervisor.SyntheticCheckpointModel().execute(
        client.encode_frame(_request(), response=False)
    )
    assert type(outcome) is supervisor.SyntheticDurableOutcome
    assert PreCompactionResult not in type(outcome).__mro__
    assert not hasattr(outcome, "stdout")
    assert outcome.grants_authority is False


def test_repeated_synthetic_failures_restore_threads_and_file_descriptors() -> None:
    before_threads = threading.active_count()
    before_fds = len(os.listdir("/dev/fd"))
    frame = client.encode_frame(_request(), response=False)
    for _ in range(64):
        with pytest.raises(supervisor.SyntheticCrash):
            supervisor.SyntheticCheckpointModel().execute(frame, crash_stage="before-spool")
    assert threading.active_count() == before_threads
    assert len(os.listdir("/dev/fd")) == before_fds


def test_late_completion_restart_replay_cannot_change_prior_fixed_false_value() -> None:
    request = _request()
    frame = client.encode_frame(request, response=False)
    model = supervisor.SyntheticCheckpointModel()
    prior = model.census(str(request["request_key"]))
    completed: list[supervisor.SyntheticDurableOutcome] = []
    thread = threading.Thread(target=lambda: completed.append(model.execute(frame)))
    thread.start()
    thread.join(2)
    assert prior.classification == "fixed-false" and prior.graph_count == 0
    assert completed[0].classification == "fresh-graph"
    replay = model.execute(frame)
    assert replay.classification == "replay-graph"


def test_direct_socket_round_trip_and_temp_supervisor_preserve_listener_path() -> None:
    request = _request()
    sent = client.encode_frame(request, response=False)
    expected = client.encode_frame(_failure(request), response=True)
    left, right = socket.socketpair()

    def peer() -> None:
        received = bytearray()
        while len(received) < len(sent):
            received.extend(right.recv(4096))
        assert bytes(received) == sent
        right.sendall(expected)
        right.close()

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        assert (
            client._exchange(left, sent, deadline_ns=time.monotonic_ns() + 1_000_000_000)
            == expected
        )
    finally:
        left.close()
        thread.join(1)
        assert not thread.is_alive()

    with TemporaryDirectory(prefix="zkpc-", dir="/private/tmp") as directory:
        socket_path = Path(directory) / "s.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)
        observed: list[supervisor.SyntheticSupervisorObservation] = []
        service = threading.Thread(
            target=lambda: observed.append(supervisor.serve_synthetic_once(listener, expected))
        )
        service.start()
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.connect(str(socket_path))
        try:
            assert (
                client._exchange(endpoint, sent, deadline_ns=time.monotonic_ns() + 1_000_000_000)
                == expected
            )
        finally:
            endpoint.close()
            service.join(2)
            listener.close()
        assert not service.is_alive()
        assert len(observed) == 1 and observed[0].grants_authority is False
        assert socket_path.is_socket()


def test_one_shot_canary_binds_real_peer_and_fails_closed_without_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "c" * 64
    with TemporaryDirectory(prefix="zkpc-canary-", dir="/private/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            activation = _canary(root, listener, nonce, monkeypatch)
            outcomes: list[int] = []
            failures: list[BaseException] = []

            def serve() -> None:
                try:
                    outcomes.append(supervisor.serve_canary_once(activation))
                except BaseException as exc:  # test records the service boundary
                    failures.append(exc)

            service = threading.Thread(target=serve)
            service.start()
            response = client.canary_exchange(
                root / "canary.sock",
                _live_request(nonce),
                deadline_ns=time.monotonic_ns() + client.TOTAL_DEADLINE_NS,
            )
            service.join(3)
            assert not service.is_alive() and not failures
            assert outcomes == [os.EX_UNAVAILABLE]
            assert response["classification"] == "STORAGE_UNAVAILABLE"
            assert response["decision_body"] is None
            with pytest.raises(PolicyViolation):
                supervisor.serve_canary_once(activation)


@pytest.mark.parametrize("coordinate", ("attempt_nonce", "client_pid"))
def test_canary_wrong_nonce_or_peer_never_returns_ack(
    coordinate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "d" * 64
    with TemporaryDirectory(prefix="zkpc-canary-", dir="/private/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            activation = _canary(root, listener, nonce, monkeypatch)
            failures: list[BaseException] = []

            def serve() -> None:
                try:
                    supervisor.serve_canary_once(activation)
                except BaseException as exc:
                    failures.append(exc)

            service = threading.Thread(target=serve)
            service.start()
            request = _live_request(nonce)
            request[coordinate] = "e" * 64 if coordinate == "attempt_nonce" else os.getpid() + 1
            with pytest.raises((ConnectionError, OSError, PolicyViolation)):
                client.canary_exchange(
                    root / "canary.sock",
                    request,
                    deadline_ns=time.monotonic_ns() + client.TOTAL_DEADLINE_NS,
                )
            service.join(3)
            assert not service.is_alive()
            assert len(failures) == 1 and isinstance(failures[0], PolicyViolation)


def test_launch_activate_uses_libsystem_exact_count_and_frees_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Function:
        argtypes: object = None
        restype: object = None
        array: object

        def __call__(self, key: bytes, values: Any, count: Any) -> int:
            calls.append(("activate", key))
            self.array = (ctypes.c_int * 1)(123)
            ctypes.cast(values, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)))[0] = ctypes.cast(
                self.array, ctypes.POINTER(ctypes.c_int)
            )
            ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t))[0] = 1
            return 0

    class Free:
        argtypes: object = None
        restype: object = None

        def __call__(self, value: object) -> None:
            calls.append(("free", bool(value)))

    class Library:
        launch_activate_socket = Function()
        free = Free()

    def load(name: str, *, use_errno: bool) -> Library:
        calls.append(("library", name, use_errno))
        return Library()

    monkeypatch.setattr(ctypes, "CDLL", load)
    assert supervisor._DarwinAuthorityAdapter._launch_activate_socket() == (123,)
    assert calls == [
        ("library", "/usr/lib/libSystem.B.dylib", True),
        ("activate", b"PreCompactionListener"),
        ("free", True),
    ]


def test_failure_response_rejects_decision_census_and_arbitrary_success_body() -> None:
    request = _request()
    for name in ("decision_digest", "verified_census_digest"):
        forged = _failure(request)
        forged[name] = digest("forged")
        with pytest.raises(PolicyViolation):
            client.encode_frame(forged, response=True)
    forged_success = _success(request)
    body = copy.deepcopy(forged_success["decision_body"])
    assert type(body) is dict
    body["schema"] = "not-an-ack-decision/v1"
    forged_success["decision_body"] = body
    forged_success["decision_digest"] = digest(body)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        client.encode_frame(forged_success, response=True)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("client_id", "other"),
        ("client_kind", "other"),
        ("client_version", "0.152.0"),
        ("source", "compact"),
        ("reason", "secret"),
        ("permission_mode", "bypassPermissions"),
    ),
)
def test_request_observation_literal_mutations_are_rejected(name: str, value: object) -> None:
    body = _request()
    observation = copy.deepcopy(body["event_observation"])
    assert type(observation) is dict
    observation[name] = value
    body["event_observation"] = observation
    with pytest.raises((PolicyViolation, ValidationFailed)):
        client.encode_frame(body, response=False)


def test_lifecycle_has_no_import_time_home_or_ps_child_and_surface_is_dormant() -> None:
    from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle

    lifecycle_source = inspect.getsource(lifecycle)
    supervisor_source = inspect.getsource(supervisor)
    assert "Path.home()" not in lifecycle_source
    assert '"/bin/ps"' not in lifecycle_source
    assert "subprocess" not in supervisor_source
    assert hasattr(supervisor, "_DarwinJobObservation")
    assert hasattr(supervisor, "_DarwinPeerObservation")
    assert hasattr(supervisor, "_ProductionService")
    with pytest.raises(TypeError):
        supervisor.production_service_entry(object())  # type: ignore[call-arg]
