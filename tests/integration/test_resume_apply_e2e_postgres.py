from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import tests.integration.test_checkpoint_v2_postgres as checkpoint_fixture

from zekam.application.governance import EffectRequest, GovernanceService, default_capabilities
from zekam.application.resume_apply_service import ResumeApplyService
from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind, DispatchOutcome, DispatchResult
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resume_apply import ResumeApplyRequest, ResumeApplyState
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.resume_repository import ResumeRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class RecordingAdapter:
    descriptor = ClientDescriptor(
        kind=ClientKind.CODEX,
        client_id="codex",
        executable="recording-codex",
        capabilities=frozenset({"structured-result", "cancellation"}),
    )

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def dispatch(self, request: Any, *, cwd: Path, permit: Any) -> DispatchResult:
        permit.assert_valid(request)
        self.requests.append(request)
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.SUCCESS,
            exit_code=0,
            payload={"status": "ok"},
        )


class EnvironmentGuard:
    def assert_envelope_current(self, envelope_id, *, now):  # type: ignore[no-untyped-def]
        return SimpleNamespace(id=envelope_id, captured_at=now)


@pytest.mark.parametrize(
    ("assignment_risk", "expected_state"),
    (("medium", ResumeApplyState.COMPLETED), ("high", ResumeApplyState.RECOVERY_REQUIRED)),
)
def test_real_resume_apply_safe_continue_and_replay(
    realm_session: tuple[Any, Any],
    tmp_path: Path,
    assignment_risk: str,
    expected_state: ResumeApplyState,
) -> None:
    observed: dict[str, Any] = {}

    def consume(values: dict[str, Any]) -> None:
        connection = values["connection"]
        realm = values["realm"]
        now: dt.datetime = values["now"]
        claim_two = values["claim_two"]
        result_digest = digest("reconciled-effect")
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.effect_receipt"
                "(id,realm_id,claim_id,status,result_digest)"
                " values(%s,%s,%s,'completed',%s)",
                (values["receipt_two"], realm.id, claim_two, result_digest),
            )
            cursor.execute(
                "update runtime.job set state='failed' where id=%s",
                (values["job_id"],),
            )
            replacement_job_id = uuid4()
            replacement_assignment_id = uuid4()
            instruction_digest = digest("instruction:resume-builder:build")
            assignment_digest = digest(
                {
                    "id": str(replacement_assignment_id),
                    "realm_id": str(realm.id),
                    "project_id": str(values["project"].id),
                    "work_item_id": str(values["work"].id),
                    "plan_id": str(values["plan"].id),
                    "step_id": "build",
                    "parent_assignment_id": str(values["coordinator_id"]),
                    "role": "builder",
                    "agent_ref": "resume-builder",
                    "risk": assignment_risk,
                    "instruction_digest": instruction_digest,
                    "context_manifest_digest": values["manifest"].manifest_digest,
                    "read_resources": [],
                    "write_resources": [],
                }
            )
            cursor.execute(
                "insert into agents.assignment"
                "(id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
                "agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                "(%s,%s,%s,%s,%s,'build',%s,'builder','resume-builder','active',%s,"
                "%s,%s,%s,%s)",
                (
                    replacement_assignment_id,
                    realm.id,
                    values["project"].id,
                    values["work"].id,
                    values["plan"].id,
                    values["coordinator_id"],
                    assignment_risk,
                    instruction_digest,
                    values["manifest"].manifest_digest,
                    assignment_digest,
                    now,
                ),
            )
            if assignment_risk == "high":
                stale_verifier_invocation = uuid4()
                stale_identity = f"stale-verifier:{replacement_job_id}"
                cursor.execute(
                    "insert into agents.invocation"
                    "(id,realm_id,assignment_id,client_id,execution_identity,"
                    "invocation_digest,created_at) values(%s,%s,%s,'codex',%s,%s,%s)",
                    (
                        stale_verifier_invocation,
                        realm.id,
                        values["verifier_id"],
                        stale_identity,
                        digest(
                            {
                                "id": str(stale_verifier_invocation),
                                "realm_id": str(realm.id),
                                "assignment_id": str(values["verifier_id"]),
                                "client_id": "codex",
                                "execution_identity": stale_identity,
                            }
                        ),
                        now,
                    ),
                )
                cursor.execute(
                    "insert into agents.result_receipt"
                    "(realm_id,assignment_id,invocation_id,envelope_digest,created_at)"
                    " values(%s,%s,%s,%s,%s)",
                    (
                        realm.id,
                        values["verifier_id"],
                        stale_verifier_invocation,
                        digest("historical-unbound-verifier-result"),
                        now,
                    ),
                )
            cursor.execute(
                "insert into runtime.job"
                "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,attempt_count,"
                "idempotency_key,assignment_id,run_id,available_at) values"
                "(%s,%s,%s,%s,%s,'build','mutation','ready',0,%s,%s,%s,%s)",
                (
                    replacement_job_id,
                    realm.id,
                    values["project"].id,
                    values["work"].id,
                    values["plan"].id,
                    f"resume-replacement-{replacement_job_id}",
                    replacement_assignment_id,
                    values["run"].id,
                    now,
                ),
            )
        connection.commit()
        apply_at = now
        prepared = ResumeCoordinator(ResumeRepository(connection, realm.id)).prepare(
            values["work"].id, client_id="codex", observed_at=apply_at
        )
        assert prepared.disposition.value == "safe-continue"
        assert prepared.next_step_id == "build"

        actor = ActorRepository(connection, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="apply-e2e", now=apply_at)
        )
        governance = GovernanceService(connection, realm, actor_id=actor.id)
        for capability in default_capabilities(realm.id):
            governance.capabilities.append(capability)
        governance.ensure_default_policy(now=apply_at)
        effect = EffectRequest(
            action="resume.apply.next-step",
            effects=(EffectKind.DATABASE_WRITE, EffectKind.PROCESS_RUN),
            resources=tuple(
                sorted(set(prepared.logical_read_resources + prepared.logical_write_resources))
            ),
            required_capabilities=("database.write", "process.run"),
        )
        authorization = Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=prepared.work_item_id,
            plan_id=values["plan"].id,
            plan_digest=values["plan"].plan_digest,
            effect_digest=effect.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=effect.resources,
                allowed_effects=tuple(item.value for item in effect.effects),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=3),
            now=apply_at,
        )
        AuthorizationRepository(connection, realm.id).issue(authorization)
        adapter = RecordingAdapter()
        request = ResumeApplyRequest(
            plan=prepared,
            supplied_plan_digest=prepared.plan_digest,
            actor_id=actor.id,
            authorization_id=authorization.id,
            worker_label="resume-e2e",
            capabilities=("database.write", "process.run", "sandbox.write"),
            lease_seconds=60,
        )
        service = ResumeApplyService(connection, governance, EnvironmentGuard())
        first = service.apply(request, adapter, cwd=tmp_path, timeout_seconds=30, now=apply_at)
        replay = service.apply(request, adapter, cwd=tmp_path, timeout_seconds=30, now=apply_at)
        assert first.state is expected_state
        assert replay.event_digest == first.event_digest
        assert len(adapter.requests) == 1
        with connection.cursor() as cursor:
            cursor.execute(
                "select state,attempt_count,fencing_token from runtime.job where id=%s",
                (prepared.runtime.job_id,),
            )
            job_state = tuple(cursor.fetchone())
            cursor.execute(
                "select outcome from runtime.job_attempt where job_id=%s",
                (prepared.runtime.job_id,),
            )
            attempt_outcome = str(cursor.fetchone()[0])
            cursor.execute(
                "select count(*) from runtime.lease where job_id=%s",
                (prepared.runtime.job_id,),
            )
            lease_count = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*),count(*) filter(where state='consumed')"
                " from security.authorization where id=%s",
                (authorization.id,),
            )
            auth_counts = tuple(int(value) for value in cursor.fetchone())
            cursor.execute(
                "select count(*),count(distinct claim_id),count(distinct receipt_id),"
                " array_agg(state order by sequence) from runtime.resume_apply_event"
                " where resume_apply_id=%s",
                (first.apply_id,),
            )
            event_row = cursor.fetchone()
            cursor.execute(
                "select count(*) from runtime.effect_claim where idempotency_key=%s",
                (f"resume:{first.apply_id}:dispatch",),
            )
            claim_count = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*) from runtime.effect_receipt er join runtime.effect_claim cl"
                " on cl.id=er.claim_id where cl.idempotency_key=%s",
                (f"resume:{first.apply_id}:dispatch",),
            )
            receipt_count = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*) from runtime.execution_envelope"
                " where idempotency_key=%s and checkpoint_id is null"
                " and checkpoint_v2_id is not null",
                (f"resume:{first.apply_id}",),
            )
            envelope_count = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*),bool_and(work.validate_checkpoint_v2(realm_id,id)),"
                "bool_and(cardinality(pending_steps)=0) from work.checkpoint_v2"
                " where job_id=%s and attempt_id in"
                " (select id from runtime.job_attempt where job_id=%s)",
                (prepared.runtime.job_id, prepared.runtime.job_id),
            )
            checkpoint_evidence = tuple(cursor.fetchone())
        expected_job_state = (
            "completed" if expected_state is ResumeApplyState.COMPLETED else "recovery-required"
        )
        expected_attempt = (
            "succeeded" if expected_state is ResumeApplyState.COMPLETED else "recovery-required"
        )
        assert job_state[0] == expected_job_state
        assert int(job_state[1]) == 1
        assert int(job_state[2]) == 1
        assert attempt_outcome == expected_attempt
        assert lease_count == 0
        assert auth_counts == (1, 1)
        expected_receipt_events = 1 if expected_state is ResumeApplyState.COMPLETED else 0
        assert tuple(event_row[:3]) == (3, 1, expected_receipt_events)
        assert list(event_row[3]) == ["claimed", "dispatched", expected_state.value]
        assert claim_count == 1
        assert receipt_count == 1
        assert envelope_count == 1
        if expected_state is ResumeApplyState.COMPLETED:
            assert checkpoint_evidence == (1, True, True)
        else:
            assert checkpoint_evidence == (0, None, None)
        observed["completed"] = True

    checkpoint_fixture.E2E_FIXTURE_CONSUMER = consume
    try:
        checkpoint_fixture.test_checkpoint_v2_evidence_revision_and_terminal_gate(
            realm_session, tmp_path
        )
    finally:
        checkpoint_fixture.E2E_FIXTURE_CONSUMER = None
    assert observed == {"completed": True}
