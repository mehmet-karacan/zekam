"""Canonical client lifecycle ingest and ACK PostgreSQL tests."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

import zekam.infrastructure.postgres.client_lifecycle_repository as lifecycle_repository_module
from zekam.application.opencode_lifecycle import record_event
from zekam.domain.canonical import digest as canonical_digest
from zekam.domain.errors import ConcurrencyConflict, ValidationFailed
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.client_lifecycle_repository import ClientLifecycleRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.UTC)


def test_ingest_is_ordered_idempotent_and_acknowledged(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    first = record_event(
        tmp_path, event_type="session.created", session_id="ses_ack", now=NOW
    ).document()
    second = record_event(
        tmp_path,
        event_type="session.idle",
        session_id="ses_ack",
        now=NOW + dt.timedelta(seconds=1),
    ).document()
    repository = ClientLifecycleRepository(connection, realm.id)

    first_ack = repository.ingest(first, client_instance_id="client-test", now=NOW)
    replay_ack = repository.ingest(first, client_instance_id="client-test", now=NOW)
    second_ack = repository.ingest(
        second, client_instance_id="client-test", now=NOW + dt.timedelta(seconds=1)
    )

    assert replay_ack == first_ack
    assert second_ack.event_id != first_ack.event_id
    with connection.cursor() as cursor:
        cursor.execute(
            "select head_sequence,head_digest from client.lifecycle_stream"
            " where realm_id=%s and client_instance_id='client-test' and session_id='ses_ack'",
            (realm.id,),
        )
        assert cursor.fetchone() == (2, second["event_digest"])
        cursor.execute(
            "select count(*) from client.lifecycle_ack where realm_id=%s", (realm.id,)
        )
        assert cursor.fetchone()[0] == 2


def test_out_of_order_event_is_rejected_without_advancing_head(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    record_event(tmp_path, event_type="session.created", session_id="ses_gap", now=NOW)
    second = record_event(
        tmp_path,
        event_type="session.status",
        session_id="ses_gap",
        now=NOW + dt.timedelta(seconds=1),
    ).document()
    repository = ClientLifecycleRepository(connection, realm.id)

    with pytest.raises(ConcurrencyConflict, match="ilk sequence"):
        repository.ingest(second, client_instance_id="client-gap", now=NOW)

    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from client.lifecycle_stream where realm_id=%s", (realm.id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "select count(*) from client.lifecycle_event where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 0


def test_supplied_digest_tamper_is_rejected_before_db_write(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    document = record_event(
        tmp_path, event_type="session.created", session_id="ses_tamper", now=NOW
    ).document()
    document["status"] = "forged"

    with pytest.raises(ValidationFailed, match="supplied digest"):
        ClientLifecycleRepository(connection, realm.id).ingest(
            document, client_instance_id="client-tamper", now=NOW
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from client.lifecycle_event where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 0


def test_db_trigger_and_append_only_guards_reject_direct_mutation(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    first = record_event(
        tmp_path, event_type="session.created", session_id="ses_guard", now=NOW
    ).document()
    repository = ClientLifecycleRepository(connection, realm.id)
    acknowledgement = repository.ingest(first, client_instance_id="client-guard", now=NOW)

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("savepoint guard_test")
        with pytest.raises(Exception, match="head/previous mismatch"):
            cursor.execute(
                "insert into client.lifecycle_event"
                " (id,realm_id,stream_id,sequence,previous_digest,event_digest,payload,"
                " occurred_at,ingested_at,grants_authority)"
                " select gen_random_uuid(),realm_id,id,3,head_digest,%s,'{}'::jsonb,%s,%s,false"
                " from client.lifecycle_stream where realm_id=%s and client_instance_id=%s",
                ("sha256:" + "f" * 64, NOW, NOW, realm.id, "client-guard"),
            )
        cursor.execute("rollback to savepoint guard_test")
        cursor.execute("savepoint immutable_event")
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            cursor.execute(
                "update client.lifecycle_event set payload='{}'::jsonb"
                " where realm_id=%s and id=%s",
                (realm.id, acknowledgement.event_id),
            )
        cursor.execute("rollback to savepoint immutable_event")
        cursor.execute("savepoint immutable_ack")
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            cursor.execute(
                "delete from client.lifecycle_ack where realm_id=%s and event_id=%s",
                (realm.id, acknowledgement.event_id),
            )
        cursor.execute("rollback to savepoint immutable_ack")


def test_ack_failure_rolls_back_event_and_stream_head_atomically(
    realm_session: tuple[Any, Any], tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    realm, connection = realm_session
    document = record_event(
        tmp_path, event_type="session.created", session_id="ses_atomic", now=NOW
    ).document()
    calls = 0

    def fail_canonical_ack(value: Any) -> str:
        nonlocal calls
        calls += 1
        return canonical_digest(value) if calls == 1 else "invalid-canonical-digest"

    monkeypatch.setattr(lifecycle_repository_module, "digest", fail_canonical_ack)
    with pytest.raises(Exception, match=r"canonical_digest|check constraint"):
        ClientLifecycleRepository(connection, realm.id).ingest(
            document, client_instance_id="client-atomic", now=NOW
        )

    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from client.lifecycle_stream"
            " where realm_id=%s and client_instance_id='client-atomic'",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "select count(*) from client.lifecycle_event where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 0


def test_cross_realm_lifecycle_rows_are_hidden_by_rls(migrated_database: Any, tmp_path) -> None:
    first_realm = Realm.create(slug="lifecycle-first", display_name="Lifecycle First")
    second_realm = Realm.create(slug="lifecycle-second", display_name="Lifecycle Second")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(first_realm)
        RealmRepository(owner).create(second_realm)
    document = record_event(
        tmp_path, event_type="session.created", session_id="ses_rls", now=NOW
    ).document()
    with connect(migrated_database) as first_connection:
        configure_session(first_connection, realm_id=first_realm.id)
        ClientLifecycleRepository(first_connection, first_realm.id).ingest(
            document, client_instance_id="client-rls", now=NOW
        )
    with connect(migrated_database) as second_connection:
        configure_session(second_connection, realm_id=second_realm.id)
        with second_connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from client.lifecycle_event where realm_id=%s",
                (first_realm.id,),
            )
            assert cursor.fetchone()[0] == 0
