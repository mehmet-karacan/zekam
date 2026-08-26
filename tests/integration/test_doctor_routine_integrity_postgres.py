"""Real PostgreSQL acceptance for migration-bound routine inventory."""

from __future__ import annotations

from typing import Any

import pytest

from zekam.application.composition import ApplicationContext
from zekam.application.config import DatabaseSettings
from zekam.application.doctor_repair import build_doctor_repair_plan
from zekam.application.doctor_repair_runtime import apply_doctor_repair_with_runtime
from zekam.application.governance import GovernanceService, default_capabilities
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import RealmContext
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import AuthorizationState
from zekam.domain.work import WorkState
from zekam.infrastructure.postgres import routine_integrity
from zekam.infrastructure.postgres.connection import configure_session, connect, reset_role
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_migrated_database_has_exact_routine_inventory(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection:
        current = routine_integrity.status(connection)

    assert current.migration_head is not None
    assert len(current.expected) == len(current.present)
    assert current.missing == ()
    assert current.unexpected == ()
    assert current.is_healthy


def test_missing_routine_is_recreated_with_canonical_acl_and_comment(
    isolated_migrated_database: DatabaseSettings,
) -> None:
    with connect(isolated_migrated_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute("drop function core.find_realm_id(text)")
        missing = routine_integrity.status(connection)
        assert [item.key.label for item in missing.missing] == [
            "core.find_realm_id:function"
        ]

        result = routine_integrity.repair_missing_routines(
            connection, plan_digest=missing.repair_plan_digest
        )
        after = routine_integrity.status(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select has_function_privilege('public', "
                "'core.find_realm_id(text)', 'execute'), "
                "has_function_privilege('zekam_app', "
                "'core.find_realm_id(text)', 'execute'), "
                "obj_description('core.find_realm_id(text)'::regprocedure, 'pg_proc')"
            )
            public_execute, app_execute, comment = cursor.fetchone()

    assert result.verified
    assert after.missing == ()
    assert public_execute is False
    assert app_execute is True
    assert "Slug ile realm kimligi" in comment


def test_runtime_repair_writes_work_authorization_claim_and_terminal_receipt(
    realm_session: tuple[Any, Any], context: ApplicationContext
) -> None:
    realm, connection = realm_session
    realm_context = RealmContext(realm=realm, connection=connection)
    governance = GovernanceService(connection, realm)
    governance.ensure_default_policy()
    for capability in default_capabilities(realm.id):
        if governance.capabilities.current(capability.name) is None:
            governance.capabilities.append(capability)
    actor = Actor.create(
        realm=realm,
        kind=ActorKind.HUMAN,
        slug="doctor-tester",
    )
    ActorRepository(connection, realm.id).add(actor)
    project = ProjectIntegrationService(connection, realm).register(
        source_path=context.core_path,
        slug="doctor-runtime-test",
    )

    reset_role(connection)
    with connection.cursor() as cursor:
        cursor.execute("drop function core.find_realm_id(text)")
    configure_session(connection, realm_id=realm.id)
    try:
        repair_plan = build_doctor_repair_plan(
            core_path=context.core_path,
            connection=connection,
            migrations_directory=context.core_path / "migrations",
        )
        assert repair_plan.next_step == "postgres-routine-repair"

        result = apply_doctor_repair_with_runtime(
            realm_context,
            context,
            repair_plan=repair_plan,
            plan_digest=repair_plan.plan_digest,
            actor_id=actor.id,
            project_id=project.id,
        )

        authorization = AuthorizationRepository(connection, realm.id).get(
            result.authorization_id
        )
        work = WorkItemRepository(connection, realm.id).get(result.work_id)
        assert authorization.state is AuthorizationState.CONSUMED
        assert work.state is WorkState.COMPLETED
        assert result.receipt_id
        assert routine_integrity.status(connection).missing == ()
    finally:
        reset_role(connection)
        missing = routine_integrity.status(connection)
        if missing.missing:
            routine_integrity.repair_missing_routines(
                connection, plan_digest=missing.repair_plan_digest
            )
        configure_session(connection, realm_id=realm.id)
