"""Zekam-managed detached worktree yonetimi.

Entegre kaynak main tree'ye asla yazilmaz. Her builder icin `git worktree add
--detach` ile ayri bir calisma agaci acilir; islem sonunda main tree'nin HEAD ve
tree parmak izi yeniden dogrulanir.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import TreeFingerprint, assert_relative_path
from zekam.infrastructure.git.source_reader import COMMAND_TIMEOUT, GitCommandError, run_read_only

#: Worktree yonetimi icin gereken, main tree icerigini degistirmeyen komutlar.
_WORKTREE_COMMANDS = frozenset({"worktree", "diff", "apply"})


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
    """Acilmis detached worktree."""

    workspace_id: str
    path: Path
    revision: str

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    def resolve(self, relative: str) -> Path:
        """Allowlist kontrolunden gecmis bir yolu guvenle cozer.

        Symlink kacisi burada yakalanir: cozulmus hedef worktree kokunun disina
        cikamaz.
        """

        assert_relative_path(relative, "hedef path")
        base = self.path.resolve()
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            raise PolicyViolation("hedef path worktree kokunun disina cikiyor")
        return target


@dataclass(frozen=True, slots=True)
class WorktreeManager:
    """Detached worktree yasam dongusu."""

    source_root: Path
    workspaces_root: Path

    def create(self, workspace_id: str, *, revision: str = "HEAD") -> ManagedWorktree:
        target = self.workspaces_root / workspace_id
        if target.exists():
            raise PolicyViolation("worktree kimligi zaten kullanimda")
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(self.source_root, "worktree", "add", "--detach", str(target), revision)
        resolved = run_read_only(target, "rev-parse", "HEAD").strip()
        return ManagedWorktree(workspace_id=workspace_id, path=target, revision=resolved)

    def remove(self, worktree: ManagedWorktree) -> None:
        _run(self.source_root, "worktree", "remove", "--force", str(worktree.path), check=False)
        _run(self.source_root, "worktree", "prune")

    def diff(self, worktree: ManagedWorktree) -> str:
        """Worktree'deki degisikligi yama metni olarak uretir."""

        return _run(worktree.path, "diff", "--no-color", "HEAD").stdout

    def changed_paths(self, worktree: ManagedWorktree) -> tuple[str, ...]:
        output = _run(worktree.path, "diff", "--name-only", "HEAD").stdout
        return tuple(sorted(line.strip() for line in output.splitlines() if line.strip()))

    def apply_check(self, patch: str) -> bool:
        """Yamayi hedefe uygulamadan once dogrular; hicbir sey yazmaz."""

        command = [
            "git",
            "-c",
            "core.hooksPath=",
            "-C",
            str(self.source_root),
            "apply",
            "--check",
            "-",
        ]
        try:
            # Girdi bayt olarak verilir: text modunda Windows satir sonu cevrimi
            # yamanin baglam satirlarini bozar ve check daima basarisiz olur.
            completed = subprocess.run(
                command,
                input=patch.encode("utf-8"),
                capture_output=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitCommandError("git apply --check calistirilamadi") from exc
        return completed.returncode == 0
