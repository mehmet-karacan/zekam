"""Realm yalitiminin row-level security ile fail-closed uygulandigi negatif testler."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from zekam.application.config import DatabaseSettings
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]


def _create_realm(connection: Any, slug: str) -> Realm:
    realm = Realm.create(slug=slug, display_name=slug)
    configure_session(connection, realm_id=realm.id, role=None)
    RealmRepository(connection).create(realm)
    return realm


def test_rls_denies_reads_when_realm_is_not_selected(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        realm = _create_realm(connection, f"gizli-{uuid4().hex[:8]}")
        ActorRepository(connection, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet")
        )
        # Realm secilmeden uygulama rolune gecilir.
        configure_session(connection, realm_id=None)
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from core.actor")
            assert cursor.fetchone()[0] == 0
            cursor.execute("select count(*) from core.realm")
            assert cursor.fetchone()[0] == 0


def test_rls_hides_other_realms(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        first = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        ActorRepository(connection, first.id).add(
            Actor.create(realm=first, kind=ActorKind.HUMAN, slug="birinci")
        )
        second = _create_realm(connection, f"iki-{uuid4().hex[:8]}")
        ActorRepository(connection, second.id).add(
            Actor.create(realm=second, kind=ActorKind.HUMAN, slug="ikinci")
        )

        configure_session(connection, realm_id=first.id)
        actors = ActorRepository(connection, first.id).list_all()
        assert [actor.slug for actor in actors] == ["birinci"]
        assert RealmRepository(connection).count_visible() == 1


def test_rls_blocks_insert_into_foreign_realm(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        first = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        second = _create_realm(connection, f"iki-{uuid4().hex[:8]}")

        configure_session(connection, realm_id=first.id)
        intruder = Actor.create(realm=second, kind=ActorKind.AGENT, slug="sizinti")
        # Uygulama katmani atlanarak dogrudan yazma denenir.
        with (
            pytest.raises(Exception, match=r"row-level security|policy"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into core.actor"
                " (id, realm_id, kind, slug, display_name) values (%s, %s, %s, %s, %s)",
                (
                    intruder.id,
                    intruder.realm_id,
                    intruder.kind.value,
                    intruder.slug,
                    intruder.display_name,
                ),
            )


def test_rls_blocks_update_of_foreign_realm_row(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        first = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        second = _create_realm(connection, f"iki-{uuid4().hex[:8]}")
        target = ActorRepository(connection, second.id).add(
            Actor.create(realm=second, kind=ActorKind.HUMAN, slug="hedef")
        )

        configure_session(connection, realm_id=first.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "update core.actor set display_name = 'ele gecirildi' where id = %s", (target.id,)
            )
            assert cursor.rowcount == 0


def test_rls_blocks_delete_of_foreign_realm_row(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        first = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        second = _create_realm(connection, f"iki-{uuid4().hex[:8]}")
        target = ActorRepository(connection, second.id).add(
            Actor.create(realm=second, kind=ActorKind.HUMAN, slug="hedef")
        )

        configure_session(connection, realm_id=first.id)
        with connection.cursor() as cursor:
            cursor.execute("delete from core.actor where id = %s", (target.id,))
            assert cursor.rowcount == 0

        configure_session(connection, realm_id=second.id)
        assert ActorRepository(connection, second.id).get(target.id).slug == "hedef"


def test_revision_and_event_are_realm_scoped(migrated_database: DatabaseSettings) -> None:
    from zekam.infrastructure.postgres.core_repository import EventStore, RevisionStore

    with connect(migrated_database) as connection:
        first = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        configure_session(connection, realm_id=first.id)
        entity_id = uuid4()
        RevisionStore(connection, first.id).append(
            entity_type="work.item", entity_id=entity_id, payload={"a": 1}, reason="ilk"
        )
        EventStore(connection, first.id).append(
            event_type="work.created", entity_type="work.item", entity_id=entity_id
        )

        second = _create_realm(connection, f"iki-{uuid4().hex[:8]}")
        configure_session(connection, realm_id=second.id)
        assert (
            RevisionStore(connection, second.id).history(
                entity_type="work.item", entity_id=entity_id
            )
            == ()
        )
        assert EventStore(connection, second.id).stream() == ()


def test_application_role_cannot_bypass_rls(migrated_database: DatabaseSettings) -> None:
    with connect(migrated_database) as connection:
        realm = _create_realm(connection, f"bir-{uuid4().hex[:8]}")
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute("select current_user")
            assert cursor.fetchone()[0] == "zekam_app"
            cursor.execute("select rolbypassrls from pg_roles where rolname = current_user")
            assert cursor.fetchone()[0] is False
