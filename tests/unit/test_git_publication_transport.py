"""Real local bare-Git fixtures for the typed transport; no live remote required."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git.publication import BoundGitPushTransport


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    ).stdout.strip()


@pytest.fixture
def transport_world(tmp_path, monkeypatch):
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home))
    root, remote = tmp_path / "source", tmp_path / "remote.git"
    root.mkdir()
    remote.mkdir()
    _git(root, "init")
    _git(remote, "init", "--bare")
    (root / "source.txt").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "source.txt")
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    _git(root, "remote", "add", "origin", str(remote))
    return root, remote, BoundGitPushTransport(root, timeout_seconds=30)


def test_transport_pushes_only_exact_oid_branch_and_reads_remote(transport_world):
    root, remote, transport = transport_world
    target = transport.inspect(remote="origin", ref="refs/heads/delivery")
    _git(root, "tag", "must-not-publish")
    assert transport.push(target, cancel=threading.Event()) == target.head
    assert _git(remote, "show-ref", "--heads").split() == [target.head, target.ref]
    assert _git(remote, "tag", "--list") == ""


def test_transport_rejects_remote_drift_before_push(transport_world, tmp_path):
    root, remote, transport = transport_world
    target = transport.inspect(remote="origin", ref="refs/heads/delivery")
    other = tmp_path / "other.git"
    other.mkdir()
    _git(other, "init", "--bare")
    _git(root, "remote", "set-url", "origin", str(other))
    with pytest.raises(PolicyViolation, match="drift"):
        transport.push(target, cancel=threading.Event())
    assert _git(remote, "for-each-ref", "--format=%(refname)") == ""
    assert _git(other, "for-each-ref", "--format=%(refname)") == ""


def test_transport_refuses_rewrites_multi_url_and_dirty_source(transport_world):
    root, remote, transport = transport_world
    _git(root, "config", "url.file:///redirect.insteadOf", str(remote))
    with pytest.raises(PolicyViolation, match="rewrite"):
        transport.inspect(remote="origin", ref="refs/heads/delivery")
    _git(root, "config", "--unset-all", "url.file:///redirect.insteadOf")
    _git(root, "remote", "set-url", "--add", "--push", "origin", str(remote))
    _git(root, "remote", "set-url", "--add", "--push", "origin", str(remote.parent / "other.git"))
    with pytest.raises(PolicyViolation, match="URL"):
        transport.inspect(remote="origin", ref="refs/heads/delivery")
    _git(root, "config", "--unset-all", "remote.origin.pushurl")
    (root / "source.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="temiz"):
        transport.inspect(remote="origin", ref="refs/heads/delivery")


def test_transport_cancellation_and_foreign_git_env_do_not_publish(transport_world, monkeypatch):
    root, remote, transport = transport_world
    target = transport.inspect(remote="origin", ref="refs/heads/delivery")
    monkeypatch.setenv("GIT_DIR", str(remote))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.pushurl")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://invalid.example/foreign")
    assert transport.inspect(remote="origin", ref=target.ref) == target
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PolicyViolation, match="cancellation"):
        transport.push(target, cancel=cancel)
    assert _git(remote, "for-each-ref", "--format=%(refname)") == ""
