"""Cross-platform private local path identity checks."""

from __future__ import annotations

import csv
import ctypes
import os
import re
import stat
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

_ACE = re.compile(r"\(([^()]*)\)")
_ALLOWED_WINDOWS_TRUSTEES = frozenset({"SY", "S-1-5-18", "BA", "S-1-5-32-544", "OW"})


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid")  # noqa: B009 -- absent from Windows stubs
    return int(getter())


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


@lru_cache(maxsize=1)
def windows_user_sid() -> str:
    if os.name != "nt":
        raise RuntimeError("Windows SID requested outside Windows")
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = Path(system_root) / "System32" / "whoami.exe"
    run = subprocess.run(
        [str(executable), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    try:
        row = next(csv.reader([run.stdout.strip()]))
        sid = row[1]
    except (IndexError, StopIteration) as exc:
        raise OSError("Windows user SID discovery failed") from exc
    if run.returncode != 0 or not re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", sid):
        raise OSError("Windows user SID discovery failed")
    return sid


def _windows_sddl(path: Path) -> str:
    windll: Any = getattr(ctypes, "windll")  # noqa: B009 -- Windows-only API
    security_descriptor = ctypes.c_void_p()
    result = windll.advapi32.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000005,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        None,
        None,
        None,
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0 or not security_descriptor.value:
        raise OSError("Windows security descriptor unavailable")
    rendered = ctypes.c_wchar_p()
    try:
        converted = windll.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            security_descriptor,
            1,
            0x00000005,
            ctypes.byref(rendered),
            None,
        )
        if not converted or rendered.value is None:
            raise OSError("Windows security descriptor conversion failed")
        return rendered.value
    finally:
        if rendered:
            windll.kernel32.LocalFree(rendered)
        windll.kernel32.LocalFree(security_descriptor)


def _windows_acl_is_private(path: Path) -> bool:
    try:
        sddl = _windows_sddl(path)
        sid = windows_user_sid()
    except OSError:
        return False
    owner = sddl.partition("O:")[2].partition("G:")[0].partition("D:")[0]
    if owner not in {sid, "OW"}:
        return False
    allowed = _ALLOWED_WINDOWS_TRUSTEES | {sid}
    grants = 0
    for raw in _ACE.findall(sddl.partition("D:")[2].partition("S:")[0]):
        fields = raw.split(";")
        if len(fields) != 6 or fields[0] not in {"A", "OA"}:
            continue
        grants += 1
        if fields[5] not in allowed:
            return False
    return grants > 0


def private_regular(path: Path, mode: int = 0o600) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or path.is_symlink()
        or _is_reparse(info)
    ):
        return False
    if os.name == "nt":
        readonly = bool(
            int(getattr(info, "st_file_attributes", 0))
            & int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0))
        )
        return readonly == (mode & 0o222 == 0) and _windows_acl_is_private(path)
    return (
        info.st_uid == _effective_user_id()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == mode
    )


def owned_regular(path: Path) -> bool:
    """Accept an owner-controlled regular executable without requiring mode 0600."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or _is_reparse(info):
        return False
    if os.name == "nt":
        return _windows_acl_is_private(path)
    return (
        info.st_uid == _effective_user_id()
        and info.st_nlink == 1
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def private_directory(path: Path, mode: int = 0o700) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or _is_reparse(info):
        return False
    if os.name == "nt":
        return _windows_acl_is_private(path)
    return info.st_uid == _effective_user_id() and stat.S_IMODE(info.st_mode) == mode


def restrict_private_tree(root: Path) -> None:
    """Replace inherited Windows ACLs with owner/SYSTEM/Administrators only."""
    if os.name != "nt":
        root.chmod(0o700)
        return
    sid = windows_user_sid()
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = Path(system_root) / "System32" / "icacls.exe"
    commands = (
        [str(executable), str(root), "/reset", "/Q"],
        [
            str(executable),
            str(root),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            "/Q",
        ],
        [str(executable), str(root / "*"), "/reset", "/T", "/C", "/Q"],
    )
    for command in commands:
        run = subprocess.run(command, capture_output=True, check=False, timeout=30)
        if run.returncode != 0 or len(run.stdout) > 8192 or len(run.stderr) > 8192:
            raise OSError("Windows private ACL application failed")
    if not private_directory(root):
        raise OSError("Windows private ACL verification failed")


def restrict_private_file(path: Path, *, mode: int = 0o600) -> None:
    if os.name != "nt":
        path.chmod(mode)
        return
    sid = windows_user_sid()
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = Path(system_root) / "System32" / "icacls.exe"
    run = subprocess.run(
        [
            str(executable),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:F",
            "*S-1-5-18:F",
            "*S-1-5-32-544:F",
            "/Q",
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if run.returncode != 0 or len(run.stdout) > 8192 or len(run.stderr) > 8192:
        raise OSError("Windows private file ACL application failed")
    path.chmod(mode)
    if not private_regular(path, mode):
        raise OSError("Windows private file ACL verification failed")
