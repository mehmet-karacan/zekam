from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ContextPacket,
    ContextPacketSection,
    ExecutionEnvelope,
    ExecutionRun,
    ProviderBindingSnapshot,
)

D = "sha256:" + "a" * 64


def _run(**changes: object) -> ExecutionRun:
    now = dt.datetime.now(dt.UTC)
    values: dict[str, object] = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "client_id": "opencode",
        "session_id": "session-1",
        "source_revision": "abc123",
        "policy_digest": D,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_micros": 1000,
        "deadline": now + dt.timedelta(minutes=5),
        "created_at": now,
    }
    values.update(changes)
    return ExecutionRun.create(**values)


def _packet(**changes: object) -> ContextPacket:
    values: dict[str, object] = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "manifest_id": uuid4(),
        "manifest_digest": D,
        "sections": (ContextPacketSection("required-1", D, 1),),
        "created_at": dt.datetime.now(dt.UTC),
    }
    values.update(changes)
    return ContextPacket.create(**values)


def _envelope(**changes: object) -> ExecutionEnvelope:
    now = dt.datetime.now(dt.UTC)
    values: dict[str, object] = {
        "realm_id": uuid4(),
        "run_id": uuid4(),
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "lease_id": uuid4(),
        "fencing_token": 1,
        "request_ordinal": 1,
        "idempotency_key": "request-1",
        "assignment_id": uuid4(),
        "role": "builder",
        "route_decision_id": uuid4(),
        "route_decision_digest": D,
        "route_expires_at": now + dt.timedelta(minutes=5),
        "model_id": "provider/model",
        "provider_binding_id": uuid4(),
        "provider_binding_digest": D,
        "provider_ref": "provider:test",
        "context_manifest_id": uuid4(),
        "context_manifest_digest": D,
        "context_packet_id": uuid4(),
        "context_packet_digest": D,
        "checkpoint_id": None,
        "checkpoint_digest": None,
        "checkpoint_disposition": CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        "source_revision": "abc123",
        "policy_digest": D,
        "authorization_scope_digest": D,
        "output_schema_digest": D,
        "payload_digest": D,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_micros": 1000,
        "deadline": now + dt.timedelta(minutes=4),
        "created_at": now,
    }
    values.update(changes)
    return ExecutionEnvelope.create(**values)


def test_run_digest_covers_budget_and_identity() -> None:
    item = _run()
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, max_cost_micros=item.max_cost_micros + 1)


def test_context_packet_requires_exact_order_and_digest() -> None:
    with pytest.raises(ValidationFailed, match="ordinal"):
        _packet(sections=(ContextPacketSection("required-1", D, 2),))
    item = _packet()
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, manifest_id=uuid4())


def test_envelope_genesis_and_bound_checkpoint_are_discriminated() -> None:
    genesis = _envelope()
    assert genesis.checkpoint_disposition is CheckpointDisposition.NOT_APPLICABLE_GENESIS
    with pytest.raises(ValidationFailed, match="Genesis"):
        _envelope(checkpoint_id=uuid4(), checkpoint_digest=D)
    bound = _envelope(
        checkpoint_id=uuid4(),
        checkpoint_digest=D,
        checkpoint_disposition=CheckpointDisposition.BOUND,
    )
    assert bound.checkpoint_digest == D


def test_envelope_rejects_deadline_after_route_expiry() -> None:
    now = dt.datetime.now(dt.UTC)
    with pytest.raises(ValidationFailed, match="deadline"):
        _envelope(
            created_at=now,
            deadline=now + dt.timedelta(minutes=6),
            route_expires_at=now + dt.timedelta(minutes=5),
        )


def test_provider_binding_digest_covers_provider_and_endpoint() -> None:
    now = dt.datetime.now(dt.UTC)
    item = ProviderBindingSnapshot.create(
        realm_id=uuid4(),
        model_id="provider/model",
        provider_ref="model:provider-model",
        endpoint_ref="model-endpoint:provider-model",
        operation="invoke",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=5),
    )
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, provider_ref="forged-provider")
