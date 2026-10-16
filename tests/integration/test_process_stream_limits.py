"""Bounded streams and cleanup with real local Python children, no providers."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import ProcessSpec
from zekam.infrastructure.process import runner


@pytest.mark.parametrize("stream", [1, 2])
def test_output_cap_terminates_writer_before_sleep_finishes(tmp_path: Path, stream: int) -> None:
    spec = ProcessSpec(
        argv=(
            sys.executable,
            "-c",
            f"import os,time; os.write({stream},b'x'*262144); time.sleep(30)",
        ),
        timeout_seconds=30,
        max_output_bytes=1024,
    )
    output = runner.run(spec, cwd=tmp_path)
    assert output.result.truncated is True
    assert output.result.succeeded is False
    assert len(output.stdout) <= 1024 and len(output.stderr) <= 1024
    assert output.result.duration_ms < 10000


@pytest.mark.parametrize("interruption", ["cancel", "keyboard"])
def test_interruption_cleans_process_tree_and_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    trees = []
    start = runner.start_process_tree
    wait = runner.wait_for
    cancel = threading.Event()

    def capture(*args, **kwargs):
        tree = start(*args, **kwargs)
        trees.append(tree)
        return tree

    def interrupt(process, deadline, *overflow, **kwargs):
        if interruption == "keyboard":
            raise KeyboardInterrupt()
        cancel.set()
        return wait(process, deadline, *overflow, **kwargs)

    monkeypatch.setattr(runner, "start_process_tree", capture)
    monkeypatch.setattr(runner, "wait_for", interrupt)
    with pytest.raises(KeyboardInterrupt if interruption == "keyboard" else PolicyViolation):
        runner.run(
            ProcessSpec(argv=(sys.executable, "-c", "import time; time.sleep(30)")),
            cwd=tmp_path,
            cancel=cancel,
        )
    assert len(trees) == 1
    assert trees[0].process.poll() is not None
    assert all(
        stream.closed
        for stream in (trees[0].process.stdin, trees[0].process.stdout, trees[0].process.stderr)
    )


def test_terminal_parent_does_not_leave_inherited_pipes_open(tmp_path: Path) -> None:
    script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('fixture-child-started')"
    output = runner.run(
        ProcessSpec(argv=(sys.executable, "-c", script), timeout_seconds=30), cwd=tmp_path
    )
    assert output.result.succeeded is True
    assert output.stdout.strip() == b"fixture-child-started"
    assert output.result.duration_ms < 5000


def test_precancel_starts_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event = threading.Event()
    event.set()
    monkeypatch.setattr(
        runner, "start_process_tree", lambda *_args, **_kwargs: pytest.fail("unexpected child")
    )
    with pytest.raises(PolicyViolation, match="iptal"):
        runner.run(ProcessSpec(argv=(sys.executable, "-c", "pass")), cwd=tmp_path, cancel=event)
