"""Radar reads cannot create or migrate the campaign store."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from zekam.application.scheduler.radar_proposal_handler import (
    RadarProposalHandler,
    default_capabilities,
)
from zekam.application.home import HomeLayout
from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository, RadarSchemaPort
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.interfaces.cli.research import app


def test_missing_read_only_store_does_not_create_parent_or_database(tmp_path):
    path = tmp_path / "absent" / "radar-campaigns.db"
    with pytest.raises(NotFound):
        RadarCampaignRepository(path, read_only=True)
    assert not path.parent.exists()
    assert RadarSchemaPort.status(path) == {"exists": False, "version": None}
    assert not path.parent.exists()


def test_old_schema_read_does_not_migrate_or_write(tmp_path):
    path = tmp_path / "radar.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "create table radar_schema_migrations(version integer,name text,"
            "migration_digest text,applied_at text)"
        )
        db.execute("insert into radar_schema_migrations values(1,'old','old','2026-01-01')")
    before = path.read_bytes()
    with pytest.raises(ValidationFailed, match="migration gerekli"):
        RadarCampaignRepository(path, read_only=True)
    assert path.read_bytes() == before
    assert RadarSchemaPort.status(path)["version"] == 1
    assert path.read_bytes() == before


def test_current_schema_digest_drift_does_not_run_repair(tmp_path):
    path = tmp_path / "radar.db"
    RadarCampaignRepository(path)
    with sqlite3.connect(path) as db:
        db.execute("update radar_schema_migrations set migration_digest=?", (digest("wrong"),))
    before = path.read_bytes()
    with pytest.raises(ValidationFailed, match="digest uyumsuz"):
        RadarCampaignRepository(path, read_only=True)
    assert path.read_bytes() == before


def test_current_store_read_and_write_guard_preserve_file_bytes(tmp_path):
    path = tmp_path / "radar.db"
    writer = RadarCampaignRepository(path)
    campaign = writer.create_campaign(
        project_id="project",
        campaign_id="campaign",
        stage="discover",
        state="ready",
        plan_digest=digest("plan"),
        plan={"safe": True},
        idempotency_key="read-only-test",
    )
    before = path.read_bytes()
    reader = RadarCampaignRepository(path, read_only=True)
    assert reader.get_campaign(campaign.campaign_id) == campaign
    assert reader.candidates_document(campaign.campaign_id)["read_only"] is True
    connection = reader._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("delete from radar_campaign")
    finally:
        connection.close()
    assert path.read_bytes() == before


def test_read_only_handler_missing_campaign_does_not_create_database(tmp_path):
    path = tmp_path / "absent" / "radar.db"
    result = RadarProposalHandler(path).handle(
        campaign_id="missing",
        capabilities=default_capabilities(enabled=True, authorized=True),
    )
    assert result.status == "completed"
    assert result.body["candidates"]["campaign_state"] == "unknown"
    assert result.body["mutations"] == 0
    assert not path.parent.exists()


def test_symlink_store_is_not_followed(tmp_path):
    source = tmp_path / "source.db"
    RadarCampaignRepository(source)
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    before = source.read_bytes()
    with pytest.raises(PolicyViolation):
        RadarCampaignRepository(alias, read_only=True)
    with pytest.raises(PolicyViolation):
        RadarSchemaPort.status(alias)
    assert source.read_bytes() == before


@pytest.mark.parametrize("command", ["status", "report", "candidates"])
def test_cli_read_surfaces_do_not_bootstrap_missing_campaign_store(tmp_path, command):
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    bootstrap(home / "state" / "operational.db")
    SQLiteLocalRuntimeStore(home / "state" / "operational.db")
    result = CliRunner().invoke(app, ["radar", command, "missing", "--json", "--home", str(home)])
    assert result.exit_code != 0
    assert "Radar store bulunamadi" in result.output
    assert not (home / "state" / "radar-campaigns.db").exists()
