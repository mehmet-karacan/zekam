from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.main import app


def test_ask_exact_work_history_uses_project_scope_without_rag(tmp_path: Path, monkeypatch) -> None:
    from zekam.interfaces.cli import main

    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _project(runner, home, tmp_path / "gpu", "gpu")
    _project(runner, home, tmp_path / "sky", "sky")
    database = home / "state" / "operational.db"
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        for slug in ("gpu", "sky"):
            project = uow.resolve_project(slug)
            uow.create_work(
                project_id=project.id, kind="task", title=slug, state="ready", external_number="17"
            )
        uow.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Work reads must not retrieve or call providers")

    monkeypatch.setattr(main, "query_registered_project", forbidden)
    before = database.read_bytes()
    result = runner.invoke(
        app,
        [
            "ask",
            "17 numaralı işin geçmişini göster",
            "--project",
            "gpu",
            "--json",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-natural-work-read/v1"
    assert document["project_ref"] == "gpu"
    assert document["work"]["title"] == "gpu"
    assert document["events"][0]["event_kind"] == "created"
    assert document["work"]["dependencies"]["supported"] is False
    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert database.read_bytes() == before
    for question in ("work 17 detayını göster", "Mars projesinde work 17 durumunu göster"):
        ambiguous = runner.invoke(app, ["ask", question, "--json", "--home", str(home)])
        assert ambiguous.exit_code != 0
        assert json.loads(ambiguous.output)["status"] == "clarification-required"
    assert database.read_bytes() == before


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


def test_ask_work_status_bounds_details_but_keeps_exact_project_totals(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _project(runner, home, tmp_path / "gpu", "gpu")
    with SQLiteOperationalStore(home / "state" / "operational.db").unit_of_work() as uow:
        project = uow.resolve_project("gpu")
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("task"), sanitized_config={}
        )
        for index in range(105):
            uow.create_work(
                project_id=project.id, kind="task", title=f"Other {index}", state="ready"
            )
        work = uow.create_work(
            project_id=project.id, kind="task", title="Long history", state="ready"
        )
        for index in range(24):
            target = "active" if index % 2 == 0 else "blocked"
            work = uow.transition_work(
                work_item_id=work.id,
                expected_revision=work.revision,
                to_state=target,
                payload_digest=digest("payload"),
                event_digest=digest({"transition": index}),
            )
        for ordinal in range(8):
            run = uow.create_run(
                work_item_id=work.id,
                config_revision_id=config.id,
                plan_digest=digest({"plan": ordinal}),
                budget={"seconds": 30},
            )
            for index in range(23):
                uow.add_run_step(
                    run_id=run.id, step_key=f"step-{index}", input_digest=digest(index)
                )
            for sequence in range(1, 9):
                payload = {"sequence": sequence, "run": run.id}
                uow.record_checkpoint(
                    run_id=run.id,
                    sequence=sequence,
                    checkpoint_digest=digest(payload),
                    payload=payload,
                )
        uow.commit()
    result = runner.invoke(
        app, ["ask", "ne kadar iş kaldı?", "--project", "gpu", "--json", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    project_doc = document["projects"][0]
    assert sum(project_doc["counts"].values()) == 106
    assert project_doc["open_count"] == 106
    assert len(project_doc["open_items"]) == 100
    assert project_doc["open_truncated"] is True
    detail = next(item for item in project_doc["items"] if item["id"] == work.id)
    assert detail["events_truncated"] and len(detail["events"]) == 20
    assert detail["runs_truncated"] and len(detail["runs"]) == 5
    assert all(run["steps_truncated"] and len(run["steps"]) == 20 for run in detail["runs"])
    assert all(
        run["checkpoints_truncated"] and len(run["checkpoints"]) == 5 for run in detail["runs"]
    )
    assert document["bounded"] and document["read_only"]
    assert not document["grants_authority"]
    text_result = runner.invoke(
        app, ["ask", "ne kadar iş kaldı?", "--project", "gpu", "--home", str(home)]
    )
    assert text_result.exit_code == 0
    assert "toplam is: 106" in text_result.output
