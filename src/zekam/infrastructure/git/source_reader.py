"""Harici Git kaynaklarinin salt okunur gozlemi.

Yalnizca `READ_ONLY_COMMANDS` icindeki alt komutlar calistirilir. Yazma yapan bir
alt komut istenirse `PolicyViolation` yukselir; boylece "yanlislikla yazma"
kod seviyesinde engellenir.

Ek olarak:

- Hook calistirilmaz (`core.hooksPath=/dev/null` benzeri etkisi icin `-c` ile
  `core.hooksPath` bos birakilir).
- Submodule guncellemesi, fetch, checkout ve temizlik yapilmaz.
- Kullaniciya donen kayitlarda absolute path bulunmaz.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from zekam.domain.errors import PolicyViolation

#: Izin verilen salt okunur alt komutlar.
READ_ONLY_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "rev-parse",
        "status",
        "log",
        "show-ref",
        "ls-files",
        "ls-tree",
        "config",
        "describe",
        "symbolic-ref",
        "cat-file",
        "diff",
        "for-each-ref",
    }
)

#: Komut zaman asimi (saniye).
COMMAND_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class GitObservation:
    """Bir Git deposunun gozlemlenen durumu."""

    commit: str
    branch: str | None
    is_dirty: bool
    tracked_file_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "branch": self.branch,
            "is_dirty": self.is_dirty,
            "tracked_file_count": self.tracked_file_count,
        }


class GitCommandError(PolicyViolation):
    """Git komutu calistirilamadi veya hata dondurdu."""

    code = "git-command-error"


def run_read_only(root: Path, *arguments: str) -> str:
    """Salt okunur bir Git komutu calistirir ve stdout dondurur."""
    if not arguments:
        raise PolicyViolation("Git komutu bos olamaz")
    subcommand = arguments[0]
    if subcommand not in READ_ONLY_COMMANDS:
        raise PolicyViolation(f"Salt okunur olmayan Git alt komutu reddedildi: {subcommand}")

    command = [
        "git",
        "-c",
        "core.hooksPath=",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        str(root),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitCommandError(f"Git komutu calistirilamadi: {subcommand}") from exc
    if completed.returncode != 0:
        raise GitCommandError(f"Git komutu basarisiz: {subcommand}")
    return completed.stdout


def is_inside_work_tree(root: Path) -> bool:
    """Verilen dizinin herhangi bir Git calisma agacinin icinde olup olmadigini soyler.

    Ust dizinlerdeki depolar da `true` dondurur; bu nedenle baglama karari icin
    `is_git_repository` kullanilir.
    """
    try:
        output = run_read_only(root, "rev-parse", "--is-inside-work-tree")
    except PolicyViolation:
        return False
    return output.strip() == "true"


def repository_root(root: Path) -> Path | None:
    """Git kok dizinini dondurur."""
    try:
        output = run_read_only(root, "rev-parse", "--show-toplevel")
    except PolicyViolation:
        return None
    text = output.strip()
    return Path(text) if text else None


def is_git_repository(root: Path) -> bool:
    """Verilen dizinin **kendisinin** bir Git deposu koku olup olmadigini soyler.

    Yalnizca ust dizinde depo bulunmasi yeterli degildir: bir alt dizini Git
    deposu gibi baglamak, kaynak surumunu yanlis dosya kumesine baglar.
    """
    toplevel = repository_root(root)
    if toplevel is None:
        return False
    try:
        return toplevel.resolve() == root.resolve()
    except OSError:  # pragma: no cover - cozulemeyen yol
        return False


def observe(root: Path) -> GitObservation | None:
    """Depo durumunu gozlemler. Git deposu degilse `None` doner."""
    if not is_git_repository(root):
        return None
    try:
        commit = run_read_only(root, "rev-parse", "HEAD").strip()
    except PolicyViolation:
        # Henuz commit'i olmayan depo.
        commit = ""
    try:
        branch_text = run_read_only(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except PolicyViolation:  # pragma: no cover - ortam bagimli
        branch_text = ""
    branch = None if branch_text in {"", "HEAD"} else branch_text

    try:
        status_text = run_read_only(root, "status", "--porcelain")
    except PolicyViolation:  # pragma: no cover - ortam bagimli
        status_text = ""
    is_dirty = any(line.strip() for line in status_text.splitlines())

    try:
        tracked = run_read_only(root, "ls-files")
    except PolicyViolation:  # pragma: no cover - ortam bagimli
        tracked = ""
    tracked_file_count = len([line for line in tracked.splitlines() if line.strip()])

    return GitObservation(
        commit=commit or "uncommitted",
        branch=branch,
        is_dirty=is_dirty,
        tracked_file_count=tracked_file_count,
    )
