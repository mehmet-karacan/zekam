"""Realm, actor ve calisma kimligi alan kurallari."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.realm import (
    Actor,
    ActorKind,
    ClientIdentity,
    ExecutionIdentity,
    LifecycleStatus,
    Realm,
    active_actors,
    assert_same_realm,
    realm_of,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)


def _realm(slug: str = "yerel") -> Realm:
    return Realm.create(slug=slug, now=NOW)


def test_realm_create_uses_uuid7_and_defaults() -> None:
    realm = _realm()
    assert realm.id.version == 7
    assert realm.revision == 1
    assert realm.status is LifecycleStatus.ACTIVE
    assert realm.display_name == "yerel"


def test_realm_is_frozen() -> None:
    realm = _realm()
    with pytest.raises((AttributeError, TypeError)):
        realm.slug = "baska"  # type: ignore[misc]


@pytest.mark.parametrize("slug", ["Yerel", "-x", "x" * 100])
def test_realm_rejects_invalid_slug(slug: str) -> None:
    with pytest.raises(ValidationFailed):
        Realm.create(slug=slug, now=NOW)


def test_realm_rejects_blank_display_name() -> None:
    with pytest.raises(ValidationFailed):
        Realm(id=_realm().id, slug="yerel", display_name="   ", created_at=NOW)


def test_realm_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationFailed):
        Realm(
            id=_realm().id,
            slug="yerel",
            display_name="Yerel",
            created_at=dt.datetime(2026, 8, 20),
        )


def test_realm_rejects_zero_revision() -> None:
    with pytest.raises(ValidationFailed):
        Realm(id=_realm().id, slug="yerel", display_name="Yerel", created_at=NOW, revision=0)


def test_actor_belongs_to_creating_realm() -> None:
    realm = _realm()
    actor = Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet", now=NOW)
    assert actor.realm_id == realm.id
    assert actor.is_active
    assert realm_of(actor) == realm.id


def test_actor_kinds_are_closed_set() -> None:
    assert {kind.value for kind in ActorKind} == {"human", "agent", "service", "system"}


def test_assert_same_realm_accepts_matching_records() -> None:
    realm = _realm()
    actor = Actor.create(realm=realm, kind=ActorKind.AGENT, slug="builder", now=NOW)
    assert assert_same_realm(realm, actor) == realm.id


def test_assert_same_realm_rejects_cross_realm() -> None:
    first = _realm("bir")
    second = _realm("iki")
    actor = Actor.create(realm=second, kind=ActorKind.AGENT, slug="builder", now=NOW)
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        assert_same_realm(first, actor)


def test_assert_same_realm_requires_argument() -> None:
    with pytest.raises(ValidationFailed):
        assert_same_realm()


def test_active_actors_filters_suspended() -> None:
    realm = _realm()
    first = Actor.create(realm=realm, kind=ActorKind.AGENT, slug="bir", now=NOW)
    second = Actor.create(realm=realm, kind=ActorKind.AGENT, slug="iki", now=NOW)
    suspended = Actor(
        id=second.id,
        realm_id=second.realm_id,
        kind=second.kind,
        slug=second.slug,
        display_name=second.display_name,
        created_at=second.created_at,
        status=LifecycleStatus.SUSPENDED,
    )
    assert active_actors([first, suspended]) == (first,)


def test_client_identity_declares_capabilities_without_authority() -> None:
    client = ClientIdentity(name="claude-code", version="1.0", capabilities=frozenset({"subagent"}))
    assert client.supports("subagent")
    assert not client.supports("parallel")
    assert "authority" not in client.as_dict()


def test_client_identity_requires_version() -> None:
    with pytest.raises(ValidationFailed):
        ClientIdentity(name="codex", version="  ")


def test_execution_identity_is_canonically_hashable() -> None:
    realm = _realm()
    actor = Actor.create(realm=realm, kind=ActorKind.AGENT, slug="builder", now=NOW)
    identity = ExecutionIdentity(
        realm_id=realm.id,
        actor_id=actor.id,
        client=ClientIdentity(name="zekam-cli", version="0.1.0"),
        process_label="worker-1",
        started_at=NOW,
    )
    assert digest(identity.as_dict()) == digest(identity.as_dict())
    assert identity.as_dict()["process_label"] == "worker-1"


def test_execution_identity_rejects_blank_process_label() -> None:
    realm = _realm()
    with pytest.raises(ValidationFailed):
        ExecutionIdentity(
            realm_id=realm.id,
            actor_id=realm.id,
            client=ClientIdentity(name="zekam-cli", version="0.1.0"),
            process_label="",
            started_at=NOW,
        )
