"""P1-008 LoopPolicy PostgreSQL acceptance testleri."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.execution import ExecutionHost
from zekam.application.loop_control import LoopControlService, LoopControlState
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
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
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.runtime import Job, JobKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.measured_loop_repository import PostgresMeasuredLoopRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
TERMINALS = tuple(sorted(LoopTerminalState, key=str))
DELTAS = tuple(sorted(LoopDeltaKind, key=str))
FORBIDDEN = tuple(
    sorted(
        (
            LoopEffectClass.DEPLOY,
            LoopEffectClass.EXTERNAL_MESSAGE,
            LoopEffectClass.MIGRATION_APPLY,
        ),
        key=str,
    )
)


def _assignment(
    *,
    realm,
    project,
    work,
    plan,
    manifest_digest: str,
    role: AssignmentRole,
    agent_ref: str,
    instruction_digest: str,
    now: dt.datetime,
    parent_id=None,
) -> AgentAssignment:  # type: ignore[no-untyped-def]
    candidate = AgentAssignment(
        id=new_uuid7(now=now),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        role=role,
        agent_ref=agent_ref,
        instruction_digest=instruction_digest,
        context_manifest_digest=manifest_digest,
        assignment_digest=digest("placeholder"),
        parent_assignment_id=parent_id,
        plan_id=None if role is AssignmentRole.COORDINATOR else plan.id,
        step_id=None if role is AssignmentRole.COORDINATOR else "build",
        created_at=now,
    )
    return replace(candidate, assignment_digest=digest(candidate.identity_body()))


def _setup(realm_session: tuple[Any, Any], tmp_path: Path, *, effect: EffectKind = EffectKind.NONE):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    source = tmp_path / "loop-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Bounded loop")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=digest("work-policy"),
        steps=(PlanStep("build", "Build", effect),),
    )
    repository = PostgresLoopPolicyRepository(connection, realm.id)
    moment = repository.current_database_time()
    manifest = compile_context(
        (), token_budget=5, minimum_authority=AuthorityLevel.OBSERVED, now=moment
    )
    manifest_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(manifest)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=manifest.manifest_digest,
        role=AssignmentRole.COORDINATOR,
        agent_ref="coordinator",
        instruction_digest=digest("coordinate"),
        now=moment,
    )
    assignments.create(coordinator)
    builder = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=manifest.manifest_digest,
        role=AssignmentRole.BUILDER,
        agent_ref="builder",
        instruction_digest=digest("prompt"),
        now=moment,
        parent_id=coordinator.id,
    )
    verifier = _assignment(
        realm=realm,
        project=project,
        work=work,
        plan=plan,
        manifest_digest=manifest.manifest_digest,
        role=AssignmentRole.VERIFIER,
        agent_ref="verifier",
        instruction_digest=digest("validator"),
        now=moment,
        parent_id=coordinator.id,
    )
    assignments.create(builder)
    assignments.create(verifier)
    return (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest.manifest_digest,
        assignments,
        builder,
        verifier,
    )


def _policy(
    realm,
    project,
    work,
    plan,
    moment: dt.datetime,
    manifest_id,
    manifest_digest,
    builder,
    verifier,
    **changes: object,
):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "id": new_uuid7(now=moment),
        "realm_id": realm.id,
        "project_id": project.id,
        "work_item_id": work.id,
        "plan_id": plan.id,
        "step_id": "build",
        "assignment_id": builder.id,
        "context_manifest_id": manifest_id,
        "validator_assignment_id": verifier.id,
        "max_attempts": 3,
        "max_tokens": 1000,
        "max_cost_micros": 10000,
        "deadline": moment + dt.timedelta(minutes=10),
        "validator_spec_digest": digest("validator"),
        "required_delta": DELTAS,
        "forbidden_effects": FORBIDDEN,
        "terminal_states": TERMINALS,
        "source_revision": "revision-1",
        "context_manifest_digest": manifest_digest,
        "plan_digest": plan.plan_digest,
        "policy_revision_digest": digest("loop-policy-revision"),
        "canonical_effect_kind": plan.steps[0].effect.value,
        "created_at": moment,
    }
    values.update(changes)
    return LoopPolicy(**values)  # type: ignore[arg-type]


def _request(policy: LoopPolicy, **changes: object) -> LoopAttemptRequest:
    values: dict[str, object] = {
        "loop_id": policy.id,
        "prompt_digest": digest("prompt"),
        "context_digest": policy.context_manifest_digest,
        "action_digest": digest("action"),
        "source_revision": policy.source_revision,
        "plan_digest": policy.plan_digest,
        "policy_revision_digest": policy.policy_revision_digest,
        "validator_spec_digest": policy.validator_spec_digest,
        "reserved_input_tokens": 100,
        "reserved_output_tokens": 50,
        "reserved_cost_micros": 1000,
    }
    values.update(changes)
    return LoopAttemptRequest(**values)  # type: ignore[arg-type]


def _validation(
    repository: PostgresLoopPolicyRepository,
    assignments: AgentAssignmentRepository,
    builder: AgentAssignment,
    verifier: AgentAssignment,
    attempt_id,
    request: LoopAttemptRequest,
    *,
    outcome: LoopAttemptOutcome = LoopAttemptOutcome.RETRYABLE_FAILURE,
    actual_input_tokens: int = 90,
) -> LoopValidation:  # type: ignore[no-untyped-def]
    now = repository.current_database_time()

    def receipt(assignment: AgentAssignment, identity: str, envelope: str) -> AgentInvocation:
        invocation_id = new_uuid7(now=repository.current_database_time())
        body = {
            "id": str(invocation_id),
            "realm_id": str(assignment.realm_id),
            "assignment_id": str(assignment.id),
            "client_id": "opencode",
            "execution_identity": identity,
        }
        invocation = AgentInvocation(
            invocation_id,
            assignment.realm_id,
            assignment.id,
            "opencode",
            identity,
            digest(body),
            now,
        )
        repository.bind_dispatch(attempt_id, "agent", invocation.id)
        assignments.record_invocation(invocation)
        assignments.store_result(
            assignment_id=assignment.id,
            invocation_id=invocation.id,
            envelope_digest=digest(envelope),
        )
        return invocation

    result = receipt(builder, f"builder:{attempt_id}", f"result:{attempt_id}")
    checked = receipt(verifier, f"verifier:{attempt_id}", f"validation:{attempt_id}:{outcome}")
    return LoopValidation(
        outcome=outcome,
        validator_spec_digest=request.validator_spec_digest,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=30,
        actual_cost_micros=900,
        result_invocation_id=result.id,
        verifier_invocation_id=checked.id,
    )


def _latest_verifier_evidence(
    repository: PostgresLoopPolicyRepository, policy: LoopPolicy, verifier: AgentAssignment
):
    with repository.connection.cursor() as cursor:
        cursor.execute(
            "select i.id from agents.invocation i join agents.result_receipt r"
            " on r.realm_id=i.realm_id and r.invocation_id=i.id"
            " where i.realm_id=%s and i.assignment_id=%s"
            " order by r.created_at desc,i.id desc limit 1",
            (repository.realm_id, verifier.id),
        )
        source_id = cursor.fetchone()[0]
    return repository.register_delta_evidence(
        policy.id, LoopDeltaKind.NEW_EVIDENCE.value, source_id
    )


def test_delta_ile_retry_pass_ve_terminal_sonrasi_dispatch_reddi(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    assert repository.store_policy(policy) == (policy.id, True)
    assert repository.store_policy(policy) == (policy.id, False)
    with repository.connection.cursor() as cursor:
        cursor.execute(
            "select policy_digest = 'sha256:' || encode("
            " public.digest(convert_to(canonical_body::text,'UTF8'),'sha256'),'hex'),"
            " canonical_body->>'plan_digest',canonical_body->>'source_revision',"
            " canonical_body->>'canonical_effect_kind'"
            " from runtime.loop_policy where realm_id=%s and id=%s",
            (realm.id, policy.id),
        )
        digest_current, plan_current, source_current, effect_current = cursor.fetchone()
    assert digest_current is True
    assert (plan_current, source_current, effect_current) == (
        plan.plan_digest,
        plan.source_revision,
        plan.steps[0].effect.value,
    )

    first_request = _request(policy)
    first = repository.admit(first_request)
    assert first.admitted and first.ordinal == 1 and first.attempt_id is not None
    assert (
        repository.complete(
            first.attempt_id,
            _validation(
                repository, assignments, builder, verifier, first.attempt_id, first_request
            ),
        )
        == "active"
    )

    second_request = replace(
        first_request,
        predecessor_attempt_id=first.attempt_id,
        delta_evidence_ids=(_latest_verifier_evidence(repository, policy, verifier),),
    )
    second = repository.admit(second_request)
    assert second.admitted and second.ordinal == 2 and second.attempt_id is not None
    passed = _validation(
        repository,
        assignments,
        builder,
        verifier,
        second.attempt_id,
        second_request,
        outcome=LoopAttemptOutcome.PASSED,
    )
    assert repository.complete(second.attempt_id, passed) == "passed"
    assert repository.terminal_state(policy.id) is LoopTerminalState.PASSED
    with repository.connection.cursor() as cursor:
        cursor.execute(
            "select count(*),bool_and(checkpoint_digest = 'sha256:' || encode("
            " public.digest(convert_to(checkpoint_body::text,'UTF8'),'sha256'),'hex'))"
            " from runtime.loop_checkpoint where realm_id=%s and loop_id=%s",
            (realm.id, policy.id),
        )
        assert cursor.fetchone() == (2, True)
        cursor.execute(
            "select exists(select 1 from runtime.loop_terminal t"
            " join runtime.loop_checkpoint c on c.realm_id=t.realm_id and c.id=t.checkpoint_id"
            " where t.realm_id=%s and t.loop_id=%s and c.attempt_id=t.attempt_id)",
            (realm.id, policy.id),
        )
        assert cursor.fetchone()[0] is True

    denied = repository.admit(replace(second_request, predecessor_attempt_id=second.attempt_id))
    assert not denied.admitted and denied.terminal_state is LoopTerminalState.PASSED


def test_same_semantic_request_no_delta_uuid_degisse_de_blocked(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(policy)
    request = _request(policy)
    first = repository.admit(request)
    assert first.attempt_id is not None
    repository.complete(
        first.attempt_id,
        _validation(repository, assignments, builder, verifier, first.attempt_id, request),
    )

    blocked = repository.admit(replace(request, predecessor_attempt_id=first.attempt_id))
    assert not blocked.admitted
    assert blocked.terminal_state is LoopTerminalState.BLOCKED
    assert repository.terminal_state(policy.id) is LoopTerminalState.BLOCKED


@pytest.mark.parametrize("forbidden", (True, False))
def test_forbidden_effect_ve_budget_dispatch_oncesi_terminal_olur(
    realm_session: tuple[Any, Any],
    tmp_path: Path,
    forbidden: bool,
) -> None:
    effect = EffectKind.GIT_PUSH if forbidden else EffectKind.NONE
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        _assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path, effect=effect)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(policy)
    change = {} if forbidden else {"reserved_input_tokens": 9999}
    terminal = LoopTerminalState.MANUAL_REVIEW if forbidden else LoopTerminalState.BUDGET_EXHAUSTED
    admission = repository.admit(_request(policy, **change))
    assert not admission.admitted and admission.terminal_state is terminal
    with repository.connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.loop_attempt where realm_id=%s and loop_id=%s",
            (realm.id, policy.id),
        )
        assert cursor.fetchone()[0] == 0


def test_predecessor_context_validator_ve_receipt_forgery_fail_closed(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(policy)
    request = _request(policy)
    first = repository.admit(request)
    assert first.attempt_id is not None
    repository.complete(
        first.attempt_id,
        _validation(repository, assignments, builder, verifier, first.attempt_id, request),
    )
    evidence_id = _latest_verifier_evidence(repository, policy, verifier)

    with pytest.raises(Exception, match="predecessor"):
        repository.admit(
            replace(
                request,
                predecessor_attempt_id=new_uuid7(now=moment),
                delta_evidence_ids=(evidence_id,),
            )
        )
    with pytest.raises(Exception, match="drift"):
        repository.admit(
            replace(
                request,
                predecessor_attempt_id=first.attempt_id,
                context_digest=digest("forged-context"),
            )
        )

    second = repository.admit(
        replace(
            request,
            predecessor_attempt_id=first.attempt_id,
            delta_evidence_ids=(evidence_id,),
        )
    )
    assert second.attempt_id is not None
    with pytest.raises(Exception, match="canonical loop result receipt"):
        repository.complete(
            second.attempt_id,
            LoopValidation(
                outcome=LoopAttemptOutcome.RETRYABLE_FAILURE,
                validator_spec_digest=request.validator_spec_digest,
                actual_input_tokens=1,
                actual_output_tokens=1,
                actual_cost_micros=1,
                result_invocation_id=new_uuid7(now=moment),
                verifier_invocation_id=new_uuid7(now=moment),
            ),
        )


def test_effect_receipt_exact_loop_attempta_fresh_ve_tek_kullanimlidir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path, effect=EffectKind.FILE_WRITE)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(policy)
    request = _request(policy)
    admitted = repository.admit(request)
    assert admitted.attempt_id is not None

    host = ExecutionHost(repository.connection, realm.id, worker_label="loop-effect")
    job = Job.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=builder.id,
        kind=JobKind.MUTATION,
        idempotency_key=f"loop-effect-{policy.id}",
        resources=(),
        required_capabilities=(),
    )
    host.jobs.enqueue(job)
    claimed = host.acquire_work(capabilities=())
    assert claimed is not None and claimed.job.id == job.id
    claim = host.claim_effect(
        claimed,
        operation="file-write",
        effect_digest=digest("loop-file-write"),
        authorization_digest=digest("loop-authorization"),
        resources=job.resources,
        adapter_digest=digest("loop-adapter"),
    )
    receipt = host.record_success(claim, result_digest=digest("loop-effect-result"))
    repository.bind_dispatch(admitted.attempt_id, "tool", claim.id)
    validation = replace(
        _validation(
            repository,
            assignments,
            builder,
            verifier,
            admitted.attempt_id,
            request,
            outcome=LoopAttemptOutcome.PASSED,
        ),
        effect_receipt_id=receipt.id,
    )
    assert repository.complete(admitted.attempt_id, validation) == "passed"

    second_policy = _policy(
        realm,
        project,
        work,
        plan,
        repository.current_database_time(),
        manifest_id,
        manifest_digest,
        builder,
        verifier,
        policy_revision_digest=digest("second-loop-policy"),
    )
    repository.store_policy(second_policy)
    second_request = _request(second_policy)
    second = repository.admit(second_request)
    assert second.attempt_id is not None
    second_validation = replace(
        _validation(
            repository,
            assignments,
            builder,
            verifier,
            second.attempt_id,
            second_request,
            outcome=LoopAttemptOutcome.PASSED,
        ),
        effect_receipt_id=receipt.id,
    )
    with pytest.raises(Exception, match="canonical effect claim/receipt"):
        repository.complete(second.attempt_id, second_validation)


def test_son_attempt_budget_slotu_iki_connectionda_tek_winner_uretir(
    realm_session: tuple[Any, Any],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
) -> None:
    (
        realm,
        project,
        work,
        plan,
        first_repository,
        moment,
        manifest_id,
        manifest_digest,
        _assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm,
        project,
        work,
        plan,
        moment,
        manifest_id,
        manifest_digest,
        builder,
        verifier,
        max_attempts=1,
    )
    first_repository.store_policy(policy)
    request = _request(policy)

    with connect(migrated_database) as second_connection:
        configure_session(second_connection, realm_id=realm.id)
        second_repository = PostgresLoopPolicyRepository(second_connection, realm.id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(
                pool.map(
                    lambda repository: repository.admit(request),
                    (first_repository, second_repository),
                )
            )

    assert sum(item.admitted for item in results) == 1
    loser = next(item for item in results if not item.admitted)
    assert loser.terminal_state is LoopTerminalState.BUDGET_EXHAUSTED
    with first_repository.connection.cursor() as cursor:
        cursor.execute(
            "select count(*),min(ordinal),max(ordinal) from runtime.loop_attempt"
            " where realm_id=%s and loop_id=%s",
            (realm.id, policy.id),
        )
        assert cursor.fetchone() == (1, 1, 1)


def test_receiptsiz_onceki_attempt_ve_usage_overrun_manual_review_olur(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    unresolved_policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(unresolved_policy)
    request = _request(unresolved_policy)
    first = repository.admit(request)
    assert first.attempt_id is not None
    denied = repository.admit(
        replace(
            request,
            predecessor_attempt_id=first.attempt_id,
            delta_evidence_ids=(new_uuid7(now=moment),),
        )
    )
    assert not denied.admitted
    assert denied.terminal_state is LoopTerminalState.MANUAL_REVIEW

    overrun_policy = _policy(
        realm,
        project,
        work,
        plan,
        moment,
        manifest_id,
        manifest_digest,
        builder,
        verifier,
        policy_revision_digest=digest("loop-policy-overrun"),
    )
    repository.store_policy(overrun_policy)
    overrun_request = _request(overrun_policy)
    admitted = repository.admit(overrun_request)
    assert admitted.attempt_id is not None
    state = repository.complete(
        admitted.attempt_id,
        _validation(
            repository,
            assignments,
            builder,
            verifier,
            admitted.attempt_id,
            overrun_request,
            actual_input_tokens=overrun_request.reserved_input_tokens + 1,
        ),
    )
    assert state == "manual-review"
    assert repository.terminal_state(overrun_policy.id) is LoopTerminalState.MANUAL_REVIEW


def test_policy_current_plan_revision_degistiginde_admission_fail_closed(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    (
        realm,
        project,
        work,
        plan,
        repository,
        moment,
        manifest_id,
        manifest_digest,
        _assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm, project, work, plan, moment, manifest_id, manifest_digest, builder, verifier
    )
    repository.store_policy(policy)
    WorkGraphService(repository.connection, realm).create_plan(
        work.id,
        source_revision="revision-2",
        policy_digest=digest("work-policy-v2"),
        steps=(PlanStep("build", "Build revised", EffectKind.NONE),),
    )
    with pytest.raises(Exception, match="scope/currentness drift"):
        repository.admit(_request(policy))


@pytest.mark.parametrize(
    "target_state",
    (LoopControlState.PAUSED, LoopControlState.DRAINING, LoopControlState.CANCELLED),
)
def test_loop_control_event_exact_authorizationla_yazilir_ve_admissioni_kapatir(
    realm_session: tuple[Any, Any],
    tmp_path: Path,
    target_state: LoopControlState,
) -> None:
    (
        realm,
        project,
        work,
        task_plan,
        policies,
        moment,
        manifest_id,
        manifest_digest,
        _assignments,
        builder,
        verifier,
    ) = _setup(realm_session, tmp_path)
    policy = _policy(
        realm,
        project,
        work,
        task_plan,
        moment,
        manifest_id,
        manifest_digest,
        builder,
        verifier,
    )
    policies.store_policy(policy)
    connection = policies.connection
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(
            realm=realm,
            kind=ActorKind.HUMAN,
            slug=f"loop-control-{target_state.value}",
            now=moment,
        )
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    measured = PostgresMeasuredLoopRepository(connection, realm.id)
    service = LoopControlService(measured, authorizations)

    control = service.prepare(
        policy.id,
        target_state=target_state,
        reason_digest=digest((target_state.value, "reviewed-control")),
    )
    wrong = authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=task_plan.id,
            plan_digest=task_plan.plan_digest,
            effect_digest=digest("wrong-loop-control-effect"),
            scope=AuthorizationScope(
                allowed_resources=(control.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=moment,
        )
    )
    with pytest.raises(Exception, match="exact authorization binding yok"):
        service.apply(control, authorization_id=wrong.id, now=moment)

    authorization = authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=task_plan.id,
            plan_digest=task_plan.plan_digest,
            effect_digest=control.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(control.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=moment,
        )
    )
    receipt = service.apply(control, authorization_id=authorization.id, now=moment)

    assert receipt.target_state is target_state
    assert authorizations.get(authorization.id).state.value == "consumed"
    assert measured.read_loop_control_snapshot(policy.id).current_state is target_state
    with pytest.raises(Exception, match="Paused/draining/cancelled"):
        measured.assert_loop_open(policy.id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select state,plan_digest,authorization_id,reason_digest"
            " from runtime.loop_control_event where realm_id=%s and id=%s",
            (realm.id, receipt.event_id),
        )
        assert cursor.fetchone() == (
            target_state.value,
            task_plan.plan_digest,
            authorization.id,
            control.reason_digest,
        )

    if target_state is LoopControlState.CANCELLED:
        with pytest.raises(Exception, match="transition gecersiz"):
            service.prepare(
                policy.id,
                target_state=LoopControlState.ACTIVE,
                reason_digest=digest("cancelled-cannot-resume"),
            )
        invalid_reason = digest("cancelled-direct-db-resume")
        invalid_effect = digest(
            {
                "effect": "database-write",
                "resource": f"loop:{policy.id}",
                "loop_id": str(policy.id),
                "plan_digest": task_plan.plan_digest,
                "source_state": "cancelled",
                "target_state": "active",
                "reason_digest": invalid_reason,
            }
        )
        invalid_authorization = authorizations.issue(
            Authorization.issue(
                realm_id=realm.id,
                actor_id=actor.id,
                work_item_id=work.id,
                plan_id=task_plan.id,
                plan_digest=task_plan.plan_digest,
                effect_digest=invalid_effect,
                scope=AuthorizationScope(
                    allowed_resources=(control.resource,),
                    allowed_effects=("database-write",),
                ),
                risk="high",
                lifetime=dt.timedelta(minutes=5),
                now=moment,
            )
        )
        with (
            pytest.raises(Exception, match="transition gecersiz"),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "select runtime.record_loop_control_event(%s,%s,%s,%s,%s,%s)",
                (
                    new_uuid7(now=moment),
                    policy.id,
                    "active",
                    invalid_authorization.id,
                    invalid_authorization.authorization_digest,
                    invalid_reason,
                ),
            )
        return

    resume = service.prepare(
        policy.id,
        target_state=LoopControlState.ACTIVE,
        reason_digest=digest((target_state.value, "reviewed-resume")),
    )
    resume_authorization = authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=task_plan.id,
            plan_digest=task_plan.plan_digest,
            effect_digest=resume.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(resume.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=moment,
        )
    )
    service.apply(resume, authorization_id=resume_authorization.id, now=moment)
    measured.assert_loop_open(policy.id)
