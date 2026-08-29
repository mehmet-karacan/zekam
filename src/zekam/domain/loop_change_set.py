"""Loop-owned, path-bounded reversible source change contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.sandbox import assert_relative_path


def _canonical_paths(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValidationFailed(f"{label} tekil ve sirali olmali")
    for value in values:
        assert_relative_path(value, label)


class SourceEntryKind(StrEnum):
    MISSING = "missing"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class LoopSourceEntry:
    """A content-free snapshot of one exact source path."""

    path: str
    kind: SourceEntryKind
    content_digest: str | None

    def __post_init__(self) -> None:
        assert_relative_path(self.path, "source entry path")
        if self.kind is SourceEntryKind.MISSING:
            if self.content_digest is not None:
                raise ValidationFailed("missing source entry content digest tasiyamaz")
        elif self.content_digest is None:
            raise ValidationFailed("file source entry content digest ister")
        if self.content_digest is not None:
            parse_digest(self.content_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": str(self.kind),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class LoopChangeBaseline:
    """Pre-attempt HEAD/tree and exact allowed/protected path observation."""

    attempt_id: UUID
    source_revision: str
    tree_digest: str
    dirty_state_digest: str
    allowed_paths: tuple[str, ...]
    allowed_entries: tuple[LoopSourceEntry, ...]
    protected_dirty_entries: tuple[LoopSourceEntry, ...]
    captured_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop change baseline authority veremez")
        if not self.source_revision.strip():
            raise ValidationFailed("Loop change baseline source revision ister")
        if self.captured_at.tzinfo is None:
            raise ValidationFailed("Loop change baseline timezone-aware zaman ister")
        parse_digest(self.tree_digest)
        parse_digest(self.dirty_state_digest)
        _canonical_paths(self.allowed_paths, "allowed path")
        if not self.allowed_paths:
            raise PolicyViolation("Loop change baseline exact allowed path ister")
        allowed_entry_paths = tuple(item.path for item in self.allowed_entries)
        protected_paths = tuple(item.path for item in self.protected_dirty_entries)
        if allowed_entry_paths != self.allowed_paths:
            raise ValidationFailed("Loop allowed path snapshot cardinality/order drift")
        _canonical_paths(protected_paths, "protected dirty path")
        if set(allowed_entry_paths) & set(protected_paths):
            raise PolicyViolation("Baslangicta dirty path loop-owned olamaz")

    def semantic_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-loop-change-baseline/v1",
            "attempt_id": str(self.attempt_id),
            "source_revision": self.source_revision,
            "tree_digest": self.tree_digest,
            "dirty_state_digest": self.dirty_state_digest,
            "allowed_paths": list(self.allowed_paths),
            "allowed_entries": [item.as_dict() for item in self.allowed_entries],
            "protected_dirty_entries": [
                item.as_dict() for item in self.protected_dirty_entries
            ],
            "captured_at": self.captured_at,
            "grants_authority": False,
        }

    @property
    def baseline_digest(self) -> str:
        return digest(self.semantic_body())


@dataclass(frozen=True, slots=True)
class LoopOwnedChangeSet:
    """Exact resources and forward/inverse patch evidence owned by one attempt."""

    attempt_id: UUID
    baseline_digest: str
    source_revision: str
    changed_resources: tuple[str, ...]
    before_entries: tuple[LoopSourceEntry, ...]
    after_entries: tuple[LoopSourceEntry, ...]
    forward_patch_digest: str
    inverse_patch_digest: str
    created_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop-owned change set authority veremez")
        if not self.source_revision.strip() or self.created_at.tzinfo is None:
            raise ValidationFailed("Loop-owned change set source/zaman binding ister")
        for value in (
            self.baseline_digest,
            self.forward_patch_digest,
            self.inverse_patch_digest,
        ):
            parse_digest(value)
        _canonical_paths(self.changed_resources, "changed resource")
        if not self.changed_resources:
            raise ValidationFailed("Loop-owned change set bos olamaz")
        if tuple(item.path for item in self.before_entries) != self.changed_resources:
            raise ValidationFailed("Loop change set before snapshot drift")
        if tuple(item.path for item in self.after_entries) != self.changed_resources:
            raise ValidationFailed("Loop change set after snapshot drift")
        if self.before_entries == self.after_entries:
            raise ValidationFailed("Loop change set gercek kaynak deltasi ister")

    @classmethod
    def create(
        cls,
        *,
        baseline: LoopChangeBaseline,
        changed_resources: tuple[str, ...],
        before_entries: tuple[LoopSourceEntry, ...],
        after_entries: tuple[LoopSourceEntry, ...],
        forward_patch_digest: str,
        inverse_patch_digest: str,
        created_at: dt.datetime,
    ) -> LoopOwnedChangeSet:
        baseline.__post_init__()
        changed = tuple(sorted(set(changed_resources)))
        if changed != changed_resources:
            raise ValidationFailed("changed resources tekil ve sirali olmali")
        if not set(changed).issubset(baseline.allowed_paths):
            raise PolicyViolation("Loop change set allowed path disina cikamaz")
        protected = {item.path for item in baseline.protected_dirty_entries}
        if protected & set(changed):
            raise PolicyViolation("Loop change set user dirty path sahiplenemez")
        expected_before = tuple(
            item for item in baseline.allowed_entries if item.path in set(changed)
        )
        if before_entries != expected_before:
            raise PolicyViolation("Loop change set exact baseline snapshot ister")
        return cls(
            attempt_id=baseline.attempt_id,
            baseline_digest=baseline.baseline_digest,
            source_revision=baseline.source_revision,
            changed_resources=changed,
            before_entries=before_entries,
            after_entries=after_entries,
            forward_patch_digest=forward_patch_digest,
            inverse_patch_digest=inverse_patch_digest,
            created_at=created_at,
        )

    def semantic_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-loop-owned-change-set/v1",
            "attempt_id": str(self.attempt_id),
            "baseline_digest": self.baseline_digest,
            "source_revision": self.source_revision,
            "changed_resources": list(self.changed_resources),
            "before_entries": [item.as_dict() for item in self.before_entries],
            "after_entries": [item.as_dict() for item in self.after_entries],
            "forward_patch_digest": self.forward_patch_digest,
            "inverse_patch_digest": self.inverse_patch_digest,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def change_set_digest(self) -> str:
        return digest(self.semantic_body())


@dataclass(frozen=True, slots=True)
class LoopRollbackPlan:
    change_set_digest: str
    attempt_id: UUID
    source_revision: str
    changed_resources: tuple[str, ...]
    inverse_patch_digest: str
    protected_state_digest: str
    reason_code: str
    prepared_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop rollback plan authority veremez")
        for value in (
            self.change_set_digest,
            self.inverse_patch_digest,
            self.protected_state_digest,
        ):
            parse_digest(value)
        _canonical_paths(self.changed_resources, "rollback resource")
        if not self.source_revision.strip() or not self.reason_code.strip():
            raise ValidationFailed("Loop rollback source ve reason ister")
        if self.prepared_at.tzinfo is None:
            raise ValidationFailed("Loop rollback timezone-aware zaman ister")

    def semantic_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-loop-rollback-plan/v1",
            "change_set_digest": self.change_set_digest,
            "attempt_id": str(self.attempt_id),
            "source_revision": self.source_revision,
            "changed_resources": list(self.changed_resources),
            "inverse_patch_digest": self.inverse_patch_digest,
            "protected_state_digest": self.protected_state_digest,
            "reason_code": self.reason_code,
            "prepared_at": self.prepared_at,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.semantic_body())


@dataclass(frozen=True, slots=True)
class LoopPatchApplyCheck:
    plan_digest: str
    inverse_patch_digest: str
    source_revision: str
    changed_resources: tuple[str, ...]
    protected_state_digest: str
    applicable: bool
    checked_at: dt.datetime

    def __post_init__(self) -> None:
        for value in (
            self.plan_digest,
            self.inverse_patch_digest,
            self.protected_state_digest,
        ):
            parse_digest(value)
        _canonical_paths(self.changed_resources, "apply-check resource")
        if not self.source_revision.strip() or self.checked_at.tzinfo is None:
            raise ValidationFailed("Loop apply-check source/zaman binding ister")

    @property
    def check_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-loop-patch-apply-check/v1",
                "plan_digest": self.plan_digest,
                "inverse_patch_digest": self.inverse_patch_digest,
                "source_revision": self.source_revision,
                "changed_resources": list(self.changed_resources),
                "protected_state_digest": self.protected_state_digest,
                "applicable": self.applicable,
                "checked_at": self.checked_at,
            }
        )


@dataclass(frozen=True, slots=True)
class LoopRollbackReceipt:
    plan_digest: str
    change_set_digest: str
    apply_check_digest: str
    inverse_patch_digest: str
    changed_resources: tuple[str, ...]
    post_state_digest: str
    applied_at: dt.datetime
    status: str = "applied"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority or self.status != "applied":
            raise PolicyViolation("Loop rollback receipt yalniz applied authority-free olabilir")
        for value in (
            self.plan_digest,
            self.change_set_digest,
            self.apply_check_digest,
            self.inverse_patch_digest,
            self.post_state_digest,
        ):
            parse_digest(value)
        _canonical_paths(self.changed_resources, "rollback receipt resource")
        if self.applied_at.tzinfo is None:
            raise ValidationFailed("Loop rollback receipt timezone-aware zaman ister")

    @property
    def receipt_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-loop-rollback-receipt/v1",
                "plan_digest": self.plan_digest,
                "change_set_digest": self.change_set_digest,
                "apply_check_digest": self.apply_check_digest,
                "inverse_patch_digest": self.inverse_patch_digest,
                "changed_resources": list(self.changed_resources),
                "post_state_digest": self.post_state_digest,
                "applied_at": self.applied_at,
                "status": self.status,
                "grants_authority": False,
            }
        )
