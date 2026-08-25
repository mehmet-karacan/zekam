from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from zekam.application.context_materializer import FragmentMaterialization, materialize_fragments
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, compile_context
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.memory import (
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)
from zekam.domain.model_invocation import GatewayMode, GatewaySourceLabel, ModelRequestManifest
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.runtime import EffectClaim, EffectReceipt
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.memory_repository import MemoryRepository
from zekam.infrastructure.postgres.memory_telemetry_repository import MemoryTelemetryRepository
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.runtime_repository import EffectLedger
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
D = "sha256:" + "a" * 64


@pytest.fixture
def invocation_scope(realm_session, tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "source"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    work_id, plan_id, run_id, assignment_id, job_id, attempt_id = (uuid4() for _ in range(6))
    now = dt.datetime.now(dt.UTC)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item(id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','gateway test',%s)",
            (work_id, realm.id, project.id, D),
        )
        cursor.execute(
            "insert into work.task_plan"
            " (id,realm_id,project_id,work_item_id,revision,source_revision,policy_digest,"
            "steps,effect_digest,plan_digest,created_at) values"
            " (%s,%s,%s,%s,1,'gateway-revision',%s,"
            ' \'[{"step_id":"invoke","effect":"provider-call"}]\'::jsonb,%s,%s,%s)',
            (plan_id, realm.id, project.id, work_id, D, digest("plan-effect"), digest("plan"), now),
        )
        cursor.execute(
            "insert into runtime.execution_run"
            " (id,realm_id,project_id,work_item_id,plan_id,client_id,source_revision,"
            "policy_digest,max_input_tokens,max_output_tokens,max_cost_micros,deadline,state,"
            "run_digest,created_at,started_at) values"
            " (%s,%s,%s,%s,%s,'codex','gateway-revision',%s,100,20,1000,%s,'active',%s,%s,%s)",
            (
                run_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                D,
                now + dt.timedelta(minutes=5),
                digest("run"),
                now,
                now,
            ),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,role,agent_ref,status,risk,"
            " instruction_digest,context_manifest_digest,assignment_digest,created_at)"
            " values(%s,%s,%s,%s,%s,'invoke','coordinator','gateway','active','medium',"
            "%s,%s,%s,now())",
            (
                assignment_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                D,
                D,
                digest({"id": str(assignment_id)}),
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,idempotency_key,"
            "assignment_id,run_id) values(%s,%s,%s,%s,%s,'invoke','provider-call','running',"
            "%s,%s,%s)",
            (
                job_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                f"gateway-{job_id}",
                assignment_id,
                run_id,
            ),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label)"
            " values(%s,%s,%s,1,1,'gateway-test')",
            (attempt_id, realm.id, job_id),
        )
    return (
        realm,
        connection,
        project.id,
        work_id,
        assignment_id,
        job_id,
        attempt_id,
        plan_id,
        run_id,
    )


def _manifest(scope, **changes):  # type: ignore[no-untyped-def]
    realm, _, project_id, work_id, assignment_id, job_id, attempt_id, plan_id, run_id = scope
    now = dt.datetime.now(dt.UTC)
    values = {
        "realm_id": realm.id,
        "project_id": project_id,
        "work_item_id": work_id,
        "plan_id": plan_id,
        "step_id": "invoke",
        "execution_envelope_id": None,
        "execution_envelope_digest": None,
        "run_id": run_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "assignment_id": assignment_id,
        "role": "builder",
        "risk": "medium",
        "route_decision_digest": D,
        "model_id": "provider/model",
        "provider_ref": "provider:x",
        "context_manifest_digest": D,
        "context_fragment_set_digest": None,
        "model_visible_payload_digest": None,
        "context_packet_digest": D,
        "checkpoint_digest": D,
        "source_revision": "abc123",
        "policy_digest": D,
        "payload_digest": D,
        "authorization_scope_digest": D,
        "output_schema_digest": D,
        "idempotency_key": f"request-{uuid4()}",
        "max_input_tokens": 100,
        "max_output_tokens": 20,
        "max_cost_micros": 1000,
        "deadline": now + dt.timedelta(minutes=5),
        "route_expires_at": now + dt.timedelta(minutes=5),
        "created_at": now,
        "source_label": GatewaySourceLabel.MODEL_CAPABILITY,
        "missing_bindings": (
            "context_fragment_set_digest",
            "execution_envelope_digest",
            "execution_envelope_id",
            "model_visible_payload_digest",
        ),
    }
    values.update(changes)
    return ModelRequestManifest.create(**values)


def test_manifest_idempotency_append_only_and_default_audit(invocation_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    repository = ModelInvocationRepository(connection, realm.id)
    item = _manifest(invocation_scope)
    assert repository.mode() is GatewayMode.AUDIT
    assert repository.store_manifest(item) == (item.id, True)
    assert repository.store_manifest(item) == (item.id, False)
    with connection.cursor() as cursor:
        cursor.execute(
            "select context_fragment_set_digest,model_visible_payload_digest,payload_digest"
            " from models.request_manifest where realm_id=%s and id=%s",
            (realm.id, item.id),
        )
        assert cursor.fetchone() == (None, None, D)
    with pytest.raises(InsufficientPrivilege), connection.cursor() as cursor:
        cursor.execute(
            "update models.request_manifest set model_id='other' where id=%s", (item.id,)
        )


def test_repeated_semantic_model_attempt_loop_admission_olmadan_reddedilir(
    invocation_scope,
) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    repository = ModelInvocationRepository(connection, realm.id)
    first = _manifest(invocation_scope)
    second = _manifest(invocation_scope)
    repository.store_manifest(first)
    repository.store_manifest(second)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.invocation_attempt"
            " (id,realm_id,manifest_id,ordinal,state) values(%s,%s,%s,1,'prepared')",
            (uuid4(), realm.id, first.id),
        )
    with (
        pytest.raises(Exception, match="repeated semantic model dispatch"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into models.invocation_attempt"
            " (id,realm_id,manifest_id,ordinal,state) values(%s,%s,%s,1,'prepared')",
            (uuid4(), realm.id, second.id),
        )


def test_manifest_rejects_nonexistent_canonical_fragment_set_binding(
    invocation_scope,
) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    item = _manifest(
        invocation_scope,
        context_fragment_set_digest=D,
        model_visible_payload_digest=D,
        missing_bindings=("execution_envelope_digest", "execution_envelope_id"),
    )
    with pytest.raises(CheckViolation):
        ModelInvocationRepository(connection, realm.id).store_manifest(item)


def test_manifest_rejects_canonical_fragment_set_from_another_work(
    invocation_scope,
) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, *_ = invocation_scope
    other_work_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item(id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','other work',%s)",
            (other_work_id, realm.id, project_id, D),
        )
    content = "cross work context"
    candidate = ContextCandidate(
        "cross-work",
        AuthorityLevel.VERIFIED,
        dt.datetime.now(dt.UTC),
        "revision/cross-work",
        digest(content),
        3,
        True,
    )
    manifest = compile_context(
        (candidate,),
        token_budget=10,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=candidate.observed_at,
    )
    fragment_set = materialize_fragments(
        manifest,
        (candidate,),
        (
            FragmentMaterialization(
                "cross-work",
                ContextContentKind.WORK_CONTEXT,
                ContextRole.USER,
                ContextVisibility.MODEL,
                "work/cross-work",
                content,
            ),
        ),
    )
    context_repository = ContextContinuityRepository(
        connection, realm.id, project_id, other_work_id
    )
    context_repository.store_manifest(manifest)
    context_repository.store_fragment_set(fragment_set, created_at=candidate.observed_at)
    item = _manifest(
        invocation_scope,
        context_manifest_digest=manifest.manifest_digest,
        context_fragment_set_digest=fragment_set.fragment_set_digest,
        model_visible_payload_digest=D,
        missing_bindings=("execution_envelope_digest", "execution_envelope_id"),
    )
    with pytest.raises(CheckViolation):
        ModelInvocationRepository(connection, realm.id).store_manifest(item)


def test_enforce_activation_requires_zero_unbound_or_bypass(invocation_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    repository = ModelInvocationRepository(connection, realm.id)
    repository.record_audit(
        source_label=GatewaySourceLabel.MODEL_CAPABILITY.value,
        disposition="bypass",
        call_digest=D,
        payload_digest=D,
    )
    with pytest.raises(CheckViolation):
        repository.activate_enforce(D)
    assert repository.mode() is GatewayMode.AUDIT


def test_legacy_unbound_manifest_persists_without_forged_identity(invocation_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    missing = (
        "assignment_id",
        "authorization_scope_digest",
        "checkpoint_digest",
        "context_fragment_set_digest",
        "context_manifest_digest",
        "context_packet_digest",
        "execution_envelope_digest",
        "execution_envelope_id",
        "model_visible_payload_digest",
        "output_schema_digest",
        "policy_digest",
        "route_decision_digest",
        "route_expires_at",
        "run_id",
    )
    item = _manifest(
        invocation_scope,
        run_id=None,
        execution_envelope_id=None,
        execution_envelope_digest=None,
        assignment_id=None,
        route_decision_digest=None,
        route_expires_at=None,
        context_manifest_digest=None,
        context_fragment_set_digest=None,
        model_visible_payload_digest=None,
        context_packet_digest=None,
        checkpoint_digest=None,
        policy_digest=None,
        authorization_scope_digest=None,
        output_schema_digest=None,
        missing_bindings=missing,
    )
    ModelInvocationRepository(connection, realm.id).store_manifest(item)
    with connection.cursor() as cursor:
        cursor.execute(
            "select binding_status,run_id,assignment_id,missing_bindings"
            " from models.request_manifest where id=%s",
            (item.id,),
        )
        row = cursor.fetchone()
    assert row == ("unbound", None, None, list(missing))


def test_enforce_activation_succeeds_for_clean_realm(invocation_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    repository = ModelInvocationRepository(connection, realm.id)
    repository.activate_enforce(D)
    assert repository.mode() is GatewayMode.ENFORCE


def test_enforce_activation_rejects_bound_audit_without_manifest(invocation_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, *_ = invocation_scope
    repository = ModelInvocationRepository(connection, realm.id)
    repository.record_audit(
        source_label=GatewaySourceLabel.MODEL_CAPABILITY.value,
        disposition="bound",
        call_digest=D,
        payload_digest=D,
    )
    with pytest.raises(CheckViolation):
        repository.activate_enforce(D)


def test_concurrent_manifest_replay_is_singleton(invocation_scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    realm, _, *_ = invocation_scope
    item = _manifest(invocation_scope)

    def store():  # type: ignore[no-untyped-def]
        with connect(migrated_database) as worker:
            configure_session(worker, realm_id=realm.id)
            return ModelInvocationRepository(worker, realm.id).store_manifest(item)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: store(), range(2)))
    assert sorted(created for _, created in outcomes) == [False, True]


def test_verified_invocation_records_only_model_visible_memory_usage(
    invocation_scope,
) -> None:  # type: ignore[no-untyped-def]
    (
        realm,
        connection,
        project_id,
        work_id,
        _,
        job_id,
        runtime_attempt_id,
        plan_id,
        _,
    ) = invocation_scope
    now = dt.datetime.now(dt.UTC)
    with connection.cursor() as cursor:
        cursor.execute("select slug from projects.project where id=%s", (project_id,))
        project_ref = str(cursor.fetchone()[0])

    content = "Kanonik bellek yalniz model baglamina girdiginde kullanilmis sayilir"
    record = MemoryRecord(
        memory_id="usage-family",
        key=MemoryKey(MemoryScope.PROJECT, realm.slug, project_ref=project_ref),
        memory_class=MemoryClass.SEMANTIC,
        content=content,
        state=MemoryState.ACTIVE,
        revision=1,
        created_at=now - dt.timedelta(minutes=2),
        evidence=(MemoryEvidence("test", "tests/memory-usage", digest("evidence")),),
        reviewed_by="verifier-b",
        author_ref="builder-a",
        valid_from=now - dt.timedelta(minutes=2),
    )
    record_id = MemoryRepository(
        connection, realm.id, realm.slug, project_id, project_ref
    ).store_record(record)

    candidate = ContextCandidate(
        "memory-usage",
        AuthorityLevel.VERIFIED,
        now,
        record.record_digest,
        digest(content),
        9,
        True,
    )
    context_manifest = compile_context(
        (candidate,), token_budget=20, minimum_authority=AuthorityLevel.OBSERVED, now=now
    )
    fragment_set = materialize_fragments(
        context_manifest,
        (candidate,),
        (
            FragmentMaterialization(
                "memory-usage",
                ContextContentKind.MEMORY,
                ContextRole.USER,
                ContextVisibility.MODEL,
                f"memory-record/{record_id}",
                content,
            ),
        ),
    )
    context_repository = ContextContinuityRepository(connection, realm.id, project_id, work_id)
    context_repository.store_manifest(context_manifest)
    context_repository.store_fragment_set(fragment_set, created_at=now)

    request = _manifest(
        invocation_scope,
        context_manifest_digest=context_manifest.manifest_digest,
        context_fragment_set_digest=fragment_set.fragment_set_digest,
        model_visible_payload_digest=D,
        missing_bindings=("execution_envelope_digest", "execution_envelope_id"),
    )
    invocation_repository = ModelInvocationRepository(connection, realm.id)
    invocation_repository.store_manifest(request)

    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="usage-operator", now=now)
    )
    effect_digest = digest("provider-effect")
    authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=work_id,
        plan_id=plan_id,
        plan_digest=digest("plan"),
        effect_digest=effect_digest,
        scope=AuthorizationScope(allowed_effects=("provider-call",), provider_refs=("provider:x",)),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        now=now,
    )
    AuthorizationRepository(connection, realm.id).issue(authorization)
    claim = EffectClaim.create(
        realm_id=realm.id,
        job_id=job_id,
        attempt_id=runtime_attempt_id,
        operation="provider.invoke",
        effect_digest=effect_digest,
        authorization_digest=authorization.authorization_digest,
        idempotency_key=f"usage-provider-{uuid4()}",
        resources=(),
        execution_identity="usage-test:1",
        fencing_token=1,
        adapter_digest=digest("adapter"),
        now=now,
    )
    ledger = EffectLedger(connection, realm.id)
    ledger.claim(claim, authorization_id=authorization.id)
    receipt = EffectReceipt.completed(
        realm_id=realm.id, claim=claim, result_digest=digest("provider-result"), now=now
    )
    ledger.receipt(receipt)
    assert (
        AuthorizationRepository(connection, realm.id)
        .consume(
            authorization.id,
            effect_digest=effect_digest,
            consumed_by="model-gateway",
            now=now,
        )
        .consumed
    )
    ledger_attempt_id = invocation_repository.record_attempt(
        manifest_id=request.id,
        effect_claim_id=claim.id,
        authorization_id=authorization.id,
    )

    with connection.cursor() as cursor:
        cursor.execute("select count(*) from memory.usage_event")
        assert int(cursor.fetchone()[0]) == 0
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into models.invocation_result"
            " (id,realm_id,manifest_id,attempt_id,effect_receipt_id,state,response_digest)"
            " values(%s,%s,%s,%s,%s,'verified',%s)",
            (
                uuid4(),
                realm.id,
                request.id,
                ledger_attempt_id,
                receipt.id,
                digest("forged-response"),
            ),
        )
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from memory.usage_event")
        assert int(cursor.fetchone()[0]) == 0
    invocation_repository.record_result(
        manifest_id=request.id,
        attempt_id=ledger_attempt_id,
        effect_receipt_id=receipt.id,
        state="verified",
        response_digest=receipt.result_digest,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select record_id,request_manifest_id,invocation_attempt_id,record_digest"
            " from memory.usage_event"
        )
        usage = cursor.fetchone()
        assert usage == (record_id, request.id, ledger_attempt_id, record.record_digest)
        cursor.execute("select last_used_at from memory.record where id=%s", (record_id,))
        assert cursor.fetchone()[0] is not None
        cursor.execute(
            "select usage_count,verified_outcome_count from memory.usage_effectiveness"
            " where record_id=%s",
            (record_id,),
        )
        assert cursor.fetchone() == (1, 0)
    telemetry = MemoryTelemetryRepository(connection, realm.id)
    assert len(telemetry.usage_for_record(record_id)) == 1
    assert telemetry.effectiveness(record_id) is not None
    assert telemetry.outcomes_for_record(record_id) == ()
    with pytest.raises(InsufficientPrivilege), connection.cursor() as cursor:
        cursor.execute("update memory.record set last_used_at=now() where id=%s", (record_id,))
    connection.rollback()


def test_non_model_visible_memory_fragment_never_becomes_usage(
    invocation_scope,
) -> None:  # type: ignore[no-untyped-def]
    # The capture query has an explicit model-visible predicate; assert the schema-level
    # evidence path rather than treating a retrieval hit or runtime-only fragment as usage.
    realm, connection, *_ = invocation_scope
    with connection.cursor() as cursor:
        cursor.execute(
            "select pg_get_functiondef('memory.capture_verified_invocation_usage()'::regprocedure)"
        )
        definition = str(cursor.fetchone()[0])
    assert "f.visibility = 'model-visible'" in definition
    assert "f.content_kind = 'memory'" in definition
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from memory.usage_event where realm_id=%s", (realm.id,))
        assert int(cursor.fetchone()[0]) == 0
