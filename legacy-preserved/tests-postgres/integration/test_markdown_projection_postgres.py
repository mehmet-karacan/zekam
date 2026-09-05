from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from zekam.application.markdown_projection import rebuild_markdown_projection_from_database
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.project import Project
from zekam.domain.realm import Realm
from zekam.domain.work import RelationKind, WorkItem, WorkRelation, WorkState, WorkType
from zekam.infrastructure.postgres.connection import configure_session, reset_role
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.markdown_projection_repository import (
    PostgresMarkdownProjectionRepository,
)
from zekam.infrastructure.postgres.project_repository import ProjectRepository
from zekam.infrastructure.postgres.work_repository import (
    WorkItemRepository,
    WorkRelationRepository,
)

NOW = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC)


def test_db_snapshot_projection_byte_identical_rebuild(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    project = Project.create(realm=realm, slug="projection-db", now=NOW)
    ProjectRepository(connection, realm.id).add(project)
    first = WorkItem.create(
        realm_id=realm.id,
        project_id=project.id,
        type=WorkType.TASK,
        title="Birinci is",
        summary="Kanonik DB satirindan gelir.",
        external_number="WORK-1",
        now=NOW,
    )
    second = WorkItem.create(
        realm_id=realm.id,
        project_id=project.id,
        type=WorkType.RESEARCH,
        title="Ikinci is",
        summary="Iliski ile birlikte projekte edilir.",
        external_number="WORK-2",
        now=NOW + dt.timedelta(seconds=1),
    )
    works = WorkItemRepository(connection, realm.id)
    works.add(first)
    works.add(second)
    relations = WorkRelationRepository(connection, realm.id)
    relation = WorkRelation.create(
        source=first, target=second, kind=RelationKind.DEPENDS_ON, now=NOW
    )
    relations.add(relation)
    source = PostgresMarkdownProjectionRepository(connection, realm.id)
    left = rebuild_markdown_projection_from_database(source, project.id)
    right = rebuild_markdown_projection_from_database(source, project.id)
    assert left.manifest_bytes() == right.manifest_bytes()
    assert tuple(item.payload for item in left.files) == tuple(item.payload for item in right.files)
    assert left.source_snapshot_digest == right.source_snapshot_digest
    assert len(left.records) == 2
    by_id = {item.entity_id: item for item in left.records}
    assert str(second.id) in by_id[str(first.id)].related_entity_ids
    assert all(b"read_only_projection: true" in item.payload for item in left.files)
    changed = first.with_state(WorkState.READY, now=NOW + dt.timedelta(minutes=1))
    works.replace(changed, expected_revision=first.revision)
    rebuilt = rebuild_markdown_projection_from_database(source, project.id)
    assert rebuilt.source_snapshot_digest != left.source_snapshot_digest
    assert rebuilt.manifest_bytes() != left.manifest_bytes()
    relations.remove(relation.id)
    relations.add(
        WorkRelation.create(
            source=changed,
            target=second,
            kind=RelationKind.BLOCKS,
            now=NOW + dt.timedelta(minutes=2),
        )
    )
    relation_changed = rebuild_markdown_projection_from_database(source, project.id)
    assert relation_changed.source_snapshot_digest != rebuilt.source_snapshot_digest
    assert relation_changed.manifest_bytes() != rebuilt.manifest_bytes()
    assert b"`outgoing` `blocks`" in b"\n".join(item.payload for item in relation_changed.files)


def test_db_projection_query_is_repeatable_read_only_and_bounded(
    realm_session: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm, connection = realm_session
    project = Project.create(realm=realm, slug="projection-empty", now=NOW)
    ProjectRepository(connection, realm.id).add(project)
    source = PostgresMarkdownProjectionRepository(connection, realm.id)
    try:
        source.load_project_records(project.id, limit=0)
    except ValidationFailed as exc:
        assert "1..1000" in str(exc)
    else:  # pragma: no cover - fail path
        raise AssertionError("bounded limit reddedilmeliydi")
    works = WorkItemRepository(connection, realm.id)
    items = []
    for number in ("BOUND-1", "BOUND-2", "BOUND-3"):
        item = WorkItem.create(
            realm_id=realm.id,
            project_id=project.id,
            type=WorkType.TASK,
            title=number,
            summary="Bounded projection kaydi.",
            external_number=number,
            now=NOW,
        )
        works.add(item)
        items.append(item)
    with pytest.raises(ValidationFailed, match="bounded limiti"):
        source.load_project_records(project.id, limit=1)
    relation_repository = WorkRelationRepository(connection, realm.id)
    for target in items[1:]:
        relation_repository.add(
            WorkRelation.create(
                source=items[0], target=target, kind=RelationKind.RELATES_TO, now=NOW
            )
        )
    monkeypatch.setattr(
        "zekam.infrastructure.postgres.markdown_projection_repository.MAX_PROJECTION_RELATIONS",
        1,
    )
    try:
        source.load_project_records(project.id)
    except ValidationFailed as exc:
        assert "relation bounded limiti" in str(exc)
    else:  # pragma: no cover - fail path
        raise AssertionError("silent relation truncation reddedilmeliydi")


def test_db_projection_rejects_semantically_tampered_record_digest(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    project = Project.create(realm=realm, slug="projection-tamper", now=NOW)
    ProjectRepository(connection, realm.id).add(project)
    item = WorkItem.create(
        realm_id=realm.id,
        project_id=project.id,
        type=WorkType.TASK,
        title="Digest tamper",
        summary="Stored digest semantik govdeyle eslesmeli.",
        now=NOW,
    )
    WorkItemRepository(connection, realm.id).add(item)
    with connection.cursor() as cursor:
        cursor.execute(
            "update work.work_item set record_digest=%s where id=%s",
            (digest("forged-record"), item.id),
        )
    source = PostgresMarkdownProjectionRepository(connection, realm.id)
    with pytest.raises(ValidationFailed, match="record digest"):
        source.load_project_records(project.id)


def test_db_projection_cross_realm_rows_are_invisible(
    realm_session: tuple[Any, Any],
) -> None:
    first_realm, connection = realm_session
    second_realm = Realm.create(slug="projection-other", display_name="Other realm")
    reset_role(connection)
    configure_session(connection, realm_id=second_realm.id, role=None)
    RealmRepository(connection).create(second_realm)
    configure_session(connection, realm_id=second_realm.id)
    second_project = Project.create(realm=second_realm, slug="other-project", now=NOW)
    ProjectRepository(connection, second_realm.id).add(second_project)
    WorkItemRepository(connection, second_realm.id).add(
        WorkItem.create(
            realm_id=second_realm.id,
            project_id=second_project.id,
            type=WorkType.TASK,
            title="Baska realm",
            summary="Ilk realm tarafindan gorulemez.",
            now=NOW,
        )
    )
    reset_role(connection)
    configure_session(connection, realm_id=first_realm.id)
    source = PostgresMarkdownProjectionRepository(connection, first_realm.id)
    assert source.load_project_records(second_project.id) == ()
