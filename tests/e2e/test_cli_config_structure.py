"""Kayitli projede local-only config yapi CLI kabul testi."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_config_structure_uses_bound_source_without_value_disclosure(
    home_root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "sample-project"
    source.mkdir()
    config = source / ".env.local"
    config.write_text(
        "SERVICE_HOST=test-only-marker-42\n"
        "DB_PASSWORD=synthetic-secret-marker\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home_root)]).exit_code == 0
    added = runner.invoke(
        app,
        [
            "project", "add", str(source), "--slug", "sample-project",
            "--uygula", "--home", str(home_root),
        ],
    )
    assert added.exit_code == 0, added.output
    bound = runner.invoke(
        app,
        ["project", "bind", "sample-project", str(source), "--uygula", "--home", str(home_root)],
    )
    assert bound.exit_code == 0, bound.output

    result = runner.invoke(
        app,
        [
            "project", "config-structure", "sample-project", ".env.local",
            "--json", "--home", str(home_root),
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["classification"] == "local-only"
    assert document["redacted_key_count"] == 1
    assert [item["name"] for item in document["keys"]] == ["SERVICE_HOST"]
    assert "test-only-marker-42" not in result.output
    assert "synthetic-secret-marker" not in result.output
    assert "DB_PASSWORD" not in result.output
    assert config.read_bytes() == before

    plain = runner.invoke(
        app,
        [
            "project", "config-structure", "sample-project", ".env.local",
            "--home", str(home_root),
        ],
    )
    assert plain.exit_code == 0, plain.output
    assert "SERVICE_HOST" in plain.output
    assert "DB_PASSWORD" not in plain.output
    assert "synthetic-secret-marker" not in plain.output
    for artifact in home_root.rglob("*"):
        if artifact.is_file():
            data = artifact.read_bytes()
            assert b"test-only-marker-42" not in data
            assert b"synthetic-secret-marker" not in data


def test_config_structure_rejects_escaping_path(home_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "sample-project"
    source.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home_root)]).exit_code == 0
    assert runner.invoke(
        app,
        [
            "project", "add", str(source), "--slug", "sample-project",
            "--uygula", "--home", str(home_root),
        ],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["project", "bind", "sample-project", str(source), "--uygula", "--home", str(home_root)],
    ).exit_code == 0

    result = runner.invoke(
        app,
        [
            "project", "config-structure", "sample-project", "../outside.env",
            "--json", "--home", str(home_root),
        ],
    )
    assert result.exit_code != 0
