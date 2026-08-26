"""Additive, shadow-first Memory Continuity upgrade protocol.

The protocol deliberately separates read-only detection/planning from effects.
It never applies SQL migrations itself and it never treats a feature-policy row
as authority.  Enforced mode and the final component stamp require separate,
exact authorization records.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization

COMPONENT = "memory-continuity-plane"
FEATURE_RESOURCE = f"continuity:memory-upgrade:{COMPONENT}"


def canonical_projection_source_digest(
    *,
    source_head: str,
    source_tree_digest: str,
    migration_head: int,
    database_revision_digest: str,
) -> str:
    if not source_head.strip() or migration_head < 1:
        raise ValidationFailed("Projection source HEAD/migration binding gecersiz")
    parse_digest(source_tree_digest)
    parse_digest(database_revision_digest)
    return digest(
        {
            "source_head": source_head,
            "source_tree_digest": source_tree_digest,
            "migration_head": migration_head,
            "database_revision_digest": database_revision_digest,
        }
    )


class MemoryFeatureMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"


class UpgradeTarget(StrEnum):
    SHADOW = "shadow"
    ENFORCED = "enforced"
    STAMPED = "stamped"


@dataclass(frozen=True, slots=True)
class MemoryUpgradeSnapshot:
    migration_current: bool
    migration_head: int
    target_migration: int
    component_present: bool
    component_revision: int | None
    mode: MemoryFeatureMode | None
    policy_digest: str | None
    projection_receipt_count: int
    latest_projection_receipt_digest: str | None
    latest_projection_digest: str | None
    legacy_projection_count: int
    required_hook_invalid_count: int
    current_hook_set_digest: str | None
    project_id: UUID | None
    work_item_id: UUID | None
    source_head: str | None
    source_tree_digest: str | None
    database_revision_digest: str | None
    projection_source_digest: str | None
    projection_current: bool
    detected_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.migration_head < 0 or self.target_migration < 1:
            raise ValidationFailed("Memory upgrade migration surumu gecersiz")
        if self.required_hook_invalid_count < 0:
            raise ValidationFailed("Memory required hook sayimi negatif olamaz")
        if self.detected_at.tzinfo is None:
            raise ValidationFailed("Memory upgrade detect zamani timezone-aware olmali")
        if self.component_present != (
            self.component_revision is not None and self.mode is not None
        ):
            raise ValidationFailed("Memory component state eksik veya tutarsiz")
        for value in (
            self.policy_digest,
            self.latest_projection_receipt_digest,
            self.latest_projection_digest,
            self.current_hook_set_digest,
        ):
            if value is not None:
                parse_digest(value)
        if (self.project_id is None) is not (self.work_item_id is None):
            raise ValidationFailed("Memory projection project/work binding birlikte olmali")
        if self.projection_source_digest is not None:
            parse_digest(self.projection_source_digest)
        for value in (self.source_tree_digest, self.database_revision_digest):
            if value is not None:
                parse_digest(value)
        source_fields = (
            self.source_head,
            self.source_tree_digest,
            self.database_revision_digest,
            self.projection_source_digest,
        )
        if any(value is not None for value in source_fields) and not all(
            value is not None for value in source_fields
        ):
            raise ValidationFailed("Projection source HEAD/tree/DB binding eksik")
        if self.projection_current and (
            self.project_id is None or self.projection_source_digest is None
        ):
            raise ValidationFailed("Current projection exact canonical source binding ister")
        if self.required_hook_invalid_count == 0 and self.current_hook_set_digest is None:
            raise ValidationFailed("Exact required hook set current digest ister")
        if self.grants_authority:
            raise PolicyViolation("Memory upgrade snapshot authority uretemez")

    @property
    def snapshot_digest(self) -> str:
        # Observation time is useful evidence but not semantic drift.  Separate
        # CLI invocations must be able to reproduce the same exact plan.
        return digest({key: value for key, value in self.body().items() if key != "detected_at"})

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-upgrade-snapshot/v1",
            "migration_current": self.migration_current,
            "migration_head": self.migration_head,
            "target_migration": self.target_migration,
            "component_present": self.component_present,
            "component_revision": self.component_revision,
            "mode": None if self.mode is None else self.mode.value,
            "policy_digest": self.policy_digest,
            "projection_receipt_count": self.projection_receipt_count,
            "latest_projection_receipt_digest": self.latest_projection_receipt_digest,
            "latest_projection_digest": self.latest_projection_digest,
            "legacy_projection_count": self.legacy_projection_count,
            "required_hook_invalid_count": self.required_hook_invalid_count,
            "current_hook_set_digest": self.current_hook_set_digest,
            "project_id": None if self.project_id is None else str(self.project_id),
            "work_item_id": None if self.work_item_id is None else str(self.work_item_id),
            "source_head": self.source_head,
            "source_tree_digest": self.source_tree_digest,
            "database_revision_digest": self.database_revision_digest,
            "projection_source_digest": self.projection_source_digest,
            "projection_current": self.projection_current,
            "detected_at": self.detected_at,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class MemoryUpgradePlan:
    target: UpgradeTarget
    snapshot_digest: str
    expected_mode: MemoryFeatureMode | None
    expected_revision: int | None
    rollback_ref: str
    rollback_digest: str
    verification_digest: str | None
    verifier_identity_digest: str | None
    package_digest: str | None
    project_id: UUID | None
    work_item_id: UUID | None
    projection_source_digest: str | None
    effect_digest: str
    plan_digest: str
    requires_confirmation: bool = True
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        snapshot: MemoryUpgradeSnapshot,
        target: UpgradeTarget,
        rollback_ref: str,
        rollback_digest: str,
        verification: MemoryVerificationEvidence | None = None,
        package_digest: str | None = None,
    ) -> MemoryUpgradePlan:
        if not rollback_ref or rollback_ref != rollback_ref.strip() or len(rollback_ref) > 512:
            raise ValidationFailed("Memory upgrade portable rollback ref ister")
        if "\\" in rollback_ref or rollback_ref.startswith("/") or ".." in rollback_ref.split("/"):
            raise PolicyViolation("Memory upgrade rollback ref absolute/traversal olamaz")
        verification_digest = None if verification is None else verification.verification_digest
        verifier_identity_digest = (
            None if verification is None else verification.verifier_identity_digest
        )
        for value in (
            rollback_digest,
            verification_digest,
            verifier_identity_digest,
            package_digest,
        ):
            if value is not None:
                parse_digest(value)
        if target in {UpgradeTarget.ENFORCED, UpgradeTarget.STAMPED} and (
            verification is None or not verification.passed
        ):
            raise PolicyViolation("Enforced/stamp plani passing independent verification ister")
        if target is UpgradeTarget.STAMPED and package_digest is None:
            raise PolicyViolation("Component stamp package digest ister")
        if target is UpgradeTarget.SHADOW and (
            snapshot.project_id is None
            or snapshot.work_item_id is None
            or snapshot.projection_source_digest is None
        ):
            raise PolicyViolation("Shadow bootstrap exact canonical project/work source ister")
        if verification is not None and (
            verification.verified_snapshot_digest != snapshot.snapshot_digest
        ):
            raise PolicyViolation("Memory verification farkli snapshot'a bagli")
        effect_digest = digest(
            {
                "effect": "database-write",
                "resource": FEATURE_RESOURCE,
                "target": target.value,
                "snapshot_digest": snapshot.snapshot_digest,
                "verification_digest": verification_digest,
                "verifier_identity_digest": verifier_identity_digest,
                "package_digest": package_digest,
            }
        )
        draft = cls(
            target=target,
            snapshot_digest=snapshot.snapshot_digest,
            expected_mode=snapshot.mode,
            expected_revision=snapshot.component_revision,
            rollback_ref=rollback_ref,
            rollback_digest=rollback_digest,
            verification_digest=verification_digest,
            verifier_identity_digest=verifier_identity_digest,
            package_digest=package_digest,
            project_id=snapshot.project_id,
            work_item_id=snapshot.work_item_id,
            projection_source_digest=snapshot.projection_source_digest,
            effect_digest=effect_digest,
            plan_digest="",
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-upgrade-plan/v1",
            "target": self.target.value,
            "snapshot_digest": self.snapshot_digest,
            "expected_mode": None if self.expected_mode is None else self.expected_mode.value,
            "expected_revision": self.expected_revision,
            "rollback_ref": self.rollback_ref,
            "rollback_digest": self.rollback_digest,
            "verification_digest": self.verification_digest,
            "verifier_identity_digest": self.verifier_identity_digest,
            "package_digest": self.package_digest,
            "project_id": None if self.project_id is None else str(self.project_id),
            "work_item_id": None if self.work_item_id is None else str(self.work_item_id),
            "projection_source_digest": self.projection_source_digest,
            "effect_digest": self.effect_digest,
            "requires_confirmation": True,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        if self.plan_digest != digest(self.body()):
            raise PolicyViolation("Memory upgrade plan digest mismatch")


@dataclass(frozen=True, slots=True)
class MemoryVerificationEvidence:
    verified_snapshot_digest: str
    fresh_database_digest: str
    upgrade_database_digest: str
    hook_digest: str
    security_digest: str
    continuity_digest: str
    projection_digest: str
    full_suite_digest: str
    verifier_model: str
    verifier_execution_identity: str
    builder_model: str
    builder_execution_identity: str
    verified_at: dt.datetime
    passed: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.verified_snapshot_digest,
            self.fresh_database_digest,
            self.upgrade_database_digest,
            self.hook_digest,
            self.security_digest,
            self.continuity_digest,
            self.projection_digest,
            self.full_suite_digest,
        ):
            parse_digest(value)
        identities = (
            self.verifier_model.strip(),
            self.verifier_execution_identity.strip(),
            self.builder_model.strip(),
            self.builder_execution_identity.strip(),
        )
        if not all(identities):
            raise ValidationFailed("Memory verifier/builder identities exact olmali")
        if (
            self.verifier_model == self.builder_model
            or self.verifier_execution_identity == self.builder_execution_identity
        ):
            raise PolicyViolation("Memory verifier builder'dan model ve execution olarak ayrilmali")
        if self.verified_at.tzinfo is None:
            raise ValidationFailed("Memory verification zamani timezone-aware olmali")
        if self.grants_authority:
            raise PolicyViolation("Memory verification authority uretemez")

    @property
    def verification_digest(self) -> str:
        return digest(self.body())

    @property
    def verifier_identity_digest(self) -> str:
        return digest(
            {
                "verifier_model": self.verifier_model,
                "verifier_execution_identity": self.verifier_execution_identity,
                "builder_model": self.builder_model,
                "builder_execution_identity": self.builder_execution_identity,
            }
        )

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-upgrade-verification/v1",
            "verified_snapshot_digest": self.verified_snapshot_digest,
            "fresh_database_digest": self.fresh_database_digest,
            "upgrade_database_digest": self.upgrade_database_digest,
            "hook_digest": self.hook_digest,
            "security_digest": self.security_digest,
            "continuity_digest": self.continuity_digest,
            "projection_digest": self.projection_digest,
            "full_suite_digest": self.full_suite_digest,
            "verifier_model": self.verifier_model,
            "verifier_execution_identity": self.verifier_execution_identity,
            "builder_model": self.builder_model,
            "builder_execution_identity": self.builder_execution_identity,
            "verified_at": self.verified_at,
            "passed": self.passed,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class MemoryUpgradeReceipt:
    target: UpgradeTarget
    component_revision: int
    mode: MemoryFeatureMode
    policy_digest: str
    authorization_id: UUID | None
    created: bool
    hook_set_digest: str
    projection_receipt_digest: str
    receipt_digest: str
    grants_authority: bool = False


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


class MemoryUpgradeStore(Protocol):
    connection: Any
    realm_id: UUID

    def detect(self, *, now: dt.datetime | None = None) -> MemoryUpgradeSnapshot: ...

    def append_feature_state(
        self,
        plan: MemoryUpgradePlan,
        *,
        authorization_id: UUID,
        created_at: dt.datetime,
    ) -> MemoryUpgradeReceipt: ...


@dataclass(frozen=True, slots=True)
class MemoryUpgradeService:
    repository: MemoryUpgradeStore
    authorizations: AuthorizationStore

    def detect(self, *, now: dt.datetime | None = None) -> MemoryUpgradeSnapshot:
        return self.repository.detect(now=now)

    def check_plan(
        self,
        *,
        target: UpgradeTarget,
        rollback_ref: str,
        rollback_digest: str,
        verification: MemoryVerificationEvidence | None = None,
        package_digest: str | None = None,
        now: dt.datetime | None = None,
    ) -> MemoryUpgradePlan:
        snapshot = self.detect(now=now)
        if not snapshot.migration_current or not snapshot.component_present:
            raise PolicyViolation("Memory migration/component current degil; once migration kapisi")
        if target is UpgradeTarget.SHADOW and snapshot.mode not in {
            MemoryFeatureMode.DISABLED,
            MemoryFeatureMode.SHADOW,
        }:
            raise PolicyViolation("Enforced component sessizce shadow'a dusurulemez")
        if target is UpgradeTarget.ENFORCED and snapshot.mode not in {
            MemoryFeatureMode.SHADOW,
            MemoryFeatureMode.ENFORCED,
        }:
            raise PolicyViolation("Memory enforce transition shadow state ister")
        if target is UpgradeTarget.STAMPED and snapshot.mode is not MemoryFeatureMode.ENFORCED:
            raise PolicyViolation("Memory component stamp enforced state ister")
        if target in {UpgradeTarget.ENFORCED, UpgradeTarget.STAMPED} and (
            snapshot.required_hook_invalid_count != 0 or not snapshot.projection_current
        ):
            raise PolicyViolation("Memory enforce/stamp exact hook set ve current projection ister")
        return MemoryUpgradePlan.create(
            snapshot=snapshot,
            target=target,
            rollback_ref=rollback_ref,
            rollback_digest=rollback_digest,
            verification=verification,
            package_digest=package_digest,
        )

    def verify(
        self,
        evidence: MemoryVerificationEvidence,
        *,
        expected_snapshot_digest: str,
        now: dt.datetime | None = None,
    ) -> str:
        parse_digest(expected_snapshot_digest)
        if not evidence.passed:
            raise PolicyViolation("Memory verification tum kapilarda passed olmali")
        if evidence.verified_snapshot_digest != expected_snapshot_digest:
            raise PolicyViolation("Memory verification evidence snapshot binding mismatch")
        current = self.detect(now=now)
        if current.snapshot_digest != expected_snapshot_digest:
            raise PolicyViolation(
                "Memory verification source/migration/policy drift; replan required"
            )
        return evidence.verification_digest

    def apply(
        self,
        plan: MemoryUpgradePlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> MemoryUpgradeReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        plan.assert_integrity()
        current = self.detect(now=moment)
        if current.snapshot_digest != plan.snapshot_digest:
            raise PolicyViolation("Memory upgrade apply drift; replan required")
        if (
            plan.target is UpgradeTarget.SHADOW
            and current.mode is MemoryFeatureMode.SHADOW
            and current.required_hook_invalid_count == 0
            and current.projection_current
        ):
            return MemoryUpgradeReceipt(
                UpgradeTarget.SHADOW,
                current.component_revision or 0,
                MemoryFeatureMode.SHADOW,
                current.policy_digest or digest({"component": COMPONENT, "state": "shadow"}),
                None,
                False,
                current.current_hook_set_digest
                or digest({"component": COMPONENT, "hooks": "current"}),
                current.latest_projection_receipt_digest
                or digest({"component": COMPONENT, "projection": "current"}),
                digest({"plan_digest": plan.plan_digest, "idempotent": True}),
            )
        authorization = self.authorizations.get(authorization_id)
        rejection = authorization.rejection_reason(moment)
        if (
            rejection is not None
            or authorization.realm_id != self.repository.realm_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or not authorization.scope.covers_effect("database-write")
            or not authorization.scope.covers_resource(FEATURE_RESOURCE)
        ):
            raise AuthorizationRequired(
                f"Memory upgrade exact authorization binding yok: {rejection or 'scope-mismatch'}"
            )
        with self.repository.connection.transaction():
            consumed = self.authorizations.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by=f"memory-upgrade/{plan.target.value}",
                now=moment,
            )
            if not bool(getattr(consumed, "consumed", False)):
                reason = getattr(consumed, "reason", "unknown")
                raise AuthorizationRequired(f"Memory upgrade authorization tuketilemedi: {reason}")
            return self.repository.append_feature_state(
                plan,
                authorization_id=authorization_id,
                created_at=moment,
            )
