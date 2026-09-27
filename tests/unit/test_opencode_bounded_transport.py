"""WP-02: bounded OpenCode process transport primitives.

These tests exercise the protocol-independent subprocess boundary with
synthetic child processes. They do not require a live OpenCode installation.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from zekam.infrastructure.process.bounded_stream import (
    BoundedReader,
    finish_pipes,
    hard_kill_tree,
    start_process_tree,
    wait_for,
)

pytestmark = pytest.mark.unit


def _child_script(body: str) -> str:
    return f"import sys, os\nif os.name != 'nt':\n    os.setpgrp()\n{body}\n"


def _write_child(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "child.py"
    script.write_text(_child_script(body), encoding="utf-8")
    return script


def test_bounded_reader_enforces_byte_limit(tmp_path: Path) -> None:
    """A10: output limit is applied while streaming, not after completion."""
    script = _write_child(
        tmp_path,
        "sys.stdout.write('x' * 100_000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.1)\n"
        "sys.stdout.write('done')\n"
        "sys.stdout.flush()\n",
    )
    tree = start_process_tree((sys.executable, str(script)), tmp_path)
    process = tree.process
    assert process.stdout is not None
    reader = BoundedReader(process.stdout, limit=10_000)
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    outcome = wait_for(process, deadline, reader.overflow)
    assert outcome in {"overflow", "exited"}
    assert reader.overflow.is_set()
    hard_kill_tree(tree)
    finish_pipes(tree, (thread,))
    assert len(reader.buffer) <= 10_001


def test_stderr_does_not_deadlock(tmp_path: Path) -> None:
    """A11: child fills stderr while producing stdout; no deadlock."""
    script = _write_child(
        tmp_path,
        "sys.stderr.write('e' * 50_000)\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('ok')\n"
        "sys.stdout.flush()\n",
    )
    tree = start_process_tree((sys.executable, str(script)), tmp_path, stderr=subprocess.PIPE)
    process = tree.process
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_reader = BoundedReader(process.stdout, limit=1_000)
    stderr_reader = BoundedReader(process.stderr, limit=100_000)
    stdout_thread = threading.Thread(target=stdout_reader.read, daemon=True)
    stderr_thread = threading.Thread(target=stderr_reader.read, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + 5
    outcome = wait_for(process, deadline, stdout_reader.overflow, stderr_reader.overflow)
    assert outcome == "exited"
    finish_pipes(tree, (stdout_thread, stderr_thread))
    assert bytes(stdout_reader.buffer) == b"ok"
    assert not stderr_reader.overflow.is_set()


def test_transport_timeout_kills_process_tree(tmp_path: Path) -> None:
    """A13: timeout followed by bounded hard stop and cleanup."""
    script = _write_child(
        tmp_path,
        "import time\n"
        "sys.stdout.write('start')\n"
        "sys.stdout.flush()\n"
        "time.sleep(10)\n"
        "sys.stdout.write('late')\n"
        "sys.stdout.flush()\n",
    )
    tree = start_process_tree((sys.executable, str(script)), tmp_path)
    process = tree.process
    assert process.stdout is not None
    reader = BoundedReader(process.stdout, limit=1_000_000)
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    deadline = time.monotonic() + 0.2
    outcome = wait_for(process, deadline, reader.overflow)
    assert outcome == "deadline"
    hard_kill_tree(tree)
    finish_pipes(tree, (thread,))
    assert process.poll() is not None
    assert b"late" not in bytes(reader.buffer)


def test_transport_output_limit_kills_before_completion(tmp_path: Path) -> None:
    """A10/A12: large continuous output triggers output-limit and cleanup."""
    script = _write_child(
        tmp_path,
        "while True:\n    sys.stdout.write('x' * 4096)\n    sys.stdout.flush()\n",
    )
    tree = start_process_tree((sys.executable, str(script)), tmp_path)
    process = tree.process
    assert process.stdout is not None
    reader = BoundedReader(process.stdout, limit=8_192)
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    outcome = wait_for(process, deadline, reader.overflow)
    assert outcome == "overflow"
    hard_kill_tree(tree)
    finish_pipes(tree, (thread,))
    assert process.poll() is not None
    assert len(reader.buffer) <= 8_193


def test_unicode_chunk_boundaries(tmp_path: Path) -> None:
    """A09: multibyte UTF-8 is not split at chunk boundaries."""
    text = "İstanbul 🎉" * 1000
    script = _write_child(
        tmp_path,
        f"sys.stdout.write({text!r})\nsys.stdout.flush()\n",
    )
    tree = start_process_tree((sys.executable, str(script)), tmp_path)
    process = tree.process
    assert process.stdout is not None
    reader = BoundedReader(process.stdout, limit=1_000_000)
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    outcome = wait_for(process, deadline, reader.overflow)
    assert outcome == "exited"
    finish_pipes(tree, (thread,))
    decoded = bytes(reader.buffer).decode("utf-8")
    assert decoded == text
