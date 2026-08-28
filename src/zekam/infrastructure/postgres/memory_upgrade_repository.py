"""PostgreSQL adapter for the Memory Continuity feature-state protocol."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.continuity_projection import ACTIVE_WORK_PROJECTION_REF
from zekam.application.memory_hooks import MEMORY_HOOK_EVENTS
from zekam.application.memory_upgrade import (
    COMPONENT,
    MemoryFeatureMode,
    MemoryUpgradePlan,
    MemoryUpgradeReceipt,
    MemoryUpgradeSnapshot,
    UpgradeTarget,
    canonical_projection_source_digest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.session_continuity import ProjectionGenerationReceipt
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.migrations import (
    detect_drift,
    discover_migrations,
    read_applied,
)

PROJECTION_GENERATOR = "memory-continuity-shadow/v1"


@dataclass(frozen=True, slots=True)
class PostgresMemoryUpgradeRepository:
    connection: Any
    realm_id: UUID
    legacy_projection_count: int = 0
    project_id: UUID | None = None
    work_item_id: UUID | None = None

    def detect(self, *, now: dt.datetime | None = None) -> MemoryUpgradeSnapshot:
        """Read exact migration/component/projection state without mutation."""

        moment = now or dt.datetime.now(dt.UTC)
        available = discover_migrations()
        applied = read_applied(self.connection)
        drift = detect_drift(applied, available)
        applied_versions = {item.version for item in applied}
        migration_head = max(applied_versions, default=0)
        target_migration = max((item.version for item in available), default=0)
        migration_current = not drift and applied_versions == {item.version for item in available}
        component_present = False
        component_revision: int | None = None
        mode: MemoryFeatureMode | None = None
        policy_digest: str | None = None
        projection_count = 0
        latest_projection_receipt_digest: str | None = None
        latest_projection_digest: str | None = None
        required_hook_invalid_count = len(MEMORY_HOOK_EVENTS)
        current_hook_set_digest: str | None = None
        source_head: str | None = None
        source_tree_digest: str | None = None
        database_revision_digest: str | None = None
        projection_source_digest: str | None = None
        projection_current = False
        with self.connection.cursor() as cursor:
            cursor.execute("select to_regclass('continuity.feature_policy_state')")
            if cursor.fetchone()[0] is not None:
                cursor.execute(
                    "select revision,state,policy_digest from continuity.feature_policy_state"
                    " where realm_id=%s and component=%s and is_current",
                    (self.realm_id, COMPONENT),
                )
                row = cursor.fetchone()
                if row is not None:
                    component_present = True
                    component_revision = int(row[0])
                    mode = MemoryFeatureMode(str(row[1]))
                    policy_digest = str(row[2])
                cursor.execute(
                    "select count(*),"
                    " (array_agg(receipt_digest order by generated_at desc,id desc))[1],"
                    " (array_agg(projection_digest order by generated_at desc,id desc))[1]"
                    " from continuity.projection_generation_receipt"
                    " where realm_id=%s and projection_ref=%s",
                    (self.realm_id, ACTIVE_WORK_PROJECTION_REF),
                )
                projection = cursor.fetchone()
                projection_count = int(projection[0] or 0)
                latest_projection_receipt_digest = (
                    None if projection[1] is None else str(projection[1])
                )
                latest_projection_digest = None if projection[2] is None else str(projection[2])
            required_hook_invalid_count = self._required_hook_invalid_count(cursor)
            cursor.execute(
                "select hook_set_digest from hooks.current_generation where realm_id=%s",
                (self.realm_id,),
            )
            current_hook = cursor.fetchone()
            current_hook_set_digest = None if current_hook is None else str(current_hook[0])
            if self.project_id is not None and self.work_item_id is not None:
                source = self._canonical_projection_source(
                    cursor, self.project_id, self.work_item_id
                )
                if source is not None:
                    (
                        _,
                        _,
                        source_head,
                        source_tree_digest,
                        database_revision_digest,
                        projection_source_digest,
                    ) = source
                cursor.execute(
                    "select receipt_digest,projection_digest"
                    " from continuity.projection_generation_receipt"
                    " where realm_id=%s and project_id=%s and work_item_id=%s"
                    " and projection_ref=%s and source_digest=%s"
                    " order by generated_at desc,id desc limit 1",
                    (
                        self.realm_id,
                        self.project_id,
                        self.work_item_id,
                        ACTIVE_WORK_PROJECTION_REF,
                        projection_source_digest,
                    ),
                )
                current_projection = cursor.fetchone()
                projection_current = current_projection is not None
                if current_projection is not None:
                    latest_projection_receipt_digest = str(current_projection[0])
                    latest_projection_digest = str(current_projection[1])
        return MemoryUpgradeSnapshot(
            migration_current=migration_current,
            migration_head=migration_head,
            target_migration=target_migration,
            component_present=component_present,
            component_revision=component_revision,
            mode=mode,
            policy_digest=policy_digest,
            projection_receipt_count=projection_count,
            latest_projection_receipt_digest=latest_projection_receipt_digest,
            latest_projection_digest=latest_projection_digest,
            legacy_projection_count=self.legacy_projection_count,
            required_hook_invalid_count=required_hook_invalid_count,
            current_hook_set_digest=current_hook_set_digest,
            project_id=self.project_id,
            work_item_id=self.work_item_id,
            source_head=source_head,
            source_tree_digest=source_tree_digest,
            database_revision_digest=database_revision_digest,
            projection_source_digest=projection_source_digest,
            projection_current=projection_current,
            detected_at=moment,
        )

    def _required_hook_invalid_count(self, cursor: Any) -> int:
        cursor.execute(
            "select spec.event_type,count(*)"
            " from hooks.current_generation current_set"
            " join hooks.compiled_set_entry entry on entry.realm_id=current_set.realm_id"
            " and entry.compiled_set_id=current_set.compiled_set_id"
            " join hooks.spec_revision spec on spec.realm_id=entry.realm_id"
            " and spec.id=entry.spec_revision_id"
            " where current_set.realm_id=%s and spec.required"
            " and entry.runtime_revision_id is not null and entry.disabled_reason is null"
            " and spec.event_type=any(%s) group by spec.event_type",
            (self.realm_id, [item.value for item in MEMORY_HOOK_EVENTS]),
        )
        counts = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
        return sum(counts.get(item.value, 0) != 1 for item in MEMORY_HOOK_EVENTS)

    def _canonical_projection_source(
        self,
        cursor: Any,
        project_id: UUID,
        work_item_id: UUID,
        *,
        lock: bool = False,
    ) -> tuple[int, str, str, str, str, str] | None:
        cursor.execute(
            "select revision,state,record_digest from work.work_item"
            " where realm_id=%s and project_id=%s and id=%s" + (" for share" if lock else ""),
            (self.realm_id, project_id, work_item_id),
        )
        work = cursor.fetchone()
        if work is None:
            raise NotFound("Memory projection canonical project/work binding bulunamadi")
        cursor.execute(
            "select revision,tree_digest from projects.source_revision revision"
            " join projects.source_binding binding on binding.realm_id=revision.realm_id"
            " and binding.id=revision.binding_id"
            " where binding.realm_id=%s and binding.project_id=%s"
            " order by revision.observed_at desc,revision.id desc limit 1",
            (self.realm_id, project_id),
        )
        source = cursor.fetchone()
        if source is None:
            return None
        cursor.execute("select coalesce(max(version),0) from core.schema_migrations")
        migration_head = int(cursor.fetchone()[0])
        work_revision = int(work[0])
        work_state = str(work[1])
        work_record_digest = str(work[2])
        source_head = str(source[0])
        source_tree_digest = str(source[1])
        database_revision_digest = digest(
            {
                "project_id": str(project_id),
                "work_item_id": str(work_item_id),
                "work_revision": work_revision,
                "work_state": work_state,
                "work_record_digest": work_record_digest,
            }
        )
        projection_source_digest = canonical_projection_source_digest(
            source_head=source_head,
            source_tree_digest=source_tree_digest,
            migration_head=migration_head,
            database_revision_digest=database_revision_digest,
        )
        return (
            work_revision,
            work_state,
            source_head,
            source_tree_digest,
            database_revision_digest,
            projection_source_digest,
        )

    def append_feature_state(
        self,
        plan: MemoryUpgradePlan,
        *,
        authorization_id: UUID,
        created_at: dt.datetime,
    ) -> MemoryUpgradeReceipt:
        """Append one exact state revision; the caller owns the transaction."""

        plan.assert_integrity()
        if plan.target is UpgradeTarget.SHADOW and (
            plan.project_id is None
            or plan.work_item_id is None
            or plan.projection_source_digest is None
        ):
            raise PolicyViolation("Shadow bootstrap exact projection binding ister")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,revision,state,policy_digest,policy_body"
                " from continuity.feature_policy_state"
                " where realm_id=%s and component=%s and is_current for update",
                (self.realm_id, COMPONENT),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Memory feature policy current state bulunamadi")
            predecessor_id = UUID(str(row[0]))
            revision = int(row[1])
            current_mode = MemoryFeatureMode(str(row[2]))
            if revision != plan.expected_revision or current_mode is not plan.expected_mode:
                raise ConcurrencyConflict("Memory feature policy revision/state drift")
            if plan.target is UpgradeTarget.SHADOW:
                if current_mode not in {
                    MemoryFeatureMode.DISABLED,
                    MemoryFeatureMode.SHADOW,
                }:
                    raise PolicyViolation("Memory shadow bootstrap enforced state'e yazamaz")
                target_mode = MemoryFeatureMode.SHADOW
            else:
                if (
                    current_mode is not MemoryFeatureMode.SHADOW
                    and plan.target is UpgradeTarget.ENFORCED
                ):
                    raise PolicyViolation("Memory enforce transition shadow state ister")
                if (
                    current_mode is not MemoryFeatureMode.ENFORCED
                    and plan.target is UpgradeTarget.STAMPED
                ):
                    raise PolicyViolation("Memory component stamp enforced state ister")
                target_mode = MemoryFeatureMode.ENFORCED
            if plan.target is UpgradeTarget.SHADOW:
                hook_receipt = PostgresMemoryHookInstaller(self.connection, self.realm_id).ensure(
                    installed_at=created_at
                )
                hook_created = hook_receipt.created
                hook_set_digest = hook_receipt.hook_set_digest
                projection_created, projection_receipt_digest, projection_digest = (
                    self._ensure_projection(plan, created_at=created_at)
                )
            else:
                hook_created = False
                hook_set_digest = self._current_hook_set_digest(cursor)
                projection_created = False
                projection_receipt_digest, projection_digest = self._current_projection(
                    cursor, plan
                )
            if plan.target is UpgradeTarget.SHADOW and current_mode is MemoryFeatureMode.SHADOW:
                receipt_digest = digest(
                    {
                        "schema": "zekam-memory-upgrade-receipt/v1",
                        "target": plan.target.value,
                        "component_revision": revision,
                        "mode": current_mode.value,
                        "policy_digest": str(row[3]),
                        "authorization_id": str(authorization_id),
                        "hook_set_digest": hook_set_digest,
                        "projection_receipt_digest": projection_receipt_digest,
                        "projection_digest": projection_digest,
                        "created": hook_created or projection_created,
                        "grants_authority": False,
                    }
                )
                return MemoryUpgradeReceipt(
                    target=plan.target,
                    component_revision=revision,
                    mode=current_mode,
                    policy_digest=str(row[3]),
                    authorization_id=authorization_id,
                    created=hook_created or projection_created,
                    hook_set_digest=hook_set_digest,
                    projection_receipt_digest=projection_receipt_digest,
                    receipt_digest=receipt_digest,
                )
            next_revision = revision + 1
            body: dict[str, Any] = {
                "schema": "zekam-memory-continuity-feature-policy/v1",
                "component": COMPONENT,
                "state": target_mode.value,
                "version": next_revision,
                "transition": plan.target.value,
                "plan_digest": plan.plan_digest,
                "rollback_digest": plan.rollback_digest,
                "verification_digest": plan.verification_digest,
                "verifier_identity_digest": plan.verifier_identity_digest,
                "component_success": plan.target is UpgradeTarget.STAMPED,
                "package_digest": plan.package_digest,
                "hook_set_digest": hook_set_digest,
                "projection_receipt_digest": projection_receipt_digest,
                "grants_authority": False,
            }
            policy_digest = digest(body)
            state_id = new_uuid7(now=created_at)
            cursor.execute(
                "insert into continuity.feature_policy_state"
                " (id,realm_id,component,revision,state,policy_body,policy_digest,predecessor_id,"
                " is_current,verification_digest,authorization_id,created_at,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,true,%s,%s,%s,false)",
                (
                    state_id,
                    self.realm_id,
                    COMPONENT,
                    next_revision,
                    target_mode.value,
                    canonical_json(body),
                    policy_digest,
                    predecessor_id,
                    plan.verification_digest,
                    authorization_id,
                    created_at,
                ),
            )
        receipt_digest = digest(
            {
                "schema": "zekam-memory-upgrade-receipt/v1",
                "target": plan.target.value,
                "component_revision": next_revision,
                "mode": target_mode.value,
                "policy_digest": policy_digest,
                "authorization_id": str(authorization_id),
                "hook_set_digest": hook_set_digest,
                "projection_receipt_digest": projection_receipt_digest,
                "projection_digest": projection_digest,
                "created": True,
                "grants_authority": False,
            }
        )
        return MemoryUpgradeReceipt(
            target=plan.target,
            component_revision=next_revision,
            mode=target_mode,
            policy_digest=policy_digest,
            authorization_id=authorization_id,
            created=True,
            hook_set_digest=hook_set_digest,
            projection_receipt_digest=projection_receipt_digest,
            receipt_digest=receipt_digest,
        )

    def _ensure_projection(
        self, plan: MemoryUpgradePlan, *, created_at: dt.datetime
    ) -> tuple[bool, str, str]:
        project_id = plan.project_id
        work_item_id = plan.work_item_id
        source_digest = plan.projection_source_digest
        if project_id is None or work_item_id is None or source_digest is None:
            raise PolicyViolation("Projection receipt exact canonical source binding ister")
        with self.connection.cursor() as cursor:
            canonical_source = self._canonical_projection_source(
                cursor, project_id, work_item_id, lock=True
            )
            if canonical_source is None:
                raise NotFound("Projection receipt canonical source revision bulunamadi")
            (
                work_revision,
                work_state,
                source_head,
                source_tree_digest,
                database_revision_digest,
                current_source_digest,
            ) = canonical_source
            if current_source_digest != source_digest:
                raise ConcurrencyConflict("Projection receipt canonical source digest drift")
            projection_body = {
                "schema": "zekam-memory-continuity-public-projection/v1",
                "project_id": str(project_id),
                "work_item_id": str(work_item_id),
                "work_revision": work_revision,
                "work_state": work_state,
                "source_head": source_head,
                "source_tree_digest": source_tree_digest,
                "migration_head": self._migration_head(cursor),
                "database_revision_digest": database_revision_digest,
                "source_digest": source_digest,
                "classification": "public",
                "public_filtered": True,
                "content_included": False,
                "fresh": True,
                "read_only": True,
                "grants_authority": False,
            }
            projection_digest = digest(projection_body)
            cursor.execute(
                "select receipt_digest,projection_digest"
                " from continuity.projection_generation_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and projection_ref=%s and source_digest=%s",
                (
                    self.realm_id,
                    project_id,
                    work_item_id,
                    ACTIVE_WORK_PROJECTION_REF,
                    source_digest,
                ),
            )
            replay = cursor.fetchone()
        if replay is not None:
            if str(replay[1]) != projection_digest:
                raise ConcurrencyConflict("Projection receipt deterministic replay drift")
            return False, str(replay[0]), projection_digest
        receipt = ProjectionGenerationReceipt(
            receipt_id=new_uuid7(now=created_at),
            realm_id=self.realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            source_ref=f"work-item/{work_item_id}/revision/{work_revision}",
            source_digest=source_digest,
            projection_ref=ACTIVE_WORK_PROJECTION_REF,
            projection_digest=projection_digest,
            generator_version=PROJECTION_GENERATOR,
            generated_at=created_at,
        )
        idempotency_key = f"memory-shadow-projection/{work_item_id}/{source_digest[7:]}"
        created = MemoryContinuityRepository(
            self.connection, self.realm_id
        ).store_projection_receipt(receipt, idempotency_key=idempotency_key)
        return created, receipt.receipt_digest, projection_digest

    @staticmethod
    def _migration_head(cursor: Any) -> int:
        cursor.execute("select coalesce(max(version),0) from core.schema_migrations")
        return int(cursor.fetchone()[0])

    def _current_hook_set_digest(self, cursor: Any) -> str:
        if self._required_hook_invalid_count(cursor) != 0:
            raise PolicyViolation("Enforce/stamp current exact-one hook set ister")
        cursor.execute(
            "select hook_set_digest from hooks.current_generation where realm_id=%s",
            (self.realm_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Current hook generation bulunamadi")
        return str(row[0])

    def _current_projection(self, cursor: Any, plan: MemoryUpgradePlan) -> tuple[str, str]:
        if (
            plan.project_id is None
            or plan.work_item_id is None
            or plan.projection_source_digest is None
        ):
            raise PolicyViolation("Enforce/stamp exact current projection binding ister")
        cursor.execute(
            "select receipt_digest,projection_digest"
            " from continuity.projection_generation_receipt"
            " where realm_id=%s and project_id=%s and work_item_id=%s"
            " and projection_ref=%s and source_digest=%s"
            " order by generated_at desc,id desc limit 1",
            (
                self.realm_id,
                plan.project_id,
                plan.work_item_id,
                ACTIVE_WORK_PROJECTION_REF,
                plan.projection_source_digest,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Enforce/stamp current projection receipt bulunamadi")
        return str(row[0]), str(row[1])
