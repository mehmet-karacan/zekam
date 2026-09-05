"""Content-safe Git tracking, staging and bounded history security scans."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from zekam.application.secret_detection import SecretFinding, scan_text
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BLOBS = 20_000
_BACKUP_SUFFIXES = frozenset({".bak", ".backup", ".dump", ".key", ".p12", ".pfx", ".pem"})
_BACKUP_NAMES = frozenset({".env", "credentials", "credentials.json", "secrets.json"})


class GitSurface(StrEnum):
    TRACKED = "tracked"
    STAGED = "staged"
    WORKTREE = "worktree"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class GitSecurityFinding:
    surface: GitSurface
    code: str
    relative_path: str
    rule_id: str | None = None
    line_number: int | None = None
    fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface.value,
            "code": self.code,
            "path": self.relative_path,
            "rule_id": self.rule_id,
            "line": self.line_number,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class GitSecurityReport:
    repository: str
    findings: tuple[GitSecurityFinding, ...]
    tracked_blob_count: int
    worktree_file_count: int
    history_blob_count: int
    history_complete: bool
    reviewed_allowance_count: int = 0
    allowlist_digest: str | None = None
    grants_authority: bool = False

    @property
    def passed(self) -> bool:
        return not self.findings and self.history_complete

    @property
    def requires_revoke_or_rotate(self) -> bool:
        return any(item.surface is GitSurface.HISTORY and item.rule_id for item in self.findings)

    @property
    def report_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-git-security-report/v1",
            "repository": self.repository,
            "findings": [item.as_dict() for item in self.findings],
            "tracked_blob_count": self.tracked_blob_count,
            "worktree_file_count": self.worktree_file_count,
            "history_blob_count": self.history_blob_count,
            "history_complete": self.history_complete,
            "reviewed_allowance_count": self.reviewed_allowance_count,
            "allowlist_digest": self.allowlist_digest,
            "passed": self.passed,
            "requires_revoke_or_rotate": self.requires_revoke_or_rotate,
            "grants_authority": self.grants_authority,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"report_digest": self.report_digest}


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return completed.stdout


def _git_with_input(root: Path, payload: bytes, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=payload,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return completed.stdout


def _validate_root(root: Path) -> Path:
    if root.is_symlink():
        raise PolicyViolation("Git security scan repository root symlink olamaz")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or not (resolved / ".git").exists():
        raise ValidationFailed("Git security scan exact repository root ister")
    return resolved


def _index_entries(root: Path) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.decode("ascii", errors="strict").split()
        if not separator or len(parts) != 3 or parts[2] != "0":
            continue
        path = raw_path.decode("utf-8", errors="strict")
        _portable_path(path)
        entries.append((parts[1], path))
    return tuple(entries)


def _portable_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not value:
        raise ValidationFailed("Git security scan portable path ister")


def _staged_paths(root: Path) -> frozenset[str]:
    paths: set[str] = set()
    for raw in _git(root, "diff", "--cached", "--name-only", "-z").split(b"\0"):
        if not raw:
            continue
        value = raw.decode("utf-8", errors="strict")
        _portable_path(value)
        paths.add(value)
    return frozenset(paths)


def _worktree_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    payload = _git(root, "ls-files", "--modified", "--others", "--exclude-standard", "-z")
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        value = raw.decode("utf-8", errors="strict")
        _portable_path(value)
        if value == "legacy-preserved" or value.startswith("legacy-preserved/"):
            continue
        paths.append(value)
    return tuple(sorted(set(paths)))


def _history_entries(root: Path) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in _git(root, "rev-list", "--objects", "--all").splitlines():
        raw_oid, separator, raw_path = line.partition(b" ")
        oid = raw_oid.decode("ascii", errors="strict")
        if oid in seen or not separator or not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="strict")
        try:
            _portable_path(path)
        except ValidationFailed:
            continue
        seen.add(oid)
        entries.append((oid, path))
    return tuple(entries)


def _blob_texts(root: Path, object_ids: tuple[str, ...]) -> dict[str, str]:
    unique = tuple(dict.fromkeys(object_ids))
    if not unique:
        return {}
    request = "".join(f"{oid}\n" for oid in unique).encode("ascii")
    metadata = _git_with_input(
        root,
        request,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
    )
    eligible: list[str] = []
    for line in metadata.splitlines():
        parts = line.decode("ascii", errors="strict").split()
        if len(parts) == 3 and parts[1] == "blob" and int(parts[2]) <= MAX_BLOB_BYTES:
            eligible.append(parts[0])
    batch_request = "".join(f"{oid}\n" for oid in eligible).encode("ascii")
    batch = _git_with_input(root, batch_request, "cat-file", "--batch")
    texts: dict[str, str] = {}
    offset = 0
    for expected_oid in eligible:
        header_end = batch.find(b"\n", offset)
        if header_end < 0:
            raise ValidationFailed("Git batch blob header eksik")
        header = batch[offset:header_end].decode("ascii", errors="strict").split()
        if len(header) != 3 or header[0] != expected_oid or header[1] != "blob":
            raise ValidationFailed("Git batch blob sirasi gecersiz")
        size = int(header[2])
        body_start = header_end + 1
        body_end = body_start + size
        payload = batch[body_start:body_end]
        if len(payload) != size or batch[body_end : body_end + 1] != b"\n":
            raise ValidationFailed("Git batch blob govdesi eksik")
        offset = body_end + 1
        if b"\0" in payload:
            continue
        try:
            texts[expected_oid] = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
    return texts


def _backup_finding(surface: GitSurface, path: str) -> GitSecurityFinding | None:
    candidate = PurePosixPath(path)
    if (
        candidate.name.casefold() in _BACKUP_NAMES
        or candidate.suffix.casefold() in _BACKUP_SUFFIXES
    ):
        return GitSecurityFinding(
            surface=surface,
            code="secret-or-backup-artifact",
            relative_path=path,
        )
    return None


def _secret_findings(
    *, surface: GitSurface, path: str, text: str
) -> tuple[GitSecurityFinding, ...]:
    findings: tuple[SecretFinding, ...] = scan_text(text, relative_path=path)
    return tuple(
        GitSecurityFinding(
            surface=surface,
            code="secret-pattern",
            relative_path=path,
            rule_id=item.rule_id,
            line_number=item.line_number,
            fingerprint=item.fingerprint,
        )
        for item in findings
    )


def _worktree_text(root: Path, relative_path: str) -> str | None:
    target = root / PurePosixPath(relative_path)
    if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_BLOB_BYTES:
        return None
    payload = target.read_bytes()
    if b"\0" in payload:
        return None
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def scan_git_security(root: Path, *, include_history: bool = True) -> GitSecurityReport:
    """Scan index blobs and unique historical blobs without emitting content values."""

    repository = _validate_root(root)
    index = _index_entries(repository)
    staged = _staged_paths(repository)
    worktree = _worktree_paths(repository)
    findings: list[GitSecurityFinding] = []
    current_oids = {oid for oid, _ in index}
    current_texts = _blob_texts(repository, tuple(oid for oid, _ in index))
    for oid, path in index:
        surface = GitSurface.STAGED if path in staged else GitSurface.TRACKED
        backup = _backup_finding(surface, path)
        if backup is not None:
            findings.append(backup)
        text = current_texts.get(oid)
        if text is not None:
            findings.extend(_secret_findings(surface=surface, path=path, text=text))

    for path in worktree:
        backup = _backup_finding(GitSurface.WORKTREE, path)
        if backup is not None:
            findings.append(backup)
        text = _worktree_text(repository, path)
        if text is not None:
            findings.extend(_secret_findings(surface=GitSurface.WORKTREE, path=path, text=text))

    history_count = 0
    history_complete = _git(repository, "rev-parse", "--is-shallow-repository").strip() == b"false"
    if include_history:
        history = _history_entries(repository)
        if len(history) > MAX_HISTORY_BLOBS:
            history = history[:MAX_HISTORY_BLOBS]
            history_complete = False
        history_texts = _blob_texts(
            repository,
            tuple(oid for oid, _ in history if oid not in current_oids),
        )
        for oid, path in history:
            if oid in current_oids:
                continue
            history_count += 1
            backup = _backup_finding(GitSurface.HISTORY, path)
            if backup is not None:
                findings.append(backup)
            text = history_texts.get(oid)
            if text is not None:
                findings.extend(_secret_findings(surface=GitSurface.HISTORY, path=path, text=text))
    findings.sort(
        key=lambda item: (
            item.surface.value,
            item.relative_path,
            item.line_number or 0,
            item.rule_id or "",
        )
    )
    return GitSecurityReport(
        repository=repository.name,
        findings=tuple(findings),
        tracked_blob_count=len(index),
        worktree_file_count=len(worktree),
        history_blob_count=history_count,
        history_complete=history_complete,
    )


@dataclass(frozen=True, slots=True)
class SecretScanAllowance:
    """Exact synthetic-fixture allowance; never stores or matches a secret value."""

    surface: str
    relative_path: str
    rule_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        if self.surface not in {"current", "history"}:
            raise ValidationFailed("Secret scan allowance surface gecersiz")
        _portable_path(self.relative_path)
        if not self.rule_id or not self.fingerprint or len(self.fingerprint) != 12:
            raise ValidationFailed("Secret scan allowance exact rule/fingerprint ister")
        if any(character not in "0123456789abcdef" for character in self.fingerprint):
            raise ValidationFailed("Secret scan allowance fingerprint gecersiz")

    def body(self) -> dict[str, str]:
        return {
            "surface": self.surface,
            "path": self.relative_path,
            "rule_id": self.rule_id,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SecretScanAllowlist:
    allowances: tuple[SecretScanAllowance, ...]

    def __post_init__(self) -> None:
        keys = tuple(
            (item.surface, item.relative_path, item.rule_id, item.fingerprint)
            for item in self.allowances
        )
        if len(keys) != len(set(keys)):
            raise ValidationFailed("Secret scan allowance tekrarli olamaz")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-secret-scan-allowlist/v1",
                "allowances": [
                    item.body()
                    for item in sorted(
                        self.allowances,
                        key=lambda value: (
                            value.surface,
                            value.relative_path,
                            value.rule_id,
                            value.fingerprint,
                        ),
                    )
                ],
            }
        )


def apply_secret_scan_allowlist(
    report: GitSecurityReport, allowlist: SecretScanAllowlist
) -> GitSecurityReport:
    """Suppress only exact reviewed fixture fingerprints; backup artifacts never qualify."""

    allowed = {
        (item.surface, item.relative_path, item.rule_id, item.fingerprint)
        for item in allowlist.allowances
    }
    remaining: list[GitSecurityFinding] = []
    ignored = 0
    for finding in report.findings:
        surface = "history" if finding.surface is GitSurface.HISTORY else "current"
        key = (surface, finding.relative_path, finding.rule_id, finding.fingerprint)
        if finding.code == "secret-pattern" and key in allowed:
            ignored += 1
            continue
        remaining.append(finding)
    return replace(
        report,
        findings=tuple(remaining),
        reviewed_allowance_count=ignored,
        allowlist_digest=allowlist.policy_digest,
    )
