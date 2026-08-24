from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.model_invocation import GatewayMode, GatewaySourceLabel, ModelRequestManifest
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
D = "sha256:" + "a" * 64


@pytest.fixture
def invocation_scope(realm_session, tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "source"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    work_id, assignment_id, job_id, attempt_id = (uuid4() for _ in range(4))
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item(id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','gateway test',%s)",
            (work_id, realm.id, project.id, D),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,role,agent_ref,status,risk,"
            " instruction_digest,context_manifest_digest,assignment_digest,created_at)"
            " values(%s,%s,%s,%s,'coordinator','gateway','active','medium',%s,%s,%s,now())",
            (
                assignment_id,
                realm.id,
                project.id,
                work_id,
                D,
                D,
                digest({"id": str(assignment_id)}),
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,state,idempotency_key,assignment_id)"
            " values(%s,%s,%s,%s,'provider-call','running',%s,%s)",
            (job_id, realm.id, project.id, work_id, f"gateway-{job_id}", assignment_id),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label)"
            " values(%s,%s,%s,1,1,'gateway-test')",
            (attempt_id, realm.id, job_id),
        )
    return realm, connection, project.id, work_id, assignment_id, job_id, attempt_id


def _manifest(scope, **changes):  # type: ignore[no-untyped-def]
    realm, _, project_id, work_id, assignment_id, job_id, attempt_id = scope
    now = dt.datetime.now(dt.UTC)
    values = {
        "realm_id": realm.id,
        "project_id": project_id,
        "work_item_id": work_id,
        "plan_id": uuid4(),
        "step_id": "invoke",
        "execution_envelope_id": None,
        "execution_envelope_digest": None,
        "run_id": uuid4(),
        "job_id": job_id,
        "attempt_id": attempt_id,
        "assignment_id": assignment_id,
        "role": "builder",
        "risk": "medium",
        "route_decision_digest": D,
        "model_id": "provider/model",
        "provider_ref": "provider:x",
        "context_manifest_digest": D,
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
        "missing_bindings": ("execution_envelope_digest", "execution_envelope_id"),
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
    with pytest.raises(InsufficientPrivilege), connection.cursor() as cursor:
        cursor.execute(
            "update models.request_manifest set model_id='other' where id=%s", (item.id,)
        )


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
        "context_manifest_digest",
        "context_packet_digest",
        "execution_envelope_digest",
        "execution_envelope_id",
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
