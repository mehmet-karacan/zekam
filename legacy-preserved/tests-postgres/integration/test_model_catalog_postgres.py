from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.domain.model_catalog import (
    CatalogFetchStatus,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
)
from zekam.infrastructure.postgres.model_catalog_repository import ModelCatalogRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 25, 9, tzinfo=dt.UTC)


def _snapshot(realm_id, *, prior=None, status=CatalogFetchStatus.FETCHED):  # type: ignore[no-untyped-def]
    entries = (
        ()
        if status is CatalogFetchStatus.FAILED
        else (
            ModelCatalogEntry(
                "model-a",
                CatalogVisibility.AUTHENTICATED,
                True,
                "chat-completions",
                ("text",),
            ),
        )
    )
    return ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=realm_id,
        provider_id="aihub",
        entries=entries,
        etag=None,
        fetched_at=NOW if prior is None else NOW + dt.timedelta(minutes=1),
        expires_at=(NOW if prior is None else NOW + dt.timedelta(minutes=1))
        + dt.timedelta(hours=1),
        client_version="zekam-test/1",
        source=CatalogSource.PACKAGE,
        fetch_status=status,
        error_category="provider-unavailable" if status is CatalogFetchStatus.FAILED else None,
        prior_snapshot_id=None if prior is None else prior.id,
    )


def test_catalog_snapshot_round_trip_history_and_append_only(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = ModelCatalogRepository(connection, realm.id)
    first = _snapshot(realm.id)
    assert repository.store(first) == (first.id, True)
    second = _snapshot(realm.id, prior=first, status=CatalogFetchStatus.FETCHED)
    assert repository.store(second) == (second.id, True)
    loaded = repository.latest("aihub")
    assert loaded is not None and loaded.snapshot_digest == second.snapshot_digest
    assert loaded.catalog_digest == first.catalog_digest
    assert [item.id for item in repository.history("aihub")] == [second.id, first.id]
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute("update models.catalog_snapshot set etag='forged' where id=%s", (second.id,))
    connection.rollback()


def test_database_rejects_forged_digest_and_cross_realm_visibility(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    snapshot = _snapshot(realm.id)
    ModelCatalogRepository(connection, realm.id).store(snapshot)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into models.catalog_snapshot"
            "(id,realm_id,provider_id,catalog_digest,snapshot_digest,etag,fetched_at,expires_at,"
            "client_version,source,entries,fetch_status,error_category,prior_snapshot_id,"
            "manifest_body,grants_authority) values(%s,%s,'aihub',%s,%s,null,%s,%s,'x',"
            "'remote','[]'::jsonb,'fetched',null,null,'{}'::jsonb,false)",
            (
                uuid4(),
                realm.id,
                "sha256:" + "0" * 64,
                "sha256:" + "1" * 64,
                NOW,
                NOW + dt.timedelta(hours=1),
            ),
        )
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute(
            "select has_table_privilege(current_user,'models.catalog_snapshot','update'),"
            "has_table_privilege(current_user,'models.catalog_snapshot','delete')"
        )
        assert cursor.fetchone() == (False, False)
