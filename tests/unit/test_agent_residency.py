from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.agent_residency import AgentResidencyManager
from zekam.domain.agent_residency import (
    AssignmentRuntimeSnapshot,
    ReloadRequest,
    ResidencyState,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

NOW = dt.datetime(2026, 8, 25, 10, 0, tzinfo=dt.UTC)


def _snapshot(realm_id=None) -> AssignmentRuntimeSnapshot:  # type: ignore[no-untyped-def]
    return AssignmentRuntimeSnapshot.create(
        realm_id=realm_id or uuid4(),
        edge_id=uuid4(),
        assignment_id=uuid4(),
        execution_envelope_id=uuid4(),
        role="builder",
        model_id="model-a",
        provider_binding_id=uuid4(),
        provider_binding_digest=digest("provider"),
        route_decision_id=uuid4(),
        route_decision_digest=digest("route"),
        environment_snapshot_digest=digest("environment"),
        permission_profile_digest=digest("permission"),
        config_effective_digest=digest("config"),
        source_revision="revision-1",
        policy_digest=digest("policy"),
        created_at=NOW,
    )


def test_runtime_snapshot_is_digest_bound_and_authority_free() -> None:
    snapshot = _snapshot()
    assert snapshot.snapshot_digest == snapshot.computed_digest
    with pytest.raises(PolicyViolation, match="authority"):
        AssignmentRuntimeSnapshot.create(
            **{
                **{field: getattr(snapshot, field) for field in snapshot.__dataclass_fields__},
                "id": uuid4(),
                "snapshot_digest": "",
                "grants_authority": True,
            }
        )


def test_reload_request_requires_session_and_is_authority_free() -> None:
    snapshot = _snapshot()
    values = {
        "realm_id": snapshot.realm_id,
        "edge_id": snapshot.edge_id,
        "current_environment_snapshot_digest": snapshot.environment_snapshot_digest,
        "current_route_decision_id": snapshot.route_decision_id,
        "current_provider_binding_id": snapshot.provider_binding_id,
        "runtime_session_ref": "runtime:child-2",
        "requested_at": NOW,
    }
    request = ReloadRequest.create(**values)
    assert request.request_digest == request.computed_digest
    with pytest.raises(ValidationFailed, match="session ref"):
        ReloadRequest.create(**{**values, "runtime_session_ref": ""})
    with pytest.raises(PolicyViolation, match="authority"):
        ReloadRequest.create(**{**values, "grants_authority": True})


def test_manager_evict_does_not_claim_completion_or_authority() -> None:
    snapshot = _snapshot()
    calls: list[tuple[object, ...]] = []

    class Store:
        def transition(self, edge_id, *, state, occurred_at, reason=None):  # type: ignore[no-untyped-def]
            calls.append((edge_id, state, occurred_at, reason))
            return True

    manager = AgentResidencyManager(snapshot.realm_id, Store())  # type: ignore[arg-type]
    assert manager.evict(snapshot.edge_id, occurred_at=NOW)
    assert calls == [(snapshot.edge_id, ResidencyState.EVICTED, NOW, None)]
