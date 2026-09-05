"""Opt-in real native hook observation, never enabled by an ordinary full-suite run."""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import pwd
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from tests.e2e import macos_codex_hook_probe as probe
from tests.e2e.macos_codex_hook_probe import run_reviewed_probe

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        platform.system() != "Darwin" or platform.machine() != "arm64",
        reason="Separate Mac arm64 hook probe; Windows contracts remain separate",
    ),
]


def _capture(
    monkeypatch: pytest.MonkeyPatch, root: Path, raw: bytes, event: str = "SessionStart"
) -> tuple[int, str]:
    class Input:
        buffer = io.BytesIO(raw)

    stdout = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", Input())
        patch.setattr(sys, "stdout", stdout)
        patch.setattr(sys, "argv", ["capture.py", event, str(root)])
        try:
            exec(compile(probe.CAPTURE_SCRIPT, "capture.py", "exec"), {})
        except SystemExit as error:
            assert isinstance(error.code, int)
            return error.code, stdout.getvalue()
    return 0, stdout.getvalue()


def _wire() -> dict[str, object]:
    return {
        "session_id": "safe-session-1",
        "hook_event_name": "SessionStart",
        "cwd": str(probe.PROJECT),
        "source": "startup",
        "transcript_path": "PRIVATE-PATH-NOT-FOR-EVIDENCE",
        "prompt": "PRIVATE-CONTENT-NOT-FOR-EVIDENCE",
    }


def test_capture_persists_only_bounded_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, reply = _capture(monkeypatch, tmp_path, json.dumps(_wire()).encode())
    assert code == 0
    assert json.loads(reply) == {"continue": False, "stopReason": probe.STOP_MARKER}
    raw = (tmp_path / "SessionStart.json").read_bytes()
    assert b"PRIVATE" not in raw and str(probe.PROJECT).encode() not in raw
    assert b"prompt" not in raw and len(raw) < 4096
    assert json.loads(raw)["raw_wire_persisted"] is False
    assert probe._receipt(tmp_path / "SessionStart.json", "SessionStart")["schema"] == 1
    original = raw
    assert _capture(monkeypatch, tmp_path, json.dumps(_wire()).encode())[0] == 2
    assert (tmp_path / "SessionStart.json").read_bytes() == original


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"",
        b"x" * 65537,
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":' + b"[" * 14 + b"0" + b"]" * 14 + b"}",
    ],
)
def test_capture_invalid_json_still_aborts_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    code, reply = _capture(monkeypatch, tmp_path, raw)
    assert code == 2 and json.loads(reply)["continue"] is False
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", None),
        ("session_id", False),
        ("session_id", "../secret"),
        ("session_id", "x" * 129),
        ("cwd", "/other-project"),
        ("source", "resume"),
        ("hook_event_name", "Stop"),
        ("source", []),
        ("transcript_path", False),
    ],
)
def test_capture_rejects_wrong_type_or_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    wire = _wire()
    wire[field] = value
    code, reply = _capture(monkeypatch, tmp_path, json.dumps(wire).encode())
    assert code == 2 and json.loads(reply)["continue"] is False
    assert not tuple(tmp_path.iterdir())


def test_capture_symlink_directory_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    code, _ = _capture(monkeypatch, link, json.dumps(_wire()).encode())
    assert code == 2 and not tuple(target.iterdir())


def test_plan_preparation_is_inert_and_drift_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("No process execution during preparation or rejection")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root, digest = probe.prepare_probe()
    plan = probe._validate(root, digest)
    assert plan["network_permitted"] is False
    assert plan["zsh"] == {
        "path": "/bin/zsh",
        "sha256": probe.ZSH_SHA,
        "identity": list(probe.system_zsh_identity()[0]),
        "uid": 0,
        "nlink": 1,
        "mode": "0755",
        "size": probe.ZSH_SIZE,
    }
    assert plan["framework_python"] == {
        "path": str(probe.PYTHON_RUNTIME),
        "sha256": probe.PYTHON_RUNTIME_SHA,
        "identity": list(probe.framework_python_identity()[0]),
        "uid": os.geteuid(),
        "nlink": 1,
        "mode": "0755",
        "size": probe.PYTHON_RUNTIME_SIZE,
    }
    profile = (root / "sandbox.sb").read_text()
    assert "(deny network*)" in profile and "(deny mach-lookup)" in profile
    assert "(deny file-write*)" in profile and "(deny process-exec)" in profile
    config = (root / "codex/config.toml").read_text()
    assert 'base_url = "http://127.0.0.1:9/v1"' in config
    assert "requires_openai_auth = false" in config
    environment = json.loads((root / "environment.json").read_bytes())
    assert "HOME" not in environment and "OPENAI_API_KEY" not in environment
    with pytest.raises(ValueError, match="reviewed exact plan"):
        probe.run_reviewed_probe(root, "0" * 64)
    assert not (root / "attempt.json").exists()
    (root / "capture.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact drift"):
        probe.run_reviewed_probe(root, digest)
    assert not (root / "attempt.json").exists()


def test_reviewed_macos_codex_natural_start_end() -> None:
    root = os.environ.get("ZEKAM_MAC_HOOK_REVIEW_ROOT")
    digest = os.environ.get("ZEKAM_MAC_HOOK_REVIEW_DIGEST")
    if root is None and digest is None:
        pytest.skip("Requires independent exact ephemeral config/script/profile/argv review")
    assert root is not None and digest is not None, "Both exact review selectors are required"
    result = run_reviewed_probe(Path(root), digest)
    assert result["natural_events"] == ["SessionStart", "SessionEnd"]
    assert result["real_model_proven"] is result["full_hydration_proven"] is False
    assert result["compaction_proven"] is result["lifecycle_accepted"] is False


def test_preseeded_receipt_is_not_natural_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    _capture(monkeypatch, root / "receipts", json.dumps(_wire()).encode())

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Preseeded evidence must fail before Popen")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="pristine receipt"):
        probe.run_reviewed_probe(root, digest)
    assert not (root / "attempt.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("grants_authority", 0),
        ("startup_abort_requested", 1),
        ("event_scope", "resume"),
        ("session_id", "../secret"),
        ("known_field_types", {}),
        ("extra", "PRIVATE-UNKNOWN-FIELD"),
    ],
)
def test_receipt_exact_schema_rejects_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _capture(monkeypatch, tmp_path, json.dumps(_wire()).encode())
    path = tmp_path / "SessionStart.json"
    document = json.loads(path.read_bytes())
    document[field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        probe._receipt(path, "SessionStart")


@pytest.mark.parametrize("ending", ["normal", "timeout", "overflow"])
def test_owned_session_cleanup_reaps_separate_hook_group_without_touching_unrelated_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    pid_path = tmp_path / "child.pid"
    grandchild = "import time; time.sleep(60)"
    leader_tail = {
        "normal": "raise SystemExit(0)",
        "timeout": "time.sleep(60)",
        "overflow": "os.write(1,b'x'*256); time.sleep(60)",
    }[ending]
    leader = (
        "import os,pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-I','-S','-c',{grandchild!r}],"
        "process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True);"
        f"pathlib.Path({str(pid_path)!r}).write_text("
        "f'{p.pid}:{os.getpgid(p.pid)}:{os.getsid(p.pid)}');"
        f"{leader_tail}"
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    table = probe._MacProcessTable()
    unrelated_identity = table.read(unrelated.pid)
    assert unrelated_identity is not None
    monkeypatch.setattr(probe, "TIMEOUT", 0.15)
    monkeypatch.setattr(probe, "OUTPUT_CAP", 32)
    try:
        if ending == "normal":
            code, stdout, stderr = probe._collect([sys.executable, "-I", "-S", "-c", leader], {})
            assert (code, stdout, stderr) == (0, b"", b"")
        else:
            expected = "output cap" if ending == "overflow" else "timeout"
            with pytest.raises(ValueError, match=expected):
                probe._collect([sys.executable, "-I", "-S", "-c", leader], {})
        child_pid, child_pgid, child_sid = (int(item) for item in pid_path.read_text().split(":"))
        assert child_pgid == child_pid
        assert child_sid != child_pgid
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            child = table.read(child_pid)
            if child is None or child.status == 5:
                break
            time.sleep(0.01)
        else:
            pytest.fail("separate hook process group survived cleanup")
        unrelated_after = table.read(unrelated.pid)
        assert unrelated_after is not None
        assert unrelated_after.stable() == unrelated_identity.stable()
        assert unrelated_after.status != 5
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=3)


def test_owned_signal_rejects_identity_drift_without_signalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = probe._ProcessIdentity(91001, os.getpid(), 91001, os.geteuid(), 100, 5, 2, 91001)
    changed = probe._ProcessIdentity(91001, os.getpid(), 91001, os.geteuid(), 100, 6, 2, 91001)

    class Table:
        def __init__(self) -> None:
            self.changed = False

        def read(self, _pid: int) -> probe._ProcessIdentity:
            return changed if self.changed else leader

        def unreaped_child(self, _pid: int) -> bool:
            return False

    table = Table()
    monkeypatch.setattr(os, "getsid", lambda pid: 777 if pid == 0 else 91001)
    owned = probe._OwnedSession(91001, table)  # type: ignore[arg-type]
    table.changed = True
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("drifted PID must not be signalled"))
    with pytest.raises(ValueError, match="anchor drift"):
        owned.signal(leader, signal.SIGKILL)


def test_cleanup_failure_has_explicit_failed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()

    def failed(*_args: object) -> tuple[int, bytes, bytes]:
        _identity_file(root)
        raise ValueError("owned session cleanup failed")

    monkeypatch.setattr(probe, "_collect", failed)
    with pytest.raises(AssertionError):
        probe.run_reviewed_probe(root, digest)
    result = json.loads((root / "result.json").read_bytes())
    assert result["passed"] is False
    assert result["failure"] == "owned-session-cleanup-failed"
    assert result["output_capture"] == "unavailable"


def test_admission_failure_emergency_cleanup_reaps_preexisting_separate_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "child.pid"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(60)'],"
        "process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid));"
        "time.sleep(60)"
    )
    real = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", script],
        cwd=probe.PROJECT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    child_pid = int(pid_path.read_text())
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: real)

    class RejectedOwner:
        def __init__(self, *_args: object) -> None:
            raise ValueError("forced post-Popen admission failure")

    monkeypatch.setattr(probe, "_OwnedSession", RejectedOwner)
    with pytest.raises(ValueError, match="owned session admission failed"):
        probe._collect(["ignored-by-controlled-factory"], {})
    assert real.returncode is not None
    table = probe._MacProcessTable()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        child = table.read(child_pid)
        if child is None or child.status == 5:
            break
        time.sleep(0.01)
    else:
        pytest.fail("separate-group descendant survived emergency cleanup")


def test_emergency_cleanup_rechecks_descendant_identity_before_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = probe._ProcessIdentity(92002, 92001, 92002, os.geteuid(), 10, 1, 2, 92001)
    reused = probe._ProcessIdentity(92002, 1, 92002, os.geteuid(), 11, 1, 2, 93000)

    class Table:
        def __init__(self) -> None:
            self.reads = 0

        def pids(self) -> tuple[int, ...]:
            return (92002,)

        def read(self, _pid: int) -> probe._ProcessIdentity:
            self.reads += 1
            return original if self.reads == 1 else reused

    class Process:
        pid = 92001

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            return -signal.SIGKILL

    calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(ValueError, match="emergency owned session cleanup failed"):
        probe._emergency_cleanup(Process(), Table())  # type: ignore[arg-type]
    assert calls == [(92001, signal.SIGSTOP), (92001, signal.SIGKILL)]


def test_emergency_census_failure_still_kills_and_reaps_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )

    class BrokenTable:
        def pids(self) -> tuple[int, ...]:
            raise ValueError("forced census failure")

    with pytest.raises(ValueError, match="emergency owned session cleanup failed"):
        probe._emergency_cleanup(process, BrokenTable())  # type: ignore[arg-type]
    assert process.returncode == -signal.SIGKILL
    probe._close_process_streams(process)


def test_sigcont_failure_remains_inside_cleanup_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_popen = subprocess.Popen
    original_kill = os.kill
    observed: list[subprocess.Popen[bytes]] = []

    def factory(args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", original_popen(args, **kwargs))
        observed.append(process)
        return process

    def fail_cont(pid: int, value: signal.Signals) -> None:
        if value == signal.SIGCONT:
            raise OSError("forced SIGCONT failure")
        original_kill(pid, value)

    monkeypatch.setattr(subprocess, "Popen", factory)
    monkeypatch.setattr(os, "kill", fail_cont)
    with pytest.raises(OSError, match="forced SIGCONT failure"):
        probe._collect([sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"], {})
    assert len(observed) == 1 and observed[0].returncode is not None


def test_selector_construction_failure_occurs_before_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        selectors,
        "DefaultSelector",
        lambda: (_ for _ in ()).throw(OSError("forced selector failure")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Popen must not run without a selector"),
    )
    with pytest.raises(OSError, match="forced selector failure"):
        probe._collect(["not-executed"], {})


def test_rich_cleanup_failure_falls_back_and_reaps_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "fallback-child.pid"
    grandchild = "import time;time.sleep(60)"
    leader = (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-I','-S','-c',{grandchild!r}],"
        "process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid));"
        "time.sleep(60)"
    )
    original_cleanup = probe._OwnedSession.cleanup
    original_popen = subprocess.Popen
    observed: list[subprocess.Popen[bytes]] = []

    def factory(args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", original_popen(args, **kwargs))
        observed.append(process)
        return process

    def failed_cleanup(_owned: probe._OwnedSession) -> None:
        raise ValueError("forced rich cleanup failure")

    monkeypatch.setattr(probe._OwnedSession, "cleanup", failed_cleanup)
    monkeypatch.setattr(subprocess, "Popen", factory)
    monkeypatch.setattr(probe, "TIMEOUT", 0.2)
    try:
        with pytest.raises(ValueError, match="owned session cleanup failed"):
            probe._collect([sys.executable, "-I", "-S", "-c", leader], {})
    finally:
        monkeypatch.setattr(probe._OwnedSession, "cleanup", original_cleanup)
    assert len(observed) == 1 and observed[0].returncode is not None
    assert pid_path.exists()
    child_pid = int(pid_path.read_text())
    table = probe._MacProcessTable()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        child = table.read(child_pid)
        if child is None or child.status == 5:
            break
        time.sleep(0.01)
    else:
        pytest.fail("fallback cleanup left a running separate-group child")


def test_failed_native_bootstrap_cannot_pass_or_persist_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    monkeypatch.setattr(
        probe,
        "_collect",
        lambda *_args: (
            73,
            b"PRIVATE-PROMPT-OR-OUTPUT",
            b"Operation not permitted database PRIVATE-STDERR",
        ),
    )
    with pytest.raises(AssertionError, match="Natural Mac hook gate failed"):
        probe.run_reviewed_probe(root, digest)
    raw = (root / "result.json").read_bytes()
    assert b"PRIVATE" not in raw
    result = json.loads(raw)
    assert result["passed"] is False and result["natural_events"] == []
    assert result["failure"] == "installation-identity-missing"
    assert result["installation_identity"]["status"] == "missing"
    assert result["diagnostic_categories"] == [
        "kernel-permission-denied",
        "local-state-initialization-mentioned",
    ]
    with pytest.raises(FileExistsError):
        probe.run_reviewed_probe(root, digest)


def test_dyld_root_directory_read_is_literal_not_recursive() -> None:
    root = Path("/review-only-private-scratch")
    profile = probe._profile(root, Path("/review-python/bin/python"), Path("/review-python/stdlib"))
    assert profile.count('(literal "/")') == 1
    assert '(allow file-read-data (literal "/") ' in profile
    assert '(subpath "/")' not in profile
    assert "Cryptex" not in profile
    for unchanged_deny in (
        "network*",
        "mach-lookup",
        "file-read-data",
        "file-write*",
        "process-exec",
    ):
        assert f"(deny {unchanged_deny})" in profile
    assert '(allow file-write* (subpath "/review-only-private-scratch/tmp") ' in profile
    assert '(subpath "/Users")' not in profile


def test_cwd_ab_profile_adds_exactly_two_directory_literals_to_reviewed_baseline() -> None:
    profile = probe._profile(
        Path("/review-only-private-scratch"),
        Path("/review-python/bin/python"),
        Path("/review-python/stdlib"),
    )
    for directory in (probe.PROJECT, probe.PROJECT.parent):
        literal = f'(literal "{directory}") '
        assert profile.count(literal) == 1
        assert f'(subpath "{directory}")' not in profile
        profile = profile.replace(literal, "", 1)
    normalized = profile.replace(pwd.getpwuid(os.geteuid()).pw_dir, "USER_HOME")
    normalized = normalized.replace(
        '(allow file-write* (literal "/review-only-private-scratch/codex/installation_id"))\n',
        "",
        1,
    )
    assert normalized.count('(literal "/bin/zsh")') == 2
    normalized = normalized.replace('(literal "/bin/zsh") ', "", 1)
    normalized = normalized.replace(' (literal "/bin/zsh")', "", 1)
    runtime = f'(literal "{probe.PYTHON_RUNTIME}")'
    assert normalized.count(runtime) == 2
    normalized = normalized.replace(runtime + " ", "", 1)
    normalized = normalized.replace(" " + runtime, "", 1)
    assert probe.sha(normalized.encode()) == (
        "6b7f93286d8c4c93acc51e5fbd4f6b5108d7071d2c59dd8dfadcc70ef35dcebc"
    )


def test_zsh_profile_delta_is_two_exact_literals_and_no_broader_grant() -> None:
    profile = probe._profile(
        Path("/review-only-private-scratch"),
        Path("/review-python/bin/python"),
        Path("/review-python/stdlib"),
    )
    assert profile.count('(literal "/bin/zsh")') == 2
    assert '(subpath "/bin")' not in profile
    assert '(subpath "/bin/zsh")' not in profile
    assert '(allow file-write* (literal "/bin/zsh"))' not in profile
    assert "network*" in profile and "mach-lookup" in profile
    assert "state_5.sqlite" not in profile and "/usr/bin/git" not in profile


def test_framework_python_profile_delta_is_two_exact_literals_only() -> None:
    profile = probe._profile(
        Path("/review-only-private-scratch"),
        Path("/review-python/bin/python"),
        Path("/review-python/stdlib"),
    )
    literal = f'(literal "{probe.PYTHON_RUNTIME}")'
    assert profile.count(literal) == 2
    assert f'(subpath "{probe.PYTHON_RUNTIME.parent}")' not in profile
    assert f'(subpath "{probe.PYTHON_RUNTIME}")' not in profile
    assert f"(allow file-write* {literal})" not in profile
    assert "/usr/bin/git" not in profile and "state_5.sqlite" not in profile
    for unchanged in ("network*", "mach-lookup", "file-write*"):
        assert f"(deny {unchanged})" in profile


def test_system_zsh_drift_rejected_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    identity, _ = probe.system_zsh_identity()
    monkeypatch.setattr(probe, "system_zsh_identity", lambda: (identity, b"changed"))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("zsh drift must reject before Popen"),
    )
    with pytest.raises(ValueError, match="system zsh identity drift"):
        probe.run_reviewed_probe(root, digest)
    assert not (root / "attempt.json").exists()


def test_framework_python_drift_rejected_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    identity, _ = probe.framework_python_identity()
    monkeypatch.setattr(probe, "framework_python_identity", lambda: (identity, b"changed"))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Python runtime drift must reject before Popen"),
    )
    with pytest.raises(ValueError, match="framework Python identity drift"):
        probe.run_reviewed_probe(root, digest)
    assert not (root / "attempt.json").exists()


def test_diagnostic_warning_and_fatal_are_distinct_and_content_free() -> None:
    root = Path("/review-private-root")
    stderr = (
        probe.PATH_ALIAS_WARNING
        + b" Operation not permitted /review-private-root/codex/tmp SECRET-A\n"
        + b"Error: Failed to determine current directory "
        + b"'/Users/mkaracan/Projeler/akilli-kasa' SECRET-B\n"
        + b"Error: Operation not permitted (os error 1) SECRET-C\n"
        + b"UNMATCHED /Users/private/person/key SECRET-D\n\xff\xfe\n"
    )
    result = probe.diagnostic_lines(b"", stderr, root)
    assert result["warning_line_count"] == 1 and result["error_line_count"] == 2
    assert result["unclassified_line_count"] == 2
    assert result["path_alias_warning_present"] is True
    assert result["fatal_generic_eperm_present"] is True
    assert result["warning_tags"] == ["path-alias-creation"]
    assert result["fatal_tags"] == ["cwd-resolution", "generic-permission-denied"]
    assert result["warning_resource_tags"] == ["probe-codex-temp"]
    assert result["fatal_resource_tags"] == ["source-project-root"]
    encoded = json.dumps(result)
    for canary in ("SECRET", "person", "review-private-root", "akilli-kasa", "Users", "xff"):
        assert canary not in encoded
    assert result["raw_content_persisted"] is False


@pytest.mark.parametrize(
    "prefix,tag",
    [
        (b"Failed to determine working directory", "cwd-resolution"),
        (b"failed to determine the working directory", "cwd-resolution"),
        (b"failed to read current working directory", "cwd-resolution"),
        (b"failed to canonicalize CODEX_HOME ", "home-canonicalization"),
        (b"failed to read directory", "directory-read"),
        (b"Failed to load config", "config-loading"),
        (b"failed to load bootstrap config", "config-loading"),
        (b"Failed to read project config", "config-loading"),
    ],
)
def test_diagnostic_static_prefixes_never_echo_canaries(prefix: bytes, tag: str) -> None:
    result = probe.diagnostic_lines(
        b"", b"Error: " + prefix + b" SECRET_PROMPT /other/private\n", Path("/review")
    )
    assert result["fatal_tags"] == [tag] and result["warning_tags"] == []
    assert "SECRET" not in json.dumps(result) and "private" not in json.dumps(result)


def test_diagnostic_false_path_prefix_and_unmatched_prompt_do_not_classify() -> None:
    result = probe.diagnostic_lines(
        b"arbitrary prompt containing /Users/mkaracan/Projeler/akilli-kasa\n",
        b"Error: Other failure /Users/mkaracan/Projeler/akilli-kasa-private\n",
        Path("/review"),
    )
    assert result["warning_resource_tags"] == result["fatal_resource_tags"] == []
    assert result["warning_tags"] == result["fatal_tags"] == []
    assert result["unclassified_line_count"] == 1 and result["error_line_count"] == 1


@pytest.mark.parametrize("value", [None, False, "text", bytearray(b"x")])
def test_diagnostic_wrong_type_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="must be bytes"):
        probe.diagnostic_lines(value, b"", Path("/review"))  # type: ignore[arg-type]


def test_diagnostic_byte_and_line_bounds() -> None:
    with pytest.raises(ValueError, match="byte cap"):
        probe.diagnostic_lines(b"x" * (probe.OUTPUT_CAP + 1), b"", Path("/review"))
    result = probe.diagnostic_lines(b"x\n" * (probe.DIAGNOSTIC_LINE_CAP + 1), b"", Path("/review"))
    assert result["lines_scanned"] == probe.DIAGNOSTIC_LINE_CAP
    assert result["line_limit_reached"] is True
    assert result["warning_tags"] == result["fatal_tags"] == []


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
@pytest.mark.parametrize("transcript", [None, "TRANSCRIPT-CANARY-NEVER-READ"])
def test_release_nullable_transcript_preserves_only_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, transcript: str | None
) -> None:
    wire = _wire()
    wire["hook_event_name"] = event
    wire["transcript_path"] = transcript
    if event == "SessionEnd":
        del wire["source"]
        wire["reason"] = "other"
    code, reply = _capture(monkeypatch, tmp_path, json.dumps(wire).encode(), event)
    assert code == 0
    assert json.loads(reply) == (
        {"continue": False, "stopReason": probe.STOP_MARKER} if event == "SessionStart" else {}
    )
    receipt = probe._receipt(tmp_path / (event + ".json"), event)
    assert receipt["known_field_types"]["transcript_path"] == (
        "NoneType" if transcript is None else "str"
    )
    assert "CANARY" not in json.dumps(receipt)


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
@pytest.mark.parametrize("transcript", [False, 1, [], {}, "", "x" * 4097, "MISSING"])
def test_nullable_transcript_is_not_optional_or_arbitrary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, transcript: object
) -> None:
    wire = _wire()
    wire["hook_event_name"] = event
    wire["transcript_path"] = transcript
    if transcript == "MISSING":
        del wire["transcript_path"]
    if event == "SessionEnd":
        del wire["source"]
        wire["reason"] = "other"
    code, _ = _capture(monkeypatch, tmp_path, json.dumps(wire).encode(), event)
    assert code == 2 and not tuple(tmp_path.iterdir())


ID_CANARY = b"01234567-89ab-4cde-8f01-23456789abcd"


def _identity_file(root: Path, raw: bytes = ID_CANARY) -> Path:
    (root / "codex").mkdir(mode=0o700, exist_ok=True)
    path = root / "codex/installation_id"
    path.write_bytes(raw)
    path.chmod(0o644)
    return path


def test_installation_identity_audit_never_records_value_or_digest(tmp_path: Path) -> None:
    _identity_file(tmp_path)
    result = probe.installation_id_observation(tmp_path)
    assert result == {
        "status": "valid",
        "kind": "canonical-uuid-v4",
        "bytes": 36,
        "mode": "0644",
        "single_link": True,
        "value_or_digest_recorded": False,
    }
    assert ID_CANARY.decode() not in json.dumps(result)
    assert probe.sha(ID_CANARY) not in json.dumps(result)


@pytest.mark.parametrize("kind", ["missing", "symlink", "hardlink", "directory", "mode", "owner"])
def test_installation_identity_filesystem_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = _identity_file(tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "missing-target")
    elif kind == "hardlink":
        (tmp_path / "second-link").hardlink_to(path)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "mode":
        path.chmod(0o600)
    elif kind == "owner":
        original = os.fstat

        def wrong_owner(fd: int) -> os.stat_result:
            values = list(original(fd))
            values[4] = os.geteuid() + 1
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises((ValueError, FileNotFoundError)):
        probe.installation_id_observation(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 37,
        ID_CANARY.upper(),
        b"01234567-89ab-1cde-8f01-23456789abcd",
        b"01234567-89ab-4cde-1f01-23456789abcd",
        b"!" * 36,
        b"\xff" * 36,
        ID_CANARY + b"\n",
    ],
)
def test_installation_identity_rejects_invalid_uuid_or_size(tmp_path: Path, raw: bytes) -> None:
    _identity_file(tmp_path, raw)
    with pytest.raises(ValueError):
        probe.installation_id_observation(tmp_path)


@pytest.mark.parametrize("symlink", [False, True])
def test_preset_installation_identity_rejected_before_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    if symlink:
        (root / "codex/installation_id").symlink_to(root / "missing")
    else:
        _identity_file(root)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Preset installation ID must reject before native")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="pristine installation"):
        probe.run_reviewed_probe(root, digest)
    assert not (root / "attempt.json").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_installation_identity_race_between_preflights_rejects_before_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()
    original = probe._new_file

    def insert_after_attempt(directory: int, name: str, raw: bytes) -> None:
        original(directory, name, raw)
        if name == "attempt.json":
            if symlink:
                (root / "codex/installation_id").symlink_to(root / "missing")
            else:
                _identity_file(root)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ID introduced between preflights must reject before native")

    monkeypatch.setattr(probe, "_new_file", insert_after_attempt)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(AssertionError):
        probe.run_reviewed_probe(root, digest)
    result = json.loads((root / "result.json").read_bytes())
    assert result["passed"] is False
    assert result["returncode"] is None
    assert result["output_capture"] == "unavailable"
    assert result["natural_events"] == []
    assert result["failure"] == (
        "installation-identity-invalid" if symlink else "bounded-native-invocation-failed"
    )


@pytest.mark.parametrize("valid", [False, True])
def test_post_identity_audit_runs_even_after_failed_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()

    def failed_collector(*_args: object) -> tuple[int, bytes, bytes]:
        _identity_file(root, ID_CANARY if valid else b"SECRET" * 6)
        raise ValueError("native hook observation timeout")

    monkeypatch.setattr(probe, "_collect", failed_collector)
    with pytest.raises(AssertionError):
        probe.run_reviewed_probe(root, digest)
    raw = (root / "result.json").read_bytes()
    result = json.loads(raw)
    assert result["installation_identity"]["status"] == ("valid" if valid else "invalid")
    assert result["failure"] == ("native-timeout" if valid else "installation-identity-invalid")
    assert b"SECRET" not in raw and ID_CANARY not in raw


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("native hook observation timeout"),
        ValueError("native hook observation output cap"),
        OSError("SECRET-IO-ERROR"),
    ],
)
def test_interrupted_collection_is_unavailable_not_observed_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()

    def interrupted(*_args: object) -> tuple[int, bytes, bytes]:
        _identity_file(root)
        raise failure

    monkeypatch.setattr(probe, "_collect", interrupted)
    with pytest.raises(AssertionError):
        probe.run_reviewed_probe(root, digest)
    raw = (root / "result.json").read_bytes()
    result = json.loads(raw)
    assert result["output_capture"] == "unavailable"
    assert result["passed"] is False
    for field in (
        "returncode",
        "stdout_bytes",
        "stderr_bytes",
        "diagnostic_lines",
        "diagnostic_categories",
        "startup_stop_marker_observed",
    ):
        assert result[field] is None
    assert b"SECRET" not in raw


def test_complete_empty_collection_is_observed_zero_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(probe, "EVIDENCE", tmp_path)
    root, digest = probe.prepare_probe()

    def complete(*_args: object) -> tuple[int, bytes, bytes]:
        _identity_file(root)
        return 1, b"", b""

    monkeypatch.setattr(probe, "_collect", complete)
    with pytest.raises(AssertionError):
        probe.run_reviewed_probe(root, digest)
    result = json.loads((root / "result.json").read_bytes())
    assert result["output_capture"] == "complete"
    assert result["returncode"] == 1
    assert result["stdout_bytes"] == result["stderr_bytes"] == 0
    assert result["diagnostic_lines"]["lines_scanned"] == 0
    assert result["diagnostic_categories"] == []
    assert result["startup_stop_marker_observed"] is False
    assert result["passed"] is False
