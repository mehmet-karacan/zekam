"""Harici kaynak agacinin guvenli, salt okunur kesfi.

Guvenlik sinirlari:

- Kok disina cikan yol, symlink veya `..` bileseni reddedilir (fail-closed).
- Symlink'ler varsayilan olarak izlenmez.
- Sistem deny list, `.gitignore` ve `.zekamignore` birlikte uygulanir.
- Ikili ve asiri buyuk dosyalar icerik olarak okunmaz.
- Secret iceren dosyalar indeks disi birakilir; deger hicbir yere yazilmaz.
- Toplam dosya ve bayt siniri archive-bomb benzeri durumlara karsi fail-closed'dir.

Kesif hicbir kosulda kaynak agacina yazmaz, komut calistirmaz, build/test
tetiklemez veya paket kurmaz.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from zekam.application.ignore_rules import IgnoreMatcher, system_deny_matcher
from zekam.application.secret_detection import SecretFinding, scan_text
from zekam.domain.canonical import DIGEST_PREFIX
from zekam.domain.errors import PolicyViolation

ZEKAM_IGNORE_FILE = ".zekamignore"
GIT_IGNORE_FILE = ".gitignore"

#: Ikili tespiti icin okunan on ek boyutu.
BINARY_PROBE_BYTES = 8192


class SkipReason(StrEnum):
    """Bir yolun neden disarida birakildigi."""

    IGNORED = "ignored"
    SYMLINK = "symlink"
    OUTSIDE_ROOT = "outside-root"
    TOO_LARGE = "too-large"
    BINARY = "binary"
    SECRET = "secret"
    UNREADABLE = "unreadable"
    LIMIT_REACHED = "limit-reached"


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Kesif sinirlari. Varsayilanlar fail-closed tarafta secilmistir."""

    max_file_bytes: int = 2 * 1024 * 1024
    max_total_files: int = 50_000
    max_total_bytes: int = 512 * 1024 * 1024
    follow_symlinks: bool = False
    scan_secrets: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_total_files": self.max_total_files,
            "max_total_bytes": self.max_total_bytes,
            "follow_symlinks": self.follow_symlinks,
            "scan_secrets": self.scan_secrets,
        }


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """Kesfedilen tek bir dosya."""

    relative_path: str
    size_bytes: int
    content_digest: str
    is_text: bool

    @property
    def extension(self) -> str:
        _, _, suffix = self.relative_path.rpartition(".")
        return suffix.lower() if "." in self.relative_path else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
            "content_digest": self.content_digest,
            "is_text": self.is_text,
        }


@dataclass(frozen=True, slots=True)
class SkippedPath:
    """Disarida birakilan yol."""

    relative_path: str
    reason: SkipReason
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.relative_path, "reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """Kesif sonucu."""

    root_label: str
    tree_digest: str
    files: tuple[DiscoveredFile, ...]
    skipped: tuple[SkippedPath, ...]
    secrets: tuple[SecretFinding, ...]
    policy: DiscoveryPolicy
    truncated: bool = False
    extensions: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    def skipped_by(self, reason: SkipReason) -> tuple[SkippedPath, ...]:
        return tuple(item for item in self.skipped if item.reason is reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_label": self.root_label,
            "tree_digest": self.tree_digest,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "extensions": dict(sorted(self.extensions.items())),
            "skipped": [item.as_dict() for item in self.skipped],
            "secrets": [item.as_dict() for item in self.secrets],
            "policy": self.policy.as_dict(),
        }


def _is_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:BINARY_PROBE_BYTES]


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover - okunamayan ignore dosyasi yoksayilir
        return []


def build_matcher(root: Path) -> IgnoreMatcher:
    """Sistem, `.gitignore` ve `.zekamignore` kurallarini birlestirir."""
    matcher = system_deny_matcher()
    git_ignore = root / GIT_IGNORE_FILE
    if git_ignore.is_file():
        matcher = matcher.extended(IgnoreMatcher.from_lines(_read_ignore_file(git_ignore)))
    product_ignore = root / ZEKAM_IGNORE_FILE
    if product_ignore.is_file():
        matcher = matcher.extended(IgnoreMatcher.from_lines(_read_ignore_file(product_ignore)))
    return matcher


def assert_within_root(candidate: Path, root: Path) -> Path:
    """Yolun kok icinde kaldigini dogrular; aksi halde fail-closed hata verir."""
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PolicyViolation("Yol kaynak kokunun disina cikiyor")
    return resolved


def _iter_entries(root: Path) -> Iterator[tuple[Path, str, bool]]:
    """Kok altindaki girdileri kararli sirayla dolasir."""
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:  # pragma: no cover - okunamayan dizin atlanir
            continue
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            is_directory = entry.is_dir() and not entry.is_symlink()
            yield entry, relative, is_directory
            if is_directory:
                stack.append(entry)


def discover(
    root: Path,
    *,
    policy: DiscoveryPolicy | None = None,
    matcher: IgnoreMatcher | None = None,
) -> DiscoveryReport:
    """Kaynak agacini salt okunur olarak tarar."""
    active_policy = policy or DiscoveryPolicy()
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise PolicyViolation("Kaynak koku bir dizin olmali")
    rules = matcher or build_matcher(resolved_root)

    files: list[DiscoveredFile] = []
    skipped: list[SkippedPath] = []
    secrets: list[SecretFinding] = []
    extensions: dict[str, int] = {}
    total_bytes = 0
    truncated = False
    ignored_directories: set[str] = set()

    for entry, relative, is_directory in _iter_entries(resolved_root):
        if any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in ignored_directories
        ):
            continue

        if entry.is_symlink() and not active_policy.follow_symlinks:
            skipped.append(SkippedPath(relative, SkipReason.SYMLINK, "symlink izlenmiyor"))
            if is_directory:
                ignored_directories.add(relative)
            continue

        if rules.is_ignored(relative, is_directory=is_directory):
            skipped.append(SkippedPath(relative, SkipReason.IGNORED, "yoksayma kurali"))
            if is_directory:
                ignored_directories.add(relative)
            continue

        if is_directory:
            continue

        try:
            resolved = assert_within_root(entry, resolved_root)
        except PolicyViolation:
            skipped.append(SkippedPath(relative, SkipReason.OUTSIDE_ROOT, "kok disi hedef"))
            continue

        if len(files) >= active_policy.max_total_files:
            truncated = True
            skipped.append(SkippedPath(relative, SkipReason.LIMIT_REACHED, "dosya siniri"))
            continue

        try:
            size = resolved.stat().st_size
        except OSError:
            skipped.append(SkippedPath(relative, SkipReason.UNREADABLE, "stat basarisiz"))
            continue

        if size > active_policy.max_file_bytes:
            skipped.append(SkippedPath(relative, SkipReason.TOO_LARGE, f"{size} bayt"))
            continue
        if total_bytes + size > active_policy.max_total_bytes:
            truncated = True
            skipped.append(SkippedPath(relative, SkipReason.LIMIT_REACHED, "toplam bayt siniri"))
            continue

        try:
            payload = resolved.read_bytes()
        except OSError:
            skipped.append(SkippedPath(relative, SkipReason.UNREADABLE, "okuma basarisiz"))
            continue

        digest = DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()
        if _is_binary(payload):
            files.append(
                DiscoveredFile(
                    relative_path=relative,
                    size_bytes=size,
                    content_digest=digest,
                    is_text=False,
                )
            )
            total_bytes += size
            _count_extension(extensions, relative)
            continue

        text = payload.decode("utf-8", errors="replace")
        if active_policy.scan_secrets:
            found = scan_text(text, relative_path=relative)
            if found:
                secrets.extend(found)
                skipped.append(SkippedPath(relative, SkipReason.SECRET, f"{len(found)} bulgu"))
                continue

        files.append(
            DiscoveredFile(
                relative_path=relative,
                size_bytes=size,
                content_digest=digest,
                is_text=True,
            )
        )
        total_bytes += size
        _count_extension(extensions, relative)

    return DiscoveryReport(
        root_label=resolved_root.name,
        tree_digest=compute_tree_digest(files),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
        skipped=tuple(sorted(skipped, key=lambda item: item.relative_path)),
        secrets=tuple(secrets),
        policy=active_policy,
        truncated=truncated,
        extensions=extensions,
    )


def compute_tree_digest(files: list[DiscoveredFile] | tuple[DiscoveredFile, ...]) -> str:
    """Dosya yolu ve icerik digest'lerinden kararli agac digest'i uretir."""
    hasher = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.relative_path):
        hasher.update(item.relative_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(item.content_digest.encode("ascii"))
        hasher.update(b"\n")
    return DIGEST_PREFIX + hasher.hexdigest()


def _count_extension(counter: dict[str, int], relative: str) -> None:
    name = os.path.basename(relative)
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    key = suffix or "(uzantisiz)"
    counter[key] = counter.get(key, 0) + 1
