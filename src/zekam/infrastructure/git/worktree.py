"""Legacy adla bound real-source Git yonetimi.

Yeni proje kopyasi veya detached worktree olusturmaz. Tum mutation registry'de
bagli exact gercek source rootunda yapilir.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from zekam.domain.canonical import digest, digest_of_bytes
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
    lease_stream: BinaryIO | None = None

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
        current = base
        for part in Path(relative).parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                continue
            metadata = os.lstat(current)
            is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & 0x400)
            if stat.S_ISLNK(metadata.st_mode) or is_reparse:
                raise PolicyViolation("hedef path symlink veya reparse point tasiyamaz")
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            raise PolicyViolation("hedef path bagli source kokunun disina cikiyor")
        return target


@dataclass(frozen=True, slots=True)
class WorktreeManager:
    """Uyumluluk adi altinda bound real-source yasam dongusu."""

    source_root: Path
    workspaces_root: Path

    @staticmethod
    def _acquire_source_lease(root: Path) -> BinaryIO:
        git_dir = Path(run_read_only(root, "rev-parse", "--git-dir").strip())
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        lock_path = git_dir.resolve() / "zekam-bound-source.lock"
        stream = lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                getattr(msvcrt, "locking")(  # noqa: B009
                    stream.fileno(),
                    getattr(msvcrt, "LK_NBLCK"),  # noqa: B009
                    1,
                )
            else:
                import fcntl

                getattr(fcntl, "flock")(  # noqa: B009
                    stream.fileno(),
                    getattr(fcntl, "LOCK_EX")  # noqa: B009
                    | getattr(fcntl, "LOCK_NB"),  # noqa: B009
                )
        except (OSError, ImportError) as exc:
            stream.close()
            raise PolicyViolation("bound source baska builder tarafindan kullaniliyor") from exc
        return stream

    @staticmethod
    def _release_source_lease(stream: BinaryIO) -> None:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                getattr(msvcrt, "locking")(  # noqa: B009
                    stream.fileno(),
                    getattr(msvcrt, "LK_UNLCK"),  # noqa: B009
                    1,
                )
            else:
                import fcntl

                getattr(fcntl, "flock")(  # noqa: B009
                    stream.fileno(),
                    getattr(fcntl, "LOCK_UN"),  # noqa: B009
                )
        finally:
            stream.close()

    def create(self, workspace_id: str, *, revision: str = "HEAD") -> ManagedWorktree:
        lease = self._acquire_source_lease(self.source_root)
        try:
            resolved = run_read_only(self.source_root, "rev-parse", revision).strip()
            current = run_read_only(self.source_root, "rev-parse", "HEAD").strip()
            if resolved != current:
                raise PolicyViolation("bound source revision HEAD ile ayni olmalidir")
        except Exception:
            self._release_source_lease(lease)
            raise
        return ManagedWorktree(
            workspace_id=workspace_id,
            path=self.source_root,
            revision=resolved,
            lease_stream=lease,
        )

    def remove(self, worktree: ManagedWorktree) -> None:
        if worktree.path.resolve() != self.source_root.resolve():
            raise PolicyViolation("yalniz bagli gercek source root kabul edilir")
        if worktree.lease_stream is not None and not worktree.lease_stream.closed:
            self._release_source_lease(worktree.lease_stream)

    def diff(self, worktree: ManagedWorktree) -> str:
        """Bagli gercek source rootundaki degisikligi kanit metni olarak uretir."""

        return _run(worktree.path, "diff", "--no-color", "HEAD").stdout

    def _untracked_evidence(self, worktree: ManagedWorktree) -> tuple[tuple[str, str], ...]:
        output = run_read_only(worktree.path, "ls-files", "--others", "--exclude-standard", "-z")
        evidence: list[tuple[str, str]] = []
        for relative in sorted(value for value in output.split("\0") if value):
            target = worktree.resolve(relative)
            if not target.is_file():
                raise PolicyViolation("untracked source kaniti normal dosya ister")
            hasher = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            evidence.append((relative, f"sha256:{hasher.hexdigest()}"))
        return tuple(evidence)

    def patch_digest(self, worktree: ManagedWorktree) -> str:
        """Tracked patch ve untracked file iceriklerini exact digest'e baglar."""

        return digest(
            {
                "schema": "zekam-bound-source-patch/v1",
                "tracked_patch_digest": digest_of_bytes(self.diff(worktree).encode("utf-8")),
                "untracked": [
                    {"path": path, "content_digest": content_digest}
                    for path, content_digest in self._untracked_evidence(worktree)
                ],
            }
        )

    def dirty_state_digest(self, worktree: ManagedWorktree) -> str:
        """HEAD, index, status ve patch'i tek kararli dirty-state kanitina baglar."""

        current = fingerprint(worktree.path)
        status = run_read_only(worktree.path, "status", "--porcelain=v1", "--untracked-files=all")
        return digest(
            {
                "schema": "zekam-bound-source-dirty-state/v1",
                "workspace_id": worktree.workspace_id,
                "base_revision": worktree.revision,
                "head": current.head,
                "index_tree_digest": current.tree_digest,
                "status_digest": digest_of_bytes(status.encode("utf-8")),
                "patch_digest": self.patch_digest(worktree),
                "untracked": [
                    {"path": path, "content_digest": content_digest}
                    for path, content_digest in self._untracked_evidence(worktree)
                ],
                "dirty": current.dirty,
            }
        )

    def changed_paths(self, worktree: ManagedWorktree) -> tuple[str, ...]:
        output = _run(worktree.path, "diff", "--name-only", "HEAD").stdout
        tracked = {line.strip() for line in output.splitlines() if line.strip()}
        for relative in sorted(tracked):
            worktree.resolve(relative)
        untracked = {path for path, _ in self._untracked_evidence(worktree)}
        return tuple(sorted(tracked | untracked))

    def apply_check(self, patch: str) -> bool:
        """Direct-source modunda patch yeniden uygulanmaz."""

        return bool(patch.strip())
