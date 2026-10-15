"""Research process cancellation uses local Python children, never a model."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import opencode_research as subject
from zekam.infrastructure.process.bounded_stream import start_process_tree


def _local_child(monkeypatch: pytest.MonkeyPatch):
    trees = []

    def start(_argv, cwd, **kwargs):
        tree = start_process_tree(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            cwd,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        trees.append(tree)
        return tree

    monkeypatch.setattr(subject, "_resolve_executable", lambda _: sys.executable)
    monkeypatch.setattr(subject, "start_process_tree", start)
    return trees


def _assert_closed(trees):
    assert len(trees) == 1
    process = trees[0].process
    assert process.poll() is not None
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_precancelled_research_starts_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(
        subject, "_resolve_executable", lambda _: pytest.fail("unexpected process resolution")
    )
    with pytest.raises(PolicyViolation, match="iptal"):
        subject.OpenCodeResearchAdapter(cwd=tmp_path, cancel=cancel).execute({})


def test_research_cancel_stops_child_and_closes_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trees = _local_child(monkeypatch)
    cancel = threading.Event()
    wait = subject.wait_for

    def cancel_after_start(process, deadline, *overflow, **kwargs):
        cancel.set()
        assert kwargs["cancel"] is cancel
        return wait(process, deadline, *overflow, **kwargs)

    monkeypatch.setattr(subject, "wait_for", cancel_after_start)
    with pytest.raises(PolicyViolation, match="iptal"):
        subject.OpenCodeResearchAdapter(cwd=tmp_path, cancel=cancel).execute({})
    _assert_closed(trees)


def test_research_keyboard_interrupt_cleans_child_before_reraising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trees = _local_child(monkeypatch)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(subject, "wait_for", interrupt)
    with pytest.raises(KeyboardInterrupt):
        subject.OpenCodeResearchAdapter(cwd=tmp_path).execute({})
    _assert_closed(trees)


def test_research_deadline_cleans_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trees = _local_child(monkeypatch)
    monkeypatch.setattr(subject, "wait_for", lambda *_args, **_kwargs: "deadline")
    with pytest.raises(ValidationFailed, match="timeout"):
        subject.OpenCodeResearchAdapter(cwd=tmp_path).execute({})
    _assert_closed(trees)


@pytest.mark.parametrize("timeout", [True, 0, 601, 1.5])
def test_research_timeout_requires_bounded_integer(tmp_path: Path, timeout) -> None:
    with pytest.raises(ValidationFailed, match="timeout"):
        subject.OpenCodeResearchAdapter(cwd=tmp_path, timeout_seconds=timeout)
