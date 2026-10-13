"""Kayitli projede local-only config yapi CLI kabul testi."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.interfaces.cli import main as cli

app = cli.app


def test_config_structure_uses_bound_source_without_value_disclosure(
    home_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sample-project"
    source.mkdir()
    config = source / ".env.local"
    config.write_text(
        "SERVICE_HOST=test-only-marker-42\n"
        "DB_PASSWORD=synthetic-secret-marker\n",
        encoding="utf-8",
    )
    properties = source / "app.properties"
    properties.write_text(
        "service.port=8080\ndb.password=synthetic-properties-marker\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home_root)])
    assert initialized.exit_code == 0, "".join(
        traceback.format_exception(initialized.exception)
    ) if initialized.exception else initialized.output
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

    monkeypatch.setattr(
        cli,
        "query_registered_project",
        lambda *_args, **_kwargs: {"state": "ordinary-rag", "answer_excerpt": "fixture"},
    )
    ordinary = runner.invoke(
        app,
        [
            "ask", "sample-project hangi servis?", "--project", "sample-project",
            "--json", "--home", str(home_root),
        ],
    )
    assert ordinary.exit_code == 0, ordinary.output
    assert json.loads(ordinary.output)["retrieval"]["state"] == "ordinary-rag"

    def unexpected_rag(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Config intent RAG/provider akisina girmemeli")

    monkeypatch.setattr(cli, "query_registered_project", unexpected_rag)
    natural = runner.invoke(
        app,
        [
            "ask", "sample-project .env.local ve app.properties anahtar yapisini analiz et",
            "--project", "sample-project", "--json", "--authorize-remote-query",
            "--home", str(home_root),
        ],
    )
    assert natural.exit_code == 0, natural.output
    routed = json.loads(natural.output)
    assert routed["route"] == "local-config-structure"
    assert routed["provider_calls"] == 0
    assert [item["format"] for item in routed["structures"]] == ["env", "properties"]
    assert "synthetic-secret-marker" not in natural.output
    assert "synthetic-properties-marker" not in natural.output
    assert "DB_PASSWORD" not in natural.output
    assert "db.password" not in natural.output
    natural_plain = runner.invoke(
        app,
        [
            "ask", "sample-project .env.local anahtar yapisini analiz et",
            "--project", "sample-project", "--home", str(home_root),
        ],
    )
    assert natural_plain.exit_code == 0, natural_plain.output
    assert "SERVICE_HOST" in natural_plain.output
    assert "DB_PASSWORD" not in natural_plain.output
    assert "synthetic-secret-marker" not in natural_plain.output
    ambiguous = runner.invoke(
        app,
        [
            "ask", "sample-project bu .env ve properties yapisini analiz et",
            "--project", "sample-project", "--json", "--home", str(home_root),
        ],
    )
    assert ambiguous.exit_code != 0
    assert "exact goreli dosya yolu" in ambiguous.output
    for artifact in home_root.rglob("*"):
        if artifact.is_file():
            data = artifact.read_bytes()
            assert b"test-only-marker-42" not in data
            assert b"synthetic-secret-marker" not in data
            assert b"synthetic-properties-marker" not in data
    assert config.read_bytes() == before


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
