"""macOS kernel-sandbox acceptance for local benchmark child processes."""

from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest

from zekam.application.model_benchmark_service import _run_json_process
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS acceptance gate")


def test_local_benchmark_process_cannot_write_or_use_network(tmp_path: Path) -> None:
    target = tmp_path / "forbidden-effect.txt"
    script = tmp_path / "sandbox_probe.py"
    script.write_text(
        """
import json
import socket
import sys

request = json.load(sys.stdin)
out = {}
try:
    with open(request["read_target"], encoding="utf-8") as stream:
        stream.read(1)
    out["read_errno"] = 0
except OSError as exc:
    out["read_errno"] = exc.errno
try:
    with open(request["target"], "w", encoding="utf-8") as stream:
        stream.write("forbidden")
    out["write_errno"] = 0
except OSError as exc:
    out["write_errno"] = exc.errno
try:
    with socket.socket() as client:
        client.settimeout(0.2)
        client.connect(("127.0.0.1", 9))
    out["network_errno"] = 0
except OSError as exc:
    out["network_errno"] = exc.errno
print(json.dumps(out, sort_keys=True))
""".strip(),
        encoding="utf-8",
    )

    result = _run_json_process(
        (sys.executable, str(script)),
        {"target": str(target), "read_target": str(Path(__file__).parents[2] / "pyproject.toml")},
        2,
    ).document

    assert result == {
        "network_errno": errno.EPERM,
        "read_errno": errno.EPERM,
        "write_errno": errno.EPERM,
    }
    assert not target.exists()


def test_local_benchmark_sandbox_rejects_whole_home_and_symlink_roots(
    tmp_path: Path,
) -> None:
    script = tmp_path / "ok.py"
    script.write_text('print("{}")', encoding="utf-8")
    with pytest.raises(PolicyViolation, match="whole user home"):
        _run_json_process((sys.executable, str(script)), {}, 2, read_allow_roots=(Path.home(),))
    link = tmp_path / "read-root-link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="read root invalid"):
        _run_json_process((sys.executable, str(script)), {}, 2, read_allow_roots=(link,))
