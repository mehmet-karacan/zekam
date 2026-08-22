"""Secret sizinti taramalari.

Bir secret degeri veritabanina, denetim kaydina, rapora veya CLI ciktisina
girmemelidir. Bu testler bunu uctan uca dogrular.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from zekam.application.governance import EffectRequest, GovernanceService, default_capabilities
from zekam.application.secret_broker import InMemorySecretStore, SecretBroker
from zekam.domain.errors import AuthorizationRequired
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import SecretBackend, SecretRef
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]

SECRET_VALUE = "Kx7pQm2ZrT9wLb4Nc1Vd"
LOCATOR = "ANTHROPIC_API_KEY"


@pytest.fixture
def actor_id(realm_session: tuple[Realm, Any]):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    return (
        ActorRepository(connection, realm.id)
        .add(Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet"))
        .id
    )


@pytest.fixture
def service(realm_session: tuple[Realm, Any], actor_id: Any) -> GovernanceService:
    realm, connection = realm_session
    governance = GovernanceService(connection, realm, actor_id=actor_id)
    governance.ensure_default_policy()
    for capability in default_capabilities(realm.id):
        governance.capabilities.append(capability)
    return governance


@pytest.fixture
def reference(service: GovernanceService, realm_session: tuple[Realm, Any]) -> SecretRef:
    realm, _ = realm_session
    created = SecretRef.create(
        realm_id=realm.id,
        name="anthropic-api",
        provider="anthropic",
        purpose="chat",
        allowed_operations=("chat",),
        store_backend=SecretBackend.LOCAL_ENCRYPTED,
        store_locator=LOCATOR,
    )
    return service.secrets.add(created)


def _database_contains(connection: Any, needle: str) -> bool:
    """Butun metin ve jsonb kolonlarini tarar."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select table_schema, table_name, column_name from information_schema.columns"
            " where data_type in ('text', 'jsonb', 'character varying', 'ARRAY')"
            "   and table_schema in ('core', 'projects', 'work', 'security')"
        )
        columns = cursor.fetchall()
        for schema, table, column in columns:
            cursor.execute(
                f'select count(*) from "{schema}"."{table}" where "{column}"::text like %s',
                (f"%{needle}%",),
            )
            if int(cursor.fetchone()[0]) > 0:
                return True
    return False


def test_secret_value_is_not_written_to_any_column(
    service: GovernanceService, reference: SecretRef, actor_id: Any
) -> None:
    broker = SecretBroker(
        {SecretBackend.LOCAL_ENCRYPTED: InMemorySecretStore({LOCATOR: SECRET_VALUE})}
    )
    request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(
        request=request, actor_id=actor_id, secret_ref_ids=(reference.id,)
    )
    with broker.resolve(reference, operation="chat", authorization=authorization) as secret:
        assert secret.reveal() == SECRET_VALUE

    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    assert not _database_contains(service.connection, SECRET_VALUE)


def test_secret_locator_is_stored_but_value_is_not(
    service: GovernanceService, reference: SecretRef
) -> None:
    assert _database_contains(service.connection, LOCATOR)
    assert not _database_contains(service.connection, SECRET_VALUE)


def test_audit_trail_never_contains_the_value(
    service: GovernanceService, reference: SecretRef, actor_id: Any
) -> None:
    request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(
        request=request, actor_id=actor_id, secret_ref_ids=(reference.id,)
    )
    service.evaluate(request, authorization=authorization)
    trail = json.dumps(list(service.audit.recent()), default=str)
    assert SECRET_VALUE not in trail


def test_secret_reference_report_has_no_value(
    service: GovernanceService, reference: SecretRef
) -> None:
    rendered = json.dumps([item.as_dict() for item in service.secrets.list_all()], default=str)
    assert SECRET_VALUE not in rendered
    assert LOCATOR in rendered


def test_authorization_record_carries_reference_not_value(
    service: GovernanceService, reference: SecretRef, actor_id: Any
) -> None:
    request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(
        request=request, actor_id=actor_id, secret_ref_ids=(reference.id,)
    )
    stored = service.authorizations.get(authorization.id)
    assert reference.id in stored.scope.secret_ref_ids
    assert SECRET_VALUE not in json.dumps(stored.as_dict(), default=str)


def test_broker_error_message_has_no_value(
    service: GovernanceService, reference: SecretRef
) -> None:
    broker = SecretBroker(
        {SecretBackend.LOCAL_ENCRYPTED: InMemorySecretStore({LOCATOR: SECRET_VALUE})}
    )
    with (
        pytest.raises(AuthorizationRequired) as caught,
        broker.resolve(reference, operation="chat", authorization=None),
    ):
        pass  # pragma: no cover
    assert SECRET_VALUE not in str(caught.value)
