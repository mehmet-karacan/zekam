"""SQLite seciminin CLI'dan minimum persistence profile kadar gercek kabul testi."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration


def test_cli_init_to_project_work_and_vector_search(home_root: Path) -> None:
    source = home_root.parent / "gpu-fusion"
    source.mkdir()
    initialized = CliRunner().invoke(
        app, ["init", "--home", str(home_root), "--persistence", "sqlite"]
    )
    assert initialized.exit_code == 0, initialized.output

    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "project",
            "add",
            str(source),
            "--name",
            "GPU Fusion",
            "--slug",
            "gpu-fusion",
            "--home",
            str(home_root),
            "--uygula",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "gpu-fusion" in added.output

    created = runner.invoke(
        app,
        [
            "work",
            "create",
            "gpu-fusion",
            "Index source",
            "--home",
            str(home_root),
            "--uygula",
        ],
    )
    assert created.exit_code == 0, created.output

    indexed = runner.invoke(
        app,
        [
            "knowledge",
            "vector-index",
            "gpu-fusion",
            "--source-ref",
            "source:gpu-fusion/src/main.py",
            "--body",
            "GPU fusion entry point",
            "--model-ref",
            "local/test",
            "--vector-json",
            "[1,0,0]",
            "--home",
            str(home_root),
            "--uygula",
        ],
    )
    assert indexed.exit_code == 0, indexed.output

    projects = runner.invoke(app, ["project", "list", "--json", "--home", str(home_root)])
    works = runner.invoke(
        app,
        ["work", "list", "--proje", "gpu-fusion", "--json", "--home", str(home_root)],
    )
    searched = runner.invoke(
        app,
        [
            "knowledge",
            "vector-search",
            "gpu-fusion",
            "--model-ref",
            "local/test",
            "--vector-json",
            "[1,0,0]",
            "--home",
            str(home_root),
        ],
    )
    assert projects.exit_code == works.exit_code == searched.exit_code == 0
    assert "gpu-fusion" in projects.output
    assert "Index source" in works.output
    assert "GPU fusion entry point" in searched.output
