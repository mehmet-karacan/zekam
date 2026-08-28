"""Provider-free lifecycle template preparation against PostgreSQL."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from zekam.application.governance import GovernanceService
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleRuntimeTemplatePrepareService,
    run_lifecycle_template_prepare_once,
)
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
    realm_session: tuple[Any, Any], tmp_path: Any
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
    plan = service.prepare(
        project_id=project.id,
        work_item_id=work.id,
        actor_id=actor.id,
        source_revision=scan.revision.revision,
        now=now,
    )
    enqueued = service.apply(plan, supplied_plan_digest=plan.plan_digest)
    assert enqueued["effect_started"] is False
    result = run_lifecycle_template_prepare_once(connection, realm)
    assert result is not None

    assert result["provider_calls"] == 0
    assert result["network_calls"] == 0
    assert result["grants_authority"] is False
    template = LifecycleRuntimeTemplateRepository(connection, realm.id).current(
        plan.project_id, plan.source_revision, plan.policy_digest
    )
    assert template.execution_target_digest
    assert template.execution_environment_snapshot_digest
