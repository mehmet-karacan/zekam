"""Bounded loop -> canonical agent dispatch -> validator E2E."""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.loop_service import LoopBoundAgentDispatchService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind, DispatchOutcome
from zekam.domain.context_continuity import AuthorityLevel, compile_context
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
    LoopValidation,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.clients.adapters import SubprocessClientAdapter
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_client.py"


def _assignment(
    *,
    realm_id,
    project_id,
    work_item_id,
    role: AssignmentRole,
    agent_ref: str,
    context_digest: str,
    instruction_digest: str,
    created_at: dt.datetime,
    parent_assignment_id=None,
    plan_id=None,
    step_id=None,
) -> AgentAssignment:  # type: ignore[no-untyped-def]
    candidate = AgentAssignment(
        id=uuid4(),
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_item_id,
        role=role,
        agent_ref=agent_ref,
        instruction_digest=instruction_digest,
        context_manifest_digest=context_digest,
        assignment_digest=digest("placeholder"),
        parent_assignment_id=parent_assignment_id,
        plan_id=plan_id,
        step_id=step_id,
        created_at=created_at,
    )
    return replace(candidate, assignment_digest=digest(candidate.identity_body()))


def test_bounded_loop_real_subprocess_dispatch_validator_ve_terminal_receipt(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "bounded-loop-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Loop E2E")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=digest("work-policy"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    loop_repository = PostgresLoopPolicyRepository(connection, realm.id)
    now = loop_repository.current_database_time()
    manifest = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    manifest_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(manifest)
    context_digest = manifest.manifest_digest
    instruction_digest = digest("bounded-instruction")
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        role=AssignmentRole.COORDINATOR,
        agent_ref="coordinator",
        context_digest=context_digest,
        instruction_digest=digest("coordinate"),
        created_at=now,
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        role=AssignmentRole.BUILDER,
        agent_ref="bounded-builder",
        context_digest=context_digest,
        instruction_digest=instruction_digest,
        created_at=now,
        parent_assignment_id=coordinator.id,
        plan_id=plan.id,
        step_id="build",
    )
    verifier_spec = digest("deterministic-result-validator")
    verifier = _assignment(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        role=AssignmentRole.VERIFIER,
        agent_ref="bounded-verifier",
        context_digest=context_digest,
        instruction_digest=verifier_spec,
        created_at=now,
        parent_assignment_id=coordinator.id,
        plan_id=plan.id,
        step_id="build",
    )
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
        max_attempts=2,
        max_tokens=1000,
        max_cost_micros=10000,
        deadline=now + dt.timedelta(minutes=5),
        validator_spec_digest=verifier_spec,
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
        context_manifest_digest=context_digest,
        plan_digest=plan.plan_digest,
        policy_revision_digest=digest("loop-policy"),
        canonical_effect_kind="none",
        created_at=now,
    )
    loop_repository.store_policy(policy)
    invocation_id = uuid4()
    execution_identity = f"loop-e2e:{invocation_id}"
    invocation = AgentInvocation(
        id=invocation_id,
        realm_id=realm.id,
        assignment_id=builder.id,
        client_id="opencode",
        execution_identity=execution_identity,
        invocation_digest=digest(
            {
                "id": str(invocation_id),
                "realm_id": str(realm.id),
                "assignment_id": str(builder.id),
                "client_id": "opencode",
                "execution_identity": execution_identity,
            }
        ),
        created_at=now,
    )
    adapter = SubprocessClientAdapter(
        ClientDescriptor(
            ClientKind.OPENCODE,
            "opencode",
            str(FIXTURE),
            frozenset({"code", "structured-result", "cancellation"}),
            "test-v1",
        ),
        launcher=(sys.executable,),
        env=(("ZEKAM_FAKE_CLIENT_MODE", "success"),),
    )
    request = LoopAttemptRequest(
        loop_id=policy.id,
        prompt_digest=instruction_digest,
        context_digest=context_digest,
        action_digest=digest({"client": "opencode", "role": "builder"}),
        source_revision=policy.source_revision,
        plan_digest=policy.plan_digest,
        policy_revision_digest=policy.policy_revision_digest,
        validator_spec_digest=policy.validator_spec_digest,
        reserved_input_tokens=200,
        reserved_output_tokens=100,
        reserved_cost_micros=1000,
    )

    def validate(value, admission):  # type: ignore[no-untyped-def]
        assert admission.attempt_id is not None
        verifier_invocation_id = uuid4()
        identity = f"loop-verifier:{verifier_invocation_id}"
        verifier_invocation = AgentInvocation(
            id=verifier_invocation_id,
            realm_id=realm.id,
            assignment_id=verifier.id,
            client_id="opencode",
            execution_identity=identity,
            invocation_digest=digest(
                {
                    "id": str(verifier_invocation_id),
                    "realm_id": str(realm.id),
                    "assignment_id": str(verifier.id),
                    "client_id": "opencode",
                    "execution_identity": identity,
                }
            ),
            created_at=loop_repository.current_database_time(),
        )
        loop_repository.bind_dispatch(admission.attempt_id, "agent", verifier_invocation.id)
        assignments.record_invocation(verifier_invocation)
        validator_digest = digest(
            {"result_digest": value.result_digest, "success": value.is_success}
        )
        assignments.store_result(
            assignment_id=verifier.id,
            invocation_id=verifier_invocation.id,
            envelope_digest=validator_digest,
        )
        return LoopValidation(
            outcome=(
                LoopAttemptOutcome.PASSED
                if value.outcome is DispatchOutcome.SUCCESS
                else LoopAttemptOutcome.RETRYABLE_FAILURE
            ),
            validator_spec_digest=policy.validator_spec_digest,
            actual_input_tokens=120,
            actual_output_tokens=40,
            actual_cost_micros=600,
            result_invocation_id=invocation.id,
            verifier_invocation_id=verifier_invocation.id,
        )

    result = LoopBoundAgentDispatchService(loop_repository, assignments).dispatch(
        request,
        assignment=builder,
        invocation=invocation,
        adapter=adapter,
        cwd=source,
        timeout_seconds=30,
        validator=validate,
    )

    assert result.value.outcome is DispatchOutcome.SUCCESS
    assert result.terminal_state is LoopTerminalState.PASSED
    with connection.cursor() as cursor:
        cursor.execute(
            "select"
            " (select count(*) from runtime.loop_attempt where loop_id=%s),"
            " (select count(*) from runtime.loop_attempt_outcome where loop_id=%s),"
            " (select state from runtime.loop_terminal where loop_id=%s),"
            " (select count(*) from agents.invocation where assignment_id=%s),"
            " (select count(*) from agents.result_receipt where assignment_id=%s)",
            (policy.id, policy.id, policy.id, builder.id, builder.id),
        )
        assert cursor.fetchone() == (1, 1, "passed", 1, 1)
