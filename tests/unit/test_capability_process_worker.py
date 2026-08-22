from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation
from zekam.domain.security import SecretValue
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


def test_request_is_bounded_before_process_start(tmp_path: Path) -> None:
    script = _script(tmp_path, "raise AssertionError('must not start')")
    request = CapabilityWorkerRequest("request-6", {"value": "x" * 2000})

    with pytest.raises(PolicyViolation, match="request byte"):
        CapabilityProcessWorker().run(_spec(tmp_path, script, limit=1024), request)


def test_protocol_constant_is_versioned() -> None:
    assert CAPABILITY_WORKER_SCHEMA == "zekam-capability-worker/v1"
    assert os.linesep
    assert json.loads(json.dumps({"schema": CAPABILITY_WORKER_SCHEMA}))["schema"]


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

        with pytest.raises(ProbeUnavailable, match="provider-http-status-302"):
            transport.post_json(
                f"http://127.0.0.1:{server.server_port}/v1/chat",
                {"model": "test-model"},
                SecretValue("secret-token"),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
