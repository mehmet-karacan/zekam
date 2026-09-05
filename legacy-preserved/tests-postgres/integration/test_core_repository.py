"""Realm, actor, revision ve event adapterlerinin gercek PostgreSQL davranisi."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest

from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.infrastructure.postgres.connection import reset_role
from zekam.infrastructure.postgres.core_repository import (
    ActorRepository,
    EventStore,
    RealmRepository,
    RevisionStore,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ENTITY = "work.item"


def _actor(realm: Realm, slug: str = "mehmet") -> Actor:
    return Actor.create(realm=realm, kind=ActorKind.HUMAN, slug=slug)


def test_realm_roundtrip(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    stored = RealmRepository(connection).get(realm.id)
    assert stored.id == realm.id
    assert stored.slug == realm.slug
    assert stored.revision == 1


def test_realm_lookup_by_slug(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    assert RealmRepository(connection).find_by_slug(realm.slug) is not None
    assert RealmRepository(connection).find_by_slug("olmayan-realm") is None


def test_actor_roundtrip(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    repository = ActorRepository(connection, realm.id)
    actor = repository.add(_actor(realm))
    assert repository.get(actor.id).slug == actor.slug
    assert repository.find_by_slug(actor.slug) is not None
    assert len(repository.list_all()) == 1


def test_missing_actor_raises_not_found(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(NotFound):
        ActorRepository(connection, realm.id).get(uuid4())


def test_cross_realm_actor_insert_is_rejected_by_application(
    realm_session: tuple[Realm, Any],
) -> None:
    realm, connection = realm_session
    other = Realm.create(slug="baska-realm")
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        ActorRepository(connection, realm.id).add(_actor(other))


def test_duplicate_actor_slug_is_rejected(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    repository = ActorRepository(connection, realm.id)
    repository.add(_actor(realm))
    with pytest.raises(Exception, match="actor_slug_unique_per_realm"):
        repository.add(_actor(realm))


def test_revision_chain_is_built_and_verifiable(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    entity_id = uuid4()

    first = store.append(
        entity_type=ENTITY, entity_id=entity_id, payload={"state": "proposed"}, reason="olustur"
    )
    second = store.append(
        entity_type=ENTITY, entity_id=entity_id, payload={"state": "ready"}, reason="hazirla"
    )

    assert first.revision == 1
    assert first.previous_digest is None
    assert second.revision == 2
    assert second.previous_digest == first.payload_digest
    assert store.verify_chain(entity_type=ENTITY, entity_id=entity_id)
    assert [
        record.revision for record in store.history(entity_type=ENTITY, entity_id=entity_id)
    ] == [
        1,
        2,
    ]


def test_optimistic_concurrency_rejects_stale_writer(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    entity_id = uuid4()
    store.append(entity_type=ENTITY, entity_id=entity_id, payload={"n": 1}, reason="ilk")
    with pytest.raises(ConcurrencyConflict):
        store.append(
            entity_type=ENTITY,
            entity_id=entity_id,
            payload={"n": 2},
            reason="eski",
            expected_revision=0,
        )


#: Uygulama rolu UPDATE/DELETE yetkisi almaz; tablo sahibinde ise trigger devreye girer.
DENIED_PATTERN = "append-only|permission denied"


def test_revision_update_is_denied_for_application_role(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    store.append(entity_type=ENTITY, entity_id=uuid4(), payload={"n": 1}, reason="ilk")
    with pytest.raises(Exception, match=DENIED_PATTERN), connection.cursor() as cursor:
        cursor.execute("update core.revision set reason = 'degistirildi'")


def test_revision_delete_is_denied_for_application_role(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    store.append(entity_type=ENTITY, entity_id=uuid4(), payload={"n": 1}, reason="ilk")
    with pytest.raises(Exception, match=DENIED_PATTERN), connection.cursor() as cursor:
        cursor.execute("delete from core.revision")


def test_revision_update_is_denied_even_for_table_owner(realm_session: tuple[Realm, Any]) -> None:
    """Yetki katmani asilsa bile append-only trigger fail-closed calisir."""
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    store.append(entity_type=ENTITY, entity_id=uuid4(), payload={"n": 1}, reason="ilk")
    reset_role(connection)
    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("update core.revision set reason = 'degistirildi'")


def test_revision_delete_is_denied_even_for_table_owner(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    store.append(entity_type=ENTITY, entity_id=uuid4(), payload={"n": 1}, reason="ilk")
    reset_role(connection)
    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("delete from core.revision")


def test_revision_gap_is_rejected_by_database(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    entity_id = uuid4()
    with pytest.raises(Exception, match="ilk revision 1 olmali"), connection.cursor() as cursor:
        cursor.execute(
            "insert into core.revision"
            " (id, realm_id, entity_type, entity_id, revision, payload, payload_digest,"
            "  previous_digest, reason)"
            " values (%s, %s, %s, %s, 5, '{}'::jsonb, %s, %s, 'bosluk')",
            (uuid4(), realm.id, ENTITY, entity_id, "sha256:" + "0" * 64, "sha256:" + "1" * 64),
        )


def test_broken_chain_digest_is_rejected_by_database(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = RevisionStore(connection, realm.id)
    entity_id = uuid4()
    store.append(entity_type=ENTITY, entity_id=entity_id, payload={"n": 1}, reason="ilk")
    with pytest.raises(Exception, match="zinciri kopuk"), connection.cursor() as cursor:
        cursor.execute(
            "insert into core.revision"
            " (id, realm_id, entity_type, entity_id, revision, payload, payload_digest,"
            "  previous_digest, reason)"
            " values (%s, %s, %s, %s, 2, '{}'::jsonb, %s, %s, 'kopuk')",
            (uuid4(), realm.id, ENTITY, entity_id, "sha256:" + "0" * 64, "sha256:" + "1" * 64),
        )


def test_events_are_sequenced_and_queryable(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    store = EventStore(connection, realm.id)
    entity_id = uuid4()
    correlation = uuid4()

    first = store.append(
        event_type="work.created",
        entity_type=ENTITY,
        entity_id=entity_id,
        payload={"title": "ilk is"},
        correlation_id=correlation,
    )
    second = store.append(
        event_type="work.updated",
        entity_type=ENTITY,
        entity_id=entity_id,
        correlation_id=correlation,
        causation_id=first.id,
    )

    assert second.sequence > first.sequence
    stream = store.stream()
    assert [event.event_type for event in stream] == ["work.created", "work.updated"]
    assert store.stream(after_sequence=first.sequence)[0].id == second.id
    assert len(store.for_entity(entity_type=ENTITY, entity_id=entity_id)) == 2


def test_event_update_is_denied_for_application_role(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    EventStore(connection, realm.id).append(
        event_type="work.created", entity_type=ENTITY, entity_id=uuid4()
    )
    with pytest.raises(Exception, match=DENIED_PATTERN), connection.cursor() as cursor:
        cursor.execute("update core.event set event_type = 'degistirildi'")


def test_event_mutation_is_denied_even_for_table_owner(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    EventStore(connection, realm.id).append(
        event_type="work.created", entity_type=ENTITY, entity_id=uuid4()
    )
    reset_role(connection)
    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("update core.event set event_type = 'degistirildi'")
    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("delete from core.event")


def test_event_actor_must_belong_to_same_realm(realm_session: tuple[Realm, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(Exception, match="event_actor_same_realm"), connection.cursor() as cursor:
        cursor.execute(
            "insert into core.event"
            " (id, realm_id, event_type, entity_type, entity_id, payload_digest, actor_id,"
            "  occurred_at)"
            " values (%s, %s, 'work.created', %s, %s, %s, %s, %s)",
            (
                uuid4(),
                realm.id,
                ENTITY,
                uuid4(),
                "sha256:" + "0" * 64,
                uuid4(),
                dt.datetime.now(dt.UTC),
            ),
        )
