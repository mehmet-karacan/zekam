"""Shell-free bounded Git push transport; native shell isolation is a host concern."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from zekam.application.git_publication import GitPublicationTarget
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.process.bounded_stream import (
    BoundedReader,
    finish_pipes,
    hard_kill_tree,
    start_process_tree,
    wait_for,
)


@dataclass(frozen=True, slots=True)
class BoundGitPushTransport:
    source_root: Path
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600:
            raise ValidationFailed("Git publication timeout 1..600 saniye olmali")

    def _command(
        self, *arguments: str, cancel: threading.Event | None = None, allow_missing: bool = False
    ) -> str:
        root = self.source_root.resolve(strict=True)
        # Never inherit Git command/config/directory overrides from the caller.
        # Existing OS credentials/SSH agent remain host-owned, not copied to plans.
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        command = (
            "git",
            "-c",
            "core.hooksPath=",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "push.followTags=false",
            "-C",
            str(root),
            *arguments,
        )
        tree = start_process_tree(command, root, env=environment, stderr=subprocess.DEVNULL)
        process = tree.process
        reader = BoundedReader(process.stdout, 256 * 1024)
        thread = threading.Thread(target=reader.read, daemon=True)
        thread.start()
        if process.stdin is not None:
            process.stdin.close()
        state = "unknown"
        try:
            state = wait_for(
                process, time.monotonic() + self.timeout_seconds, reader.overflow, cancel
            )
            if state != "exited":
                raise PolicyViolation("Git publication process kesildi; recovery gerekli")
            # Drain the bounded reader before decoding a fast process's output.
            thread.join(timeout=2)
            if thread.is_alive() or reader.overflow.is_set():
                raise PolicyViolation("Git publication output bounded siniri asildi")
            if process.returncode != 0:
                if allow_missing and process.returncode == 1:
                    return ""
                raise PolicyViolation("Git publication command basarisiz; ham cikti gizlendi")
            return bytes(reader.buffer).decode("utf-8", errors="replace").strip()
        finally:
            if state != "exited" or process.poll() is None or thread.is_alive():
                hard_kill_tree(tree)
            finish_pipes(tree, (thread,))

    def _snapshot(self, *, remote: str, ref: str) -> tuple[GitPublicationTarget, str]:
        # Validate argument tokens before any subprocess (target carries no URL).
        GitPublicationTarget(digest("validation"), remote, digest("validation"), ref, "0" * 40)
        root = self.source_root.resolve(strict=True)
        if self.source_root.is_symlink() or root != self.source_root.absolute():
            raise PolicyViolation("Git publication exact source root ister")
        top = Path(self._command("rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != root or self._command("status", "--porcelain", "--untracked-files=normal"):
            raise PolicyViolation("Git publication temiz exact kaynak kokunu ister")
        # URL rewrites can redirect an apparently exact target. This typed
        # transport refuses them rather than claiming to sandbox arbitrary Git config.
        rewrites = self._command(
            "config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", allow_missing=True
        )
        if rewrites:
            raise PolicyViolation("Git publication URL rewrite ayariyla kullanilamaz")
        urls = self._command("remote", "get-url", "--push", "--all", remote).splitlines()
        if (
            len(urls) != 1
            or not urls[0]
            or len(urls[0]) > 4096
            or any(ord(char) < 32 for char in urls[0])
        ):
            raise PolicyViolation("Git publication tek exact push URL ister")
        url = urls[0]
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise PolicyViolation("Git publication remote URL gecersiz") from exc
        if parsed.scheme in {"http", "https", "ssh", "file"}:
            if (
                parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.scheme in {"http", "https"} and parsed.username)
                or (parsed.scheme != "file" and any(char.isspace() for char in url))
            ):
                raise PolicyViolation("Git publication remote URL credential tasiyamaz")
        elif not (
            re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url)
            or Path(url).is_absolute()
        ):
            raise PolicyViolation("Git publication remote transport desteklenmiyor")
        head = self._command("rev-parse", "--verify", "HEAD^{commit}")
        identity = root.stat()
        target = GitPublicationTarget(
            digest(
                {
                    "root": os.path.normcase(str(root)),
                    "device": identity.st_dev,
                    "inode": identity.st_ino,
                }
            ),
            remote,
            digest({"push_url": url}),
            ref,
            head,
        )
        return target, url

    def inspect(self, *, remote: str, ref: str) -> GitPublicationTarget:
        return self._snapshot(remote=remote, ref=ref)[0]

    def push(self, target: GitPublicationTarget, *, cancel: threading.Event) -> str:
        if type(target) is not GitPublicationTarget:
            raise ValidationFailed("Git publication typed target ister")
        if cancel.is_set():
            raise PolicyViolation("Git publication cancellation")
        actual, url = self._snapshot(remote=target.remote, ref=target.ref)
        if actual != target or cancel.is_set():
            raise PolicyViolation("Git publication target drift veya cancellation")
        # Explicit OID/refspec, one URL, no force/mirror/tags/submodule recursion.
        self._command(
            "push",
            "--porcelain",
            "--no-verify",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--",
            url,
            f"{target.head}:{target.ref}",
            cancel=cancel,
        )
        readback = self._command("ls-remote", "--refs", "--", url, target.ref, cancel=cancel)
        rows = [line.split() for line in readback.splitlines() if line.strip()]
        if (
            len(rows) != 1
            or len(rows[0]) != 2
            or rows[0][1] != target.ref
            or rows[0][0] != target.head
        ):
            raise PolicyViolation("Git publication remote ref readback uyusmuyor")
        return rows[0][0]
