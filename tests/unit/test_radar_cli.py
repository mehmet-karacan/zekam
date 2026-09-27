"""WP-04: Radar CLI unit tests with Typer CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import build_radar_plan
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.research import app

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Path, OperationalStore, Any]:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("demo")
    home = layout.root
    database = home / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.commit()
    return home, store, project


def _write_plan(home: Path, plan_body: dict[str, Any]) -> Path:
    path = home / "radar-plan.json"
    path.write_text(canonical_json(plan_body), encoding="utf-8")
    return path


def test_radar_plan_discover_json(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, _store, _project = runtime
    result = runner.invoke(
        app,
        [
            "radar",
            "plan",
            "--project",
            "demo",
            "--stage",
            "discover",
            "--json",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-radar-campaign-plan/v1"
    assert document["stage"] == "discover"
    assert document["dry_run"] is True
    assert document["provider_calls_performed"] == 0
    assert document["grants_authority"] is False


def test_radar_plan_analyse_without_inventory_needs_discovery(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    result = runner.invoke(
        app,
        [
            "radar",
            "plan",
            "--project",
            "demo",
            "--stage",
            "analyse",
            "--json",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 77
    assert "needs-discovery" in result.output


def test_radar_run_dry_run(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, store, _project = runtime
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=("openai",),
    )
    path = _write_plan(home, plan.body)
    result = runner.invoke(
        app,
        [
            "radar",
            "run",
            "--plan-file",
            str(path),
            "--plan-digest",
            plan.plan_digest,
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def test_radar_run_apply_no_authorization_fails(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=("openai",),
    )
    path = _write_plan(home, plan.body)
    result = runner.invoke(
        app,
        [
            "radar",
            "run",
            "--plan-file",
            str(path),
            "--plan-digest",
            plan.plan_digest,
            "--uygula",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 77


def test_radar_run_apply_wrong_digest_fails(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=("openai",),
    )
    path = _write_plan(home, plan.body)
    result = runner.invoke(
        app,
        [
            "radar",
            "run",
            "--plan-file",
            str(path),
            "--plan-digest",
            digest("tampered"),
            "--uygula",
            "--authorize-public-source-read",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 77


def test_radar_status_report_candidates(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=("openai",),
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign = repo.create_campaign(
        project_id="p1",
        campaign_id="camp-test-1",
        stage="discover",
        state="completed",
        plan_digest=plan.plan_digest,
        plan=plan.body,
        idempotency_key="ik-test",
    )
    repo.record_stage(campaign.id, "discover", "completed", next_safe_action="report")

    for command in ("status", "report", "candidates"):
        result = runner.invoke(
            app,
            [
                "radar",
                command,
                campaign.campaign_id,
                "--json",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["grants_authority"] is False
        assert document["read_only"] is True
