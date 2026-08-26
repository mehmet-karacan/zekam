from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleClientContract,
    LifecycleRequest,
)
from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.hook_runtime import (
    HookAdapterResult,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookResultKind,
    HookRuntimeRevision,
    HookSpecRevision,
)
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import DataClassification

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC)


class TransactionConnection:
    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


@dataclass(frozen=True)
class Stage:
    event_id: UUID
    outbox_id: UUID
    created: bool


class FakeRepository:
    def __init__(self, operations: list[str]) -> None:
        self.connection = TransactionConnection()
        self.operations = operations
        self.finalized: tuple[UUID, str, str] | None = None
        self.snapshot = SimpleNamespace(
            compaction_receipt_digest=None,
            close_receipt_digest=None,
        )

    def stage_lifecycle_delivery(self, event, *, idempotency_key, plan_digest):  # type: ignore[no-untyped-def]
        self.operations.append("stage")
        assert event.event_digest and idempotency_key and plan_digest
        return Stage(event.event_id, uuid4(), True)

    def finalize_lifecycle_delivery(
        self, *, outbox_id: UUID, receipt_digest: str, status: str, completed_at: dt.datetime
    ) -> None:
        self.operations.append("finalize")
        self.finalized = (outbox_id, receipt_digest, status)

    def read_session_snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["session_id"] == "session-one"
        return self.snapshot


@dataclass(frozen=True)
class ConsumeResult:
    consumed: bool
    reason: str = "consumed"


class FakeAuthorizations:
    def __init__(self) -> None:
        self.authorization: Authorization | None = None
        self.consume_calls = 0

    def get(self, authorization_id: UUID) -> Authorization:
        assert self.authorization is not None and authorization_id == self.authorization.id
        return self.authorization

    def consume(self, authorization_id: UUID, *, effect_digest: str, consumed_by: str, now=None):  # type: ignore[no-untyped-def]
        assert self.authorization is not None
        assert authorization_id == self.authorization.id
        assert effect_digest == self.authorization.effect_digest
        assert consumed_by == "client-lifecycle-bridge/v1"
        self.consume_calls += 1
        return ConsumeResult(True)


class FakeHookOutcomes:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    def record_outcome(self, **kwargs: Any) -> tuple[UUID, UUID]:
        self.operations.append("hook-receipt")
        assert kwargs["outcome"].status == "completed"
        return uuid4(), uuid4()


def _configured_runtime(realm_id: UUID) -> tuple[HookRuntime, Any]:
    profile = PermissionProfileRevision.from_flags(
        realm_id=realm_id,
        name="hook-readonly",
        revision=1,
        permission_flags={
            "filesystem.read": True,
            "filesystem.write": False,
            "network.access": False,
            "process.run": False,
        },
        managed=True,
        created_at=NOW,
    )
    input_schema = {
        "type": "object",
        "properties": {
            "lifecycle": {"type": "object"},
            "data": {
                "type": "object",
                "properties": {"checkpoint_digest": {"type": "string"}},
                "required": ["checkpoint_digest"],
                "additionalProperties": False,
            },
        },
        "required": ["lifecycle", "data"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"ack_digest": {"type": "string"}},
        "required": ["ack_digest"],
        "additionalProperties": False,
    }
    spec = HookSpecRevision.create(
        realm_id=realm_id,
        hook_id="opencode-pre-compaction",
        revision=1,
        event_type=HookEventType.PRE_COMPACTION,
        required=True,
        source_layer="managed-policy",
        timeout_ms=500,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=input_schema,
        output_schema=output_schema,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=HookFailurePolicy.ABORT,
        created_at=NOW,
    )
    revision = HookRuntimeRevision.create(
        realm_id=realm_id,
        hook_id=spec.hook_id,
        hook_revision=1,
        adapter_ref="opencode-pre-compaction-v1",
        adapter_digest=digest("opencode-pre-compaction-v1"),
        permission_capabilities=("filesystem.read",),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    adapter = LoadedHookAdapter(
        "opencode-pre-compaction-v1",
        digest("opencode-pre-compaction-v1"),
        HookExecutionMode.INTERNAL,
        lambda payload: HookAdapterResult(
            HookResultKind.OBSERVATION,
            {"ack_digest": digest(payload["data"]["checkpoint_digest"])},
        ),
    )
    runtime = HookRuntime()
    runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("config"),
        specs=(spec,),
        runtimes=(revision,),
        profiles=(profile,),
        adapters=(adapter,),
        now=NOW,
        required_events=(HookEventType.PRE_COMPACTION,),
    )
    return runtime, runtime.start_session()


def _descriptor() -> ClientDescriptor:
    return ClientDescriptor(
        ClientKind.OPENCODE,
        "opencode-local",
        "opencode.exe",
        frozenset({"chat", "structured-result", "lifecycle-events-v2"}),
        version="1.0.0-reviewed",
    )


def _contract() -> LifecycleClientContract:
    return LifecycleClientContract.verified(
        descriptor=_descriptor(),
        installed_version="1.0.0-reviewed",
        event_mapping=(("session.compacting", HookEventType.PRE_COMPACTION),),
        contract_evidence_digest=digest("official-opencode-contract"),
    )


def _request(realm_id: UUID, **changes: Any) -> LifecycleRequest:
    values: dict[str, Any] = {
        "realm_id": realm_id,
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "run_id": uuid4(),
        "session_id": "session-one",
        "client_id": "opencode-local",
        "event_id": uuid4(),
        "external_event_type": "session.compacting",
        "sequence": 1,
        "previous_digest": None,
        "origin": "client:opencode-local",
        "causation_id": "client-event:one",
        "correlation_id": "work-run:one",
        "recursion_depth": 0,
        "max_recursion_depth": 3,
        "source_revision": "git:b8d970c",
        "work_plan_ref": "work-plan:revision-3",
        "checkpoint_ref": "checkpoint:draft-1",
        "context_ref": "context:bounded-1",
        "metadata": (),
        "classification": DataClassification.INTERNAL,
        "payload": {"checkpoint_digest": digest("checkpoint")},
        "idempotency_key": "opencode:session-one:pre-compaction:1",
        "occurred_at": NOW,
        "ingested_at": NOW,
    }
    values.update(changes)
    return LifecycleRequest(**values)


def test_opencode_pre_compaction_maps_to_common_bridge_and_requires_finalize() -> None:
    realm_id = uuid4()
    runtime, session = _configured_runtime(realm_id)
    operations: list[str] = []
    repository = FakeRepository(operations)
    authorizations = FakeAuthorizations()
    bridge = ClientLifecycleBridge(
        runtime, repository, authorizations, FakeHookOutcomes(operations)
    )
    request = _request(realm_id)
    plan = bridge.prepare(
        request,
        _contract(),
        session,
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
    )
    replay_plan = bridge.prepare(
        request,
        _contract(),
        session,
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
    )
    assert replay_plan.plan_digest == plan.plan_digest
    authorizations.authorization = Authorization.issue(
        realm_id=realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,), allowed_effects=("database-write",)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )

    applied = bridge.apply(
        plan,
        session,
        session_binding_id=uuid4(),
        authorization_id=authorizations.authorization.id,
        current_source_digest=digest("source"),
        current_policy_digest=digest("policy"),
        current_migration_digest=digest("migration"),
        now=NOW,
    )

    assert plan.event.event_type == "pre_compaction"
    assert operations == ["stage", "hook-receipt"]
    assert applied.status == "awaiting-finalization" and applied.terminal is False
    assert applied.result_digest.startswith("sha256:")
    with pytest.raises(PolicyViolation, match="canonical receipt"):
        bridge.finalize(
            applied,
            receipt_digest=digest("compaction-receipt"),
            status="completed",
            completed_at=NOW,
        )
    repository.snapshot.compaction_receipt_digest = digest("compaction-receipt")
    finalized = bridge.finalize(
        applied,
        receipt_digest=digest("compaction-receipt"),
        status="completed",
        completed_at=NOW,
    )
    assert operations == ["stage", "hook-receipt", "finalize"]
    assert finalized.terminal and finalized.grants_authority is False
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_unsupported_client_and_internal_recursion_are_visible_fail_closed() -> None:
    realm_id = uuid4()
    runtime, session = _configured_runtime(realm_id)
    bridge = ClientLifecycleBridge(
        runtime, FakeRepository([]), FakeAuthorizations(), FakeHookOutcomes([])
    )
    unsupported = LifecycleClientContract.unsupported(
        descriptor=_descriptor(), reason="official-contract-not-verified"
    )
    decision = bridge.check(_request(realm_id), unsupported)
    assert not decision.allowed and "client-unsupported" in decision.reason

    recursion = bridge.check(
        _request(
            realm_id,
            origin="zekam-internal",
            recursion_depth=4,
            max_recursion_depth=3,
        ),
        _contract(),
    )
    assert not recursion.allowed and recursion.reason == "recursion-depth-quarantine"
    forged_origin = bridge.check(_request(realm_id, origin="client:another-instance"), _contract())
    assert not forged_origin.allowed and forged_origin.reason == "origin-client-binding-invalid"
    with pytest.raises(PolicyViolation, match="recursion-depth-quarantine"):
        bridge.prepare(
            _request(
                realm_id,
                origin="zekam-internal",
                recursion_depth=4,
                max_recursion_depth=3,
            ),
            _contract(),
            session,
            source_digest=digest("source"),
            policy_digest=digest("policy"),
            migration_digest=digest("migration"),
        )
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_apply_revalidates_drift_and_exact_authorization_before_stage() -> None:
    realm_id = uuid4()
    runtime, session = _configured_runtime(realm_id)
    operations: list[str] = []
    authorizations = FakeAuthorizations()
    bridge = ClientLifecycleBridge(
        runtime, FakeRepository(operations), authorizations, FakeHookOutcomes(operations)
    )
    plan = bridge.prepare(
        _request(realm_id),
        _contract(),
        session,
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
    )
    wrong = Authorization.issue(
        realm_id=realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=digest("wrong-effect"),
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,), allowed_effects=("database-write",)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.authorization = wrong
    with pytest.raises(AuthorizationRequired, match="exact authorization"):
        bridge.apply(
            plan,
            session,
            session_binding_id=uuid4(),
            authorization_id=wrong.id,
            current_source_digest=digest("source"),
            current_policy_digest=digest("policy"),
            current_migration_digest=digest("migration"),
            now=NOW,
        )
    assert operations == [] and authorizations.consume_calls == 0

    with pytest.raises(PolicyViolation, match="binding drift"):
        bridge.apply(
            plan,
            session,
            session_binding_id=uuid4(),
            authorization_id=wrong.id,
            current_source_digest=digest("changed-source"),
            current_policy_digest=digest("policy"),
            current_migration_digest=digest("migration"),
            now=NOW,
        )
    assert operations == [] and authorizations.consume_calls == 0
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_content_safe_telemetry_rejects_raw_transcript_fields() -> None:
    realm_id = uuid4()
    runtime, session = _configured_runtime(realm_id)
    bridge = ClientLifecycleBridge(
        runtime, FakeRepository([]), FakeAuthorizations(), FakeHookOutcomes([])
    )
    decision = bridge.check(
        _request(realm_id, payload={"raw_transcript": "hostile directive"}), _contract()
    )
    assert not decision.allowed and "hassas alan" in decision.reason
    leaked = bridge.check(
        _request(realm_id, payload={"detail": "api_key=must-not-leak"}), _contract()
    )
    assert not leaked.allowed and "bounded" in leaked.reason
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)
