from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ContextPacket,
    ContextPacketSection,
    ExecutionEnvelope,
    ExecutionRun,
    ExecutionRunState,
    ProviderBindingSnapshot,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _provider(**changes: Any) -> ProviderBindingSnapshot:
    values: dict[str, Any] = {
        "realm_id": uuid4(),
        "model_id": "provider/model",
        "provider_ref": "model:provider-model",
        "endpoint_ref": "model-endpoint:provider-model",
        "operation": "invoke",
        "captured_at": NOW,
        "expires_at": NOW + dt.timedelta(minutes=5),
    }
    values.update(changes)
    return ProviderBindingSnapshot.create(**values)


def _run(**changes: Any) -> ExecutionRun:
    values: dict[str, Any] = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "client_id": "opencode",
        "session_id": "session-1",
        "source_revision": "abc123",
        "policy_digest": DIGEST,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_micros": 1000,
        "deadline": NOW + dt.timedelta(minutes=5),
        "created_at": NOW,
    }
    values.update(changes)
    return ExecutionRun.create(**values)


def _section(candidate_id: str = "required-1", ordinal: int = 1) -> ContextPacketSection:
    return ContextPacketSection(candidate_id, DIGEST, ordinal)


def _packet(**changes: Any) -> ContextPacket:
    values: dict[str, Any] = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "manifest_id": uuid4(),
        "manifest_digest": DIGEST,
        "sections": (_section(),),
        "created_at": NOW,
    }
    values.update(changes)
    return ContextPacket.create(**values)


def _envelope(**changes: Any) -> ExecutionEnvelope:
    values: dict[str, Any] = {
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
        "route_decision_digest": DIGEST,
        "route_expires_at": NOW + dt.timedelta(minutes=5),
        "model_id": "provider/model",
        "provider_binding_id": uuid4(),
        "provider_binding_digest": DIGEST,
        "provider_ref": "provider:test",
        "context_manifest_id": uuid4(),
        "context_manifest_digest": DIGEST,
        "context_packet_id": uuid4(),
        "context_packet_digest": DIGEST,
        "turn_execution_snapshot_id": uuid4(),
        "turn_execution_snapshot_digest": DIGEST,
        "checkpoint_id": None,
        "checkpoint_digest": None,
        "checkpoint_disposition": CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        "source_revision": "abc123",
        "policy_digest": DIGEST,
        "authorization_scope_digest": DIGEST,
        "output_schema_digest": DIGEST,
        "payload_digest": DIGEST,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_micros": 1000,
        "deadline": NOW + dt.timedelta(minutes=4),
        "created_at": NOW,
    }
    values.update(changes)
    return ExecutionEnvelope.create(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"grants_authority": True},
        {"captured_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW},
        {"model_id": " "},
        {"provider_ref": " "},
        {"endpoint_ref": " "},
        {"operation": " "},
    ],
)
def test_provider_binding_rejects_invalid_authority_time_and_identity(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _provider(**changes)


def test_provider_binding_rejects_invalid_supplied_digest() -> None:
    item = _provider()
    with pytest.raises(ValidationFailed):
        replace(item, binding_digest="bad")


@pytest.mark.parametrize(
    "changes",
    [
        {"grants_authority": True},
        {"state": ExecutionRunState.ACTIVE},
        {"deadline": NOW.replace(tzinfo=None)},
        {"created_at": NOW.replace(tzinfo=None)},
        {"deadline": NOW},
        {"client_id": " "},
        {"source_revision": " "},
        {"session_id": " "},
        {"max_input_tokens": 0},
        {"max_output_tokens": 0},
        {"max_cost_micros": 0},
    ],
)
def test_execution_run_rejects_invalid_state_time_identity_and_budget(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _run(**changes)


def test_execution_run_rejects_invalid_policy_and_run_digests() -> None:
    with pytest.raises(ValidationFailed):
        _run(policy_digest="bad")
    item = _run()
    with pytest.raises(ValidationFailed):
        replace(item, run_digest="bad")


@pytest.mark.parametrize("candidate_id, ordinal", [(" ", 1), ("ok", 0)])
def test_context_section_rejects_blank_identity_and_nonpositive_ordinal(
    candidate_id: str, ordinal: int
) -> None:
    with pytest.raises(ValidationFailed):
        _section(candidate_id, ordinal)


def test_context_section_rejects_invalid_digest() -> None:
    with pytest.raises(ValidationFailed):
        ContextPacketSection("ok", "bad", 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"grants_authority": True},
        {"created_at": NOW.replace(tzinfo=None)},
        {"sections": ()},
        {"sections": (_section("a", 1), _section("b", 3))},
        {"sections": (_section("same", 1), _section("same", 2))},
        {"manifest_digest": "bad"},
    ],
)
def test_context_packet_rejects_invalid_authority_time_sections_and_manifest(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _packet(**changes)


def test_context_packet_rejects_invalid_supplied_digest() -> None:
    item = _packet()
    with pytest.raises(ValidationFailed):
        replace(item, packet_digest="bad")


@pytest.mark.parametrize(
    "changes",
    [
        {"grants_authority": True},
        {"created_at": NOW.replace(tzinfo=None)},
        {"deadline": NOW.replace(tzinfo=None)},
        {"route_expires_at": NOW.replace(tzinfo=None)},
        {"deadline": NOW},
        {"fencing_token": 0},
        {"request_ordinal": 0},
        {"max_input_tokens": 0},
        {"max_output_tokens": 0},
        {"max_cost_micros": 0},
        {"idempotency_key": " "},
        {"role": " "},
        {"model_id": " "},
        {"provider_ref": " "},
        {"source_revision": " "},
    ],
)
def test_envelope_rejects_invalid_authority_time_fence_budget_and_identity(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _envelope(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"checkpoint_disposition": CheckpointDisposition.BOUND},
        {
            "checkpoint_disposition": CheckpointDisposition.BOUND,
            "checkpoint_id": uuid4(),
        },
        {
            "checkpoint_disposition": CheckpointDisposition.BOUND,
            "checkpoint_id": uuid4(),
            "checkpoint_digest": DIGEST,
            "checkpoint_v2_id": uuid4(),
        },
        {"checkpoint_disposition": CheckpointDisposition.BOUND_V2},
        {
            "checkpoint_disposition": CheckpointDisposition.BOUND_V2,
            "checkpoint_v2_id": uuid4(),
        },
        {
            "checkpoint_disposition": CheckpointDisposition.BOUND_V2,
            "checkpoint_v2_id": uuid4(),
            "checkpoint_v2_digest": DIGEST,
            "checkpoint_id": uuid4(),
        },
        {"checkpoint_digest": DIGEST},
        {"checkpoint_v2_id": uuid4()},
        {"checkpoint_v2_digest": DIGEST},
    ],
)
def test_envelope_rejects_inconsistent_checkpoint_discriminators(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationFailed):
        _envelope(**changes)


@pytest.mark.parametrize(
    "field",
    [
        "route_decision_digest",
        "context_manifest_digest",
        "context_packet_digest",
        "turn_execution_snapshot_digest",
        "provider_binding_digest",
        "policy_digest",
        "authorization_scope_digest",
        "output_schema_digest",
        "payload_digest",
    ],
)
def test_envelope_rejects_invalid_required_digest(field: str) -> None:
    with pytest.raises(ValidationFailed):
        _envelope(**{field: "bad"})


def test_envelope_rejects_invalid_optional_and_supplied_digests() -> None:
    with pytest.raises(ValidationFailed):
        _envelope(
            checkpoint_disposition=CheckpointDisposition.BOUND,
            checkpoint_id=uuid4(),
            checkpoint_digest="bad",
        )
    with pytest.raises(ValidationFailed):
        _envelope(
            checkpoint_disposition=CheckpointDisposition.BOUND_V2,
            checkpoint_v2_id=uuid4(),
            checkpoint_v2_digest="bad",
        )
    item = _envelope()
    with pytest.raises(ValidationFailed):
        replace(item, envelope_digest="bad")
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, max_cost_micros=item.max_cost_micros + 1)
