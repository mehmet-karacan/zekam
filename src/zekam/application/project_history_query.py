"""Jira anahtari -> bounded, salt okunur Git gecmisi kaniti (RAG26-R07).

"SKYRSM-5659 kapsaminda ne degisti?" gibi sorular kod vektor aramasiyla degil, exact
anahtar ile yerel HEAD gecmisinden cevaplanir. Yalnizca izinli salt okunur Git alt
komutlari (`source_reader.run_read_only`) calisir; fetch, ag, `--all` ve serbest ref
yoktur. Tum cikti sinirlidir ve gizli deger kalibi tasiyan diff'ler maskelenir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from zekam.application.secret_detection import scan_text
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.git.source_reader import GitCommandError, run_read_only

SCHEMA: Final = "zekam-project-issue-history/v1"
MAX_COMMITS: Final = 20
MAX_DIFF_FILES: Final = 3
MAX_DIFF_LINES: Final = 2000
MAX_SUBJECT_CHARS: Final = 300
MAX_HUNK_CHARS: Final = 6000

_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,15}-[0-9]{1,7}$")
_FIELD_SEPARATOR = "\x1f"
_RECORD_SEPARATOR = "\x1e"
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class HistoryFile:
    path: str
    status: str
    hunks: str | None
    truncated: bool
    redacted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "hunks": self.hunks,
            "truncated": self.truncated,
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class HistoryCommit:
    commit: str
    parents: tuple[str, ...]
    authored_at: str
    subject: str
    is_revert: bool
    is_merge: bool
    matched_in: str
    subject_redacted: bool
    files_total: int | None
    files: tuple[HistoryFile, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "parents": list(self.parents),
            "authored_at": self.authored_at,
            "subject": self.subject,
            "is_revert": self.is_revert,
            "is_merge": self.is_merge,
            "matched_in": self.matched_in,
            "subject_redacted": self.subject_redacted,
            "files_total": self.files_total,
            "files": [item.as_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class IssueHistory:
    issue_key: str
    head: str
    shallow: bool
    commits: tuple[HistoryCommit, ...]
    truncated_commits: bool

    @property
    def partial(self) -> bool:
        return self.shallow or self.truncated_commits

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "issue_key": self.issue_key,
            "ref": "HEAD",
            "head": self.head,
            "shallow": self.shallow,
            "truncated_commits": self.truncated_commits,
            "partial": self.partial,
            "commit_count": len(self.commits),
            "commits": [item.as_dict() for item in self.commits],
            "grants_authority": False,
            "source_mutated": False,
        }


def validate_issue_key(issue_key: str) -> str:
    if not isinstance(issue_key, str) or _ISSUE_KEY.fullmatch(issue_key) is None:
        raise ValidationFailed("Exact Jira issue anahtari gerekli (ornek SKYRSM-5659)")
    return issue_key


def query_issue_history(root: Path, issue_key: str) -> IssueHistory:
    """HEAD gecmisinde exact `issue_key` iceren commit'leri ve bounded diff'lerini dondurur."""

    key = validate_issue_key(issue_key)
    head = run_read_only(root, "rev-parse", "HEAD").strip()
    shallow = run_read_only(root, "rev-parse", "--is-shallow-repository").strip() == "true"
    # Anahtar sinirlari: `SKYRSM-56590` ve `XSKYRSM-5659` eslesmez.
    pattern = rf"(^|[^A-Za-z0-9-]){re.escape(key)}([^0-9]|$)"
    log = run_read_only(
        root,
        "log",
        "HEAD",
        "-n",
        str(MAX_COMMITS + 1),
        "--no-color",
        "--extended-regexp",
        f"--grep={pattern}",
        f"--format=%H{_FIELD_SEPARATOR}%P{_FIELD_SEPARATOR}%aI{_FIELD_SEPARATOR}%s{_RECORD_SEPARATOR}",
    )
    entries = [item for item in log.split(_RECORD_SEPARATOR) if item.strip()]
    truncated = len(entries) > MAX_COMMITS
    commits = tuple(_commit(root, entry, key) for entry in entries[:MAX_COMMITS])
    return IssueHistory(
        issue_key=key,
        head=head,
        shallow=shallow,
        commits=commits,
        truncated_commits=truncated,
    )


def _commit(root: Path, entry: str, key: str) -> HistoryCommit:
    fields = entry.strip("\n").split(_FIELD_SEPARATOR)
    if len(fields) != 4 or _SHA.fullmatch(fields[0]) is None:
        raise GitCommandError("Git log kaydi beklenen sekilde degil")
    sha, parents_raw, authored_at, subject = fields
    parents = tuple(item for item in parents_raw.split() if _SHA.fullmatch(item))
    subject = subject[:MAX_SUBJECT_CHARS]
    # `--grep` govdede de arar; anahtar subject'te yoksa bunu acikca isaretle.
    key_pattern = re.compile(rf"(^|[^A-Za-z0-9-]){re.escape(key)}([^0-9]|$)")
    matched_in = "subject" if key_pattern.search(subject) else "body"
    subject_redacted = bool(scan_text(subject, relative_path="history-subject.txt"))
    if subject_redacted:
        subject = "[redacted: secret-pattern]"
    files_total: int | None = None
    files: tuple[HistoryFile, ...] = ()
    if parents:
        files_total, files = _diff_files(root, parents[0], sha)
    return HistoryCommit(
        commit=sha,
        parents=parents,
        authored_at=authored_at,
        subject=subject,
        is_revert=subject.startswith("Revert "),
        is_merge=len(parents) > 1,
        matched_in=matched_in,
        subject_redacted=subject_redacted,
        files_total=files_total,
        files=files,
    )


def _diff_files(root: Path, parent: str, sha: str) -> tuple[int, tuple[HistoryFile, ...]]:
    # `-z`: dosya adlari tirnaklanmaz/octal-escape edilmez (Turkce ve ozel karakterler).
    listing = run_read_only(
        root, "diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", parent, sha
    )
    tokens = [item for item in listing.split("\0") if item]
    rows = list(zip(tokens[0::2], tokens[1::2], strict=False))
    remaining_lines = MAX_DIFF_LINES
    files: list[HistoryFile] = []
    for status, path in rows[:MAX_DIFF_FILES]:
        hunks, was_truncated, redacted, used = _hunks(root, parent, sha, path, remaining_lines)
        remaining_lines = max(0, remaining_lines - used)
        files.append(
            HistoryFile(
                path=path,
                status=status[:1],
                hunks=hunks,
                truncated=was_truncated,
                redacted=redacted,
            )
        )
    return len(rows), tuple(files)


def _hunks(
    root: Path, parent: str, sha: str, path: str, line_budget: int
) -> tuple[str | None, bool, bool, int]:
    if line_budget <= 0:
        return None, True, False, 0
    text = run_read_only(
        root,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        parent,
        sha,
        "--",
        f":(literal){path}",
    )
    lines = text.splitlines()
    # Dosya basligi (diff --git/index/---/+++) atilir; hunk ilk `@@` satirinda baslar.
    start = next((index for index, line in enumerate(lines) if line.startswith("@@")), len(lines))
    body = lines[start:]
    truncated = len(body) > line_budget
    body = body[:line_budget]
    joined = "\n".join(body)
    if len(joined) > MAX_HUNK_CHARS:
        joined = joined[:MAX_HUNK_CHARS]
        truncated = True
    if scan_text(joined, relative_path="history-diff.txt"):
        return None, truncated, True, len(body)
    return joined, truncated, False, len(body)
