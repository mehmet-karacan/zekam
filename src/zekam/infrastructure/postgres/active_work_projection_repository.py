"""PostgreSQL reader for the deterministic root active-work projection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.active_work_projection import ActiveWorkProjection
from zekam.application.continuity_projection import ACTIVE_WORK_PROJECTION_REF
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.domain.errors import NotFound, PolicyViolation


def _file_digest(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise NotFound(f"Legacy evidence file cannot be read: {path.name}") from exc


@dataclass(frozen=True, slots=True)
class ActiveWorkProjectionRepository:
    connection: Any
    realm_id: UUID
    core_path: Path

    def load(self, *, project_id: UUID, work_item_id: UUID) -> ActiveWorkProjection:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("set transaction isolation level repeatable read read only")
            cursor.execute(
                "select slug from projects.project where realm_id=%s and id=%s",
                (self.realm_id, project_id),
            )
            project = cursor.fetchone()
            cursor.execute(
                "select title,summary,state,revision,record_digest,acceptance_criteria"
                " from work.work_item where realm_id=%s and project_id=%s and id=%s",
                (self.realm_id, project_id, work_item_id),
            )
            work = cursor.fetchone()
            cursor.execute(
                "select id,revision,source_revision,steps,effect_digest,plan_digest"
                " from work.task_plan where realm_id=%s and project_id=%s and work_item_id=%s"
                " order by revision desc,id desc limit 1",
                (self.realm_id, project_id, work_item_id),
            )
            plan = cursor.fetchone()
            cursor.execute(
                "select id,state,run_digest from runtime.execution_run"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " order by created_at desc,id desc limit 1",
                (self.realm_id, project_id, work_item_id),
            )
            run = cursor.fetchone()
            cursor.execute(
                "select revision.id,revision.revision,revision.tree_digest,revision.branch,"
                " revision.is_dirty,revision.file_count from projects.source_revision revision"
                " join projects.source_binding binding on binding.realm_id=revision.realm_id"
                " and binding.id=revision.binding_id where binding.realm_id=%s"
                " and binding.project_id=%s order by revision.observed_at desc,revision.id desc"
                " limit 1",
                (self.realm_id, project_id),
            )
            source = cursor.fetchone()
            cursor.execute("select coalesce(max(version),0) from core.schema_migrations")
            migration_head = int(cursor.fetchone()[0])
            cursor.execute(
                "select state from continuity.feature_policy_state where realm_id=%s"
                " and component='memory-continuity-plane' and is_current",
                (self.realm_id,),
            )
            feature = cursor.fetchone()
            cursor.execute(
                "select hook_set_digest from hooks.current_generation where realm_id=%s",
                (self.realm_id,),
            )
            hook = cursor.fetchone()
            cursor.execute(
                "select receipt_digest,source_digest from continuity.projection_generation_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and projection_ref=%s"
                " order by generated_at desc,id desc limit 1",
                (self.realm_id, project_id, work_item_id, ACTIVE_WORK_PROJECTION_REF),
            )
            projection = cursor.fetchone()
            cursor.execute(
                "select count(*) filter(where state='blocked'),"
                " count(*) filter(where state in ('ready','running')),"
                " count(*) filter(where state='recovery-required') from runtime.job"
                " where realm_id=%s and work_item_id=%s",
                (self.realm_id, work_item_id),
            )
            queues = cursor.fetchone()
            cursor.execute(
                "select count(*) from runtime.claim_without_receipt where realm_id=%s"
                " and job_id in (select id from runtime.job where realm_id=%s and work_item_id=%s)",
                (self.realm_id, self.realm_id, work_item_id),
            )
            pending_claims = int(cursor.fetchone()[0])
        required = (project, work, plan, run, source, feature, hook, projection)
        if any(value is None for value in required):
            raise NotFound("Canonical active-work projection source is incomplete")
        assert project is not None and work is not None and plan is not None
        assert run is not None and source is not None and feature is not None
        assert hook is not None and projection is not None
        expected_source = canonical_projection_source_digest(
            source_head=str(source[1]),
            source_tree_digest=str(source[2]),
            migration_head=migration_head,
            database_revision_digest=self._database_revision_digest(project_id, work_item_id),
        )
        if str(projection[1]) != expected_source:
            raise PolicyViolation("Root projection source receipt is stale")
        return ActiveWorkProjection(
            project_id=project_id,
            project_slug=str(project[0]),
            work_id=work_item_id,
            title=str(work[0]),
            summary=str(work[1]),
            state=str(work[2]),
            work_revision=int(work[3]),
            work_record_digest=str(work[4]),
            acceptance_criteria=tuple(dict(item) for item in (work[5] or [])),
            plan_id=UUID(str(plan[0])),
            plan_revision=int(plan[1]),
            source_revision=str(plan[2]),
            plan_steps=tuple(dict(item) for item in (plan[3] or [])),
            plan_effect_digest=str(plan[4]),
            plan_digest=str(plan[5]),
            run_id=UUID(str(run[0])),
            run_state=str(run[1]),
            run_digest=str(run[2]),
            source_observation_id=UUID(str(source[0])),
            source_head=str(source[1]),
            source_tree_digest=str(source[2]),
            source_branch=None if source[3] is None else str(source[3]),
            source_dirty=bool(source[4]),
            source_file_count=int(source[5]),
            migration_head=migration_head,
            memory_mode=str(feature[0]),
            hook_set_digest=str(hook[0]),
            projection_receipt_digest=str(projection[0]),
            projection_source_digest=str(projection[1]),
            queue_blocked=int(queues[0]),
            queue_pending=int(queues[1]),
            queue_recovery=int(queues[2]),
            claim_without_receipt=pending_claims,
            global_dod_digest=_file_digest(self.core_path / "GLOBAL_DOD_DURUM.md"),
            release_report_digest=_file_digest(self.core_path / "SURUM_RAPORU.md"),
        )

    def _database_revision_digest(self, project_id: UUID, work_item_id: UUID) -> str:
        from zekam.infrastructure.postgres.memory_upgrade_repository import (
            PostgresMemoryUpgradeRepository,
        )

        snapshot = PostgresMemoryUpgradeRepository(
            self.connection,
            self.realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
        ).detect()
        if snapshot.database_revision_digest is None:
            raise NotFound("Canonical database revision digest is missing")
        return snapshot.database_revision_digest
