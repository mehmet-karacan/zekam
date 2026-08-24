from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zekam.application.model_gateway import ModelGateway
from zekam.application.provider_adapter import ProviderCall
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_invocation import (
    GatewayBindingStatus,
    GatewayMode,
    GatewaySourceLabel,
    ModelRequestManifest,
    _issue_gateway_permit,
)

D = "sha256:" + "a" * 64


class _GatewayRepository:
    def __init__(self) -> None:
        self.result: dict[str, object] | None = None

    def store_manifest(self, item: ModelRequestManifest) -> tuple[object, bool]:
        return item.id, True

    def record_audit(self, **values: object) -> object:
        return uuid4()

    def mode(self) -> GatewayMode:
        return GatewayMode.AUDIT

    def record_attempt(self, **values: object) -> object:
        return uuid4()

    def record_result(self, **values: object) -> object:
        self.result = values
        return uuid4()


def _manifest(**changes):  # type: ignore[no-untyped-def]
    now = dt.datetime.now(dt.UTC)
    values = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "step_id": "build",
        "run_id": uuid4(),
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "assignment_id": uuid4(),
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
        "idempotency_key": "request-1",
        "max_input_tokens": 100,
        "max_output_tokens": 20,
        "max_cost_micros": 1000,
        "deadline": now + dt.timedelta(minutes=1),
        "route_expires_at": now + dt.timedelta(minutes=1),
        "created_at": now,
        "source_label": GatewaySourceLabel.MODEL_CAPABILITY,
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
    gateway = ModelGateway(repository=repository, source_label=GatewaySourceLabel.PROVIDER_CONTRACT)  # type: ignore[arg-type]
    item = _manifest()
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "request-1", {"input": "x"})

    with pytest.raises(RuntimeError, match="transport failed"):
        gateway.invoke(
            item,
            claim_id=uuid4(),
            authorization=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
            call=call,
            effect=lambda _permit: (_ for _ in ()).throw(RuntimeError("transport failed")),
        )

    assert repository.result is not None
    assert repository.result["state"] == "reconciliation-required"
    assert repository.result["failure_digest"] == digest({"category": "RuntimeError"})


def test_legacy_audit_manifest_uses_none_and_exact_missing_bindings() -> None:
    missing = (
        "assignment_id",
        "authorization_scope_digest",
        "checkpoint_digest",
        "context_manifest_digest",
        "context_packet_digest",
        "output_schema_digest",
        "policy_digest",
        "route_decision_digest",
        "route_expires_at",
        "run_id",
    )
    item = _manifest(
        run_id=None,
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
    assert item.binding_status is GatewayBindingStatus.UNBOUND
    item.assert_digest()
