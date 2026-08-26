"""OpenCode plugin spool status and reversible legacy candidate hygiene."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed

_CANDIDATE = re.compile(r"^\.drain\.candidate\.([0-9a-fA-F-]{36})$")
_LOCK_NAME = ".drain.lock"
_MINIMUM_STALE_SECONDS = 300


def plugin_spool_root(home: Path) -> Path:
    return home / "global" / "runtime" / "opencode-plugin-spool"


def _ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return path.is_symlink() or bool(reparse and attributes & reparse)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ctypes.get_last_error() == 5
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LegacyCandidate:
    name: str
    candidate_ref: str
    pid: int
    age_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "pid": self.pid,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class SpoolStatus:
    exists: bool
    queued: int
    quarantine: int
    lock_present: bool
    legacy_candidates: int
    eligible_legacy_candidates: int
    invalid_legacy_candidates: int
    unrecognized_entries: int
    cleanup_plan_digest: str

    @property
    def healthy(self) -> bool:
        return not (
            self.queued
            or self.lock_present
            or self.legacy_candidates
            or self.invalid_legacy_candidates
            or self.unrecognized_entries
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-opencode-spool-status/v1",
            "exists": self.exists,
            "queued": self.queued,
            "quarantine": self.quarantine,
            "lock_present": self.lock_present,
            "legacy_candidates": self.legacy_candidates,
            "eligible_legacy_candidates": self.eligible_legacy_candidates,
            "invalid_legacy_candidates": self.invalid_legacy_candidates,
            "unrecognized_entries": self.unrecognized_entries,
            "cleanup_plan_digest": self.cleanup_plan_digest,
            "healthy": self.healthy,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class LegacyCleanupPlan:
    home: Path
    candidates: tuple[LegacyCandidate, ...]
    invalid_count: int
    unrecognized_count: int
    observed_at: dt.datetime
    plan_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-opencode-legacy-cleanup-plan/v1",
            "candidate_count": len(self.candidates),
            "candidate_refs": [item.candidate_ref for item in self.candidates],
            "invalid_count": self.invalid_count,
            "unrecognized_count": self.unrecognized_count,
            "observed_at": self.observed_at,
            "plan_digest": self.plan_digest,
            "reversible": True,
            "raw_delete": False,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class LegacyCleanupReceipt:
    plan_digest: str
    receipt_digest: str
    moved: int
    quarantine_refs: tuple[str, ...]
    completed_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-opencode-legacy-cleanup-receipt/v1",
            "plan_digest": self.plan_digest,
            "receipt_digest": self.receipt_digest,
            "moved": self.moved,
            "quarantine_refs": list(self.quarantine_refs),
            "completed_at": self.completed_at,
            "reversible": True,
            "raw_delete": False,
            "grants_authority": False,
        }


def _candidate(
    path: Path,
    *,
    now: dt.datetime,
    minimum_stale_seconds: int,
) -> LegacyCandidate | None:
    matched = _CANDIDATE.fullmatch(path.name)
    if matched is None or not path.is_dir() or _is_link_or_reparse(path):
        return None
    try:
        token = str(UUID(matched.group(1)))
    except ValueError:
        return None
    entries = tuple(path.iterdir())
    if tuple(item.name for item in entries) != ("owner.json",):
        return None
    owner_path = entries[0]
    if not owner_path.is_file() or _is_link_or_reparse(owner_path):
        return None
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
        owner_token = str(UUID(str(owner["ownerToken"])))
    except (KeyError, TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if owner_token != token or pid <= 0 or _process_alive(pid):
        return None
    modified_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    age_seconds = int((now - modified_at).total_seconds())
    if age_seconds < minimum_stale_seconds:
        return None
    return LegacyCandidate(path.name, _ref(path.name), pid, age_seconds)


def plan_legacy_candidate_cleanup(
    home: Path,
    *,
    now: dt.datetime | None = None,
    minimum_stale_seconds: int = _MINIMUM_STALE_SECONDS,
) -> LegacyCleanupPlan:
    if minimum_stale_seconds < _MINIMUM_STALE_SECONDS:
        raise ValidationFailed("Legacy candidate stale siniri 300 saniyeden kucuk olamaz")
    observed_at = now or dt.datetime.now(dt.UTC)
    root = plugin_spool_root(home)
    eligible: list[LegacyCandidate] = []
    invalid = 0
    unrecognized = 0
    if root.exists():
        if not root.is_dir() or _is_link_or_reparse(root):
            raise ConfigurationError("OpenCode plugin spool regular directory olmali")
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name in {"quarantine", _LOCK_NAME} or (
                path.is_file() and path.name.endswith(".json")
            ):
                continue
            if _CANDIDATE.fullmatch(path.name):
                item = _candidate(
                    path,
                    now=observed_at,
                    minimum_stale_seconds=minimum_stale_seconds,
                )
                if item is None:
                    invalid += 1
                else:
                    eligible.append(item)
            else:
                unrecognized += 1
    body = {
        "schema": "zekam-opencode-legacy-cleanup-plan/v1",
        "candidate_refs": [item.candidate_ref for item in eligible],
        "invalid_count": invalid,
        "unrecognized_count": unrecognized,
        "minimum_stale_seconds": minimum_stale_seconds,
    }
    return LegacyCleanupPlan(
        home=home,
        candidates=tuple(eligible),
        invalid_count=invalid,
        unrecognized_count=unrecognized,
        observed_at=observed_at,
        plan_digest=digest(body),
    )


def inspect_spool(home: Path, *, now: dt.datetime | None = None) -> SpoolStatus:
    root = plugin_spool_root(home)
    plan = plan_legacy_candidate_cleanup(home, now=now)
    if not root.exists():
        return SpoolStatus(False, 0, 0, False, 0, 0, 0, 0, plan.plan_digest)
    paths = tuple(root.iterdir())
    legacy = sum(1 for item in paths if _CANDIDATE.fullmatch(item.name))
    quarantine = root / "quarantine"
    return SpoolStatus(
        exists=True,
        queued=sum(1 for item in paths if item.is_file() and item.name.endswith(".json")),
        quarantine=(len(tuple(quarantine.iterdir())) if quarantine.is_dir() else 0),
        lock_present=(root / _LOCK_NAME).exists(),
        legacy_candidates=legacy,
        eligible_legacy_candidates=len(plan.candidates),
        invalid_legacy_candidates=plan.invalid_count,
        unrecognized_entries=plan.unrecognized_count,
        cleanup_plan_digest=plan.plan_digest,
    )


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply_legacy_candidate_cleanup(
    home: Path,
    *,
    expected_plan_digest: str,
    now: dt.datetime | None = None,
) -> LegacyCleanupReceipt:
    plan = plan_legacy_candidate_cleanup(home, now=now)
    if plan.plan_digest != expected_plan_digest:
        raise PolicyViolation("OpenCode legacy cleanup plan digest drift")
    if plan.invalid_count or plan.unrecognized_count:
        raise PolicyViolation("OpenCode legacy cleanup yalniz exact typed candidate kabul eder")
    root = plugin_spool_root(home)
    quarantine = root / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(quarantine):
        raise ConfigurationError("OpenCode quarantine link veya reparse point olamaz")
    moved: list[tuple[Path, Path]] = []
    try:
        for item in plan.candidates:
            source = root / item.name
            target = quarantine / f"legacy-drain-candidate.{uuid4()}"
            source.replace(target)
            moved.append((source, target))
    except BaseException:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                target.replace(source)
        raise
    completed_at = dt.datetime.now(dt.UTC)
    quarantine_refs = tuple(_ref(target.name) for _, target in moved)
    body = {
        "schema": "zekam-opencode-legacy-cleanup-receipt/v1",
        "plan_digest": plan.plan_digest,
        "moved": len(moved),
        "candidate_refs": [item.candidate_ref for item in plan.candidates],
        "quarantine_refs": list(quarantine_refs),
        "completed_at": completed_at.isoformat(),
        "reversible": True,
        "raw_delete": False,
        "grants_authority": False,
    }
    receipt_digest = digest(body)
    _atomic_json(
        quarantine / f"cleanup-receipt-{receipt_digest.removeprefix('sha256:')}.json",
        body | {"receipt_digest": receipt_digest},
    )
    return LegacyCleanupReceipt(
        plan.plan_digest,
        receipt_digest,
        len(moved),
        quarantine_refs,
        completed_at,
    )
