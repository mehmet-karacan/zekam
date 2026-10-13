from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn

import pytest

from zekam.application.provider_adapter import ProviderOutcomeUnknown
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_invocation import GatewayTransportProvenance
from zekam.domain.security import SecretValue
from zekam.infrastructure.process import capability_worker as worker
from zekam.infrastructure.process.capability_worker import (
    CAPABILITY_WORKER_SCHEMA,
    CapabilityProcessWorker,
    CapabilityWorkerRequest,
    CapabilityWorkerSpec,
    CapabilityWorkerStatus,
    ProcessIsolatedJsonProviderTransport,
)


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "worker_fixture.py"
    path.write_text(source, encoding="utf-8")
    return path


def _gateway_provenance() -> GatewayTransportProvenance:
    from uuid import uuid4

    return GatewayTransportProvenance(digest("manifest"), uuid4(), uuid4())


def _spec(
    tmp_path: Path, script: Path, *, timeout: float = 1, limit: int = 4096
) -> CapabilityWorkerSpec:
    return CapabilityWorkerSpec(
        argv=(sys.executable, str(script)),
        cwd=tmp_path,
        timeout_seconds=timeout,
        max_ipc_bytes=limit,
    )


def test_returns_only_strict_typed_json_result(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
import json, sys
request = json.loads(sys.stdin.readline())
result = {
    "schema": request["schema"], "type": "result",
    "request_id": request["request_id"], "status": "completed",
    "payload": {"result_digest": "sha256:ok"}, "error_code": None,
}
sys.stdout.write(json.dumps(result))
""",
    )

    result = CapabilityProcessWorker().run(
        _spec(tmp_path, script),
        CapabilityWorkerRequest("request-1", {"task_digest": "sha256:task"}),
    )

    assert result.status is CapabilityWorkerStatus.COMPLETED
    assert result.payload == {"result_digest": "sha256:ok"}
    assert result.error_code is None
    assert not result.cancel_sent


def test_rejects_extra_fields_and_never_returns_raw_output(tmp_path: Path) -> None:
    secret = "-".join(("super", "secret", "value"))
    script = _script(
        tmp_path,
        f"""
import json, sys
request = json.loads(sys.stdin.readline())
result = {{
    "schema": request["schema"], "type": "result",
    "request_id": request["request_id"], "status": "failed",
    "payload": None, "error_code": "sanitized", "raw": {secret!r},
}}
sys.stderr.write({secret!r})
sys.stdout.write(json.dumps(result))
""",
    )

    result = CapabilityProcessWorker().run(
        _spec(tmp_path, script), CapabilityWorkerRequest("request-2", {})
    )

    assert result.status is CapabilityWorkerStatus.PROTOCOL_ERROR
    assert result.error_code == "invalid-envelope"
    assert secret not in repr(result)


def test_output_limit_hard_kills_worker(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
import sys, time
sys.stdin.readline()
sys.stdout.write("x" * 10000)
sys.stdout.flush()
time.sleep(30)
""",
    )

    result = CapabilityProcessWorker(cancellation_grace_seconds=0.02).run(
        _spec(tmp_path, script, limit=1024), CapabilityWorkerRequest("request-3", {})
    )

    assert result.status is CapabilityWorkerStatus.OUTPUT_LIMIT
    assert result.hard_killed


def test_deadline_sends_cancel_and_suppresses_late_result(tmp_path: Path) -> None:
    cancel_marker = tmp_path / "cancel-observed"
    script = _script(
        tmp_path,
        """
import json, pathlib, sys, threading, time
request = json.loads(sys.stdin.readline())
def listen():
    cancel = json.loads(sys.stdin.readline())
    if cancel["type"] == "cancel":
        pathlib.Path("cancel-observed").write_text("yes", encoding="utf-8")
threading.Thread(target=listen, daemon=True).start()
time.sleep(0.08)
result = {
    "schema": request["schema"], "type": "result",
    "request_id": request["request_id"], "status": "completed",
    "payload": {"late": True}, "error_code": None,
}
sys.stdout.write(json.dumps(result))
""",
    )

    result = CapabilityProcessWorker(cancellation_grace_seconds=1.0).run(
        _spec(tmp_path, script, timeout=0.03), CapabilityWorkerRequest("request-4", {})
    )

    assert result.status is CapabilityWorkerStatus.TIMEOUT
    assert result.payload is None
    assert result.cancel_sent
    assert not result.hard_killed
    assert result.late_result_suppressed
    assert cancel_marker.read_text(encoding="utf-8") == "yes"


def test_preset_cancel_event_starts_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(
        worker,
        "start_process_tree",
        lambda *args, **kwargs: (
            started.append(args)
            or (_ for _ in ()).throw(AssertionError("preset cancel must not start a child"))
        ),
    )
    cancel = threading.Event()
    cancel.set()

    result = CapabilityProcessWorker().run(
        _spec(tmp_path, Path("unused.py")),
        CapabilityWorkerRequest("request-cancel-preset", {}),
        cancel=cancel,
    )

    assert started == []
    assert result.status is CapabilityWorkerStatus.CANCELLED
    assert result.error_code == "user-cancelled"
    assert result.payload is None
    assert result.cancel_sent is False


def test_cancel_event_during_active_child_returns_cancelled(tmp_path: Path) -> None:
    cancel_marker = tmp_path / "cancel-observed"
    script = _script(
        tmp_path,
        """
import json, pathlib, sys, threading, time
request = json.loads(sys.stdin.readline())
def listen():
    cancel = json.loads(sys.stdin.readline())
    if cancel["type"] == "cancel":
        pathlib.Path("cancel-observed").write_text("yes", encoding="utf-8")
threading.Thread(target=listen, daemon=True).start()
time.sleep(0.08)
result = {
    "schema": request["schema"], "type": "result",
    "request_id": request["request_id"], "status": "completed",
    "payload": {"late": True}, "error_code": None,
}
sys.stdout.write(json.dumps(result))
""",
    )
    cancel = threading.Event()
    timer = threading.Timer(0.04, cancel.set)
    timer.start()

    result = CapabilityProcessWorker(cancellation_grace_seconds=1.0).run(
        _spec(tmp_path, script, timeout=5),
        CapabilityWorkerRequest("request-cancel-active", {}),
        cancel=cancel,
    )
    timer.join()

    assert result.status is CapabilityWorkerStatus.CANCELLED
    assert result.error_code == "user-cancelled"
    assert result.payload is None
    assert result.cancel_sent
    assert not result.hard_killed
    assert result.late_result_suppressed
    assert cancel_marker.read_text(encoding="utf-8") == "yes"


def test_hard_timeout_kills_descendant_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    script = _script(
        tmp_path,
        """
import json, subprocess, sys, time
json.loads(sys.stdin.readline())
subprocess.Popen([
    sys.executable, "-c",
    "import pathlib,time; time.sleep(0.35); pathlib.Path('descendant-survived').write_text('bad')",
])
time.sleep(30)
""",
    )

    result = CapabilityProcessWorker(cancellation_grace_seconds=0.03).run(
        _spec(tmp_path, script, timeout=0.03), CapabilityWorkerRequest("request-5", {})
    )
    time.sleep(0.5)

    assert result.status is CapabilityWorkerStatus.TIMEOUT
    assert result.cancel_sent
    assert result.hard_killed
    assert not marker.exists()


def test_keyboard_interrupt_kills_child_and_suppresses_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "interrupted-child-survived"
    script = _script(
        tmp_path,
        """
import json, pathlib, sys, time
json.loads(sys.stdin.readline())
time.sleep(0.2)
pathlib.Path('interrupted-child-survived').write_text('bad', encoding='utf-8')
""",
    )
    trees: list[object] = []
    original_start = worker.start_process_tree

    def capture_start(*args: object, **kwargs: object) -> object:
        tree = original_start(*args, **kwargs)  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    def interrupt_wait(*_args: object, **_kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "start_process_tree", capture_start)
    monkeypatch.setattr(worker, "wait_for", interrupt_wait)

    with pytest.raises(KeyboardInterrupt):
        CapabilityProcessWorker().run(
            _spec(tmp_path, script), CapabilityWorkerRequest("request-interrupted", {})
        )
    assert len(trees) == 1
    assert trees[0].process.poll() is not None  # type: ignore[attr-defined]
    time.sleep(0.3)
    assert not marker.exists()


def test_keyboard_interrupt_before_reader_start_still_closes_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(tmp_path, "import time; time.sleep(30)\n")
    trees: list[object] = []
    original_start = worker.start_process_tree

    def capture_start(*args: object, **kwargs: object) -> object:
        tree = original_start(*args, **kwargs)  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    def interrupt_start(_self: threading.Thread) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "start_process_tree", capture_start)
    monkeypatch.setattr(worker.threading.Thread, "start", interrupt_start)
    with pytest.raises(KeyboardInterrupt):
        CapabilityProcessWorker().run(
            _spec(tmp_path, script), CapabilityWorkerRequest("request-reader-interrupted", {})
        )
    assert len(trees) == 1
    assert trees[0].process.poll() is not None  # type: ignore[attr-defined]


def test_keyboard_interrupt_hard_kills_child_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "interrupt-survived"
    script = _script(
        tmp_path,
        """
import json, pathlib, sys, time
json.loads(sys.stdin.readline())
time.sleep(30)
pathlib.Path("interrupt-survived").write_text("bad", encoding="utf-8")
""",
    )

    def interrupt_on_wait(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "zekam.infrastructure.process.capability_worker.wait_for", interrupt_on_wait
    )

    with pytest.raises(KeyboardInterrupt):
        CapabilityProcessWorker(cancellation_grace_seconds=0.05).run(
            _spec(tmp_path, script, timeout=1), CapabilityWorkerRequest("request-kb", {})
        )
    time.sleep(0.5)

    assert not marker.exists()


def test_active_child_cancel_suppresses_late_result(tmp_path: Path) -> None:
    started = tmp_path / "child-started"
    late = tmp_path / "late-result"
    script = _script(
        tmp_path,
        """
import json, pathlib, sys, time
request = json.loads(sys.stdin.readline())
pathlib.Path('child-started').write_text('yes', encoding='utf-8')
time.sleep(0.3)
pathlib.Path('late-result').write_text('bad', encoding='utf-8')
print(json.dumps({
    'schema': request['schema'], 'type': 'result',
    'request_id': request['request_id'], 'status': 'completed',
    'payload': {'late': True}, 'error_code': None,
}))
""",
    )
    cancel = threading.Event()

    def cancel_after_start() -> None:
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if started.exists():
            cancel.set()

    trigger = threading.Thread(target=cancel_after_start)
    trigger.start()
    try:
        result = CapabilityProcessWorker(cancellation_grace_seconds=0.02).run(
            _spec(tmp_path, script, timeout=2),
            CapabilityWorkerRequest("request-active-cancelled", {}),
            cancel=cancel,
        )
    finally:
        trigger.join(timeout=3)
    assert started.exists()
    assert result.status is CapabilityWorkerStatus.CANCELLED
    assert result.payload is None
    assert result.cancel_sent
    assert result.hard_killed
    time.sleep(0.4)
    assert not late.exists()


def test_request_is_bounded_before_process_start(tmp_path: Path) -> None:
    script = _script(tmp_path, "raise AssertionError('must not start')")
    request = CapabilityWorkerRequest("request-6", {"value": "x" * 2000})

    with pytest.raises(PolicyViolation, match="request byte"):
        CapabilityProcessWorker().run(_spec(tmp_path, script, limit=1024), request)


def test_protocol_constant_is_versioned() -> None:
    assert CAPABILITY_WORKER_SCHEMA == "zekam-capability-worker/v1"
    assert os.linesep
    assert json.loads(json.dumps({"schema": CAPABILITY_WORKER_SCHEMA}))["schema"]


def test_provider_child_rejects_endpoint_and_credential_without_gateway_provenance() -> None:
    request = CapabilityWorkerRequest(
        request_id="missing-gateway-provenance",
        payload={
            "operation": "provider-post-json",
            "endpoint": "http://127.0.0.1:1/v1/chat",
            "payload": {"model": "test-model"},
            "credential": "must-not-be-enough",
            "timeout_seconds": 1,
            "max_response_bytes": 1024,
        },
    )
    spec = CapabilityWorkerSpec(
        argv=(
            sys.executable,
            "-m",
            "zekam.infrastructure.process.capability_worker",
            "--provider-child",
        ),
        cwd=Path.cwd(),
        timeout_seconds=2,
        max_ipc_bytes=8192,
    )
    outcome = CapabilityProcessWorker(cancellation_grace_seconds=0.1).run(spec, request)
    assert outcome.status is CapabilityWorkerStatus.PROTOCOL_ERROR


def test_process_isolated_transport_posts_in_child_with_secret_redacted() -> None:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            captured["authorization"] = self.headers["Authorization"]
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            response = json.dumps({"echo": request["model"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = "child-only-super-secret"
    transport = ProcessIsolatedJsonProviderTransport(
        timeout_seconds=2,
        max_response_bytes=4096,
        max_ipc_bytes=8192,
        cancellation_grace_seconds=0.1,
    )
    try:
        response = transport.post_json(
            f"http://127.0.0.1:{server.server_port}/v1/chat",
            {"model": "test-model"},
            SecretValue(token),
            gateway_provenance=_gateway_provenance(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response == {"echo": "test-model"}
    assert captured == {"authorization": f"Bearer {token}"}
    assert token not in repr(transport)


def test_process_isolated_transport_denies_redirect(tmp_path: Path) -> None:
    del tmp_path

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = ProcessIsolatedJsonProviderTransport(
        timeout_seconds=2,
        max_response_bytes=4096,
        max_ipc_bytes=8192,
        cancellation_grace_seconds=0.1,
    )
    try:
        from zekam.application.model_health_service import ProbeUnavailable

        with pytest.raises(ProbeUnavailable, match="Provider HTTP status 302"):
            transport.post_json(
                f"http://127.0.0.1:{server.server_port}/v1/chat",
                {"model": "test-model"},
                SecretValue("secret-token"),
                gateway_provenance=_gateway_provenance(),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_process_isolated_transport_marks_missing_child_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_terminal_result(
        _self: CapabilityProcessWorker,
        _spec: CapabilityWorkerSpec,
        request: CapabilityWorkerRequest,
        *,
        cancel: threading.Event | None = None,
    ) -> worker.CapabilityWorkerResult:
        del cancel
        return worker.CapabilityWorkerResult(request.request_id, CapabilityWorkerStatus.TIMEOUT)

    monkeypatch.setattr(CapabilityProcessWorker, "run", no_terminal_result)
    transport = ProcessIsolatedJsonProviderTransport()
    with pytest.raises(ProviderOutcomeUnknown, match="hard deadline"):
        transport.post_json(
            "https://models.example.test/v1/embeddings",
            {"model": "synthetic-model", "input": ["synthetic-text"]},
            SecretValue("synthetic-secret"),
            gateway_provenance=_gateway_provenance(),
        )
