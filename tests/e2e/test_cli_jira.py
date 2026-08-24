from __future__ import annotations

import json

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_cli_jira_resolve_mcp_hedefini_dondurur() -> None:
    result = CliRunner().invoke(app, ["jira", "resolve", "gpu projesindeki 5661 task", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["issue_key"] == "SKYRSM-5661"
    assert payload["mcp_server"] == "jira"
