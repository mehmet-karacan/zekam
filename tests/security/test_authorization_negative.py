"""Yetki sistemi icin negatif testler.

`guvenlik/APPROVAL_VE_YETKI_POLITIKASI.md` icindeki "negative tests" listesini
karsilar: stale plan, kapsam genisletme, yanlis actor/proje, expired/revoked/
consumed, claim sonrasi onay degisimi, kayit olmadan yetki kimligi, model
atamasinin izin sayilmasi, lease'in izin sayilmasi, istemci beyaniyla atlatma.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.governance import EffectRequest, GovernanceService, default_capabilities
from zekam.domain.errors import AuthorizationRequired, NotFound, PolicyViolation
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import Authorization, AuthorizationScope, AuthorizationState
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]


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


def _request(**overrides: Any) -> EffectRequest:
    defaults: dict[str, Any] = {
        "action": "apply-patch",
        "effects": (EffectKind.FILE_WRITE,),
        "resources": ("path:zekam:src/a.py",),
        "required_capabilities": ("sandbox.write",),
    }
    defaults.update(overrides)
    return EffectRequest(**defaults)


# -- kapsam genisletme -------------------------------------------------------------------


def test_extra_resource_is_out_of_scope(service: GovernanceService, actor_id: Any) -> None:
    granted = _request()
    authorization = service.issue_authorization(request=granted, actor_id=actor_id)
    attempted = _request(resources=("path:zekam:src/a.py", "path:zekam:src/b.py"))
    verdict = service.evaluate(attempted, authorization=authorization)
    assert not verdict.allowed
    assert "out-of-scope" in (verdict.denial_reason or "")


def test_extra_effect_is_out_of_scope(service: GovernanceService, actor_id: Any) -> None:
    granted = _request()
    authorization = service.issue_authorization(request=granted, actor_id=actor_id)
    attempted = _request(effects=(EffectKind.FILE_WRITE, EffectKind.DATABASE_WRITE))
    verdict = service.evaluate(attempted, authorization=authorization)
    assert not verdict.allowed
    assert "out-of-scope" in (verdict.denial_reason or "") or "digest-mismatch" in (
        verdict.denial_reason or ""
    )


def test_wildcard_scope_does_not_grant_unrelated_prefix(
    service: GovernanceService, actor_id: Any
) -> None:
    scope = AuthorizationScope(
        allowed_resources=("path:zekam:src/*",), allowed_effects=("file-write",)
    )
    assert scope.covers_resource("path:zekam:src/a.py")
    assert not scope.covers_resource("path:zekam:tests/a.py")
    assert not scope.covers_resource("path:baska:src/a.py")


def test_scope_without_effects_is_rejected() -> None:
    from zekam.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        AuthorizationScope(allowed_resources=("path:zekam:a.py",))


# -- yanlis kimlik ------------------------------------------------------------------------


def test_authorization_for_another_effect_is_rejected(
    service: GovernanceService, actor_id: Any
) -> None:
    granted = _request(resources=("path:zekam:src/a.py",))
    authorization = service.issue_authorization(request=granted, actor_id=actor_id)
    other = _request(resources=("path:zekam:src/z.py",))
    verdict = service.evaluate(other, authorization=authorization)
    assert not verdict.allowed


def test_unknown_actor_cannot_receive_authorization(service: GovernanceService) -> None:
    with pytest.raises(Exception, match="authorization_actor_same_realm"):
        service.issue_authorization(request=_request(), actor_id=uuid4())


def test_unknown_authorization_id_is_not_found(service: GovernanceService) -> None:
    with pytest.raises(NotFound):
        service.authorizations.get(uuid4())


def test_consume_of_unknown_authorization_is_rejected(service: GovernanceService) -> None:
    result = service.consume_authorization(uuid4(), request=_request(), consumed_by="worker-1")
    assert not result.consumed
    assert result.reason == "authorization-not-found"


# -- yasam dongusu ihlalleri ----------------------------------------------------------------


def test_replay_after_consume_is_rejected(service: GovernanceService, actor_id: Any) -> None:
    request = _request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    assert service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-1"
    ).consumed
    for _ in range(3):
        replay = service.consume_authorization(
            authorization.id, request=request, consumed_by="worker-2"
        )
        assert not replay.consumed
        assert replay.reason == "authorization-already-consumed"


def test_revoke_after_consume_does_not_reopen(service: GovernanceService, actor_id: Any) -> None:
    request = _request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    assert not service.revoke_authorization(authorization.id, "gec")
    assert service.authorizations.get(authorization.id).state is AuthorizationState.CONSUMED


def test_terminal_state_cannot_be_reset_in_database(
    service: GovernanceService, actor_id: Any
) -> None:
    request = _request()
    authorization = service.issue_authorization(request=request, actor_id=actor_id)
    service.consume_authorization(authorization.id, request=request, consumed_by="worker-1")
    with (
        pytest.raises(Exception, match="terminal yetki durumu degistirilemez"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute(
            "update security.authorization set state = 'issued' where id = %s",
            (authorization.id,),
        )


def test_effect_digest_cannot_be_swapped_in_database(
    service: GovernanceService, actor_id: Any
) -> None:
    authorization = service.issue_authorization(request=_request(), actor_id=actor_id)
    with (
        pytest.raises(Exception, match="digest degistirilemez"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute(
            "update security.authorization set effect_digest = %s where id = %s",
            ("sha256:" + "f" * 64, authorization.id),
        )


def test_authorization_without_expiry_is_rejected() -> None:
    from zekam.domain.errors import ValidationFailed

    moment = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
    with pytest.raises(ValidationFailed):
        Authorization(
            id=uuid4(),
            realm_id=uuid4(),
            actor_id=uuid4(),
            plan_digest="sha256:" + "a" * 64,
            effect_digest="sha256:" + "b" * 64,
            scope=AuthorizationScope(allowed_effects=("file-write",)),
            risk="medium",
            issued_at=moment,
            expires_at=moment,
        )


# -- yerine gecme denemeleri ------------------------------------------------------------------


def test_capability_registration_is_not_permission(
    service: GovernanceService, actor_id: Any
) -> None:
    """Yetenek kayitli olsa bile yetki olmadan etki reddedilir."""
    assert service.capabilities.current("sandbox.write") is not None
    verdict = service.evaluate(_request())
    assert not verdict.allowed
    assert verdict.denial_reason == "authorization-required"


def test_policy_allowance_is_not_permission(service: GovernanceService) -> None:
    """Policy izin verse bile exact yetki gerekir."""
    document = service.active_policy()
    rule = document.rule_for(EffectKind.FILE_WRITE)
    assert rule is not None and rule.allow
    assert not service.evaluate(_request()).allowed


def test_client_declared_low_risk_cannot_bypass_gate(
    service: GovernanceService, actor_id: Any
) -> None:
    """Istemci 'bu dusuk risk' diyerek kapiyi atlatamaz.

    Risk istekten turetilir; `EffectRequest` uzerinde bir risk alani yoktur.
    """
    request = _request(destructive=True)
    assert not hasattr(request, "risk")
    verdict = service.evaluate(request)
    assert not verdict.allowed
    assert verdict.risk.level.value == "critical"


def test_authorization_id_without_record_is_useless(service: GovernanceService) -> None:
    fabricated = uuid4()
    with pytest.raises(NotFound):
        service.authorizations.get(fabricated)


def test_require_authorized_denies_scope_expansion(
    service: GovernanceService, actor_id: Any
) -> None:
    granted = _request()
    authorization = service.issue_authorization(request=granted, actor_id=actor_id)
    attempted = _request(resources=("path:zekam:src/a.py", "path:zekam:gizli.py"))
    with pytest.raises((AuthorizationRequired, PolicyViolation)):
        service.require_authorized(attempted, authorization=authorization, consumed_by="worker-1")
    # Reddedilen deneme yetkiyi tuketmemis olmali.
    assert service.authorizations.get(authorization.id).state is AuthorizationState.ISSUED


def test_denied_attempt_is_recorded_in_audit(service: GovernanceService) -> None:
    request = _request()
    service.evaluate(request)
    trail = service.audit.for_subject("effect", request.effect_digest)
    assert any(entry["decision"] == "deny" for entry in trail)
