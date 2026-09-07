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
_WINDOWS_READ_TRAVERSE_MASK = 0x001200A9


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


@lru_cache(maxsize=1)
def windows_codex_sandbox_sid() -> str | None:
    """Resolve the optional local Codex read-only traversal group."""

    if os.name != "nt":
        return None
    computer = os.environ.get("COMPUTERNAME")
    if not computer:
        return None
    account = f"{computer}\\CodexSandboxUsers"
    windll: Any = getattr(ctypes, "windll")  # noqa: B009 -- Windows-only API
    advapi32 = windll.advapi32
    lookup = advapi32.LookupAccountNameW
    lookup.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lookup.restype = ctypes.c_int
    sid_size = ctypes.c_ulong()
    domain_size = ctypes.c_ulong()
    account_type = ctypes.c_uint()
    lookup(
        None,
        account,
        None,
        ctypes.byref(sid_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(account_type),
    )
    if not sid_size.value:
        return None
    sid_buffer = ctypes.create_string_buffer(sid_size.value)
    domain_buffer = ctypes.create_unicode_buffer(max(1, domain_size.value))
    if not lookup(
        None,
        account,
        sid_buffer,
        ctypes.byref(sid_size),
        domain_buffer,
        ctypes.byref(domain_size),
        ctypes.byref(account_type),
    ):
        return None
    if account_type.value != 4 or domain_buffer.value.casefold() != computer.casefold():
        return None
    rendered = ctypes.c_wchar_p()
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_int
    if not convert(sid_buffer, ctypes.byref(rendered)) or rendered.value is None:
        return None
    try:
        return rendered.value
    finally:
        windll.kernel32.LocalFree(rendered)


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
    entries = _windows_dacl_entries(sddl)
    if entries is None:
        return False
    grants = 0
    for fields in entries:
        if fields[0] in {"D", "OD"}:
            continue
        if fields[0] not in {"A", "OA"}:
            return False
        grants += 1
        if fields[5] not in allowed:
            return False
    return grants > 0


def _windows_dacl_entries(sddl: str) -> tuple[tuple[str, ...], ...] | None:
    """Parse only simple known SDDL ACEs; conditional/callback forms fail closed."""

    dacl = sddl.partition("D:")[2].partition("S:")[0]
    first = dacl.find("(")
    if first < 0 or not re.fullmatch(r"(?:P|AI|AR)*", dacl[:first]):
        return None
    body = dacl[first:]
    matches = tuple(_ACE.finditer(body))
    if not matches or "".join(match.group(0) for match in matches) != body:
        return None
    entries = tuple(tuple(match.group(1).split(";")) for match in matches)
    if any(len(fields) != 6 for fields in entries):
        return None
    return entries


def _windows_rights_mask(value: str) -> int | None:
    if value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    symbolic = {
        "FR": 0x00120089,
        "FX": 0x001200A0,
        "GR": 0x80000000,
        "GX": 0x20000000,
        "RC": 0x00020000,
    }
    if len(value) % 2:
        return None
    mask = 0
    for offset in range(0, len(value), 2):
        right = symbolic.get(value[offset : offset + 2])
        if right is None:
            return None
        mask |= right
    return mask


def private_or_sandbox_readonly_directory(path: Path) -> bool:
    """Accept a private directory or exact Codex read/traverse access at its root."""

    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        return False
    if os.name != "nt":
        return info.st_uid == _effective_user_id() and stat.S_IMODE(info.st_mode) == 0o700
    try:
        sddl = _windows_sddl(path)
        user_sid = windows_user_sid()
        sandbox_sid = windows_codex_sandbox_sid()
    except OSError:
        return False
    owner = sddl.partition("O:")[2].partition("G:")[0].partition("D:")[0]
    if owner not in {user_sid, "OW"}:
        return False
    private_trustees = _ALLOWED_WINDOWS_TRUSTEES | {user_sid}
    entries = _windows_dacl_entries(sddl)
    if entries is None:
        return False
    grants = 0
    for fields in entries:
        if fields[0] in {"D", "OD"}:
            continue
        if fields[0] not in {"A", "OA"}:
            return False
        grants += 1
        trustee = fields[5]
        if trustee in private_trustees:
            continue
        mask = _windows_rights_mask(fields[2])
        if (
            sandbox_sid is None
            or trustee != sandbox_sid
            or mask is None
            or mask == 0
            or mask & ~(_WINDOWS_READ_TRAVERSE_MASK | 0xA0000000)
        ):
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
