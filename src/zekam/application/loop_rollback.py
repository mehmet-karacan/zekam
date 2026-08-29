"""Application boundary for regression-only loop-owned inverse rollback."""

from __future__ import annotations

import datetime as dt
from typing import Protocol, TypeVar

from zekam.domain.errors import PolicyViolation
from zekam.domain.loop_change_set import (
    LoopChangeBaseline,
    LoopOwnedChangeSet,
    LoopPatchApplyCheck,
    LoopRollbackPlan,
    LoopRollbackReceipt,
)

PatchBundleT = TypeVar("PatchBundleT")
PatchBundleContra = TypeVar("PatchBundleContra", contravariant=True)


class LoopPatchPort(Protocol[PatchBundleContra]):
    def protected_state_digest(self, baseline: LoopChangeBaseline) -> str: ...

    def apply_check(
        self,
        *,
        baseline: LoopChangeBaseline,
        captured: PatchBundleContra,
        plan: LoopRollbackPlan,
        checked_at: dt.datetime,
    ) -> LoopPatchApplyCheck: ...

    def apply_inverse(
        self,
        *,
        baseline: LoopChangeBaseline,
        captured: PatchBundleContra,
        plan: LoopRollbackPlan,
        apply_check: LoopPatchApplyCheck,
    ) -> str: ...


class LoopRollbackService[PatchBundleT]:
    """Prepare and execute a narrow inverse patch; never a blanket Git rollback."""

    _ALLOWED_REASONS = frozenset(
        {
            "metric-regression",
            "validator-invalid",
            "security-invalid",
        }
    )

    def __init__(self, adapter: LoopPatchPort[PatchBundleT]) -> None:
        self.adapter = adapter

    def prepare(
        self,
        *,
        baseline: LoopChangeBaseline,
        change_set: LoopOwnedChangeSet,
        reason_code: str,
        prepared_at: dt.datetime,
    ) -> LoopRollbackPlan:
        if reason_code not in self._ALLOWED_REASONS:
            raise PolicyViolation("Loop rollback yalniz regression/invalid sonucu icindir")
        if (
            change_set.attempt_id != baseline.attempt_id
            or change_set.baseline_digest != baseline.baseline_digest
            or change_set.source_revision != baseline.source_revision
            or not set(change_set.changed_resources).issubset(baseline.allowed_paths)
        ):
            raise PolicyViolation("Loop rollback baseline ve exact change set ister")
        protected = {item.path for item in baseline.protected_dirty_entries}
        if protected & set(change_set.changed_resources):
            raise PolicyViolation("Loop rollback user dirty path'e dokunamaz")
        return LoopRollbackPlan(
            change_set_digest=change_set.change_set_digest,
            attempt_id=change_set.attempt_id,
            source_revision=change_set.source_revision,
            changed_resources=change_set.changed_resources,
            inverse_patch_digest=change_set.inverse_patch_digest,
            protected_state_digest=self.adapter.protected_state_digest(baseline),
            reason_code=reason_code,
            prepared_at=prepared_at,
        )

    def execute(
        self,
        *,
        baseline: LoopChangeBaseline,
        captured: PatchBundleT,
        change_set: LoopOwnedChangeSet,
        plan: LoopRollbackPlan,
        checked_at: dt.datetime,
        applied_at: dt.datetime,
    ) -> LoopRollbackReceipt:
        if plan.change_set_digest != change_set.change_set_digest:
            raise PolicyViolation("Loop rollback plan exact change set'e bagli degil")
        apply_check = self.adapter.apply_check(
            baseline=baseline,
            captured=captured,
            plan=plan,
            checked_at=checked_at,
        )
        if (
            not apply_check.applicable
            or apply_check.plan_digest != plan.plan_digest
            or apply_check.inverse_patch_digest != plan.inverse_patch_digest
            or apply_check.changed_resources != plan.changed_resources
            or apply_check.protected_state_digest != plan.protected_state_digest
        ):
            raise PolicyViolation("Loop rollback canonical apply-check gecmedi")
        post_state_digest = self.adapter.apply_inverse(
            baseline=baseline,
            captured=captured,
            plan=plan,
            apply_check=apply_check,
        )
        return LoopRollbackReceipt(
            plan_digest=plan.plan_digest,
            change_set_digest=change_set.change_set_digest,
            apply_check_digest=apply_check.check_digest,
            inverse_patch_digest=change_set.inverse_patch_digest,
            changed_resources=change_set.changed_resources,
            post_state_digest=post_state_digest,
            applied_at=applied_at,
        )
