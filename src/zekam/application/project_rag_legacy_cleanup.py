"""Kullanilmayan legacy BGE proje indekslerini planli olarak kaldirir.

Project RAG'in kanonik varsayilani yerel multilingual-e5-small'dur. Eski BGE
vektor agaci derived veridir; yeni E5 indeksine sessizce tasinmaz veya dahil
edilmez. Bu modul yalniz exact plan digest'i ve acik ``apply`` ile, ZEKAM_HOME
altindaki beklenen legacy model kokunu siler.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.home import HomeLayout
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug
from zekam.infrastructure.local_file_security import path_or_parent_is_link_or_reparse

LEGACY_MODEL_DIRECTORY = "opencode-bge-m3"
LOCAL_MODEL_DIRECTORY = "multilingual-e5-small-onnx-int8"
_MAX_PROJECTS = 10_000
_MAX_TREE_ENTRIES = 250_000
_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024


def _utc(value: dt.datetime | None) -> dt.datetime:
    moment = value or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValidationFailed("Legacy index cleanup zamani timezone olmali")
    return moment.astimezone(dt.UTC)


def _home_root(home: Path) -> Path:
    if not isinstance(home, Path) or not home.is_absolute():
        raise ConfigurationError("Legacy index cleanup home absolute olmali")
    if path_or_parent_is_link_or_reparse(home):
        raise PolicyViolation("Legacy index cleanup home link/reparse olamaz")
    root = home.resolve(strict=True)
    if not root.is_dir():
        raise ConfigurationError("Legacy index cleanup home regular dizin olmali")
    issues = HomeLayout(root).verify()
    if issues:
        raise ConfigurationError("Legacy index cleanup home layout gecersiz")
    return root


def _legacy_root(home: Path) -> Path:
    root = _home_root(home) / "knowledge-index" / "vector" / LEGACY_MODEL_DIRECTORY
    if root.exists() and path_or_parent_is_link_or_reparse(root):
        raise PolicyViolation("Legacy index model kokunde link/reparse olamaz")
    return root


def _tree_fingerprint(path: Path) -> tuple[int, int, str]:
    """Read bounded metadata only; no index bytes are copied or exposed."""

    if not path.is_dir() or path_or_parent_is_link_or_reparse(path):
        raise PolicyViolation("Legacy index proje kokunde regular private dizin gerekli")
    pending = [path]
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise PolicyViolation("Legacy index agaci okunamadi") from exc
        for entry in children:
            target = Path(entry.path)
            try:
                metadata = target.lstat()
            except OSError as exc:
                raise PolicyViolation("Legacy index metadata okunamadi") from exc
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
                raise PolicyViolation("Legacy index agacinda link/reparse reddedildi")
            relative = target.relative_to(path).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(target)
                kind = "directory"
                size = 0
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                size = int(metadata.st_size)
                total_bytes += size
                if total_bytes > _MAX_TOTAL_BYTES:
                    raise PolicyViolation("Legacy index byte siniri asildi")
            else:
                raise PolicyViolation("Legacy index agacinda ozel dosya reddedildi")
            entries.append(
                {
                    "path": relative,
                    "kind": kind,
                    "size_bytes": size,
                    "mtime_ns": int(metadata.st_mtime_ns),
                }
            )
            if len(entries) > _MAX_TREE_ENTRIES:
                raise PolicyViolation("Legacy index girdi siniri asildi")
    body = {"schema": "zekam-legacy-index-tree/v1", "entries": entries}
    return len([item for item in entries if item["kind"] == "file"]), total_bytes, digest(body)


@dataclass(frozen=True, slots=True)
class LegacyIndexCandidate:
    project_slug: str
    relative_path: str
    file_count: int
    total_bytes: int
    tree_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "relative_path": self.relative_path,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "tree_digest": self.tree_digest,
        }


@dataclass(frozen=True, slots=True)
class LegacyIndexCleanupPlan:
    home: Path
    project_slug: str | None
    candidates: tuple[LegacyIndexCandidate, ...]
    observed_at: dt.datetime
    plan_digest: str

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-legacy-index-cleanup-plan/v1",
            "model_directory": LEGACY_MODEL_DIRECTORY,
            "replacement_model_directory": LOCAL_MODEL_DIRECTORY,
            "project_slug": self.project_slug,
            "candidates": [item.as_dict() for item in self.candidates],
            "observed_at": self.observed_at.isoformat(),
            "raw_delete": True,
            "grants_authority": False,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"plan_digest": self.plan_digest, "apply_required": True}


@dataclass(frozen=True, slots=True)
class LegacyIndexCleanupReceipt:
    plan_digest: str
    deleted_projects: tuple[str, ...]
    deleted_file_count: int
    deleted_bytes: int
    completed_at: dt.datetime
    receipt_digest: str

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-legacy-index-cleanup-receipt/v1",
            "plan_digest": self.plan_digest,
            "deleted_projects": list(self.deleted_projects),
            "deleted_file_count": self.deleted_file_count,
            "deleted_bytes": self.deleted_bytes,
            "completed_at": self.completed_at.isoformat(),
            "raw_delete": True,
            "grants_authority": False,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}


def _candidate(home: Path, project_path: Path) -> LegacyIndexCandidate:
    project_slug = validate_slug(project_path.name)
    file_count, total_bytes, tree_digest = _tree_fingerprint(project_path)
    relative = project_path.relative_to(home).as_posix()
    return LegacyIndexCandidate(project_slug, relative, file_count, total_bytes, tree_digest)


def plan_legacy_index_cleanup(
    home: Path,
    *,
    project_slug: str | None = None,
    now: dt.datetime | None = None,
) -> LegacyIndexCleanupPlan:
    """Build a bounded deletion plan without touching any index file."""

    root = _home_root(home)
    requested = None if project_slug is None else validate_slug(project_slug)
    model_root = _legacy_root(root)
    observed_at = _utc(now)
    candidates: list[LegacyIndexCandidate] = []
    if model_root.exists():
        if not model_root.is_dir():
            raise PolicyViolation("Legacy index model yolu dizin olmali")
        children = sorted(model_root.iterdir(), key=lambda item: item.name)
        if len(children) > _MAX_PROJECTS:
            raise PolicyViolation("Legacy index proje sayisi siniri asildi")
        for child in children:
            if not child.is_dir() or path_or_parent_is_link_or_reparse(child):
                raise PolicyViolation("Legacy index model kokunde beklenmeyen girdi")
            if requested is not None and child.name != requested:
                continue
            candidates.append(_candidate(root, child))
        if requested is not None and not candidates:
            # Missing exact project is a valid no-op; it must not create paths.
            candidates = []
    body = {
        "schema": "zekam-legacy-index-cleanup-plan/v1",
        "model_directory": LEGACY_MODEL_DIRECTORY,
        "replacement_model_directory": LOCAL_MODEL_DIRECTORY,
        "project_slug": requested,
        "candidates": [item.as_dict() for item in candidates],
        "observed_at": observed_at.isoformat(),
        "raw_delete": True,
        "grants_authority": False,
    }
    return LegacyIndexCleanupPlan(
        home=root,
        project_slug=requested,
        candidates=tuple(candidates),
        observed_at=observed_at,
        plan_digest=digest(body),
    )


def apply_legacy_index_cleanup(
    plan: LegacyIndexCleanupPlan,
    *,
    expected_plan_digest: str,
    now: dt.datetime | None = None,
) -> LegacyIndexCleanupReceipt:
    """Delete only the exact, unchanged derived BGE trees from a fresh plan."""

    if not isinstance(plan, LegacyIndexCleanupPlan) or expected_plan_digest != plan.plan_digest:
        raise PolicyViolation("Legacy index cleanup plan digest uyusmuyor")
    current = plan_legacy_index_cleanup(
        plan.home, project_slug=plan.project_slug, now=plan.observed_at
    )
    if current.plan_digest != plan.plan_digest:
        raise PolicyViolation("Legacy index cleanup plan stale veya degismis")
    deleted_projects: list[str] = []
    deleted_file_count = 0
    deleted_bytes = 0
    for item in plan.candidates:
        target = plan.home.joinpath(*item.relative_path.split("/"))
        if not target.is_dir() or path_or_parent_is_link_or_reparse(target):
            raise PolicyViolation("Legacy index cleanup hedefi kayboldu veya link oldu")
        file_count, total_bytes, tree_digest = _tree_fingerprint(target)
        if (file_count, total_bytes, tree_digest) != (
            item.file_count,
            item.total_bytes,
            item.tree_digest,
        ):
            raise PolicyViolation("Legacy index cleanup hedefi plan sonrasinda degisti")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise PolicyViolation("Legacy index cleanup silme basarisiz") from exc
        if target.exists():
            raise PolicyViolation("Legacy index cleanup silme readback basarisiz")
        deleted_projects.append(item.project_slug)
        deleted_file_count += file_count
        deleted_bytes += total_bytes
    completed_at = _utc(now)
    body = {
        "schema": "zekam-legacy-index-cleanup-receipt/v1",
        "plan_digest": plan.plan_digest,
        "deleted_projects": deleted_projects,
        "deleted_file_count": deleted_file_count,
        "deleted_bytes": deleted_bytes,
        "completed_at": completed_at.isoformat(),
        "raw_delete": True,
        "grants_authority": False,
    }
    return LegacyIndexCleanupReceipt(
        plan_digest=plan.plan_digest,
        deleted_projects=tuple(deleted_projects),
        deleted_file_count=deleted_file_count,
        deleted_bytes=deleted_bytes,
        completed_at=completed_at,
        receipt_digest=digest(body),
    )
