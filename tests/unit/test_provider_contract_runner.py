"""Provider contract runner claim/receipt ve no-silent-retry testleri."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zekam.application.provider_adapter import (
    AuthorizedProviderClient,
    ProviderCall,
    ProviderCallResult,
    reviewed_endpoint_digest,
)
from zekam.application.provider_contract_execution import (
    PreparedProviderContractCall,
    ProviderCallPlan,
)
from zekam.application.provider_contract_runner import RuntimeProviderContractRunner
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import Modality
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind

pytestmark = pytest.mark.unit


class FakeLedger:
    def __init__(self) -> None:
        self.claims: list[SimpleNamespace] = []

    def claims_for_job(self, job_id: object) -> tuple[SimpleNamespace, ...]:
        return tuple(claim for claim in self.claims if claim.job_id == job_id)


class FakeJobs:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def mark_recovery_required(self, job_id: object, reason: str) -> None:
        del job_id, reason
        self.events.append("recovery-required")


class FakeHost:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ledger = FakeLedger()
        self.jobs = FakeJobs(events)
        self.claim_idempotency_keys: list[str] = []

    def claim_effect(self, work: object, **kwargs: object) -> SimpleNamespace:
        self.claim_idempotency_keys.append(str(kwargs["idempotency_key"]))
        claim = SimpleNamespace(
            id=uuid4(),
            job_id=work.job.id,  # type: ignore[attr-defined]
            effect_digest=kwargs["effect_digest"],
        )
        self.ledger.claims.append(claim)
        self.events.append("claim")
        return claim

    def record_success(self, claim: object, **kwargs: object) -> SimpleNamespace:
        del claim, kwargs
        self.events.append("success-receipt")
        return SimpleNamespace(id=uuid4(), status=SimpleNamespace(value="completed"))

    def record_failure(self, claim: object, **kwargs: object) -> SimpleNamespace:
        del claim, kwargs
        self.events.append("failed-receipt")
        return SimpleNamespace(id=uuid4(), status=SimpleNamespace(value="failed"))


class ReceiptCrashHost(FakeHost):
    def record_success(self, claim: object, **kwargs: object) -> SimpleNamespace:
        del claim, kwargs
        self.events.append("success-receipt-crash")
        raise RuntimeError("receipt-write-failed")


class FakeGateway:
    def __init__(self) -> None:
        self.terminals: list[dict[str, object]] = []

    def prepare(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def invoke(self, manifest: object, **kwargs: object) -> tuple[object, ProviderCallResult]:
        del manifest
        effect = kwargs["effect"]
        return uuid4(), effect(object())  # type: ignore[operator]

    def record_terminal(self, manifest: object, attempt_id: object, **values: object) -> None:
        del manifest, attempt_id
        self.terminals.append(values)


class FakeClient:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.calls = 0

    def invoke(self, call: ProviderCall, **kwargs: object) -> ProviderCallResult:
        del call, kwargs
        self.events.append("transport")
        self.calls += 1
        if self.fail:
            raise RuntimeError("sanitized fake failure")
        return ProviderCallResult(
            response={"ok": True},
            response_digest="sha256:" + "a" * 64,
            outbound_request_id=uuid4(),
            authorization_id=uuid4(),
        )


class CrashClient(FakeClient):
    def invoke(self, call: ProviderCall, **kwargs: object) -> ProviderCallResult:
        del call, kwargs
        self.events.append("transport")
        self.calls += 1
        raise KeyboardInterrupt


def _case() -> tuple[PreparedProviderContractCall, SecretRef, Authorization]:
    endpoint = "https://models.example.test/v1/embeddings"
    base = ProviderCall(
        "model:test",
        "model-endpoint:test",
        "embeddings",
        "embedding-single-1",
        {"model": "embedding-test", "input": ["public fixture"]},
        data_categories=(DataClassification.PUBLIC,),
    )
    plan = ProviderCallPlan(
        call_id=base.request_identity,
        modality=Modality.EMBEDDING,
        model_id="test-model-id",
        provider_ref=base.provider_ref,
        endpoint_ref=base.endpoint_ref,
        operation=base.operation,
        secret_ref_name="test-secret-ref",
        request_format="json",
        fixture_digest="sha256:" + "b" * 64,
        payload_digest=base.payload_digest,
        endpoint_binding_digest=reviewed_endpoint_digest(endpoint, path_hint="/v1/embeddings"),
        endpoint_path_hint="/v1/embeddings",
    )
    call = ProviderCall(
        base.provider_ref,
        base.endpoint_ref,
        base.operation,
        base.request_identity,
        base.payload,
        data_categories=(DataClassification.PUBLIC,),
        endpoint_path_hint=plan.endpoint_path_hint,
        endpoint_binding_digest=plan.endpoint_binding_digest,
        authorization_plan_digest=plan.authorization_plan_digest,
        authorization_resource=plan.call_resource,
    )
    secret = SecretRef.create(
        realm_id=uuid4(),
        name="test-secret-ref",
        provider="model:test",
        purpose="exact provider contract",
        allowed_operations=("embeddings",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator="TEST_PROVIDER_CREDENTIAL",
    )
    authorization = Authorization.issue(
        realm_id=secret.realm_id,
        actor_id=uuid4(),
        plan_digest=plan.authorization_plan_digest,
        effect_digest=plan.effect_request.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.target, plan.call_resource),
            allowed_effects=(EffectKind.PROVIDER_CALL.value,),
            provider_refs=(plan.provider_ref,),
            secret_ref_ids=(secret.id,),
            data_classifications=(DataClassification.PUBLIC,),
        ),
        risk="critical",
        lifetime=dt.timedelta(minutes=5),
    )
    return PreparedProviderContractCall(plan, call), secret, authorization


def test_runner_claims_before_transport_and_writes_success_receipt() -> None:
    prepared, secret, authorization = _case()
    events: list[str] = []
    host = FakeHost(events)
    client = FakeClient(events)
    runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=client,
    )
    result = runner.invoke(
        prepared,
        secret_ref=secret,
        authorization=authorization,
        consumed_by="offline-test",
    )
    assert events == ["claim", "transport", "success-receipt"]
    assert result.call_id == "embedding-single-1"


def test_runner_scopes_claim_idempotency_to_runtime_job() -> None:
    prepared, secret, first_authorization = _case()
    events: list[str] = []
    host = FakeHost(events)
    first_runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=FakeClient(events),
    )
    first_runner.invoke(
        prepared,
        secret_ref=secret,
        authorization=first_authorization,
        consumed_by="offline-test",
    )

    second_authorization = Authorization.issue(
        realm_id=first_authorization.realm_id,
        actor_id=first_authorization.actor_id,
        plan_digest=first_authorization.plan_digest,
        effect_digest=first_authorization.effect_digest,
        scope=first_authorization.scope,
        risk="critical",
        lifetime=dt.timedelta(minutes=5),
    )
    second_runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=FakeClient(events),
    )
    second_runner.invoke(
        prepared,
        secret_ref=secret,
        authorization=second_authorization,
        consumed_by="offline-test",
    )

    assert len(host.claim_idempotency_keys) == 2
    assert len(set(host.claim_idempotency_keys)) == 2


def test_runner_failure_is_terminal_recovery_required_and_never_retried() -> None:
    prepared, secret, authorization = _case()
    events: list[str] = []
    host = FakeHost(events)
    client = FakeClient(events, fail=True)
    runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=client,
    )
    with pytest.raises(RuntimeError, match="fake failure"):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=authorization,
            consumed_by="offline-test",
        )
    assert events == ["claim", "transport", "failed-receipt", "recovery-required"]
    with pytest.raises(PolicyViolation, match="silent retry"):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=authorization,
            consumed_by="offline-test",
        )
    assert client.calls == 1


def test_runner_rejects_plan_swap_before_claim_or_transport() -> None:
    prepared, secret, authorization = _case()
    swapped = Authorization.issue(
        realm_id=authorization.realm_id,
        actor_id=authorization.actor_id,
        plan_digest="sha256:" + "c" * 64,
        effect_digest=authorization.effect_digest,
        scope=authorization.scope,
        risk="critical",
        lifetime=dt.timedelta(minutes=5),
    )
    events: list[str] = []
    host = FakeHost(events)
    client = FakeClient(events)
    runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=client,
    )
    with pytest.raises(PolicyViolation, match="plan digest mismatch"):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=swapped,
            consumed_by="offline-test",
        )
    assert events == []


def test_receipt_write_failure_records_gateway_reconciliation() -> None:
    prepared, secret, authorization = _case()
    events: list[str] = []
    host = ReceiptCrashHost(events)
    gateway = FakeGateway()
    runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=FakeClient(events),
        gateway=gateway,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="receipt-write-failed"):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=authorization,
            consumed_by="offline-test",
        )

    assert events == ["claim", "transport", "success-receipt-crash", "recovery-required"]
    assert gateway.terminals[0]["receipt_id"] is None
    assert gateway.terminals[0]["response_digest"] == "sha256:" + "a" * 64


def test_claim_after_process_crash_blocks_silent_retry_for_recovery_scan() -> None:
    prepared, secret, authorization = _case()
    events: list[str] = []
    host = FakeHost(events)
    client = CrashClient(events)
    runner = RuntimeProviderContractRunner(
        host=host,  # type: ignore[arg-type]
        work=SimpleNamespace(job=SimpleNamespace(id=uuid4())),  # type: ignore[arg-type]
        client=client,
    )
    with pytest.raises(KeyboardInterrupt):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=authorization,
            consumed_by="offline-test",
        )
    assert events == ["claim", "transport"]
    with pytest.raises(PolicyViolation, match="silent retry"):
        runner.invoke(
            prepared,
            secret_ref=secret,
            authorization=authorization,
            consumed_by="offline-test",
        )
    assert client.calls == 1


def test_invoke_binding_rejects_endpoint_host_port_or_path_drift() -> None:
    prepared, _, authorization = _case()
    for endpoint in (
        "https://other.example.test/v1/embeddings",
        "https://models.example.test:8443/v1/embeddings",
        "https://models.example.test/v1/other",
    ):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            AuthorizedProviderClient._require_exact_contract_binding(
                prepared.call,
                endpoint=endpoint,
                authorization=authorization,
            )
