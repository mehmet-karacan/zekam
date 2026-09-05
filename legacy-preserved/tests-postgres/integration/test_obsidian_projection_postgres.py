from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest

from zekam.application.obsidian_projection import build_obsidian_projection
from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound
from zekam.domain.markdown_projection import ObsidianProfile
from zekam.domain.project import Project
from zekam.domain.work import WorkItem, WorkType
from zekam.infrastructure.postgres.markdown_projection_repository import (
    PostgresMarkdownProjectionRepository,
)
from zekam.infrastructure.postgres.project_repository import ProjectRepository
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)


def test_obsidian_snapshot_reads_existing_canonical_tables_without_migration(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    project = Project.create(realm=realm, slug="obsidian-snapshot", now=NOW)
    ProjectRepository(connection, realm.id).add(project)
    other_project = Project.create(realm=realm, slug="obsidian-snapshot-other", now=NOW)
    ProjectRepository(connection, realm.id).add(other_project)
    work = WorkItem.create(
        realm_id=realm.id,
        project_id=project.id,
        type=WorkType.TASK,
        title="Projection source",
        summary="Existing work table remains canonical.",
        now=NOW,
    )
    WorkItemRepository(connection, realm.id).add(work)
    other_work = WorkItem.create(
        realm_id=realm.id,
        project_id=other_project.id,
        type=WorkType.TASK,
        title="Other projection source",
        summary="A different project must remain isolated.",
        now=NOW,
    )
    WorkItemRepository(connection, realm.id).add(other_work)
    source = PostgresMarkdownProjectionRepository(connection, realm.id)
    left = source.load_obsidian_records(project.id, realm_slug=realm.slug)
    right = source.load_obsidian_records(project.id, realm_slug=realm.slug)
    other = source.load_obsidian_records(other_project.id, realm_slug=realm.slug)
    assert left == right
    assert any(item.record.entity_id == str(work.id) for item in left)
    assert not any(item.record.entity_id == str(other_work.id) for item in left)
    assert any(item.record.entity_id == str(other_work.id) for item in other)
    assert not any(item.record.entity_id == str(work.id) for item in other)
    assert all(item.project_id == project.id for item in left)
    assert all(item.project_id == other_project.id for item in other)
    with pytest.raises(NotFound, match="exact project"):
        source.load_obsidian_records(
            UUID("00000000-0000-0000-0000-00000000ffff"),
            realm_slug=realm.slug,
        )
    private = build_obsidian_projection(
        left,
        project_id=project.id,
        profile=ObsidianProfile.PRIVATE_LOCAL,
        policy_digest=digest("policy"),
    )
    public = build_obsidian_projection(
        left,
        project_id=project.id,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=digest("policy"),
    )
    assert private.source_snapshot_digest == public.source_snapshot_digest
    assert private.projection_digest != public.projection_digest
    assert len(public.exclusions) == 1
