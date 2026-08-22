"""Ayni is kaydi uzerinde yarisan yazarlar.

Optimistic concurrency ve append-only zincir, iki surecin ayni anda yazmasi
durumunda tek bir kazanan birakmalidir.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import bootstrap_realm
from zekam.application.work_graph import WorkGraphService
from zekam.domain.errors import ConcurrencyConflict
from zekam.domain.work import WorkState, WorkType
from zekam.infrastructure.postgres.connection import configure_session, connect

pytestmark = [pytest.mark.concurrency, pytest.mark.postgres]


@pytest.fixture
def shared_realm(migrated_database: DatabaseSettings) -> Iterator[tuple[Any, str]]:
    """Iki baglantinin paylastigi realm."""
    slug = f"yaris-{secrets.token_hex(4)}"
    with connect(migrated_database) as connection:
        bootstrap_realm(connection, slug=slug)
        yield connection, slug


def _second_connection(settings: DatabaseSettings, realm_id: Any) -> Any:
    manager = connect(settings)
    connection = manager.__enter__()
    configure_session(connection, realm_id=realm_id)
    return manager, connection


def test_two_writers_race_and_only_one_wins(
    migrated_database: DatabaseSettings, shared_realm: tuple[Any, str], tmp_path: Path
) -> None:
    first_connection, slug = shared_realm
    realm = bootstrap_realm(first_connection, slug=slug).realm

    root = tmp_path / "kaynak"
    root.mkdir()
    project = ProjectIntegrationService(first_connection, realm).register(source_path=root)

    first_service = WorkGraphService(first_connection, realm)
    item = first_service.create_item(
        project_id=project.id, type=WorkType.TASK, title="Yarisilan is"
    )

    manager, second_connection = _second_connection(migrated_database, realm.id)
    try:
        second_service = WorkGraphService(second_connection, realm)
        # Iki taraf da ayni revision'i gordu.
        assert second_service.items.get(item.id).revision == item.revision

        first_service.transition(item.id, WorkState.READY)
        with pytest.raises(ConcurrencyConflict):
            second_service.items.replace(
                item.with_state(WorkState.READY), expected_revision=item.revision
            )

        assert second_service.items.get(item.id).state is WorkState.READY
        assert second_service.items.get(item.id).revision == 2
    finally:
        manager.__exit__(None, None, None)


def test_revision_chain_stays_unbroken_under_sequential_writers(
    migrated_database: DatabaseSettings, shared_realm: tuple[Any, str], tmp_path: Path
) -> None:
    first_connection, slug = shared_realm
    realm = bootstrap_realm(first_connection, slug=slug).realm

    root = tmp_path / "kaynak"
    root.mkdir()
    project = ProjectIntegrationService(first_connection, realm).register(source_path=root)
    first_service = WorkGraphService(first_connection, realm)
    item = first_service.create_item(project_id=project.id, type=WorkType.TASK, title="Zincir")

    manager, second_connection = _second_connection(migrated_database, realm.id)
    try:
        second_service = WorkGraphService(second_connection, realm)
        first_service.transition(item.id, WorkState.READY)
        second_service.transition(item.id, WorkState.ACTIVE)
        first_service.transition(item.id, WorkState.VERIFICATION)

        assert first_service.verify_history(item.id)
        assert [record["revision"] for record in first_service.history(item.id)] == [1, 2, 3, 4]
    finally:
        manager.__exit__(None, None, None)


def test_duplicate_external_number_race_is_rejected(
    migrated_database: DatabaseSettings, shared_realm: tuple[Any, str], tmp_path: Path
) -> None:
    first_connection, slug = shared_realm
    realm = bootstrap_realm(first_connection, slug=slug).realm

    root = tmp_path / "kaynak"
    root.mkdir()
    project = ProjectIntegrationService(first_connection, realm).register(source_path=root)
    first_service = WorkGraphService(first_connection, realm)
    first_service.create_item(
        project_id=project.id, type=WorkType.DEFECT, title="Ilk", external_number="777"
    )

    manager, second_connection = _second_connection(migrated_database, realm.id)
    try:
        second_service = WorkGraphService(second_connection, realm)
        with pytest.raises(Exception, match="work_item_external_number_idx"):
            second_service.create_item(
                project_id=project.id,
                type=WorkType.DEFECT,
                title="Ikinci",
                external_number="777",
            )
    finally:
        manager.__exit__(None, None, None)
