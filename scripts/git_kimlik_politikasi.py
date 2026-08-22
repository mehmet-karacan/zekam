"""Commit kimligi, ASCII mesaj ve ortak-yazar politikasini uygular."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

EXPECTED_NAME = "mehmet-karacan"
EXPECTED_EMAIL = "karacan.mehmet@hotmail.com"
ZERO_SHA = "0" * 40
_ATTRIBUTION = re.compile(
    r"^\s*(?:co-authored-by\s*:|(?:generated|created|written)\s+(?:by|with)\s+"
    r"(?:claude|chatgpt|codex|ai)\b)",
    re.IGNORECASE,
)


def message_findings(message: str) -> tuple[str, ...]:
    findings: list[str] = []
    if not message.strip():
        findings.append("commit-message-empty")
    if not message.isascii():
        findings.append("commit-message-non-ascii")
    if any(_ATTRIBUTION.match(line) for line in message.splitlines()):
        findings.append("ai-attribution-forbidden")
    return tuple(findings)


def sanitize_history_message(message: str) -> str:
    kept = [line.rstrip() for line in message.splitlines() if not _ATTRIBUTION.match(line)]
    normalized = unicodedata.normalize("NFKD", "\n".join(kept))
    ascii_message = normalized.encode("ascii", "ignore").decode("ascii")
    while "\n\n\n" in ascii_message:
        ascii_message = ascii_message.replace("\n\n\n", "\n\n")
    result = ascii_message.strip()
    return result or "Gecmis kaydini koru"


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return completed.stdout


def commit_findings(commit: str) -> tuple[str, ...]:
    values = _git(
        "show",
        "-s",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
        commit,
    ).split("\x00", 4)
    if len(values) != 5:
        return ("commit-metadata-unreadable",)
    author_name, author_email, committer_name, committer_email, message = values
    findings = list(message_findings(message))
    if (author_name, author_email) != (EXPECTED_NAME, EXPECTED_EMAIL):
        findings.append("author-identity-forbidden")
    if (committer_name, committer_email) != (EXPECTED_NAME, EXPECTED_EMAIL):
        findings.append("committer-identity-forbidden")
    return tuple(findings)


def validate_history(tips: tuple[str, ...]) -> tuple[str, ...]:
    commits: set[str] = set()
    for tip in tips:
        if tip != ZERO_SHA:
            commits.update(_git("rev-list", tip).splitlines())
    findings: list[str] = []
    for commit in sorted(commits):
        findings.extend(f"{commit}:{item}" for item in commit_findings(commit))
    return tuple(findings)


def _commit_msg(path: Path) -> int:
    findings = message_findings(path.read_text(encoding="utf-8"))
    if findings:
        print("Commit mesaji reddedildi: " + ", ".join(findings), file=sys.stderr)
        return 1
    return 0


def _pre_push() -> int:
    tips: list[str] = []
    for line in sys.stdin:
        fields = line.split()
        if len(fields) == 4 and fields[1] != ZERO_SHA:
            tips.append(fields[1])
    findings = validate_history(tuple(tips))
    if findings:
        print("Push kimlik politikasi reddetti:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    commit_parser = subparsers.add_parser("commit-msg")
    commit_parser.add_argument("path", type=Path)
    subparsers.add_parser("pre-push")
    subparsers.add_parser("history-filter")
    arguments = parser.parse_args()
    if arguments.command == "commit-msg":
        return _commit_msg(arguments.path)
    if arguments.command == "pre-push":
        return _pre_push()
    sys.stdout.write(sanitize_history_message(sys.stdin.read()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
