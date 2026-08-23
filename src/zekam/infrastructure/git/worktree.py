"""Legacy adla bound real-source Git yonetimi.

Yeni proje kopyasi veya detached worktree olusturmaz. Tum mutation registry'de
bagli exact gercek source rootunda yapilir.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import TreeFingerprint, assert_relative_path
from zekam.infrastructure.git.source_reader import COMMAND_TIMEOUT, GitCommandError, run_read_only

#: Direct-source degisiklik kanitini salt okunur cikaran Git komutlari.
_WORKTREE_COMMANDS = frozenset({"diff"})


def _run(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not arguments or arguments[0] not in _WORKTREE_COMMANDS:
        raise PolicyViolation("izinsiz git alt komutu")
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
        raise GitCommandError(f"Git komutu calistirilamadi: {arguments[0]}") from exc
    if check and completed.returncode != 0:
        raise GitCommandError(f"Git komutu basarisiz: {arguments[0]}")
    return completed


def fingerprint(root: Path) -> TreeFingerprint:
    """Main tree'nin HEAD, icerik ve kirlilik parmak izini uretir."""

    head = run_read_only(root, "rev-parse", "HEAD").strip()
    listing = run_read_only(root, "ls-files", "--stage").strip()
    status = run_read_only(root, "status", "--porcelain").strip()
    return TreeFingerprint(
        head=head,
        tree_digest=digest({"stage": listing}),
        dirty=bool(status),
    )


@dataclass(frozen=True, slots=True)
class ManagedWorktree:
    """Uyumluluk adi altinda bagli gercek source rootu."""

    workspace_id: str
    path: Path
    revision: str

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    def resolve(self, relative: str) -> Path:
        """Allowlist kontrolunden gecmis bir yolu guvenle cozer.

        Symlink kacisi burada yakalanir: cozulmus hedef source kokunun disina
        cikamaz.
        """

        assert_relative_path(relative, "hedef path")
        base = self.path.resolve()
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            raise PolicyViolation("hedef path bagli source kokunun disina cikiyor")
        return target


@dataclass(frozen=True, slots=True)
class WorktreeManager:
    """Uyumluluk adi altinda bound real-source yasam dongusu."""

    source_root: Path
    workspaces_root: Path

    def create(self, workspace_id: str, *, revision: str = "HEAD") -> ManagedWorktree:
        resolved = run_read_only(self.source_root, "rev-parse", revision).strip()
        current = run_read_only(self.source_root, "rev-parse", "HEAD").strip()
        if resolved != current:
            raise PolicyViolation("bound source revision HEAD ile ayni olmalidir")
        return ManagedWorktree(workspace_id=workspace_id, path=self.source_root, revision=resolved)

    def remove(self, worktree: ManagedWorktree) -> None:
        if worktree.path.resolve() != self.source_root.resolve():
            raise PolicyViolation("yalniz bagli gercek source root kabul edilir")

    def diff(self, worktree: ManagedWorktree) -> str:
        """Bagli gercek source rootundaki degisikligi kanit metni olarak uretir."""

        return _run(worktree.path, "diff", "--no-color", "HEAD").stdout

    def changed_paths(self, worktree: ManagedWorktree) -> tuple[str, ...]:
        output = _run(worktree.path, "diff", "--name-only", "HEAD").stdout
        return tuple(sorted(line.strip() for line in output.splitlines() if line.strip()))

    def apply_check(self, patch: str) -> bool:
        """Direct-source modunda patch yeniden uygulanmaz."""

        return bool(patch.strip())
