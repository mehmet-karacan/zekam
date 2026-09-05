"""Ayni yetkiyi tuketmeye calisan yarisan surecler.

Exact one-shot yetki, iki surec ayni anda denese bile yalnizca bir kez
tuketilebilmelidir.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.governance import EffectRequest, GovernanceService, default_capabilities
from zekam.application.realm_context import bootstrap_realm
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import AuthorizationState
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.concurrency, pytest.mark.postgres]


@pytest.fixture
def prepared(migrated_database: DatabaseSettings) -> Iterator[tuple[Realm, Any, Any]]:
    """Ayni realm'e bagli iki bagimsiz baglanti."""
    slug = f"yetki-{secrets.token_hex(4)}"
    with connect(migrated_database) as first:
        realm = bootstrap_realm(first, slug=slug).realm
        actor = ActorRepository(first, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet")
        )
        governance = GovernanceService(first, realm, actor_id=actor.id)
        governance.ensure_default_policy()
        for capability in default_capabilities(realm.id):
            governance.capabilities.append(capability)

        manager = connect(migrated_database)
        second = manager.__enter__()
        configure_session(second, realm_id=realm.id)
        try:
            yield realm, first, second
        finally:
            manager.__exit__(None, None, None)


def _request() -> EffectRequest:
    return EffectRequest(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=("path:zekam:src/a.py",),
        required_capabilities=("sandbox.write",),
    )


def _actor_id(connection: Any, realm: Realm) -> Any:
    found = ActorRepository(connection, realm.id).find_by_slug("mehmet")
    assert found is not None
    return found.id


def test_only_one_consumer_wins(prepared: tuple[Realm, Any, Any]) -> None:
    realm, first, second = prepared
    actor_id = _actor_id(first, realm)
    request = _request()

    first_service = GovernanceService(first, realm, actor_id=actor_id)
    second_service = GovernanceService(second, realm, actor_id=actor_id)
    authorization = first_service.issue_authorization(request=request, actor_id=actor_id)

    results = [
        first_service.consume_authorization(
            authorization.id, request=request, consumed_by="worker-1"
        ),
        second_service.consume_authorization(
            authorization.id, request=request, consumed_by="worker-2"
        ),
    ]
    assert sum(1 for result in results if result.consumed) == 1
    loser = next(result for result in results if not result.consumed)
    assert loser.reason == "authorization-already-consumed"
    assert second_service.authorizations.get(authorization.id).state is (
        AuthorizationState.CONSUMED
    )


def test_revoke_race_leaves_single_terminal_state(prepared: tuple[Realm, Any, Any]) -> None:
    realm, first, second = prepared
    actor_id = _actor_id(first, realm)
    request = _request()

    first_service = GovernanceService(first, realm, actor_id=actor_id)
    second_service = GovernanceService(second, realm, actor_id=actor_id)
    authorization = first_service.issue_authorization(request=request, actor_id=actor_id)

    consumed = first_service.consume_authorization(
        authorization.id, request=request, consumed_by="worker-1"
    )
    revoked = second_service.revoke_authorization(authorization.id, "yaris")

    assert consumed.consumed
    assert revoked is False
    assert first_service.authorizations.get(authorization.id).state is (AuthorizationState.CONSUMED)


def test_consumed_by_is_recorded_for_the_winner(prepared: tuple[Realm, Any, Any]) -> None:
    realm, first, second = prepared
    actor_id = _actor_id(first, realm)
    request = _request()
    service = GovernanceService(first, realm, actor_id=actor_id)
    authorization = service.issue_authorization(request=request, actor_id=actor_id)

    GovernanceService(second, realm, actor_id=actor_id).consume_authorization(
        authorization.id, request=request, consumed_by="worker-2"
    )
    stored = service.authorizations.get(authorization.id)
    assert stored.consumed_by == "worker-2"
    assert stored.consumed_at is not None
