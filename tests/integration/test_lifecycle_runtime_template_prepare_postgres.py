"""Provider-free lifecycle template preparation against PostgreSQL."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest

import zekam.application.lifecycle_runtime_template_prepare as template_prepare_module
from zekam.application.governance import GovernanceService
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleRuntimeTemplatePrepareService,
    run_lifecycle_template_prepare_once,
)
from zekam.application.lifecycle_template_recovery import LifecycleTemplateRecoveryService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.tool_registry import CompiledToolSet
from zekam.domain.work import AcceptanceCriterion, WorkType
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
    LifecycleRuntimeTemplateRepository,
)
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_prepare_materializes_exact_current_template_without_provider_call(
    realm_session: tuple[Any, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    realm, connection = realm_session
    GovernanceService(connection, realm).ensure_default_policy()
    source = tmp_path / "template-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="template-prepare-reviewer")
    )
    work = WorkGraphService(connection, realm, actor_id=actor.id).create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Prepare lifecycle template",
        acceptance_criteria=(AcceptanceCriterion("current exact template"),),
    )
    now = dt.datetime.now(dt.UTC)
    scan = ProjectIntegrationService(connection, realm).scan(project.id, now=now)
    PostgresMemoryHookInstaller(connection, realm.id).ensure(installed_at=now)
    ToolRegistryRepository(connection, realm.id).store_compiled_set(
        CompiledToolSet.create(
            realm_id=realm.id,
            role="builder",
            permission_profile_digest=digest("template-builder-permission"),
            entries=(),
            created_at=now,
        )
    )

    service = LifecycleRuntimeTemplatePrepareService(connection, realm)
    results = []
    for offset in range(2):
        plan_now = now if offset == 0 else dt.datetime.now(dt.UTC)
        plan = service.prepare(
            project_id=project.id,
            work_item_id=work.id,
            actor_id=actor.id,
            source_revision=scan.revision.revision,
            now=plan_now,
        )
        enqueued = service.apply(plan, supplied_plan_digest=plan.plan_digest)
        assert enqueued["effect_started"] is False
        result = run_lifecycle_template_prepare_once(connection, realm)
        assert result is not None
        results.append(result)

    assert all(result["provider_calls"] == 0 for result in results)
    assert all(result["network_calls"] == 0 for result in results)
    assert all(result["grants_authority"] is False for result in results)
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.effect_claim claim"
            " join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id"
            " left join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
            " and receipt.claim_id=claim.id"
            " join work.work_item item on item.realm_id=job.realm_id"
            " and item.id=job.work_item_id"
            " where claim.realm_id=%s and item.title=%s and receipt.id is null",
            (realm.id, "Codex lifecycle runtime template prerequisites"),
        )
        assert cursor.fetchone() == (0,)
    template = LifecycleRuntimeTemplateRepository(connection, realm.id).current(
        plan.project_id, plan.source_revision, plan.policy_digest
    )
    assert template.execution_target_digest
    assert template.execution_environment_snapshot_digest

    crash_plan = service.prepare(
        project_id=project.id,
        work_item_id=work.id,
        actor_id=actor.id,
        source_revision=scan.revision.revision,
        now=dt.datetime.now(dt.UTC),
    )
    crashed = service.apply(crash_plan, supplied_plan_digest=crash_plan.plan_digest)
    original_bind = template_prepare_module._bind_prepare_runtime

    def crash_after_materialize(**_: Any) -> Any:
        raise RuntimeError("injected post-materialize pre-envelope crash")

    monkeypatch.setattr(template_prepare_module, "_bind_prepare_runtime", crash_after_materialize)
    with pytest.raises(RuntimeError, match="post-materialize pre-envelope"):
        run_lifecycle_template_prepare_once(connection, realm)
    monkeypatch.setattr(template_prepare_module, "_bind_prepare_runtime", original_bind)
    with connection.cursor() as cursor:
        cursor.execute(
            "update runtime.lease set expires_at=clock_timestamp()-interval '1 second'"
            " where realm_id=%s and job_id=%s",
            (realm.id, crashed["job_id"]),
        )
    recovery = LifecycleTemplateRecoveryService(connection, realm)
    recovery_now = dt.datetime.now(dt.UTC)
    recovery_plan = recovery.prepare(
        job_id=UUID(str(crashed["job_id"])),
        actor_id=actor.id,
        now=recovery_now,
    )
    recovery_authorization = recovery.issue_authorization(
        recovery_plan, actor_id=actor.id, now=recovery_now
    )
    recovered = recovery.apply(
        recovery_plan, authorization_id=recovery_authorization.id, now=recovery_now
    )
    assert recovered.old_finalization.receipt.result_digest is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.claim_without_receipt claim"
            " join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id"
            " where claim.realm_id=%s and (job.id=%s or job.id=%s)",
            (realm.id, crashed["job_id"], recovered.recovery_job_id),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "select count(*) from runtime.recovery_envelope_admission"
            " where realm_id=%s and old_job_id=%s and recovery_job_id=%s"
            " and consumed_at is not null",
            (realm.id, crashed["job_id"], recovered.recovery_job_id),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "select runtime.consume_recovery_envelope_admission(%s,%s,%s,%s,%s)",
            (
                realm.id,
                crashed["job_id"],
                recovery_plan.reconciliation.old_completion.attempt_id,
                recovery_plan.lease_id,
                recovery_plan.reconciliation.old_completion.fencing_token,
            ),
        )
        assert cursor.fetchone() == (False,)
        cursor.execute(
            "select job.state,attempt.outcome from runtime.job job"
            " join runtime.job_attempt attempt on attempt.realm_id=job.realm_id"
            " and attempt.job_id=job.id where job.realm_id=%s and job.id=%s",
            (realm.id, crashed["job_id"]),
        )
        assert cursor.fetchone() == ("completed", "succeeded")
