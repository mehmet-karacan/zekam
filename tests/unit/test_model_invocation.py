from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zekam.application.diagnostic_trace import TraceWriteResult
from zekam.application.model_gateway import ModelGateway
from zekam.application.provider_adapter import ProviderCall, ProviderCallResult
from zekam.domain.canonical import digest
from zekam.domain.context_fragment import ModelVisiblePayloadBinding
from zekam.domain.diagnostic_trace import TraceEventType
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_invocation import (
    GatewayBindingStatus,
    GatewayMode,
    GatewaySourceLabel,
    ModelRequestManifest,
    _issue_gateway_permit,
)
from zekam.domain.tool_registry import CompiledToolSet

D = "sha256:" + "a" * 64


class _TraceSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, **values):  # type: ignore[no-untyped-def]
        self.events.append(values)
        return TraceWriteResult("recorded", uuid4(), uuid4(), digest(values["event_type"].value))


class _GatewayRepository:
    def __init__(self, mode: GatewayMode = GatewayMode.AUDIT) -> None:
        self.result: dict[str, object] | None = None
        self.mode_value = mode
        self.envelope_asserted = False
        self.fragment_set_asserted = False
        self.tool_set_asserted = False
        self.catalog_asserted = False
        self.attempt_values: dict[str, object] | None = None
        self.compiled_tools = CompiledToolSet.create(
            realm_id=uuid4(),
            role="builder",
            permission_profile_digest=D,
            entries=(),
            created_at=dt.datetime.now(dt.UTC),
        )

    def store_manifest(self, item: ModelRequestManifest) -> tuple[object, bool]:
        return item.id, True

    def record_audit(self, **values: object) -> object:
        return uuid4()

    def mode(self) -> GatewayMode:
        return self.mode_value

    def assert_current_envelope(self, item: ModelRequestManifest) -> None:
        self.envelope_asserted = True

    def assert_current_context_fragment_set(self, item: ModelRequestManifest) -> None:
        self.fragment_set_asserted = True

    def assert_current_tool_set(self, item: ModelRequestManifest) -> None:
        self.tool_set_asserted = True

    def assert_current_catalog(self, item: ModelRequestManifest) -> None:
        self.catalog_asserted = True

    def envelope_bindings(self, envelope_id):  # type: ignore[no-untyped-def]
        now = dt.datetime.now(dt.UTC)
        return {
            "execution_envelope_id": envelope_id,
            "execution_envelope_digest": D,
            "run_id": uuid4(),
            "role": "builder",
            "route_decision_digest": D,
            "catalog_provider_id": "provider",
            "catalog_digest": D,
            "catalog_snapshot_digest": D,
            "catalog_snapshot_id": uuid4(),
            "route_expires_at": now + dt.timedelta(minutes=5),
            "context_manifest_digest": D,
            "context_packet_digest": D,
            "checkpoint_digest": D,
            "source_revision": "abc123",
            "policy_digest": D,
            "output_schema_digest": D,
            "max_input_tokens": 100,
            "max_output_tokens": 20,
            "max_cost_micros": 1000,
            "deadline": now + dt.timedelta(minutes=4),
            "turn_execution_snapshot_digest": D,
            "environment_digest": D,
            "permission_profile_digest": D,
            "tool_set_digest": self.compiled_tools.tool_set_digest,
            "config_effective_digest": D,
            "hook_set_digest": D,
        }

    def record_attempt(self, **values: object) -> object:
        self.attempt_values = values
        return uuid4()

    def record_result(self, **values: object) -> object:
        self.result = values
        return uuid4()


class _EnvironmentGuard:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[object] = []

    def assert_envelope_current(self, envelope_id, *, now):  # type: ignore[no-untyped-def]
        self.calls.append((envelope_id, now))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=envelope_id, captured_at=now)


def _manifest(**changes):  # type: ignore[no-untyped-def]
    now = dt.datetime.now(dt.UTC)
    values = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "step_id": "build",
        "execution_envelope_id": uuid4(),
        "execution_envelope_digest": D,
        "run_id": uuid4(),
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "assignment_id": uuid4(),
        "role": "builder",
        "risk": "medium",
        "route_decision_digest": D,
        "catalog_provider_id": "provider",
        "catalog_digest": D,
        "catalog_snapshot_digest": D,
        "catalog_snapshot_id": uuid4(),
        "model_id": "provider/model",
        "provider_ref": "provider:x",
        "context_manifest_digest": D,
        "context_fragment_set_digest": D,
        "model_visible_payload_digest": D,
        "context_packet_digest": D,
        "checkpoint_digest": D,
        "source_revision": "abc123",
        "policy_digest": D,
        "payload_digest": D,
        "authorization_scope_digest": D,
        "output_schema_digest": D,
        "idempotency_key": "request-1",
        "max_input_tokens": 100,
        "max_output_tokens": 20,
        "max_cost_micros": 1000,
        "deadline": now + dt.timedelta(minutes=1),
        "route_expires_at": now + dt.timedelta(minutes=1),
        "created_at": now,
        "source_label": GatewaySourceLabel.MODEL_CAPABILITY,
        "turn_execution_snapshot_digest": D,
        "environment_digest": D,
        "permission_profile_digest": D,
        "tool_set_digest": D,
        "tool_visible_payload_digest": D,
        "tool_visible_payload_mode": "direct",
        "config_effective_digest": D,
        "hook_set_digest": D,
    }
    values.update(changes)
    return ModelRequestManifest.create(**values)


def test_manifest_digest_is_immutable_and_missing_bindings_drive_status() -> None:
    item = _manifest(
        context_packet_digest=None,
        route_decision_digest=None,
        missing_bindings=("context_packet_digest", "route_decision_digest"),
    )
    assert item.binding_status is GatewayBindingStatus.UNBOUND
    item.assert_digest()
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, model_id="other/model")
    with pytest.raises(PolicyViolation, match="request payload"):
        _manifest(model_visible_payload_digest=digest("different-payload"))
    with pytest.raises(ValidationFailed, match="catalog binding"):
        _manifest(catalog_snapshot_id=None, missing_bindings=("catalog_snapshot_id",))


def test_gateway_permit_is_process_local_and_exact() -> None:
    item = _manifest()
    permit = _issue_gateway_permit(item, attempt_id=item.attempt_id, claim_id=uuid4())
    permit.assert_for(item)
    with pytest.raises(PolicyViolation, match="kanonik gateway"):
        type(permit)(item.id, item.manifest_digest, item.attempt_id, uuid4(), object())
    with pytest.raises(PolicyViolation, match="eslesmiyor"):
        permit.assert_for(_manifest())


def test_gateway_records_reconciliation_result_when_effect_raises() -> None:
    repository = _GatewayRepository()
    trace = _TraceSink()
    gateway = ModelGateway(  # type: ignore[arg-type]
        repository=repository,
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        trace_sink=trace,
    )
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "request-1", {"input": "x"})
    item = _manifest(
        payload_digest=call.payload_digest,
        model_visible_payload_digest=call.payload_digest,
    )

    loop_attempt_id = uuid4()
    with pytest.raises(RuntimeError, match="transport failed"):
        gateway.invoke(
            item,
            claim_id=uuid4(),
            authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            call=call,
            effect=lambda _permit: (_ for _ in ()).throw(RuntimeError("transport failed")),
            loop_attempt_id=loop_attempt_id,
        )

    assert repository.attempt_values is not None
    assert repository.attempt_values["loop_attempt_id"] == loop_attempt_id
    assert repository.result is not None
    assert repository.result["state"] == "reconciliation-required"
    assert repository.result["failure_digest"] == digest({"category": "RuntimeError"})
    assert [item["event_type"] for item in trace.events] == [
        TraceEventType.MODEL_REQUEST,
        TraceEventType.ERROR,
    ]


def test_gateway_trace_records_final_provider_request_and_response() -> None:
    repository = _GatewayRepository()
    trace = _TraceSink()
    gateway = ModelGateway(  # type: ignore[arg-type]
        repository=repository,
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        trace_sink=trace,
    )
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "request-1", {"input": "x"})
    item = _manifest(
        payload_digest=call.payload_digest,
        model_visible_payload_digest=call.payload_digest,
    )
    response = ProviderCallResult(
        response={"output": "ok"},
        response_digest=digest({"output": "ok"}),
        outbound_request_id=uuid4(),
        authorization_id=uuid4(),
    )
    _, actual = gateway.invoke(
        item,
        claim_id=uuid4(),
        authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
        call=call,
        effect=lambda _permit: response,
    )
    assert actual is response
    assert [event["event_type"] for event in trace.events] == [
        TraceEventType.MODEL_REQUEST,
        TraceEventType.MODEL_RESPONSE,
    ]
    assert trace.events[0]["payload"] == {"input": "x"}
    assert trace.events[1]["payload"] == {"output": "ok"}


def test_gateway_enforce_rejects_envelopeless_manifest_before_effect() -> None:
    repository = _GatewayRepository(GatewayMode.ENFORCE)
    gateway = ModelGateway(repository=repository, source_label=GatewaySourceLabel.PROVIDER_CONTRACT)  # type: ignore[arg-type]
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "request-1", {"input": "x"})
    item = _manifest(
        execution_envelope_id=None,
        execution_envelope_digest=None,
        payload_digest=call.payload_digest,
        model_visible_payload_digest=call.payload_digest,
        missing_bindings=("execution_envelope_digest", "execution_envelope_id"),
    )
    called = False

    def effect(_permit):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("effect cagrilmamali")

    with pytest.raises(PolicyViolation, match="eksik binding"):
        gateway.invoke(
            item,
            claim_id=uuid4(),
            authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            call=call,
            effect=effect,
        )
    assert called is False
    assert repository.envelope_asserted is False


def test_gateway_enforce_live_probe_drift_blocks_repository_check_and_effect() -> None:
    repository = _GatewayRepository(GatewayMode.ENFORCE)
    guard = _EnvironmentGuard(PolicyViolation("environment.capability-drift"))
    gateway = ModelGateway(
        repository=repository,  # type: ignore[arg-type]
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        environment_guard=guard,
    )
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "request-1", {"input": "x"})
    item = _manifest(
        payload_digest=call.payload_digest,
        model_visible_payload_digest=call.payload_digest,
    )
    called = False

    def effect(_permit):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    with pytest.raises(PolicyViolation, match="capability-drift"):
        gateway.invoke(
            item,
            claim_id=uuid4(),
            authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            call=call,
            effect=effect,
        )
    assert len(guard.calls) == 1
    assert repository.envelope_asserted is False
    assert called is False


def test_gateway_loads_envelope_budgets_and_prepares_bound_manifest() -> None:
    repository = _GatewayRepository(GatewayMode.ENFORCE)
    gateway = ModelGateway.from_execution_envelope(
        repository,  # type: ignore[arg-type]
        GatewaySourceLabel.PROVIDER_CONTRACT,
        uuid4(),
    )
    now = dt.datetime.now(dt.UTC)
    job = SimpleNamespace(
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
        step_id="build",
        id=uuid4(),
        assignment_id=uuid4(),
    )
    prepared = SimpleNamespace(
        plan=SimpleNamespace(
            model_id="provider/model", provider_ref="provider:x", call_id="call-1"
        ),
        call=SimpleNamespace(payload_digest=D),
    )
    authorization = SimpleNamespace(
        scope=SimpleNamespace(body=lambda: {"scope": "test"}),
        risk="medium",
        expires_at=now + dt.timedelta(minutes=5),
    )
    serialized = repository.compiled_tools.compile_model_payload().serialize_request({"input": "x"})
    prepared.call.payload_digest = serialized.binding.request_payload_digest
    item = gateway.prepare(
        prepared,  # type: ignore[arg-type]
        SimpleNamespace(job=job, attempt_id=uuid4()),  # type: ignore[arg-type]
        authorization,  # type: ignore[arg-type]
        payload_binding=ModelVisiblePayloadBinding(
            D, D, ("fragment/request",), serialized.binding.request_payload_digest
        ),
        tool_payload_binding=serialized.binding,
        now=now,
    )
    assert item.missing_bindings == ()
    assert (item.max_input_tokens, item.max_output_tokens, item.max_cost_micros) == (
        100,
        20,
        1000,
    )
    assert item.deadline == gateway.bindings.deadline


def test_gateway_enforce_marks_missing_typed_payload_binding_and_rejects_effect() -> None:
    repository = _GatewayRepository(GatewayMode.ENFORCE)
    gateway = ModelGateway.from_execution_envelope(
        repository,  # type: ignore[arg-type]
        GatewaySourceLabel.PROVIDER_CONTRACT,
        uuid4(),
    )
    now = dt.datetime.now(dt.UTC)
    serialized = repository.compiled_tools.compile_model_payload().serialize_request({"input": "x"})
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "call-1", serialized.payload)
    manifest = gateway.prepare(
        SimpleNamespace(
            plan=SimpleNamespace(
                model_id="provider/model", provider_ref="provider:x", call_id="call-1"
            ),
            call=call,
        ),  # type: ignore[arg-type]
        SimpleNamespace(
            job=SimpleNamespace(
                realm_id=uuid4(),
                project_id=uuid4(),
                work_item_id=uuid4(),
                plan_id=uuid4(),
                step_id="build",
                id=uuid4(),
                assignment_id=uuid4(),
            ),
            attempt_id=uuid4(),
        ),  # type: ignore[arg-type]
        SimpleNamespace(
            scope=SimpleNamespace(body=lambda: {"scope": "test"}),
            risk="medium",
            expires_at=now + dt.timedelta(minutes=5),
        ),  # type: ignore[arg-type]
        tool_payload_binding=serialized.binding,
        now=now,
    )
    assert manifest.missing_bindings == (
        "context_fragment_set_digest",
        "model_visible_payload_digest",
    )
    with pytest.raises(PolicyViolation, match="eksik binding"):
        gateway.invoke(
            manifest,
            claim_id=uuid4(),
            authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            call=call,
            effect=lambda _permit: (_ for _ in ()).throw(AssertionError("effect cagrilmamali")),
        )


def test_gateway_rejects_authorization_shorter_than_envelope_deadline() -> None:
    repository = _GatewayRepository(GatewayMode.ENFORCE)
    gateway = ModelGateway.from_execution_envelope(
        repository,  # type: ignore[arg-type]
        GatewaySourceLabel.PROVIDER_CONTRACT,
        uuid4(),
    )
    now = dt.datetime.now(dt.UTC)
    with pytest.raises(PolicyViolation, match="deadline"):
        gateway.prepare(
            SimpleNamespace(
                plan=SimpleNamespace(
                    model_id="provider/model", provider_ref="provider:x", call_id="call-1"
                ),
                call=SimpleNamespace(payload_digest=D),
            ),  # type: ignore[arg-type]
            SimpleNamespace(
                job=SimpleNamespace(
                    realm_id=uuid4(),
                    project_id=uuid4(),
                    work_item_id=uuid4(),
                    plan_id=uuid4(),
                    step_id="build",
                    id=uuid4(),
                    assignment_id=uuid4(),
                ),
                attempt_id=uuid4(),
            ),  # type: ignore[arg-type]
            SimpleNamespace(
                scope=SimpleNamespace(body=lambda: {"scope": "test"}),
                risk="medium",
                expires_at=now + dt.timedelta(minutes=1),
            ),  # type: ignore[arg-type]
            now=now,
        )


def test_legacy_audit_manifest_uses_none_and_exact_missing_bindings() -> None:
    missing = (
        "assignment_id",
        "authorization_scope_digest",
        "catalog_digest",
        "catalog_provider_id",
        "catalog_snapshot_digest",
        "catalog_snapshot_id",
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
        run_id=None,
        execution_envelope_id=None,
        execution_envelope_digest=None,
        assignment_id=None,
        catalog_provider_id=None,
        catalog_digest=None,
        catalog_snapshot_digest=None,
        catalog_snapshot_id=None,
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
    assert item.binding_status is GatewayBindingStatus.UNBOUND
    item.assert_digest()
