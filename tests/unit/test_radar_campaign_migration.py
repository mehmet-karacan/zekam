"""A42: additive SQLite migration for radar campaigns preserves history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zekam.infrastructure.radar_campaign_store import (
    RADAR_SCHEMA_VERSION,
    RadarCampaignRepository,
    RadarSchemaPort,
)

pytestmark = pytest.mark.unit


_V1_DDL = """
create table if not exists radar_schema_migrations (
    version integer primary key,
    name text not null,
    migration_digest text not null,
    applied_at text not null
) strict;

create table if not exists radar_campaign (
    id text primary key,
    project_id text not null,
    campaign_id text not null unique,
    stage text not null check(stage in ('discover','analyse')),
    state text not null check(state in (
        'ready','running','completed','failed','blocked','recovery-required'
    )),
    plan_digest text not null,
    plan_json text not null,
    idempotency_key text not null unique,
    effect_claim_id text,
    created_at text not null,
    updated_at text not null
) strict;
"""


def _seed_v1(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(_V1_DDL)
        connection.execute(
            "insert into radar_schema_migrations (version, name, migration_digest, applied_at)"
            " values (1, 'radar-campaign-v1', 'sha256:v1', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "insert into radar_campaign"
            " (id, project_id, campaign_id, stage, state, plan_digest, plan_json,"
            " idempotency_key, effect_claim_id, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "camp-v1-001",
                "proj-001",
                "radar-campaign:v1",
                "discover",
                "completed",
                "sha256:" + "a" * 64,
                '{"schema": "zekam-radar-campaign-plan/v1"}',
                "idem-v1-001",
                None,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return "camp-v1-001"


def test_v1_to_v3_migration_preserves_records(tmp_path: Path) -> None:
    """A42: additive migration keeps existing campaign rows and adds counters."""
    path = tmp_path / "radar-migration.db"
    campaign_id = _seed_v1(path)

    status_before = RadarSchemaPort.status(path)
    assert status_before["exists"] is True
    assert status_before["version"] == 1

    RadarSchemaPort.ensure_schema(path)

    status_after = RadarSchemaPort.status(path)
    assert status_after["version"] == RADAR_SCHEMA_VERSION

    connection = sqlite3.connect(str(path))
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "select * from radar_campaign where id = ?", (campaign_id,)
        ).fetchone()
        assert row is not None
        assert str(row["campaign_id"]) == "radar-campaign:v1"
        assert str(row["state"]) == "completed"
        # New additive columns from v2/v3 migration.
        assert int(row["invocation_attempts"]) == 0
        assert int(row["planned_invocations"]) == 0
        assert int(row["started_invocations"]) == 0
        assert int(row["completed_invocations"]) == 0
        assert int(row["observed_provider_requests"]) == 0
    finally:
        connection.close()


def test_migration_is_idempotent_across_reopen(tmp_path: Path) -> None:
    """A42: interrupted/reopened migration resumes idempotently."""
    path = tmp_path / "radar-reopen.db"
    campaign_id = _seed_v1(path)

    RadarSchemaPort.ensure_schema(path)
    first_status = RadarSchemaPort.status(path)
    assert first_status["version"] == RADAR_SCHEMA_VERSION

    # Simulate reopen: close, then re-run migration on same file.
    RadarSchemaPort.ensure_schema(path)
    second_status = RadarSchemaPort.status(path)
    assert second_status["version"] == RADAR_SCHEMA_VERSION

    repo = RadarCampaignRepository(path)
    campaign = repo.get_campaign(campaign_id)
    assert campaign.state == "completed"
    assert campaign.campaign_id == "radar-campaign:v1"
    assert campaign.invocation_attempts == 0


def test_v1_record_digest_and_append_only_history_preserved(tmp_path: Path) -> None:
    """A42: migration does not rewrite historical digest or append-only rows."""
    path = tmp_path / "radar-history.db"
    campaign_id = _seed_v1(path)

    connection = sqlite3.connect(str(path))
    try:
        connection.row_factory = sqlite3.Row
        before = connection.execute(
            "select plan_digest, plan_json, idempotency_key from radar_campaign where id = ?",
            (campaign_id,),
        ).fetchone()
        history = {
            "plan_digest": str(before["plan_digest"]),
            "plan_json": str(before["plan_json"]),
            "idempotency_key": str(before["idempotency_key"]),
        }
    finally:
        connection.close()

    RadarSchemaPort.ensure_schema(path)
    repo = RadarCampaignRepository(path)
    campaign = repo.get_campaign(campaign_id)
    assert campaign.plan_digest == history["plan_digest"]
    assert campaign.plan_json == history["plan_json"]
    assert campaign.idempotency_key == history["idempotency_key"]
