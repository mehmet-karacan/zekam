from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_route_preview_uses_live_registry_and_fans_out_sky(home_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home_root)]).exit_code == 0
    for slug in ("sky-spring-ui", "sky-microservis"):
        source = tmp_path / slug
        source.mkdir()
        added = runner.invoke(
            app,
            ["project", "add", str(source), "--slug", slug, "--uygula", "--home", str(home_root)],
        )
        assert added.exit_code == 0, added.output

    result = runner.invoke(
        app,
        ["route", "preview", "Sky müşteri akışı nedir?", "--json", "--home", str(home_root)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["status"] == "selected"
    assert document["strategy"] == "parallel-project-rag"
    assert document["project_refs"] == ["sky-spring-ui", "sky-microservis"]
    assert document["provider_calls"] == 0
    assert document["grants_authority"] is False

    families_result = runner.invoke(app, ["route", "families", "--json", "--home", str(home_root)])
    families = json.loads(families_result.output)
    sky = next(item for item in families["families"] if item["family_ref"] == "sky")
    assert [item["available"] for item in sky["members"]] == [True, True]


def test_route_preview_general_question_has_no_project_target(
    home_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home_root)]).exit_code == 0
    source = tmp_path / "gpu-fusion"
    source.mkdir()
    assert (
        runner.invoke(
            app,
            [
                "project",
                "add",
                str(source),
                "--slug",
                "gpu-fusion",
                "--uygula",
                "--home",
                str(home_root),
            ],
        ).exit_code
        == 0
    )

    result = runner.invoke(
        app,
        ["route", "preview", "Dünya çevresi kaç kilometredir?", "--json", "--home", str(home_root)],
    )

    document = json.loads(result.output)
    assert document["status"] == "general"
    assert document["project_refs"] == []
