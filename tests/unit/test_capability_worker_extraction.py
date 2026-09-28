"""A15: capability worker execute/cancel protocol and 300s bound survive extraction."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.process.capability_worker import (
    CAPABILITY_WORKER_SCHEMA,
    CapabilityProcessWorker,
    CapabilityWorkerRequest,
    CapabilityWorkerSpec,
    CapabilityWorkerStatus,
)


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "worker_fixture.py"
    path.write_text(source, encoding="utf-8")
    return path


def _spec(
    tmp_path: Path,
    script: Path,
    *,
    timeout: float = 1,
    limit: int = 4096,
) -> CapabilityWorkerSpec:
    return CapabilityWorkerSpec(
        argv=(sys.executable, str(script)),
        cwd=tmp_path,
        timeout_seconds=timeout,
        max_ipc_bytes=limit,
    )


def test_300_second_upper_bound_enforced() -> None:
    """A15: capability lane 300s limit is preserved after extraction work."""
    with pytest.raises(PolicyViolation, match=r"timeout 0\.\.300"):
        CapabilityWorkerSpec(
            argv=(sys.executable,),
            cwd=Path.cwd(),
            timeout_seconds=301,
            max_ipc_bytes=4096,
        )

    spec = CapabilityWorkerSpec(
        argv=(sys.executable,),
        cwd=Path.cwd(),
        timeout_seconds=300,
        max_ipc_bytes=4096,
    )
    assert spec.timeout_seconds == 300


def test_execute_cancel_protocol_survives_extraction(tmp_path: Path) -> None:
    """A15: after extraction work, cancel still arrives and graceful shutdown works."""
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
    "payload": {"extracted": True}, "error_code": None,
}
sys.stdout.write(json.dumps(result))
""",
    )

    result = CapabilityProcessWorker(cancellation_grace_seconds=1.0).run(
        _spec(tmp_path, script, timeout=0.03),
        CapabilityWorkerRequest("extract-cancel", {"phase": "extract"}),
    )

    assert result.status is CapabilityWorkerStatus.TIMEOUT
    assert result.cancel_sent
    assert not result.hard_killed
    assert cancel_marker.read_text(encoding="utf-8") == "yes"


def test_bounded_reader_used_for_extraction_output(tmp_path: Path) -> None:
    """A15: BoundedReader byte limit applies to extracted payload."""
    script = _script(
        tmp_path,
        """
import sys, time
sys.stdin.readline()
sys.stdout.write("x" * 20000)
sys.stdout.flush()
time.sleep(30)
""",
    )

    result = CapabilityProcessWorker(cancellation_grace_seconds=0.02).run(
        _spec(tmp_path, script, timeout=1, limit=1024),
        CapabilityWorkerRequest("bounded-extract", {}),
    )

    assert result.status is CapabilityWorkerStatus.OUTPUT_LIMIT
    assert result.hard_killed


def test_messages_are_typed_not_opencode_ipc(tmp_path: Path) -> None:
    """A15: capability worker sends execute/cancel, not OpenCode CLI flags."""
    script = _script(
        tmp_path,
        """
import json, sys
request = json.loads(sys.stdin.readline())
assert request["schema"] == "zekam-capability-worker/v1"
assert request["type"] == "execute"
result = {
    "schema": request["schema"], "type": "result",
    "request_id": request["request_id"], "status": "completed",
    "payload": None, "error_code": None,
}
sys.stdout.write(json.dumps(result))
""",
    )

    result = CapabilityProcessWorker().run(
        _spec(tmp_path, script),
        CapabilityWorkerRequest("typed-ipc", {}),
    )

    assert result.status is CapabilityWorkerStatus.COMPLETED
    assert CAPABILITY_WORKER_SCHEMA == "zekam-capability-worker/v1"
    assert "opencode" not in CapabilityWorkerRequest("x", {"y": 1}).as_message()
