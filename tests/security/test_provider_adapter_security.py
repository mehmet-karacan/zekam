"""Authorized provider client'in gercek DB, sahte transport guvenlik testi.

Test hicbir ag cagrisi yapmaz. Provider etkisi exact policy + authorization ile
gercek governance tablolarinda yurutulur; credential yalniz bellek ici fake
transport'a ulasir.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from zekam.application.governance import EffectRequest, GovernanceService, default_capabilities
from zekam.application.provider_adapter import (
    AuthorizedProviderClient,
    EnvironmentEndpointResolver,
    MultipartProviderCall,
    ProviderCall,
    openai_transcription_body,
)
from zekam.application.secret_broker import InMemorySecretStore, SecretBroker
from zekam.domain.policy import PolicyDocument, PolicyRule, RiskLevel, default_policy_rules
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import AuthorizationState, OutboundState, SecretBackend, SecretRef
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]

SECRET_VALUE = "provider-secret-never-persist"
ENDPOINT_VALUE = "https://models.example.test/v1/embeddings"


@dataclass
class MemoryTransport:
    calls: int = 0

    def post_json(self, endpoint: str, payload: Any, credential: Any) -> dict[str, Any]:
        assert endpoint == ENDPOINT_VALUE
        assert payload == {"model": "embedding-test", "input": ["merhaba"]}
        assert credential.reveal() == SECRET_VALUE
        self.calls += 1
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}


@dataclass
class MemoryMultipartTransport:
    calls: int = 0

    def post_multipart(self, endpoint: str, payload: Any, credential: Any) -> dict[str, Any]:
        assert endpoint == "https://models.example.test/v1/audio/transcriptions"
        assert b"RIFF-private-fixture" in payload.body
        assert credential.reveal() == SECRET_VALUE
        self.calls += 1
        return {"text": "ornek transkript"}


def _service(realm_session: tuple[Realm, Any]) -> tuple[GovernanceService, Any]:
    realm, connection = realm_session
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="provider-test-user")
    )
    service = GovernanceService(connection, realm, actor_id=actor.id)
    service.ensure_default_policy()
    for capability in default_capabilities(realm.id):
        service.capabilities.append(capability)
    provider_rule = PolicyRule(
        name="exact-test-provider",
        effect_kinds=(EffectKind.PROVIDER_CALL,),
        allow=True,
        max_risk=RiskLevel.CRITICAL,
        allowed_resources=(
            "test-provider:model-endpoint:test:embeddings",
            "test-provider:model-endpoint:audio:audio-transcriptions",
        ),
    )
    other_rules = tuple(
        rule for rule in default_policy_rules() if EffectKind.PROVIDER_CALL not in rule.effect_kinds
    )
    service.policies.append(
        PolicyDocument.create(
            realm_id=realm.id,
            name="varsayilan",
            revision=2,
            rules=(provider_rule, *other_rules),
        )
    )
    return service, actor.id


def _reference(service: GovernanceService) -> SecretRef:
    return service.secrets.add(
        SecretRef.create(
            realm_id=service.realm.id,
            name="test-provider-credential",
            provider="test-provider",
            purpose="embedding contract test",
            allowed_operations=("embeddings",),
            store_backend=SecretBackend.LOCAL_ENCRYPTED,
            store_locator="TEST_PROVIDER_TOKEN",
        )
    )


def _database_contains(connection: Any, needle: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "select table_schema, table_name, column_name from information_schema.columns"
            " where data_type in ('text', 'jsonb', 'character varying', 'ARRAY')"
            "   and table_schema in ('core', 'security')"
        )
        for schema, table, column in cursor.fetchall():
            cursor.execute(
                f'select count(*) from "{schema}"."{table}" where "{column}"::text like %s',
                (f"%{needle}%",),
            )
            if int(cursor.fetchone()[0]) > 0:
                return True
    return False


def test_exact_provider_chain_consumes_auth_and_persists_only_digests(
    realm_session: tuple[Realm, Any],
) -> None:
    service, actor_id = _service(realm_session)
    reference = _reference(service)
    call = ProviderCall(
        provider_ref="test-provider",
        endpoint_ref="model-endpoint:test",
        operation="embeddings",
        request_identity="contract-test-1",
        payload={"model": "embedding-test", "input": ["merhaba"]},
    )
    target = "test-provider:model-endpoint:test:embeddings"
    effect = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(target,),
        provider_refs=("test-provider",),
        data_classifications=call.data_categories,
        reversible=False,
        touches_external_system=True,
        required_capabilities=("provider.call",),
    )
    authorization = service.issue_authorization(
        request=effect,
        actor_id=actor_id,
        secret_ref_ids=(reference.id,),
    )
    transport = MemoryTransport()
    client = AuthorizedProviderClient(
        service,
        EnvironmentEndpointResolver(
            {("model-endpoint:test", "embeddings"): "TEST_PROVIDER_URL"},
            {"TEST_PROVIDER_URL": ENDPOINT_VALUE},
        ),
        SecretBroker(
            {
                SecretBackend.LOCAL_ENCRYPTED: InMemorySecretStore(
                    {"TEST_PROVIDER_TOKEN": SECRET_VALUE}
                )
            }
        ),
        transport,
    )
    result = client.invoke(
        call,
        secret_ref=reference,
        authorization=authorization,
        consumed_by="provider-security-test",
    )

    assert transport.calls == 1
    assert result.response_digest.startswith("sha256:")
    assert service.authorizations.get(authorization.id).state is AuthorizationState.CONSUMED
    assert service.outbound.get(result.outbound_request_id).state is OutboundState.EXECUTED
    assert not _database_contains(service.connection, SECRET_VALUE)
    assert not _database_contains(service.connection, ENDPOINT_VALUE)


def test_provider_mismatch_fails_before_transport(
    realm_session: tuple[Realm, Any],
) -> None:
    service, actor_id = _service(realm_session)
    reference = _reference(service)
    call = ProviderCall(
        provider_ref="different-provider",
        endpoint_ref="model-endpoint:test",
        operation="embeddings",
        request_identity="contract-test-2",
        payload={"input": ["x"]},
    )
    authorization = service.issue_authorization(
        request=EffectRequest(
            action="provider-call",
            effects=(EffectKind.PROVIDER_CALL,),
            resources=("different-provider:model-endpoint:test:embeddings",),
            provider_refs=("different-provider",),
        ),
        actor_id=actor_id,
        secret_ref_ids=(reference.id,),
    )
    transport = MemoryTransport()
    client = AuthorizedProviderClient(
        service,
        EnvironmentEndpointResolver({}, {}),
        SecretBroker({}),
        transport,
    )
    with pytest.raises(Exception, match="SecretRef provider"):
        client.invoke(
            call,
            secret_ref=reference,
            authorization=authorization,
            consumed_by="provider-security-test",
        )
    assert transport.calls == 0
    assert service.authorizations.get(authorization.id).state is AuthorizationState.ISSUED


def test_exact_whisper_multipart_chain_is_digest_only(
    realm_session: tuple[Realm, Any],
) -> None:
    service, actor_id = _service(realm_session)
    reference = service.secrets.add(
        SecretRef.create(
            realm_id=service.realm.id,
            name="test-whisper-credential",
            provider="test-provider",
            purpose="whisper contract test",
            allowed_operations=("audio-transcriptions",),
            store_backend=SecretBackend.LOCAL_ENCRYPTED,
            store_locator="TEST_PROVIDER_TOKEN",
        )
    )
    body = openai_transcription_body(
        "whisper-test",
        b"RIFF-private-fixture",
        filename="fixture.wav",
        media_type="audio/wav",
        language="tr",
    )
    call = MultipartProviderCall(
        "test-provider",
        "model-endpoint:audio",
        "audio-transcriptions",
        "whisper-contract-1",
        body,
    )
    target = "test-provider:model-endpoint:audio:audio-transcriptions"
    effect = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(target,),
        provider_refs=("test-provider",),
        data_classifications=call.data_categories,
        reversible=False,
        touches_external_system=True,
        required_capabilities=("provider.call",),
    )
    authorization = service.issue_authorization(
        request=effect,
        actor_id=actor_id,
        secret_ref_ids=(reference.id,),
    )
    multipart = MemoryMultipartTransport()
    client = AuthorizedProviderClient(
        service,
        EnvironmentEndpointResolver(
            {("model-endpoint:audio", "audio-transcriptions"): "TEST_AUDIO_URL"},
            {"TEST_AUDIO_URL": "https://models.example.test/v1/audio/transcriptions"},
        ),
        SecretBroker(
            {
                SecretBackend.LOCAL_ENCRYPTED: InMemorySecretStore(
                    {"TEST_PROVIDER_TOKEN": SECRET_VALUE}
                )
            }
        ),
        MemoryTransport(),
        multipart,
    )
    result = client.invoke_multipart(
        call,
        secret_ref=reference,
        authorization=authorization,
        consumed_by="whisper-security-test",
    )
    assert multipart.calls == 1
    assert service.authorizations.get(authorization.id).state is AuthorizationState.CONSUMED
    assert service.outbound.get(result.outbound_request_id).state is OutboundState.EXECUTED
    assert not _database_contains(service.connection, "RIFF-private-fixture")
    assert not _database_contains(
        service.connection, "https://models.example.test/v1/audio/transcriptions"
    )
