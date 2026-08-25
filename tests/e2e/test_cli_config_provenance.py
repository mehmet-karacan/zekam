from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.config import CONFIG_SCHEMA, USER_CONFIG_FILE
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e
runner = CliRunner()


def test_config_explain_is_read_only_and_reports_exact_field_origin(home_root: Path) -> None:
    home_root.mkdir(parents=True)
    config = home_root / USER_CONFIG_FILE
    config.write_text(
        f"schema: {CONFIG_SCHEMA}\nruntime:\n  log_level: debug\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    result = runner.invoke(
        app,
        ["config", "explain", "runtime.log_level", "--home", str(home_root), "--json"],
    )
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["origin"] == "user-config"
    assert document["value"] == "debug"
    assert document["read_only"] is True and document["grants_authority"] is False
    assert config.read_bytes() == before


def test_permission_profile_explain_is_named_revisioned_and_read_only() -> None:
    result = runner.invoke(
        app,
        ["permission", "profile", "explain", "workspace-write-no-network", "--json"],
    )
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["name"] == "workspace-write-no-network"
    assert document["revision"] == 1 and document["managed"] is True
    assert document["denied_capabilities"] == ["network.access"]
    assert document["read_only"] is True and document["grants_authority"] is False
