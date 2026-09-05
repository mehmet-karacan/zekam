"""Explicit, digest-bound repair planning for ``zekam doctor``.

Normal doctor checks remain read-only.  This module separates immutable plans
from effects and rejects dirty/diverged Git state, remote drift, migration
drift, caller supplied SQL and missing exact plan digests.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation


class MigrationView(Protocol):
    version: int
    name: str
    checksum: str
    has_down: bool
    label: str


class MigrationStatusView(Protocol):
    head: int
    applied: tuple[MigrationView, ...]
    pending: tuple[MigrationView, ...]
    drift: tuple[Any, ...]


class RoutineIntegrityStatusView(Protocol):
    missing: tuple[Any, ...]
    migration_drift: tuple[Any, ...]
    migration_pending: tuple[Any, ...]
    repair_plan_digest: str
    migration_head: int

    def as_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class GitRepositoryState:
    root: Path
    branch: str
    head: str
    upstream: str | None
    upstream_ref: str | None
    upstream_head: str | None
    remote: str | None
    remote_branch: str | None
    ahead: int
    behind: int
    dirty_paths: tuple[str, ...]

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_paths)

    def body(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "upstream_ref": self.upstream_ref,
            "upstream_head": self.upstream_head,
            "remote": self.remote,
            "remote_branch": self.remote_branch,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty_paths": list(self.dirty_paths),
        }


@dataclass(frozen=True, slots=True)
class GitFastForwardPlan:
    state: GitRepositoryState
    blocked_reasons: tuple[str, ...]
    required: bool

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-doctor-git-fast-forward-plan/v1",
            "state": self.state.body(),
            "required": self.required,
            "blocked_reasons": list(self.blocked_reasons),
            "strategy": "remote-oid-match+fetch+ff-only",
            "network_checked": False,
            "force": False,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "plan_digest": self.plan_digest,
            "applicable": self.required and not self.blocked_reasons,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class DatabaseRoutinePlan:
    status: RoutineIntegrityStatusView
    blocked_reasons: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.status.missing)

    @property
    def plan_digest(self) -> str:
        return self.status.repair_plan_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-doctor-database-routine-plan/v1",
            "required": self.required,
            "applicable": self.required and not self.blocked_reasons,
            "blocked_reasons": list(self.blocked_reasons),
            "routine_plan_digest": self.plan_digest,
            "status": self.status.as_dict(),
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class DatabaseMigrationPlan:
    """Exact siradaki migration'i immutable bir repair adimina baglar."""

    status: MigrationStatusView
    blocked_reasons: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.status.pending)

    @property
    def next_migration(self) -> MigrationView | None:
        return self.status.pending[0] if self.status.pending else None

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        next_migration = self.next_migration
        return {
            "schema": "zekam-doctor-database-migration-plan/v1",
            "current_head": self.status.head,
            "target": (
                None
                if next_migration is None
                else {
                    "version": next_migration.version,
                    "name": next_migration.name,
                    "checksum": next_migration.checksum,
                }
            ),
            "pending": [
                {
                    "version": item.version,
                    "name": item.name,
                    "checksum": item.checksum,
                }
                for item in self.status.pending
            ],
            "drift": [
                {
                    "kind": item.kind.value,
                    "version": item.version,
                    "detail": item.detail,
                }
                for item in self.status.drift
            ],
            "blocked_reasons": list(self.blocked_reasons),
            "strategy": "one-migration-per-effect+checksum+advisory-lock",
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "required": self.required,
            "applicable": self.required and not self.blocked_reasons,
            "migration_plan_digest": self.plan_digest,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class DoctorRepairPlan:
    git: GitFastForwardPlan
    migrations: DatabaseMigrationPlan | None
    routines: DatabaseRoutinePlan | None

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    @property
    def required_steps(self) -> tuple[str, ...]:
        steps: list[str] = []
        if self.git.required:
            steps.append("git-fast-forward")
        if self.migrations is not None and self.migrations.required:
            steps.append("postgres-migration-upgrade")
        if self.routines is not None and self.routines.required:
            steps.append("postgres-routine-repair")
        return tuple(steps)

    @property
    def next_step(self) -> str | None:
        if self.git.required:
            return "git-fast-forward"
        if self.migrations is not None and self.migrations.required:
            return "postgres-migration-upgrade"
        if self.routines is not None and self.routines.required:
            return "postgres-routine-repair"
        return None

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        if self.git.required:
            return tuple(sorted(set(self.git.blocked_reasons)))
        if self.migrations is not None and self.migrations.required:
            return tuple(sorted(set(self.migrations.blocked_reasons)))
        if self.routines is not None and self.routines.required:
            return tuple(sorted(set(self.routines.blocked_reasons)))
        return ()

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-doctor-repair-plan/v1",
            "git": self.git.as_dict(),
            "migrations": None if self.migrations is None else self.migrations.as_dict(),
            "routines": None if self.routines is None else self.routines.as_dict(),
            "execution_order": list(self.required_steps),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "plan_digest": self.plan_digest,
            "required_steps": list(self.required_steps),
            "next_step": self.next_step,
            "blocked_reasons": list(self.blocked_reasons),
            "applicable": bool(self.required_steps) and not self.blocked_reasons,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class GitFastForwardResult:
    old_head: str
    new_head: str
    remote_head: str
    plan_digest: str
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_head": self.old_head,
            "new_head": self.new_head,
            "remote_head": self.remote_head,
            "plan_digest": self.plan_digest,
            "changed": self.changed,
            "strategy": "ff-only",
            "force": False,
            "verified": self.new_head == self.remote_head,
        }


def observe_git_repository(root: Path) -> GitRepositoryState:
    branch = _git_required(root, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git_required(root, "rev-parse", "HEAD")
    dirty = _git_required(root, "status", "--porcelain=v1", "--untracked-files=all")
    dirty_paths = tuple(line[3:] for line in dirty.splitlines() if len(line) > 3)
    upstream = _git_optional(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    upstream_ref = _git_optional(root, "rev-parse", "--symbolic-full-name", "@{upstream}")
    upstream_head = _git_optional(root, "rev-parse", "@{upstream}") if upstream else None
    ahead = 0
    behind = 0
    if upstream_head is not None:
        counts = _git_required(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
        left, right = counts.split()
        behind, ahead = int(left), int(right)
    remote, remote_branch = _split_upstream(root, upstream_ref)
    return GitRepositoryState(
        root=root.resolve(),
        branch=branch,
        head=head,
        upstream=upstream,
        upstream_ref=upstream_ref,
        upstream_head=upstream_head,
        remote=remote,
        remote_branch=remote_branch,
        ahead=ahead,
        behind=behind,
        dirty_paths=dirty_paths,
    )


def plan_git_fast_forward(root: Path) -> GitFastForwardPlan:
    state = observe_git_repository(root)
    blocked: list[str] = []
    if state.upstream is None or state.upstream_head is None:
        blocked.append("upstream-missing")
    if state.remote is None or state.remote_branch is None:
        blocked.append("upstream-remote-unresolved")
    if state.is_dirty:
        blocked.append("worktree-dirty")
    if state.ahead and state.behind:
        blocked.append("branch-diverged")
    elif state.ahead:
        blocked.append("local-ahead")
    required = state.behind > 0
    return GitFastForwardPlan(state=state, blocked_reasons=tuple(blocked), required=required)


def build_doctor_repair_plan(
    *,
    core_path: Path,
    connection: Any | None = None,
    migrations_directory: Path | None = None,
    migration_status_reader: Callable[[Any, Path | None], MigrationStatusView] | None = None,
    routine_status_reader: Callable[[Any, Path | None], RoutineIntegrityStatusView] | None = None,
) -> DoctorRepairPlan:
    git = plan_git_fast_forward(core_path)
    migration_plan: DatabaseMigrationPlan | None = None
    routines: DatabaseRoutinePlan | None = None
    if connection is not None:
        if migration_status_reader is None or routine_status_reader is None:
            raise PolicyViolation("Database doctor repair status adapterlari ister")
        migration_status = migration_status_reader(connection, migrations_directory)
        migration_blocked: list[str] = []
        if migration_status.drift:
            migration_blocked.append("migration-drift")
        if git.required:
            migration_blocked.append("git-fast-forward-must-run-first")
        migration_plan = DatabaseMigrationPlan(
            status=migration_status,
            blocked_reasons=tuple(migration_blocked),
        )
        routine_status = routine_status_reader(connection, migrations_directory)
        blocked: list[str] = []
        if routine_status.migration_drift:
            blocked.append("migration-drift")
        if routine_status.migration_pending:
            blocked.append("migration-pending")
        if git.required:
            blocked.append("git-fast-forward-must-run-first")
        routines = DatabaseRoutinePlan(status=routine_status, blocked_reasons=tuple(blocked))
    return DoctorRepairPlan(git=git, migrations=migration_plan, routines=routines)


def apply_git_fast_forward(
    root: Path, *, plan: GitFastForwardPlan, plan_digest: str
) -> GitFastForwardResult:
    """Verify remote exact OID, fetch it and perform only a clean ff-only merge."""

    if plan.plan_digest != plan_digest:
        raise PolicyViolation("Git repair plan digest exact degil")
    if not plan.required:
        return GitFastForwardResult(
            old_head=plan.state.head,
            new_head=plan.state.head,
            remote_head=plan.state.head,
            plan_digest=plan_digest,
            changed=False,
        )
    if plan.blocked_reasons:
        raise PolicyViolation("Git fast-forward bloke: " + ",".join(plan.blocked_reasons))
    current = observe_git_repository(root)
    if current.body() != plan.state.body():
        raise PolicyViolation("Git repair plani local state degistigi icin stale")
    assert current.remote is not None
    assert current.remote_branch is not None
    assert current.upstream_head is not None
    remote_ref = f"refs/heads/{current.remote_branch}"
    probe = _git_required(root, "ls-remote", "--heads", current.remote, remote_ref)
    rows = [line.split() for line in probe.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ConfigurationError("Exact remote branch HEAD cozumlenemedi")
    remote_head = rows[0][0]
    if remote_head != current.upstream_head:
        raise PolicyViolation("Remote HEAD plan sonrasinda degisti; yeni repair plan gerekir")
    ancestor = _git_completed(root, "merge-base", "--is-ancestor", current.head, remote_head)
    if ancestor.returncode != 0:
        raise PolicyViolation("Remote HEAD local HEAD'in fast-forward soyundan degil")
    destination = f"refs/remotes/{current.remote}/{current.remote_branch}"
    _git_required(root, "fetch", "--no-tags", current.remote, f"{remote_ref}:{destination}")
    fetched = _git_required(root, "rev-parse", destination)
    if fetched != remote_head:
        raise ConfigurationError("Fetch sonrasi remote tracking ref exact OID ile eslesmedi")
    _git_required(root, "merge", "--ff-only", remote_head)
    after = observe_git_repository(root)
    if after.head != remote_head or after.is_dirty or after.behind != 0:
        raise ConfigurationError("Fast-forward sonrasi Git dogrulamasi basarisiz")
    return GitFastForwardResult(
        old_head=current.head,
        new_head=after.head,
        remote_head=remote_head,
        plan_digest=plan_digest,
        changed=after.head != current.head,
    )


def _split_upstream(root: Path, upstream_ref: str | None) -> tuple[str | None, str | None]:
    if upstream_ref is None:
        return None, None
    remotes = tuple(sorted(_git_required(root, "remote").splitlines(), key=len, reverse=True))
    for remote in remotes:
        prefix = f"refs/remotes/{remote}/"
        if upstream_ref.startswith(prefix):
            return remote, upstream_ref.removeprefix(prefix)
    return None, None


def _git_required(root: Path, *args: str) -> str:
    completed = _git_completed(root, *args)
    if completed.returncode != 0:
        raise ConfigurationError(f"Git komutu basarisiz: {args[0]} ({completed.returncode})")
    return completed.stdout.rstrip()


def _git_optional(root: Path, *args: str) -> str | None:
    completed = _git_completed(root, *args)
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip() or None


def _git_completed(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(f"Git calistirilamadi: {type(exc).__name__}") from exc
