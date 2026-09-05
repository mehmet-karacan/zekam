from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_invocation import GatewayTransportProvenance
from zekam.domain.security import SecretValue
from zekam.infrastructure.process import capability_worker as worker


class _FakeProcess:
    def __init__(self, *, code: int | None = None, stdin: Any = None) -> None:
        self.code = code
        self.stdin = stdin
        self.killed = False
        self.pid = 1234

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.killed = True
        self.code = -9


class _WriteStream(io.BytesIO):
    def flush(self) -> None:
        return None


class _BrokenStream:
    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise AssertionError("flush must not run")


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class _Opener:
    def __init__(self, outcome: bytes | BaseException) -> None:
        self.outcome = outcome

    def open(self, *_args: Any, **_kwargs: Any) -> _Response:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _Response(self.outcome)


def _provenance() -> GatewayTransportProvenance:
    return GatewayTransportProvenance(digest("manifest"), uuid4(), uuid4())


def _child_message(**changes: Any) -> dict[str, Any]:
    provenance = _provenance()
    payload: dict[str, Any] = {
        "operation": "provider-post-json",
        "endpoint": "http://127.0.0.1:8765/v1/test",
        "payload": {"model": "local-test"},
        "credential": "in-memory-only",
        "timeout_seconds": 1,
        "max_response_bytes": 32,
        "manifest_digest": provenance.manifest_digest,
        "gateway_attempt_id": str(provenance.attempt_id),
        "gateway_claim_id": str(provenance.claim_id),
    }
    payload.update(changes.pop("payload_changes", {}))
    message: dict[str, Any] = {
        "schema": worker.CAPABILITY_WORKER_SCHEMA,
        "type": "execute",
        "request_id": "request-child",
        "payload": payload,
    }
    message.update(changes)
    return message


def _invoke_child(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    *,
    opener: _Opener | None = None,
) -> tuple[int, bytes]:
    incoming = SimpleNamespace(buffer=io.BytesIO(raw))
    outgoing_buffer = io.BytesIO()
    outgoing = SimpleNamespace(buffer=outgoing_buffer)
    monkeypatch.setattr(
        "zekam.infrastructure.process.capability_worker.sys.stdin", cast(Any, incoming)
    )
    monkeypatch.setattr(
        "zekam.infrastructure.process.capability_worker.sys.stdout", cast(Any, outgoing)
    )
    if opener is not None:
        monkeypatch.setattr(
            "zekam.infrastructure.process.capability_worker.urllib.request.build_opener",
            lambda *_: opener,
        )
    return worker._provider_child(), outgoing_buffer.getvalue()


def test_request_spec_transport_and_constructor_validation(tmp_path: Path) -> None:
    request = worker.CapabilityWorkerRequest("request-1", {"x": 1})
    assert request.as_message()["payload"] == {"x": 1}
    for request_id in ("", " ", "x" * 129):
        with pytest.raises(ValidationFailed):
            worker.CapabilityWorkerRequest(request_id, {})
    with pytest.raises(ValidationFailed, match="object"):
        worker.CapabilityWorkerRequest("request", cast(Any, []))

    valid = {
        "argv": ("python", "worker.py"),
        "cwd": tmp_path,
        "timeout_seconds": 1.0,
        "max_ipc_bytes": 1024,
    }
    bad_specs: tuple[dict[str, Any], ...] = (
        {"argv": ()},
        {"argv": ("python", "")},
        {"cwd": tmp_path / "missing"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"max_ipc_bytes": 1023},
        {"max_ipc_bytes": 16_777_217},
    )
    for changes in bad_specs:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            worker.CapabilityWorkerSpec(**cast(Any, valid | changes))
    for grace in (0, -1, 10.1):
        with pytest.raises(PolicyViolation):
            worker.CapabilityProcessWorker(cancellation_grace_seconds=grace)

    for kwargs in (
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"max_response_bytes": 0},
        {"max_response_bytes": 1024, "max_ipc_bytes": 1024},
        {"cancellation_grace_seconds": 0},
    ):
        with pytest.raises(ValidationFailed):
            worker.ProcessIsolatedJsonProviderTransport(**kwargs)


def test_strict_result_envelope_decoding_and_failed_terminal_status() -> None:
    process = cast(Any, _FakeProcess(code=7))
    adapter = worker.CapabilityProcessWorker()
    for raw, code in (
        (b"\xff", "invalid-json"),
        (b"not-json", "invalid-json"),
        (b"[]", "invalid-envelope"),
        (b'{"schema":"x"}', "invalid-envelope"),
    ):
        result = adapter._decode_result("request", raw, time.monotonic(), process)
        assert result.status is worker.CapabilityWorkerStatus.PROTOCOL_ERROR
        assert result.error_code == code and result.exit_code == 7

    base: dict[str, Any] = {
        "schema": worker.CAPABILITY_WORKER_SCHEMA,
        "type": "result",
        "request_id": "request",
        "status": "completed",
        "payload": {"ok": True},
        "error_code": None,
    }
    invalid_changes: tuple[dict[str, Any], ...] = (
        {"schema": "wrong"},
        {"type": "execute"},
        {"request_id": "other"},
        {"status": "unknown"},
        {"payload": []},
        {"error_code": 7},
    )
    for changes in invalid_changes:
        raw = json.dumps(base | changes).encode()
        assert (
            adapter._decode_result("request", raw, time.monotonic(), process).error_code
            == "invalid-envelope"
        )
    failed = adapter._decode_result(
        "request",
        json.dumps(base | {"status": "failed", "payload": None, "error_code": "safe"}).encode(),
        time.monotonic(),
        process,
    )
    assert failed.status is worker.CapabilityWorkerStatus.FAILED
    assert failed.payload is None and failed.error_code == "safe"


def test_endpoint_validation_covers_https_loopback_and_rejections() -> None:
    assert worker._validated_provider_endpoint("https://models.example/v1") == (
        "https://models.example/v1"
    )
    for endpoint in (
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
        "http://localhost:8080/v1",
    ):
        assert worker._validated_provider_endpoint(endpoint) == endpoint
    for endpoint in (
        "https://user:pass@models.example/v1",
        "https://models.example/v1?q=1",
        "https://models.example/v1#fragment",
        "models.example/v1",
        "ftp://models.example/v1",
        "http://example.com/v1",
    ):
        with pytest.raises(ValidationFailed):
            worker._validated_provider_endpoint(endpoint)


def test_bounded_encode_cancel_and_wait_contracts() -> None:
    encoded = worker._encode_message({"value": "ok"}, 100)
    assert encoded.endswith(b"\n") and json.loads(encoded) == {"value": "ok"}
    with pytest.raises(ValidationFailed):
        worker._encode_message({"value": float("nan")}, 100)
    with pytest.raises(ValidationFailed):
        worker._encode_message({"value": object()}, 100)
    with pytest.raises(PolicyViolation):
        worker._encode_message({"value": "x" * 100}, 10)

    assert not worker._send_cancel(cast(Any, _FakeProcess(code=0)), "r", 1024)
    writable = _WriteStream()
    live = _FakeProcess(stdin=writable)
    assert worker._send_cancel(cast(Any, live), "r", 1024)
    assert json.loads(writable.getvalue())["type"] == "cancel"
    assert not worker._send_cancel(cast(Any, _FakeProcess(stdin=_BrokenStream())), "r", 1024)

    overflow = threading.Event()
    overflow.set()
    assert worker._wait_for(cast(Any, _FakeProcess()), time.monotonic() + 1, overflow) == "overflow"
    assert (
        worker._wait_for(cast(Any, _FakeProcess(code=0)), time.monotonic() + 1, threading.Event())
        == "exited"
    )
    assert (
        worker._wait_for(cast(Any, _FakeProcess()), time.monotonic() - 1, threading.Event())
        == "deadline"
    )


def test_bounded_reader_and_posix_kill_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    os.write(write_descriptor, b"abcdef")
    os.close(write_descriptor)
    with os.fdopen(read_descriptor, "rb") as stream:
        reader = worker._BoundedReader(stream, 3)
        reader.read()
    assert bytes(reader.buffer) == b"abcd" and reader.overflow.is_set()

    already = _FakeProcess(code=0)
    worker.CapabilityProcessWorker._hard_kill_tree(worker._ProcessTree(cast(Any, already)))
    assert not already.killed
    live = _FakeProcess()
    monkeypatch.setattr("zekam.infrastructure.process.capability_worker.os.getpgid", lambda _: 1234)

    def fail_killpg(*_: Any) -> None:
        raise OSError("gone")

    monkeypatch.setattr("zekam.infrastructure.process.capability_worker.os.killpg", fail_killpg)
    worker.CapabilityProcessWorker._hard_kill_tree(worker._ProcessTree(cast(Any, live)))
    assert live.killed


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b"[]\n",
        json.dumps(_child_message(extra=True)).encode() + b"\n",
        json.dumps(_child_message(payload_changes={"credential": ""})).encode() + b"\n",
        json.dumps(_child_message(payload_changes={"timeout_seconds": 0})).encode() + b"\n",
        json.dumps(_child_message(payload_changes={"max_response_bytes": 16_000_001})).encode()
        + b"\n",
        json.dumps(_child_message(payload_changes={"manifest_digest": "bad"})).encode() + b"\n",
        json.dumps(_child_message(payload_changes={"gateway_attempt_id": "bad"})).encode() + b"\n",
    ],
)
def test_provider_child_rejects_malformed_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    del tmp_path
    code, output = _invoke_child(monkeypatch, raw)
    assert code == 2 and output == b""


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error"),
    [
        (
            urllib.error.HTTPError("http://127.0.0.1", 503, "down", Message(), None),
            "failed",
            "provider-http-status-503",
        ),
        (
            urllib.error.URLError("offline"),
            "failed",
            "provider-transport-unavailable",
        ),
        (b"x" * 40, "failed", "provider-response-limit"),
        (b"not-json", "failed", "provider-response-invalid-json"),
        (b"[]", "failed", "provider-response-not-object"),
        (b'{"ok":true}', "completed", None),
    ],
)
def test_provider_child_http_error_limit_and_result_paths(
    monkeypatch: pytest.MonkeyPatch,
    outcome: bytes | BaseException,
    expected_status: str,
    expected_error: str | None,
) -> None:
    raw = json.dumps(_child_message()).encode() + b"\n"
    code, output = _invoke_child(monkeypatch, raw, opener=_Opener(outcome))
    assert code == 0
    envelope = json.loads(output)
    assert envelope["status"] == expected_status
    assert envelope["error_code"] == expected_error
    if expected_status == "completed":
        assert envelope["payload"] == {"provider_response": {"ok": True}}


def test_transport_failure_messages_and_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = worker.ProcessIsolatedJsonProviderTransport(
        timeout_seconds=1,
        max_response_bytes=1024,
        max_ipc_bytes=2048,
        cancellation_grace_seconds=0.1,
    )
    provenance = _provenance()
    outcomes = iter(
        (
            worker.CapabilityWorkerResult(
                "r", worker.CapabilityWorkerStatus.TIMEOUT, error_code="deadline-exceeded"
            ),
            worker.CapabilityWorkerResult(
                "r", worker.CapabilityWorkerStatus.OUTPUT_LIMIT, error_code="ipc-output-limit"
            ),
            worker.CapabilityWorkerResult(
                "r", worker.CapabilityWorkerStatus.FAILED, error_code="provider-http-status-429"
            ),
            worker.CapabilityWorkerResult(
                "r", worker.CapabilityWorkerStatus.COMPLETED, payload={"provider_response": []}
            ),
            worker.CapabilityWorkerResult(
                "r",
                worker.CapabilityWorkerStatus.COMPLETED,
                payload={"provider_response": {"ok": True}},
            ),
        )
    )
    monkeypatch.setattr(
        worker.CapabilityProcessWorker,
        "run",
        lambda *_args, **_kwargs: next(outcomes),
    )
    from zekam.application.model_health_service import ProbeUnavailable

    for message in ("hard deadline", "IPC boyut", "provider-http-status-429", "response"):
        with pytest.raises(ProbeUnavailable, match=message):
            transport.post_json(
                "http://127.0.0.1:8765/v1/test",
                {"model": "local"},
                SecretValue("secret"),
                gateway_provenance=provenance,
            )
    assert transport.post_json(
        "http://127.0.0.1:8765/v1/test",
        {"model": "local"},
        SecretValue("secret"),
        gateway_provenance=provenance,
    ) == {"ok": True}


def test_provider_failure_message_sanitizes_unknown_errors() -> None:
    assert (
        worker._provider_failure_message(
            worker.CapabilityWorkerResult(
                "r", worker.CapabilityWorkerStatus.FAILED, error_code="raw-sensitive-detail"
            )
        )
        == "Provider process kullanilamiyor"
    )


@pytest.mark.skipif(os.name == "nt", reason="This batch explicitly covers POSIX only")
def test_posix_worker_environment_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross")
    environment = worker._worker_env()
    assert "UNRELATED_SECRET" not in environment
    assert environment["PYTHONUTF8"] == "1"
    assert str(Path(worker.__file__).resolve().parents[3]) in environment["PYTHONPATH"]
