"""Secret Broker kararlari ve cozumleme yasam dongusu."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.secret_broker import (
    EnvironmentSecretStore,
    InMemorySecretStore,
    SecretBroker,
)
from zekam.domain.errors import AuthorizationRequired, NotFound, PolicyViolation
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    SecretBackend,
    SecretRef,
    SecretStatus,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
REALM = uuid4()
ACTOR = uuid4()
SECRET_VALUE = "Kx7pQm2ZrT9wLb4Nc1Vd"
DIGEST = "sha256:" + "a" * 64


def _reference(**overrides: object) -> SecretRef:
    defaults: dict[str, object] = {
        "realm_id": REALM,
        "name": "anthropic-api",
        "provider": "anthropic",
        "purpose": "chat cagrisi",
        "allowed_operations": ("chat",),
        "store_backend": SecretBackend.LOCAL_ENCRYPTED,
        "store_locator": "ANTHROPIC_API_KEY",
        "now": NOW,
    }
    defaults.update(overrides)
    return SecretRef.create(**defaults)  # type: ignore[arg-type]


def _authorization(reference: SecretRef, **overrides: object) -> Authorization:
    base = Authorization.issue(
        realm_id=REALM,
        actor_id=ACTOR,
        plan_digest=DIGEST,
        effect_digest=DIGEST,
        scope=AuthorizationScope(
            allowed_effects=("provider-call",), secret_ref_ids=(reference.id,)
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=30),
        now=NOW,
    )
    if not overrides:
        return base
    return Authorization(
        id=base.id,
        realm_id=base.realm_id,
        actor_id=base.actor_id,
        plan_digest=base.plan_digest,
        effect_digest=base.effect_digest,
        scope=base.scope,
        risk=base.risk,
        issued_at=base.issued_at,
        expires_at=base.expires_at,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def broker() -> SecretBroker:
    return SecretBroker(
        {SecretBackend.LOCAL_ENCRYPTED: InMemorySecretStore({"ANTHROPIC_API_KEY": SECRET_VALUE})}
    )


# -- basarili yol -------------------------------------------------------------------


def test_authorized_resolution_yields_value(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference)
    with broker.resolve(
        reference, operation="chat", authorization=authorization, now=NOW
    ) as secret:
        assert secret.reveal() == SECRET_VALUE


def test_value_is_cleared_after_the_block(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference)
    with broker.resolve(
        reference, operation="chat", authorization=authorization, now=NOW
    ) as secret:
        pass
    assert secret.is_cleared
    with pytest.raises(PolicyViolation):
        secret.reveal()


def test_value_is_cleared_even_when_the_call_fails(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference)
    captured = None
    with (
        pytest.raises(RuntimeError),
        broker.resolve(reference, operation="chat", authorization=authorization, now=NOW) as secret,
    ):
        captured = secret
        raise RuntimeError("saglayici hatasi")
    assert captured is not None
    assert captured.is_cleared


# -- red yollari ---------------------------------------------------------------------


def test_missing_authorization_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    decision = broker.evaluate(reference, operation="chat", now=NOW)
    assert not decision.allowed
    assert decision.reason == "authorization-required"
    with (
        pytest.raises(AuthorizationRequired),
        broker.resolve(reference, operation="chat", now=NOW),
    ):
        pass  # pragma: no cover - buraya ulasilmamali


def test_operation_outside_allowed_list_is_rejected(broker: SecretBroker) -> None:
    reference = _reference(allowed_operations=("embedding",))
    decision = broker.evaluate(
        reference, operation="chat", authorization=_authorization(reference), now=NOW
    )
    assert decision.reason == "operation-not-permitted"


def test_revoked_secret_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    revoked = SecretRef(
        id=reference.id,
        realm_id=reference.realm_id,
        name=reference.name,
        provider=reference.provider,
        purpose=reference.purpose,
        allowed_operations=reference.allowed_operations,
        store_backend=reference.store_backend,
        store_locator=reference.store_locator,
        status=SecretStatus.REVOKED,
        created_at=NOW,
    )
    decision = broker.evaluate(
        revoked, operation="chat", authorization=_authorization(revoked), now=NOW
    )
    assert decision.reason == "secret-not-usable:revoked"


def test_expired_secret_is_rejected(broker: SecretBroker) -> None:
    reference = _reference(expires_at=NOW - dt.timedelta(minutes=1))
    decision = broker.evaluate(
        reference, operation="chat", authorization=_authorization(reference), now=NOW
    )
    assert decision.reason.startswith("secret-not-usable")


def test_secret_outside_authorization_scope_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    other = _reference(name="baska-secret")
    decision = broker.evaluate(
        reference, operation="chat", authorization=_authorization(other), now=NOW
    )
    assert decision.reason == "secret-out-of-authorization-scope"


def test_expired_authorization_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference)
    later = authorization.expires_at + dt.timedelta(seconds=1)
    decision = broker.evaluate(reference, operation="chat", authorization=authorization, now=later)
    assert decision.reason == "authorization-expired"


def test_consumed_authorization_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference, state=AuthorizationState.CONSUMED, consumed_at=NOW)
    decision = broker.evaluate(reference, operation="chat", authorization=authorization, now=NOW)
    assert decision.reason == "authorization-already-consumed"


def test_revoked_authorization_is_rejected(broker: SecretBroker) -> None:
    reference = _reference()
    authorization = _authorization(reference, state=AuthorizationState.REVOKED, revoked_at=NOW)
    decision = broker.evaluate(reference, operation="chat", authorization=authorization, now=NOW)
    assert decision.reason == "authorization-revoked"


def test_cross_realm_secret_is_rejected(broker: SecretBroker) -> None:
    reference = _reference(realm_id=uuid4())
    authorization = Authorization.issue(
        realm_id=REALM,
        actor_id=ACTOR,
        plan_digest=DIGEST,
        effect_digest=DIGEST,
        scope=AuthorizationScope(
            allowed_effects=("provider-call",), secret_ref_ids=(reference.id,)
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=30),
        now=NOW,
    )
    decision = broker.evaluate(reference, operation="chat", authorization=authorization, now=NOW)
    assert decision.reason == "cross-realm-secret"


def test_unsupported_backend_is_rejected() -> None:
    broker = SecretBroker({})
    reference = _reference()
    decision = broker.evaluate(
        reference, operation="chat", authorization=_authorization(reference), now=NOW
    )
    assert decision.reason.startswith("backend-unsupported")


def test_decision_never_contains_the_value(broker: SecretBroker) -> None:
    reference = _reference()
    decision = broker.evaluate(
        reference, operation="chat", authorization=_authorization(reference), now=NOW
    )
    assert SECRET_VALUE not in repr(decision.as_dict())


# -- arka uclar ------------------------------------------------------------------------


def test_environment_store_reads_by_locator_name() -> None:
    store = EnvironmentSecretStore(environ={"ANTHROPIC_API_KEY": SECRET_VALUE})
    reference = _reference(store_backend=SecretBackend.ENVIRONMENT)
    assert store.resolve(reference).reveal() == SECRET_VALUE


def test_environment_store_rejects_other_backends() -> None:
    store = EnvironmentSecretStore(environ={})
    with pytest.raises(PolicyViolation):
        store.resolve(_reference(store_backend=SecretBackend.VAULT))


def test_missing_value_raises_not_found() -> None:
    store = EnvironmentSecretStore(environ={})
    with pytest.raises(NotFound):
        store.resolve(_reference(store_backend=SecretBackend.ENVIRONMENT))


def test_secret_reference_record_has_no_value_field() -> None:
    document = _reference().as_dict()
    assert "value" not in document
    assert SECRET_VALUE not in repr(document)
    assert document["store_locator"] == "ANTHROPIC_API_KEY"
