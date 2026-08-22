"""Governance'in gercek PostgreSQL uzerindeki davranisi."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.governance import (
    DEFAULT_POLICY_NAME,
    EffectRequest,
    GovernanceService,
    ProviderGate,
    default_capabilities,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.errors import AuthorizationRequired, NotFound, PolicyViolation
from zekam.domain.policy import PolicyDocument, PolicyRule, RiskLevel
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import (
    AuthorizationState,
    DataClassification,
    OutboundRequest,
    OutboundState,
    SecretBackend,
    SecretRef,
    SecretStatus,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

PAYLOAD_DIGEST = "sha256:" + "e" * 64


@pytest.fixture
def actor_id(realm_session: tuple[Realm, Any]):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet")
    )
    return actor.id


@pytest.fixture
def service(realm_session: tuple[Realm, Any], actor_id: Any) -> GovernanceService:
    realm, connection = realm_session
    governance = GovernanceService(connection, realm, actor_id=actor_id)
    governance.ensure_default_policy()
    for capability in default_capabilities(realm.id):
        governance.capabilities.append(capability)
    return governance


def _write_request(**overrides: Any) -> EffectRequest:
    defaults: dict[str, Any] = {
        "action": "apply-patch",
        "effects": (EffectKind.FILE_WRITE,),
        "resources": ("path:zekam:src/zekam/x.py",),
        "required_capabilities": ("sandbox.write",),
    }
    defaults.update(overrides)
    return EffectRequest(**defaults)


# -- policy ve capability -------------------------------------------------------------


def test_default_policy_is_created_once(service: GovernanceService) -> None:
    first = service.ensure_default_policy()
    second = service.ensure_default_policy()
    assert first.policy_digest == second.policy_digest
    assert service.active_policy(DEFAULT_POLICY_NAME).revision == 1


def test_policy_is_append_only(service: GovernanceService) -> None:
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute("update security.policy set name = 'degistirildi'")


def test_missing_policy_raises_not_found(service: GovernanceService) -> None:
    with pytest.raises(NotFound):
        service.active_policy("olmayan-policy")


def test_capabilities_are_versioned(service: GovernanceService) -> None:
    assert service.capabilities.current("sandbox.write") is not None
    assert len(service.capabilities.list_all()) == 7


# -- kapi zinciri -----------------------------------------------------------------------


def test_read_only_action_passes_without_authorization(service: GovernanceService) -> None:
    request = EffectRequest(action="status", effects=(EffectKind.NONE,))
    verdict = service.evaluate(request)
    assert verdict.allowed
    assert verdict.auto_approved
    assert verdict.risk.level is RiskLevel.NONE


def test_write_without_authorization_is_denied(service: GovernanceService) -> None:
    verdict = service.evaluate(_write_request())
    assert not verdict.allowed
    assert verdict.denial_reason == "authorization-required"


def test_missing_capability_is_denied_first(service: GovernanceService) -> None:
    request = _write_request(required_capabilities=("olmayan.yetenek",))
    verdict = service.evaluate(request)
    assert not verdict.allowed
    assert verdict.denial_reason is not None
    assert verdict.denial_reason.startswith("capability-missing")
    assert verdict.gates.decisions[0].gate == "capability"


def test_network_is_denied_by_default_policy(service: GovernanceService) -> None:
    request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        required_capabilities=("provider.call",),
    )
    verdict = service.evaluate(request)
    assert not verdict.allowed
    assert "policy-denies:provider-call" in (verdict.denial_reason or "")


def test_network_policy_resource_allowlist_is_exact(service: GovernanceService) -> None:
    policy = PolicyDocument.create(
        realm_id=service.realm.id,
        name="pypi-exact",
        revision=1,
        rules=(
            PolicyRule(
                name="pypi-only",
                effect_kinds=(EffectKind.NETWORK_CALL,),
                allow=True,
                max_risk=RiskLevel.MEDIUM,
                allowed_resources=("provider:pypi.org:pip-audit-json",),
            ),
        ),
    )
    exact = service.evaluate(
        EffectRequest(
            action="dependency-audit-pypi",
            effects=(EffectKind.NETWORK_CALL,),
            resources=("provider:pypi.org:pip-audit-json",),
            provider_refs=("pypi.org",),
            touches_external_system=True,
        ),
        policy=policy,
        record_audit=False,
    )
    other = service.evaluate(
        EffectRequest(
            action="other-network",
            effects=(EffectKind.NETWORK_CALL,),
            resources=("provider:other.example:json",),
            provider_refs=("other.example",),
            touches_external_system=True,
        ),
        policy=policy,
        record_audit=False,
    )
    assert not exact.allowed
    assert exact.denial_reason == "authorization-required"
    assert not other.allowed
    assert other.denial_reason == "resource-out-of-policy:network-call:provider:other.example:json"


def test_push_is_denied_by_default_policy(service: GovernanceService) -> None:
    request = EffectRequest(action="push", effects=(EffectKind.GIT_PUSH,))
    verdict = service.evaluate(request)
    assert not verdict.allowed
    assert "policy-denies:git-push" in (verdict.denial_reason or "")


def test_destructive_work_exceeds_policy_risk_ceiling(service: GovernanceService) -> None:
    request = _write_request(destructive=True)
    verdict = service.evaluate(request)
    assert not verdict.allowed
    assert "risk-above-policy-ceiling" in (verdict.denial_reason or "")


def test_authorized_write_passes_every_gate(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    verdict = service.evaluate(request, authorization=authorization)
    assert verdict.allowed
    assert [decision.gate for decision in verdict.gates.decisions] == [
        "capability",
        "policy",
        "risk",
        "scope",
        "authorization",
    ]


# -- yetki yasam dongusu ------------------------------------------------------------------


def test_authorization_is_bound_to_effect_digest(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    assert authorization.effect_digest == request.effect_digest
    assert authorization.state is AuthorizationState.ISSUED


def test_consume_marks_authorization_used(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    result = service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-1"
    )
    assert result.consumed
    assert service.authorizations.get(authorization.id).state is AuthorizationState.CONSUMED


def test_second_consume_is_rejected(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    replay = service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-2"
    )
    assert not replay.consumed
    assert replay.reason == "authorization-already-consumed"


def test_consume_with_different_effect_is_rejected(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    other = _write_request(resources=("path:zekam:baska.py",))
    result = service.consume_authorization(authorization.id, request=other, consumed_by="worker-1")
    assert not result.consumed
    assert result.reason == "effect-digest-mismatch"
    assert service.authorizations.get(authorization.id).state is AuthorizationState.ISSUED


def test_revoked_authorization_cannot_be_consumed(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    assert service.revoke_authorization(authorization.id, "kullanici iptal etti")
    result = service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-1"
    )
    assert not result.consumed
    assert result.reason == "authorization-revoked"


def test_consumed_authorization_cannot_be_revoked(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    assert service.revoke_authorization(authorization.id, "gec kalindi") is False


def test_expired_authorization_is_rejected(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(
        request=request, actor_id=actor_id, lifetime=dt.timedelta(seconds=1)
    )
    later = authorization.expires_at + dt.timedelta(seconds=1)
    result = service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-1", now=later
    )
    assert not result.consumed
    assert result.reason == "authorization-expired"


def test_expire_stale_moves_authorizations_to_terminal(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _write_request()
    authorization = service.issue_authorization(
        request=request, actor_id=actor_id, lifetime=dt.timedelta(seconds=1)
    )
    later = authorization.expires_at + dt.timedelta(seconds=1)
    assert service.authorizations.expire_stale(now=later) >= 1
    assert service.authorizations.get(authorization.id).state is AuthorizationState.EXPIRED


def test_authorization_scope_cannot_be_widened_in_database(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    with (
        pytest.raises(Exception, match="kapsami genisletilemez"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute(
            "update security.authorization set allowed_resources = %s where id = %s",
            (["path:zekam:*"], authorization.id),
        )


def test_authorization_cannot_be_deleted(service: GovernanceService, actor_id: Any) -> None:
    service.issue_authorization(request=_write_request(), actor_id=actor_id)
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute("delete from security.authorization")


def test_require_authorized_consumes_and_returns(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    consumed = service.require_authorized(
        request, authorization=authorization, consumed_by="worker-1"
    )
    assert consumed.state is AuthorizationState.CONSUMED


def test_require_authorized_raises_without_authorization(service: GovernanceService) -> None:
    with pytest.raises(AuthorizationRequired):
        service.require_authorized(_write_request(), authorization=None, consumed_by="worker-1")


def test_require_authorized_raises_on_policy_denial(
    service: GovernanceService, actor_id: Any
) -> None:
    request = EffectRequest(action="push", effects=(EffectKind.GIT_PUSH,))
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    with pytest.raises(PolicyViolation):
        service.require_authorized(request, authorization=authorization, consumed_by="worker-1")


# -- plan baglama ---------------------------------------------------------------------------


def test_authorization_binds_to_plan(
    realm_session: tuple[Realm, Any], service: GovernanceService, actor_id: Any, tmp_path: Path
) -> None:
    realm, connection = realm_session
    root = tmp_path / "kaynak"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    work = WorkGraphService(connection, realm)
    item = work.create_item(project_id=project.id, type=WorkType.TASK, title="Yama")
    plan = work.create_plan(
        item.id,
        source_revision="sha256:" + "b" * 64,
        policy_digest=service.active_policy().policy_digest,
        steps=(
            PlanStep(
                step_id="yaz",
                title="Yaz",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:src/zekam/x.py",),
            ),
        ),
    )
    authorization = service.issue_authorization(
        request=_write_request(), actor_id=actor_id, plan=plan
    )
    stored = service.authorizations.get(authorization.id)
    assert stored.plan_id == plan.id
    assert stored.work_item_id == item.id
    assert stored.plan_digest == plan.plan_digest


# -- outbound gate ------------------------------------------------------------------------


def _outbound(realm: Realm, **overrides: Any) -> OutboundRequest:
    defaults: dict[str, Any] = {
        "realm_id": realm.id,
        "provider_ref": "anthropic",
        "endpoint_ref": "messages",
        "operation": "create",
        "payload_digest": PAYLOAD_DIGEST,
        "request_identity": "run-1",
        "data_categories": (DataClassification.INTERNAL,),
    }
    defaults.update(overrides)
    return OutboundRequest.prepare(**defaults)


def test_prepare_records_without_network(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm))
    assert request.state is OutboundState.PREPARED
    assert service.outbound.get(request.id).state is OutboundState.PREPARED


def test_secret_class_payload_is_denied_at_prepare(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm, data_categories=(DataClassification.SECRET,)))
    assert request.state is OutboundState.DENIED
    assert "forbidden-data-class" in (request.denial_reason or "")


def test_local_only_payload_is_denied(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    request = ProviderGate(service).prepare(
        _outbound(realm, data_categories=(DataClassification.LOCAL_ONLY,))
    )
    assert request.state is OutboundState.DENIED


def test_apply_requires_matching_provider_scope(
    service: GovernanceService, realm_session: tuple[Realm, Any], actor_id: Any
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm))
    effect_request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(request.target,),
        provider_refs=("baska-saglayici",),
    )
    authorization = service.issue_authorization(request=effect_request, actor_id=actor_id)
    with pytest.raises(PolicyViolation, match="provider-out-of-scope"):
        gate.apply(request, authorization=authorization)


def test_apply_requires_matching_endpoint_scope(
    service: GovernanceService, realm_session: tuple[Realm, Any], actor_id: Any
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm))
    effect_request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=("anthropic:embeddings:create",),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(request=effect_request, actor_id=actor_id)
    with pytest.raises(PolicyViolation, match="endpoint-out-of-scope"):
        gate.apply(request, authorization=authorization)


def test_apply_approves_exact_match(
    service: GovernanceService, realm_session: tuple[Realm, Any], actor_id: Any
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm))
    effect_request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(request.target,),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(request=effect_request, actor_id=actor_id)
    approved = gate.apply(request, authorization=authorization)
    assert approved.state is OutboundState.APPROVED
    assert approved.authorization_id == authorization.id


def test_restricted_data_requires_reviewed_disclosure(
    service: GovernanceService, realm_session: tuple[Realm, Any], actor_id: Any
) -> None:
    realm, _ = realm_session
    gate = ProviderGate(service)
    request = gate.prepare(_outbound(realm, data_categories=(DataClassification.RESTRICTED,)))
    effect_request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(request.target,),
        provider_refs=("anthropic",),
    )
    authorization = service.issue_authorization(request=effect_request, actor_id=actor_id)
    with pytest.raises(PolicyViolation, match="data-class-not-reviewed"):
        gate.apply(request, authorization=authorization)


def test_executed_state_requires_authorization_in_database(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    request = ProviderGate(service).prepare(_outbound(realm))
    with (
        pytest.raises(Exception, match="outbound_executed_requires_authorization"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute(
            "update security.outbound_request set state = 'executed' where id = %s",
            (request.id,),
        )


# -- SecretRef ---------------------------------------------------------------------------------


def test_secret_reference_roundtrip(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    reference = SecretRef.create(
        realm_id=realm.id,
        name="anthropic-api",
        provider="anthropic",
        purpose="chat",
        allowed_operations=("chat",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator="ANTHROPIC_API_KEY",
    )
    service.secrets.add(reference)
    stored = service.secrets.get(reference.id)
    assert stored.metadata_digest == reference.metadata_digest
    assert stored.store_locator == "ANTHROPIC_API_KEY"


def test_secret_revocation_is_visible(
    service: GovernanceService, realm_session: tuple[Realm, Any]
) -> None:
    realm, _ = realm_session
    reference = SecretRef.create(
        realm_id=realm.id,
        name="gecici",
        provider="test",
        purpose="test",
        allowed_operations=("chat",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator="GECICI",
    )
    service.secrets.add(reference)
    service.secrets.set_status(reference.id, SecretStatus.REVOKED)
    assert service.secrets.get(reference.id).status is SecretStatus.REVOKED
    assert service.secrets.current_by_name("gecici") is None


def test_cross_realm_secret_is_rejected(service: GovernanceService) -> None:
    foreign = SecretRef.create(
        realm_id=uuid4(),
        name="yabanci",
        provider="test",
        purpose="test",
        allowed_operations=("chat",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator="YABANCI",
    )
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        service.secrets.add(foreign)


# -- denetim ------------------------------------------------------------------------------------


def test_every_gate_decision_is_audited(service: GovernanceService) -> None:
    request = _write_request()
    service.evaluate(request)
    trail = service.audit.for_subject("effect", request.effect_digest)
    assert trail
    assert trail[-1]["decision"] == "deny"
    assert trail[-1]["reason"] == "authorization-required"


def test_authorization_lifecycle_is_audited(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    service.revoke_authorization(authorization.id, "artik gerekmiyor")

    trail = service.audit.for_subject("authorization", str(authorization.id))
    actions = [entry["action"] for entry in trail]
    assert actions == [
        "authorization.issued",
        "authorization.consume",
        "authorization.revoke",
    ]
    assert trail[1]["decision"] == "allow"
    assert trail[2]["decision"] == "deny"


def test_audit_is_append_only(service: GovernanceService) -> None:
    service.evaluate(_write_request())
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute("update security.audit_event set reason = 'degistirildi'")


def test_audit_links_actor_and_authorization(service: GovernanceService, actor_id: Any) -> None:
    request = _write_request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    entry = service.audit.for_subject("authorization", str(authorization.id))[0]
    assert entry["actor_id"] == str(actor_id)
    assert entry["authorization_id"] == str(authorization.id)
    assert entry["evidence_digest"].startswith("sha256:")
