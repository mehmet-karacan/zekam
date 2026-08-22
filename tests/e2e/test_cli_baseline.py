"""CLI baseline akisi: `zekam --version`, `zekam init`, `zekam doctor`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam import __version__
from zekam.application.config import USER_CONFIG_FILE
from zekam.application.diagnostics import OverallStatus
from zekam.application.home import HOME_ENTRIES, LAYOUT_FILE
from zekam.interfaces.cli.main import EXIT_CODES, app

pytestmark = pytest.mark.e2e

runner = CliRunner()


def test_version_flag_prints_product_and_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Zekam" in result.stdout
    assert __version__ in result.stdout


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "doctor" in result.stdout
    assert "init" in result.stdout


def test_init_creates_home_layout(home_root: Path) -> None:
    result = runner.invoke(app, ["init", "--home", str(home_root)])
    assert result.exit_code == 0
    for entry in HOME_ENTRIES:
        assert (home_root / entry.relative).is_dir(), entry.relative
    assert (home_root / LAYOUT_FILE).is_file()
    assert "backend: postgresql" in (home_root / USER_CONFIG_FILE).read_text(encoding="utf-8")


def test_init_dry_run_creates_nothing(home_root: Path) -> None:
    result = runner.invoke(app, ["init", "--home", str(home_root), "--dry-run"])
    assert result.exit_code == 0
    assert not home_root.exists()
    assert "olusturulacak" in result.stdout


def test_init_is_idempotent(home_root: Path) -> None:
    runner.invoke(app, ["init", "--home", str(home_root)])
    marker = home_root / "global" / "raporlar" / "kanit.txt"
    marker.write_text("kanit", encoding="utf-8")
    result = runner.invoke(app, ["init", "--home", str(home_root)])
    assert result.exit_code == 0
    assert marker.read_text(encoding="utf-8") == "kanit"


def test_init_sqlite_bootstraps_and_doctor_reports_exact_profile(home_root: Path) -> None:
    initialized = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "sqlite"])
    assert initialized.exit_code == 0, initialized.stdout
    config = (home_root / USER_CONFIG_FILE).read_text(encoding="utf-8")
    assert "backend: sqlite" in config
    assert (home_root / "global" / "runtime" / "zekam.sqlite3").is_file()

    result = runner.invoke(app, ["doctor", "--home", str(home_root), "--json", "-c", "sqlite"])
    document = json.loads(result.stdout)
    checks = {item["check_id"]: item for item in document["results"]}
    assert checks["sqlite.persistence"]["status"] == "passed"
    assert checks["sqlite.capabilities"]["evidence"]["fallback"] is False
    assert document["overall"] == "degraded"

    migration = runner.invoke(app, ["db", "status", "--home", str(home_root), "--json"])
    assert migration.exit_code == 0, migration.stdout
    migration_document = json.loads(migration.stdout)
    assert migration_document == {
        "backend": "sqlite",
        "head": 1,
        "expected_head": 1,
        "integrity_ok": True,
        "schema_ok": True,
        "drift": [],
    }


def test_init_rejects_persistence_switch_without_mutation(home_root: Path) -> None:
    first = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "sqlite"])
    assert first.exit_code == 0
    before = (home_root / USER_CONFIG_FILE).read_bytes()

    switched = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "postgresql"])
    assert switched.exit_code == 70
    assert "sessiz motor degisimi yasak" in switched.stderr
    assert (home_root / USER_CONFIG_FILE).read_bytes() == before


def test_doctor_json_output_is_parseable(home_root: Path) -> None:
    runner.invoke(app, ["init", "--home", str(home_root)])
    result = runner.invoke(app, ["doctor", "--home", str(home_root), "--json", "-c", "core"])
    document = json.loads(result.stdout)
    assert document["schema"] == "zekam-doctor-report/v1"
    assert {item["check_id"] for item in document["results"]} >= {
        "core.version",
        "core.python",
        "core.config",
        "core.home-layout",
    }
    assert result.exit_code == EXIT_CODES[OverallStatus(document["overall"])]


def test_doctor_core_category_is_healthy_after_init(home_root: Path) -> None:
    runner.invoke(app, ["init", "--home", str(home_root)])
    result = runner.invoke(app, ["doctor", "--home", str(home_root), "--json", "-c", "core"])
    document = json.loads(result.stdout)
    assert document["overall"] == "healthy"
    assert result.exit_code == 0


def test_doctor_reports_degraded_before_init(home_root: Path) -> None:
    result = runner.invoke(app, ["doctor", "--home", str(home_root), "--json", "-c", "core"])
    document = json.loads(result.stdout)
    assert document["overall"] == "degraded"
    assert result.exit_code == 1
    codes = {finding["code"] for item in document["results"] for finding in item["findings"]}
    assert "core.home-missing-root" in codes or "core.home-missing-directory" in codes


def test_doctor_output_contains_no_secret(home_root: Path) -> None:
    runner.invoke(app, ["init", "--home", str(home_root)])
    result = runner.invoke(app, ["doctor", "--home", str(home_root), "--json"])
    lowered = result.stdout.lower()
    for forbidden in ("password=", '"password"', "api_key", "bearer "):
        assert forbidden not in lowered
