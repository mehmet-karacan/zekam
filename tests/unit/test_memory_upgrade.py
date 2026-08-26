from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.memory_upgrade import (
    FEATURE_RESOURCE,
    MemoryFeatureMode,
    MemoryUpgradePlan,
    MemoryUpgradeReceipt,
    MemoryUpgradeService,
    MemoryUpgradeSnapshot,
    MemoryVerificationEvidence,
    UpgradeTarget,
    canonical_projection_source_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.security import Authorization, AuthorizationScope

NOW = dt.datetime(2026, 8, 26, 10, tzinfo=dt.UTC)


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _Connection:
    def transaction(self) -> _Transaction:
        return _Transaction()


@dataclass
class _Repository:
    snapshot: MemoryUpgradeSnapshot
    realm_id: UUID
    connection: Any = field(default_factory=_Connection)
    writes: int = 0

    def detect(self, *, now: dt.datetime | None = None) -> MemoryUpgradeSnapshot:
        del now
        return self.snapshot

    def append_feature_state(
        self,
        plan: MemoryUpgradePlan,
        *,
        authorization_id: UUID,
        created_at: dt.datetime,
    ) -> MemoryUpgradeReceipt:
        del created_at
        self.writes += 1
        return MemoryUpgradeReceipt(
            plan.target,
            self.snapshot.component_revision or 0,
            self.snapshot.mode or MemoryFeatureMode.SHADOW,
            self.snapshot.policy_digest or digest("policy"),
            authorization_id,
            True,
            digest("hooks"),
            digest("projection-receipt"),
            digest("upgrade-receipt"),
        )


@dataclass
class _Consumed:
    consumed: bool = True
    reason: str | None = None


@dataclass
class _Authorizations:
    authorization: Authorization | None = None
    consumed_by: str | None = None

    def get(self, authorization_id: UUID) -> Authorization:
        if self.authorization is None or self.authorization.id != authorization_id:
            raise AuthorizationRequired("authorization missing")
        return self.authorization

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> _Consumed:
        del authorization_id, effect_digest, now
        self.consumed_by = consumed_by
        return _Consumed()


def _snapshot(
    *,
    hook_invalid: int = 0,
    projection_current: bool = True,
    detected_at: dt.datetime = NOW,
) -> MemoryUpgradeSnapshot:
    return MemoryUpgradeSnapshot(
        migration_current=True,
        migration_head=55,
        target_migration=55,
        component_present=True,
        component_revision=1,
        mode=MemoryFeatureMode.SHADOW,
        policy_digest=digest("policy"),
        projection_receipt_count=int(projection_current),
        latest_projection_receipt_digest=(
            digest("projection-receipt") if projection_current else None
        ),
        latest_projection_digest=digest("projection") if projection_current else None,
        legacy_projection_count=4,
        required_hook_invalid_count=hook_invalid,
        current_hook_set_digest=digest("hooks") if hook_invalid == 0 else None,
        project_id=uuid4(),
        work_item_id=uuid4(),
        source_head="git/revision-one",
        source_tree_digest=digest("source-tree"),
        database_revision_digest=digest("database-revision"),
        projection_source_digest=digest("work-source"),
        projection_current=projection_current,
        detected_at=detected_at,
    )


def _verification(snapshot: MemoryUpgradeSnapshot) -> MemoryVerificationEvidence:
    return MemoryVerificationEvidence(
        verified_snapshot_digest=snapshot.snapshot_digest,
        fresh_database_digest=digest("fresh-db"),
        upgrade_database_digest=digest("upgrade-db"),
        hook_digest=digest("hooks"),
        security_digest=digest("security"),
        continuity_digest=digest("continuity"),
        projection_digest=digest("projection"),
        full_suite_digest=digest("full-suite"),
        verifier_model="verifier-model",
        verifier_execution_identity="verifier-execution",
        builder_model="builder-model",
        builder_execution_identity="builder-execution",
        verified_at=NOW,
        passed=True,
    )


def test_snapshot_digest_is_reproducible_across_detection_times() -> None:
    first = _snapshot()
    second = replace(first, detected_at=NOW + dt.timedelta(minutes=1))

    assert first.snapshot_digest == second.snapshot_digest


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    (
        ("source_head", "git/revision-two"),
        ("source_tree_digest", digest("changed-tree")),
        ("migration_head", 56),
        ("database_revision_digest", digest("changed-db")),
    ),
)
def test_projection_source_digest_detects_every_canonical_drift(
    field_name: str, changed_value: Any
) -> None:
    values: dict[str, Any] = {
        "source_head": "git/revision-one",
        "source_tree_digest": digest("source-tree"),
        "migration_head": 55,
        "database_revision_digest": digest("database-revision"),
    }
    baseline = canonical_projection_source_digest(**values)
    values[field_name] = changed_value

    assert canonical_projection_source_digest(**values) != baseline


def test_shadow_noop_requires_hooks_and_current_projection() -> None:
    snapshot = _snapshot()
    repository = _Repository(snapshot, uuid4())
    service = MemoryUpgradeService(repository, _Authorizations())
    plan = service.check_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory-shadow",
        rollback_digest=digest("rollback"),
        now=NOW,
    )

    receipt = service.apply(plan, authorization_id=UUID(int=0), now=NOW)

    assert not receipt.created
    assert repository.writes == 0


@pytest.mark.parametrize(
    ("hook_invalid", "projection_current"),
    ((1, True), (0, False)),
)
def test_incomplete_shadow_bootstrap_requires_exact_authorization(
    hook_invalid: int, projection_current: bool
) -> None:
    snapshot = _snapshot(hook_invalid=hook_invalid, projection_current=projection_current)
    service = MemoryUpgradeService(_Repository(snapshot, uuid4()), _Authorizations())
    plan = service.check_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory-shadow",
        rollback_digest=digest("rollback"),
        now=NOW,
    )

    with pytest.raises(AuthorizationRequired, match="missing"):
        service.apply(plan, authorization_id=uuid4(), now=NOW)


def test_enforcement_binds_independent_verification_to_exact_snapshot() -> None:
    snapshot = _snapshot()
    repository = _Repository(snapshot, uuid4())
    authorizations = _Authorizations()
    service = MemoryUpgradeService(repository, authorizations)
    evidence = _verification(snapshot)
    plan = service.check_plan(
        target=UpgradeTarget.ENFORCED,
        rollback_ref="rollback/memory-enforce",
        rollback_digest=digest("rollback"),
        verification=evidence,
        now=NOW,
    )
    authorization = Authorization.issue(
        realm_id=repository.realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(FEATURE_RESOURCE,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.authorization = authorization

    receipt = service.apply(plan, authorization_id=authorization.id, now=NOW)

    assert receipt.created
    assert repository.writes == 1
    assert authorizations.consumed_by == "memory-upgrade/enforced"


def test_verification_rejects_builder_identity_or_snapshot_drift() -> None:
    snapshot = _snapshot()
    with pytest.raises(PolicyViolation, match="model ve execution"):
        replace(
            _verification(snapshot),
            verifier_model="builder-model",
        )

    drifted = _snapshot(detected_at=NOW + dt.timedelta(minutes=1))
    wrong = replace(
        _verification(snapshot),
        verified_snapshot_digest=digest("other-snapshot"),
    )
    service = MemoryUpgradeService(_Repository(drifted, uuid4()), _Authorizations())
    with pytest.raises(PolicyViolation, match="farkli snapshot"):
        service.check_plan(
            target=UpgradeTarget.ENFORCED,
            rollback_ref="rollback/memory-enforce",
            rollback_digest=digest("rollback"),
            verification=wrong,
            now=NOW,
        )
