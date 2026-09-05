from __future__ import annotations

import os
import subprocess
from pathlib import Path

from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_tree,
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
