from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e
runner = CliRunner()


def test_sqlite_clean_home_supports_package_acceptance_read_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", "")

    initialized = runner.invoke(
        app,
        ["init", "--home", str(home), "--persistence", "sqlite"],
    )
    assert initialized.exit_code == 0, initialized.stdout

    status = runner.invoke(app, ["db", "status", "--json", "--home", str(home)])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["backend"] == "sqlite"

    resumed = runner.invoke(app, ["work", "resume", "--json", "--home", str(home)])
    assert resumed.exit_code == 0, resumed.stdout
    resume_document = json.loads(resumed.stdout)
    assert resume_document["source"] == "sqlite-work-graph"
    assert resume_document["open_items"] == []


def test_permission_profile_list_is_read_only_and_digest_bound() -> None:
    result = runner.invoke(app, ["permission", "profile", "list", "--json"])

    assert result.exit_code == 0, result.stdout
    document = json.loads(result.stdout)
    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert document["profiles"]
    assert all(item["profile_digest"].startswith("sha256:") for item in document["profiles"])
