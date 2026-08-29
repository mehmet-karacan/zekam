"""Bounded loop ledger RLS, privilege ve append-only guvenlik testleri."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, compile_context
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
)
from zekam.domain.realm import Realm
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]


def _stored_policy(realm_session: tuple[Any, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "loop-security-source"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Loop security")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=digest("work-policy"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    repository = PostgresLoopPolicyRepository(connection, realm.id)
    now = repository.current_database_time()
    manifest = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    manifest_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(manifest)
    assignments = AgentAssignmentRepository(connection, realm.id)

    def assignment(
        role: AssignmentRole, agent_ref: str, instruction: str, parent=None
    ) -> AgentAssignment:  # type: ignore[no-untyped-def]
        item = AgentAssignment(
            new_uuid7(now=now),
            realm.id,
            project.id,
            work.id,
            role,
            agent_ref,
            instruction,
            manifest.manifest_digest,
            digest("placeholder"),
            parent_assignment_id=parent,
            plan_id=None if role is AssignmentRole.COORDINATOR else plan.id,
            step_id=None if role is AssignmentRole.COORDINATOR else "build",
            created_at=now,
        )
        return replace(item, assignment_digest=digest(item.identity_body()))

    coordinator = assignment(AssignmentRole.COORDINATOR, "coordinator", digest("coordinate"))
    assignments.create(coordinator)
    builder = assignment(AssignmentRole.BUILDER, "builder", digest("prompt"), coordinator.id)
    verifier = assignment(AssignmentRole.VERIFIER, "verifier", digest("validator"), coordinator.id)
    assignments.create(builder)
    assignments.create(verifier)
    policy = LoopPolicy(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=builder.id,
        context_manifest_id=manifest_id,
        validator_assignment_id=verifier.id,
        max_attempts=3,
        max_tokens=1000,
        max_cost_micros=10000,
        deadline=now + dt.timedelta(minutes=5),
        validator_spec_digest=digest("validator"),
        required_delta=tuple(sorted(LoopDeltaKind, key=str)),
        forbidden_effects=tuple(
            sorted(
                (
                    LoopEffectClass.DEPLOY,
                    LoopEffectClass.EXTERNAL_MESSAGE,
                    LoopEffectClass.MIGRATION_APPLY,
                ),
                key=str,
            )
        ),
        terminal_states=tuple(sorted(LoopTerminalState, key=str)),
        source_revision="revision-1",
        context_manifest_digest=manifest.manifest_digest,
        plan_digest=plan.plan_digest,
        policy_revision_digest=digest("policy-revision"),
        canonical_effect_kind="none",
        created_at=now,
    )
    repository.store_policy(policy)
    return realm, connection, policy


def test_loop_ledger_direct_bypass_ve_mutation_reddedilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    _realm, connection, policy = _stored_policy(realm_session, tmp_path)
    with (
        pytest.raises(Exception, match=r"permission denied|append-only"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("insert into runtime.loop_policy default values")
    with (
        pytest.raises(Exception, match=r"permission denied|append-only"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("insert into runtime.loop_attempt default values")
    with (
        pytest.raises(Exception, match=r"permission denied|append-only"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("update runtime.loop_policy set max_attempts=99 where id=%s", (policy.id,))
    with (
        pytest.raises(Exception, match=r"permission denied|append-only"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("delete from runtime.loop_policy where id=%s", (policy.id,))


def test_direct_repeated_agent_dispatch_loop_admission_olmadan_reddedilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection, policy = _stored_policy(realm_session, tmp_path)
    assignments = AgentAssignmentRepository(connection, realm.id)

    def invocation(label: str) -> AgentInvocation:
        item_id = new_uuid7()
        body = {
            "id": str(item_id),
            "realm_id": str(realm.id),
            "assignment_id": str(policy.assignment_id),
            "client_id": "opencode",
            "execution_identity": label,
        }
        return AgentInvocation(
            item_id,
            realm.id,
            policy.assignment_id,
            "opencode",
            label,
            digest(body),
            policy.created_at,
        )

    first = invocation("genesis")
    assignments.record_invocation(first)
    assignments.store_result(
        assignment_id=policy.assignment_id,
        invocation_id=first.id,
        envelope_digest=digest("genesis-result"),
    )
    with pytest.raises(Exception, match="repeated semantic agent dispatch"):
        assignments.record_invocation(invocation("bypass-retry"))


def test_random_digest_canonical_delta_evidence_olamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    _realm, _connection, policy = _stored_policy(realm_session, tmp_path)
    repository = PostgresLoopPolicyRepository(_connection, _realm.id)
    with pytest.raises(Exception, match="canonical loop evidence bulunamadi"):
        repository.register_delta_evidence(policy.id, LoopDeltaKind.NEW_EVIDENCE.value, new_uuid7())


def test_supplied_semantic_binding_ve_delta_digest_forge_edilemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    _realm, connection, policy = _stored_policy(realm_session, tmp_path)
    request = LoopAttemptRequest(
        loop_id=policy.id,
        prompt_digest=digest("prompt"),
        context_digest=policy.context_manifest_digest,
        action_digest=digest("action"),
        source_revision=policy.source_revision,
        plan_digest=policy.plan_digest,
        policy_revision_digest=policy.policy_revision_digest,
        validator_spec_digest=policy.validator_spec_digest,
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_micros=10,
    )
    with (
        pytest.raises(Exception, match="supplied digest canonical body ile uyusmuyor"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "select * from runtime.admit_loop_attempt_current("
            " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            " %s,%s,%s,%s,%s,%s)",
            (
                new_uuid7(),
                policy.id,
                None,
                digest("forged-semantic-request"),
                request.prompt_digest,
                request.context_digest,
                request.action_digest,
                request.binding_digest,
                request.source_revision,
                request.plan_digest,
                request.policy_revision_digest,
                request.validator_spec_digest,
                request.reserved_input_tokens,
                request.reserved_output_tokens,
                request.reserved_cost_micros,
                [],
                request.delta_digest,
                1,
                None,
                None,
                None,
                None,
                None,
            ),
        )


def test_cross_realm_loop_policy_gorunmez_ve_admission_yapilamaz(
    realm_session: tuple[Any, Any],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
) -> None:
    first_realm, _connection, policy = _stored_policy(realm_session, tmp_path)
    second_realm = Realm.create(slug="loop-security-second", display_name="Loop Security Second")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(second_realm)
    with connect(migrated_database) as second:
        configure_session(second, realm_id=second_realm.id)
        repository = PostgresLoopPolicyRepository(second, second_realm.id)
        assert repository.terminal_state(policy.id) is None
        request = LoopAttemptRequest(
            loop_id=policy.id,
            prompt_digest=digest("prompt"),
            context_digest=policy.context_manifest_digest,
            action_digest=digest("action"),
            source_revision=policy.source_revision,
            plan_digest=policy.plan_digest,
            policy_revision_digest=policy.policy_revision_digest,
            validator_spec_digest=policy.validator_spec_digest,
            reserved_input_tokens=10,
            reserved_output_tokens=10,
            reserved_cost_micros=10,
        )
        with pytest.raises(Exception, match="policy bulunamadi"):
            repository.admit(request)
        with second.cursor() as cursor:
            cursor.execute(
                "select count(*) from runtime.loop_policy where realm_id=%s and id=%s",
                (first_realm.id, policy.id),
            )
            assert cursor.fetchone()[0] == 0


def test_loop_control_raw_insert_ve_forged_authorization_fail_closed(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    _realm, connection, policy = _stored_policy(realm_session, tmp_path)
    event_id = new_uuid7()
    authorization_id = new_uuid7()

    with (
        pytest.raises(Exception, match=r"permission denied|append-only"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into runtime.loop_control_event"
            " (id,realm_id,loop_id,state,plan_digest,authorization_id,"
            " authorization_digest,reason_digest)"
            " values (%s,core.current_realm_id(),%s,'paused',%s,%s,%s,%s)",
            (
                event_id,
                policy.id,
                policy.plan_digest,
                authorization_id,
                digest("forged-authorization"),
                digest("forged-reason"),
            ),
        )

    with (
        pytest.raises(Exception, match="exact one-shot authorization ister"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "select runtime.record_loop_control_event(%s,%s,%s,%s,%s,%s)",
            (
                event_id,
                policy.id,
                "paused",
                authorization_id,
                digest("forged-authorization"),
                digest("forged-reason"),
            ),
        )
