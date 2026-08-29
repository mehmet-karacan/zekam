"""Fail-closed Git adapter for exact loop-owned inverse patches.

This adapter never invokes reset, clean, checkout, restore, or a shell.  It
supports bounded UTF-8 text patches and rejects binary/no-final-newline inputs
instead of widening the rollback surface.
"""

from __future__ import annotations

import datetime as dt
import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_change_set import (
    LoopChangeBaseline,
    LoopOwnedChangeSet,
    LoopPatchApplyCheck,
    LoopRollbackPlan,
    LoopSourceEntry,
    SourceEntryKind,
)
from zekam.domain.sandbox import assert_relative_path
from zekam.infrastructure.git.source_reader import (
    COMMAND_TIMEOUT,
    GitCommandError,
    repository_root,
    run_read_only,
)
from zekam.infrastructure.git.worktree import ManagedWorktree


@dataclass(frozen=True, slots=True)
class CapturedLoopBaseline:
    baseline: LoopChangeBaseline
    contents: tuple[tuple[str, bytes | None], ...]

    def __post_init__(self) -> None:
        if tuple(path for path, _content in self.contents) != self.baseline.allowed_paths:
            raise ValidationFailed("Captured loop baseline content order drift")


@dataclass(frozen=True, slots=True)
class CapturedLoopChangeSet:
    change_set: LoopOwnedChangeSet
    forward_patch: bytes
    inverse_patch: bytes

    def __post_init__(self) -> None:
        if digest_of_bytes(self.forward_patch) != self.change_set.forward_patch_digest:
            raise ValidationFailed("Loop forward patch digest drift")
        if digest_of_bytes(self.inverse_patch) != self.change_set.inverse_patch_digest:
            raise ValidationFailed("Loop inverse patch digest drift")


def _run_apply(root: Path, patch: bytes, *, check: bool) -> None:
    arguments = ["apply"]
    if check:
        arguments.append("--check")
    arguments.extend(("--whitespace=nowarn", "-"))
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
            input=patch,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitCommandError("Loop inverse patch Git apply calistirilamadi") from exc
    if completed.returncode != 0:
        action = "apply-check" if check else "apply"
        raise PolicyViolation(f"Loop inverse patch {action} reddedildi")


def _entry(path: str, content: bytes | None) -> LoopSourceEntry:
    return LoopSourceEntry(
        path,
        SourceEntryKind.MISSING if content is None else SourceEntryKind.FILE,
        None if content is None else digest_of_bytes(content),
    )


def _patch_for(path: str, before: bytes | None, after: bytes | None) -> bytes:
    if before == after:
        return b""
    decoded: list[list[str]] = []
    for value in (before, after):
        if value is None:
            decoded.append([])
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyViolation("Loop rollback binary source patch desteklemiyor") from exc
        if text and not text.endswith("\n"):
            raise PolicyViolation("Loop rollback final newline olmayan source'u reddeder")
        decoded.append(text.splitlines(keepends=True))
    from_name = "/dev/null" if before is None else f"a/{path}"
    to_name = "/dev/null" if after is None else f"b/{path}"
    header = [f"diff --git a/{path} b/{path}\n"]
    if before is None:
        header.append("new file mode 100644\n")
    elif after is None:
        header.append("deleted file mode 100644\n")
    body = difflib.unified_diff(
        decoded[0],
        decoded[1],
        fromfile=from_name,
        tofile=to_name,
        lineterm="\n",
    )
    return "".join((*header, *body)).encode("utf-8")


class GitLoopPatchAdapter:
    """Observe and reverse only an exact attempt-owned path set."""

    def __init__(self, source_root: Path) -> None:
        resolved = source_root.resolve()
        actual = repository_root(resolved)
        if actual is None or actual.resolve() != resolved:
            raise PolicyViolation("Loop patch adapter exact Git source root ister")
        self.source_root = resolved

    def _resolve(self, relative: str) -> Path:
        assert_relative_path(relative, "loop source path")
        head = run_read_only(self.source_root, "rev-parse", "HEAD").strip()
        return ManagedWorktree("loop-patch", self.source_root, head).resolve(relative)

    def _content(self, relative: str) -> bytes | None:
        target = self._resolve(relative)
        if not target.exists():
            return None
        if not target.is_file():
            raise PolicyViolation("Loop-owned path normal dosya olmali")
        return target.read_bytes()

    def _dirty_paths(self) -> tuple[str, ...]:
        tracked = run_read_only(self.source_root, "diff", "--name-only", "-z", "HEAD", "--").split(
            "\0"
        )
        untracked = run_read_only(
            self.source_root, "ls-files", "--others", "--exclude-standard", "-z"
        ).split("\0")
        paths = tuple(sorted({value for value in (*tracked, *untracked) if value}))
        for path in paths:
            self._resolve(path)
        return paths

    def _tree_digest(self) -> str:
        return digest(
            {
                "head": run_read_only(self.source_root, "rev-parse", "HEAD").strip(),
                "index": run_read_only(self.source_root, "ls-files", "--stage"),
            }
        )

    @staticmethod
    def _protected_digest(entries: tuple[LoopSourceEntry, ...]) -> str:
        return digest(
            {
                "schema": "zekam-loop-protected-dirty-state/v1",
                "entries": [item.as_dict() for item in entries],
            }
        )

    def capture_baseline(
        self,
        *,
        attempt_id: UUID,
        allowed_paths: tuple[str, ...],
        captured_at: dt.datetime,
    ) -> CapturedLoopBaseline:
        normalized = tuple(sorted(set(allowed_paths)))
        if normalized != allowed_paths:
            raise ValidationFailed("Loop allowed paths tekil ve sirali olmali")
        dirty_paths = self._dirty_paths()
        overlap = set(normalized) & set(dirty_paths)
        if overlap:
            raise PolicyViolation("Baslangicta dirty allowed path loop-owned olamaz")
        contents = tuple((path, self._content(path)) for path in normalized)
        allowed_entries = tuple(_entry(path, content) for path, content in contents)
        protected = tuple(_entry(path, self._content(path)) for path in dirty_paths)
        source_revision = run_read_only(self.source_root, "rev-parse", "HEAD").strip()
        baseline = LoopChangeBaseline(
            attempt_id=attempt_id,
            source_revision=source_revision,
            tree_digest=self._tree_digest(),
            dirty_state_digest=digest(
                {
                    "schema": "zekam-loop-dirty-state/v1",
                    "source_revision": source_revision,
                    "protected_state_digest": self._protected_digest(protected),
                }
            ),
            allowed_paths=normalized,
            allowed_entries=allowed_entries,
            protected_dirty_entries=protected,
            captured_at=captured_at,
        )
        return CapturedLoopBaseline(baseline, contents)

    def _assert_source_and_protected(self, baseline: LoopChangeBaseline) -> None:
        head = run_read_only(self.source_root, "rev-parse", "HEAD").strip()
        if head != baseline.source_revision:
            raise PolicyViolation("Loop rollback source HEAD drift")
        observed = tuple(
            _entry(item.path, self._content(item.path)) for item in baseline.protected_dirty_entries
        )
        if observed != baseline.protected_dirty_entries:
            raise PolicyViolation("User dirty baseline attempt sirasinda drift etti")

    def capture_change_set(
        self, captured: CapturedLoopBaseline, *, created_at: dt.datetime
    ) -> CapturedLoopChangeSet:
        baseline = captured.baseline
        self._assert_source_and_protected(baseline)
        protected = {item.path for item in baseline.protected_dirty_entries}
        unexpected = set(self._dirty_paths()) - protected - set(baseline.allowed_paths)
        if unexpected:
            raise PolicyViolation("Loop attempt allowed path disinda kaynak degistirdi")
        after_contents = tuple((path, self._content(path)) for path in baseline.allowed_paths)
        before_map = dict(captured.contents)
        after_map = dict(after_contents)
        changed = tuple(
            path for path in baseline.allowed_paths if before_map[path] != after_map[path]
        )
        if not changed:
            raise ValidationFailed("Loop attempt kaynak degisikligi uretmedi")
        forward_patch = b"".join(
            _patch_for(path, before_map[path], after_map[path]) for path in changed
        )
        inverse_patch = b"".join(
            _patch_for(path, after_map[path], before_map[path]) for path in changed
        )
        before_entries = tuple(_entry(path, before_map[path]) for path in changed)
        after_entries = tuple(_entry(path, after_map[path]) for path in changed)
        change_set = LoopOwnedChangeSet.create(
            baseline=baseline,
            changed_resources=changed,
            before_entries=before_entries,
            after_entries=after_entries,
            forward_patch_digest=digest_of_bytes(forward_patch),
            inverse_patch_digest=digest_of_bytes(inverse_patch),
            created_at=created_at,
        )
        return CapturedLoopChangeSet(change_set, forward_patch, inverse_patch)

    def apply_check(
        self,
        *,
        baseline: LoopChangeBaseline,
        captured: CapturedLoopChangeSet,
        plan: LoopRollbackPlan,
        checked_at: dt.datetime,
    ) -> LoopPatchApplyCheck:
        change_set = captured.change_set
        if (
            plan.change_set_digest != change_set.change_set_digest
            or plan.attempt_id != change_set.attempt_id
            or plan.source_revision != change_set.source_revision
            or plan.changed_resources != change_set.changed_resources
            or plan.inverse_patch_digest != change_set.inverse_patch_digest
            or plan.protected_state_digest
            != self._protected_digest(baseline.protected_dirty_entries)
            or baseline.baseline_digest != change_set.baseline_digest
        ):
            raise PolicyViolation("Loop rollback plan exact change set/baseline ile uyusmuyor")
        self._assert_source_and_protected(baseline)
        current = tuple(_entry(path, self._content(path)) for path in change_set.changed_resources)
        if current != change_set.after_entries:
            raise PolicyViolation("Loop rollback target attempt sonu snapshot'inda degil")
        _run_apply(self.source_root, captured.inverse_patch, check=True)
        return LoopPatchApplyCheck(
            plan_digest=plan.plan_digest,
            inverse_patch_digest=plan.inverse_patch_digest,
            source_revision=plan.source_revision,
            changed_resources=plan.changed_resources,
            protected_state_digest=plan.protected_state_digest,
            applicable=True,
            checked_at=checked_at,
        )

    def apply_inverse(
        self,
        *,
        baseline: LoopChangeBaseline,
        captured: CapturedLoopChangeSet,
        plan: LoopRollbackPlan,
        apply_check: LoopPatchApplyCheck,
    ) -> str:
        if (
            not apply_check.applicable
            or apply_check.plan_digest != plan.plan_digest
            or apply_check.inverse_patch_digest != captured.change_set.inverse_patch_digest
            or apply_check.changed_resources != captured.change_set.changed_resources
        ):
            raise PolicyViolation("Loop rollback exact basarili apply-check ister")
        self._assert_source_and_protected(baseline)
        _run_apply(self.source_root, captured.inverse_patch, check=False)
        observed = tuple(
            _entry(path, self._content(path)) for path in captured.change_set.changed_resources
        )
        if observed != captured.change_set.before_entries:
            raise PolicyViolation("Loop inverse patch baseline snapshot'i geri getirmedi")
        self._assert_source_and_protected(baseline)
        return digest(
            {
                "schema": "zekam-loop-post-rollback-state/v1",
                "source_revision": plan.source_revision,
                "changed_entries": [item.as_dict() for item in observed],
                "protected_state_digest": plan.protected_state_digest,
            }
        )

    def protected_state_digest(self, baseline: LoopChangeBaseline) -> str:
        return self._protected_digest(baseline.protected_dirty_entries)
