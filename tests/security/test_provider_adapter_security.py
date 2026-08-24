"""Authorized provider client'in gercek DB, sahte transport guvenlik testi.

Test hicbir ag cagrisi yapmaz. Provider etkisi exact policy + authorization ile
gercek governance tablolarinda yurutulur; credential yalniz bellek ici fake
transport'a ulasir.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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
from zekam.domain.canonical import digest
from zekam.domain.model_invocation import (
    GatewaySourceLabel,
    GatewayTransportProvenance,
    ModelRequestManifest,
    _issue_gateway_permit,
)
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

    def post_json(
        self,
        endpoint: str,
        payload: Any,
        credential: Any,
        *,
        gateway_provenance: GatewayTransportProvenance,
    ) -> dict[str, Any]:
        assert gateway_provenance.manifest_digest.startswith("sha256:")
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


def _gateway_evidence(
    service: GovernanceService,
    call: ProviderCall | MultipartProviderCall,
    authorization: Any,
) -> tuple[ModelRequestManifest, Any]:
    created_at = dt.datetime.now(dt.UTC)
    missing = (
        "assignment_id",
        "checkpoint_digest",
        "context_fragment_set_digest",
        "context_manifest_digest",
        "context_packet_digest",
        "execution_envelope_digest",
        "execution_envelope_id",
        "max_cost_micros",
        "max_input_tokens",
        "max_output_tokens",
        "model_visible_payload_digest",
        "output_schema_digest",
        "policy_digest",
        "role",
        "route_decision_digest",
        "route_expires_at",
        "run_id",
        "source_revision",
    )
    manifest = ModelRequestManifest.create(
        execution_envelope_id=None,
        execution_envelope_digest=None,
        realm_id=service.realm.id,
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
        step_id="provider-security",
        run_id=None,
        job_id=uuid4(),
        attempt_id=uuid4(),
        assignment_id=None,
        role=None,
        risk=authorization.risk,
        route_decision_digest=None,
        model_id="security-test-model",
        provider_ref=call.provider_ref,
        context_manifest_digest=None,
        context_fragment_set_digest=None,
        model_visible_payload_digest=None,
        context_packet_digest=None,
        checkpoint_digest=None,
        source_revision=None,
        policy_digest=None,
        payload_digest=call.payload_digest,
        authorization_scope_digest=digest(authorization.scope.body()),
        output_schema_digest=None,
        idempotency_key=digest({"request_identity": call.request_identity}),
        max_input_tokens=None,
        max_output_tokens=None,
        max_cost_micros=None,
        deadline=created_at + dt.timedelta(minutes=1),
        route_expires_at=None,
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        missing_bindings=missing,
        created_at=created_at,
    )
    return manifest, _issue_gateway_permit(manifest, attempt_id=uuid4(), claim_id=uuid4())


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
    manifest, permit = _gateway_evidence(service, call, authorization)
    result = client.invoke(
        call,
        secret_ref=reference,
        authorization=authorization,
        consumed_by="provider-security-test",
        manifest=manifest,
        gateway_permit=permit,
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
    manifest, permit = _gateway_evidence(service, call, authorization)
    with pytest.raises(Exception, match="SecretRef provider"):
        client.invoke(
            call,
            secret_ref=reference,
            authorization=authorization,
            consumed_by="provider-security-test",
            manifest=manifest,
            gateway_permit=permit,
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
    manifest, permit = _gateway_evidence(service, call, authorization)
    result = client.invoke_multipart(
        call,
        secret_ref=reference,
        authorization=authorization,
        consumed_by="whisper-security-test",
        manifest=manifest,
        gateway_permit=permit,
    )
    assert multipart.calls == 1
    assert service.authorizations.get(authorization.id).state is AuthorizationState.CONSUMED
    assert service.outbound.get(result.outbound_request_id).state is OutboundState.EXECUTED
    assert not _database_contains(service.connection, "RIFF-private-fixture")
    assert not _database_contains(
        service.connection, "https://models.example.test/v1/audio/transcriptions"
    )
