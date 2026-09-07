from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from zekam.infrastructure import local_file_security
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_or_sandbox_readonly_directory,
    private_regular,
    restrict_private_tree,
    windows_codex_sandbox_sid,
)


def _system_tool(name: str) -> Path:
    return Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / name


def test_private_tree_rejects_permission_or_acl_drift(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    file = root / "state.db"
    file.write_bytes(b"state")
    restrict_private_tree(root)
    file.chmod(0o600)
    assert private_directory(root)
    assert private_regular(file)

    if os.name == "nt":
        weakened = subprocess.run(
            [str(_system_tool("icacls.exe")), str(file), "/grant", "*S-1-1-0:W", "/Q"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert weakened.returncode == 0, weakened.stderr
    else:
        file.chmod(0o644)
    assert not private_regular(file)


def test_private_directory_rejects_windows_junction_or_posix_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    restrict_private_tree(target)
    alias = tmp_path / "alias"
    if os.name == "nt":
        created = subprocess.run(
            [str(_system_tool("cmd.exe")), "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert created.returncode == 0, created.stderr
    else:
        alias.symlink_to(target, target_is_directory=True)
    assert not private_directory(alias)


def test_windows_root_accepts_only_exact_sandbox_read_traverse_acl(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    root = tmp_path / "sandbox-root"
    root.mkdir()
    restrict_private_tree(root)
    sandbox_sid = windows_codex_sandbox_sid()
    assert sandbox_sid is not None
    assert private_or_sandbox_readonly_directory(root)

    readonly = subprocess.run(
        [str(_system_tool("icacls.exe")), str(root), "/grant", f"*{sandbox_sid}:RX", "/Q"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert readonly.returncode == 0, readonly.stderr
    assert private_or_sandbox_readonly_directory(root)

    writable = subprocess.run(
        [str(_system_tool("icacls.exe")), str(root), "/grant:r", f"*{sandbox_sid}:M", "/Q"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert writable.returncode == 0, writable.stderr
    assert not private_or_sandbox_readonly_directory(root)

    restrict_private_tree(root)
    unknown = subprocess.run(
        [str(_system_tool("icacls.exe")), str(root), "/grant", "*S-1-1-0:RX", "/Q"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert unknown.returncode == 0, unknown.stderr
    assert not private_or_sandbox_readonly_directory(root)


def test_windows_root_rejects_conditional_or_callback_allow_ace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        return
    root = tmp_path / "conditional-root"
    root.mkdir()
    restrict_private_tree(root)
    monkeypatch.setattr(
        local_file_security,
        "_windows_sddl",
        lambda _path: "O:OWG:SYD:(A;;FA;;;SY)(XA;;FA;;;WD;(TRUE))",
    )
    monkeypatch.setattr(local_file_security, "windows_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(
        local_file_security, "windows_codex_sandbox_sid", lambda: "S-1-5-21-2"
    )

    assert not private_or_sandbox_readonly_directory(root)
    assert not private_directory(root)
