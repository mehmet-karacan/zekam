from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def _project(runner: CliRunner, home: Path, root: Path, slug: str) -> None:
    root.mkdir()
    result = runner.invoke(
        app,
        ["project", "add", str(root), "--slug", slug, "--uygula", "--home", str(home)],
    )
    assert result.exit_code == 0, result.output


def _work(runner: CliRunner, home: Path, slug: str, title: str) -> None:
    result = runner.invoke(
        app,
        [
            "work",
            "create",
            slug,
            title,
            "--uygula",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 0, result.output


def test_ask_work_status_reads_all_registered_projects_without_rag(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _project(runner, home, tmp_path / "gpu", "gpu")
    _project(runner, home, tmp_path / "sky", "sky")
    _work(runner, home, "gpu", "GPU isi")
    _work(runner, home, "sky", "SKY isi")

    result = runner.invoke(
        app,
        ["ask", "ne kadar iş kaldı?", "--json", "--home", str(home)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-work-status/v1"
    assert document["status"] == "reported"
    assert document["project_refs"] == ["gpu", "sky"]
    assert sum(len(item["open_items"]) for item in document["projects"]) == 2
    assert document["provider_calls"] == 0
    assert document["index_triggered"] is False
    assert document["read_only"] is True
    assert document["grants_authority"] is False


def test_ask_work_status_honors_explicit_project_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _project(runner, home, tmp_path / "gpu", "gpu")
    _project(runner, home, tmp_path / "sky", "sky")
    _work(runner, home, "gpu", "GPU isi")
    _work(runner, home, "sky", "SKY isi")

    result = runner.invoke(
        app,
        ["ask", "durum nedir?", "--project", "gpu", "--json", "--home", str(home)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["project_refs"] == ["gpu"]
    assert [item["title"] for item in document["projects"][0]["open_items"]] == ["GPU isi"]


def test_ask_work_status_reports_events_and_unknown_usage_without_inventing_metrics(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _project(runner, home, tmp_path / "gpu", "gpu")
    _work(runner, home, "gpu", "GPU isi")

    before = (home / "state" / "operational.db").read_bytes()
    result = runner.invoke(
        app,
        [
            "ask",
            "hangi model ne kadar sürdü?",
            "--project",
            "gpu",
            "--json",
            "--home",
            str(home),
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    item = document["projects"][0]["open_items"][0]
    assert item["events"]
    assert item["events"][0]["event_kind"] == "created"
    assert item["runs"] == []
    assert item["usage"] == {
        "status": "unknown",
        "reason": "run-usage-receipt-missing",
    }
    assert (home / "state" / "operational.db").read_bytes() == before
