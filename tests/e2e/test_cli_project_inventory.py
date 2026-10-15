"""Registered-project discovery remains metadata-only and resumable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application import source_discovery
from zekam.application.home import HomeLayout
from zekam.application.project_rag_runtime import bind_project_source
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.main import app


def _registered(tmp_path: Path) -> tuple[Path, Path]:
    layout = HomeLayout(tmp_path / "home").ensure()
    layout.ensure_project("sample")
    bootstrap(layout.root / "state" / "operational.db")
    with SQLiteOperationalStore(layout.root / "state" / "operational.db").unit_of_work() as uow:
        uow.create_project(slug="sample", display_name="Sample")
        uow.commit()
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("source payload", encoding="utf-8")
    (root / ".env").write_text("private fixture payload", encoding="utf-8")
    (root / "generated").mkdir()
    (root / "generated" / "out.bin").write_bytes(b"\x00fixture")
    bind_project_source(layout.root, "sample", root)
    return layout.root, root


def test_inventory_window_is_separate_from_scan_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = _registered(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("source content must not be read")

    monkeypatch.setattr(source_discovery, "_process_file", forbidden)
    result = CliRunner().invoke(
        app, ["project", "inventory", "sample", "--home", str(home), "--entry-limit", "1"]
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["read_only"] is True
    assert document["source_content_reads"] == document["provider_calls"] == 0
    assert document["grants_authority"] is False
    inventory = document["inventory"]
    assert inventory["status"] == "complete"
    assert inventory["entries_truncated"] is True
    assert len(inventory["entries"]) == 1
    assert inventory["resume_checkpoint"] is None
    assert "private fixture payload" not in result.output
    assert "source payload" not in result.output


def test_inventory_saved_json_resume_and_foreign_root_rejection(tmp_path: Path) -> None:
    home, root = _registered(tmp_path)
    runner = CliRunner()
    args = ["project", "inventory", "sample", "--home", str(home)]
    partial = runner.invoke(app, [*args, "--max-entries", "2"])
    assert partial.exit_code == 0, partial.output
    assert json.loads(partial.output)["inventory"]["status"] == "partial"
    saved = tmp_path / "checkpoint.json"
    saved.write_text(partial.output, encoding="utf-8")
    complete = runner.invoke(app, [*args, "--resume-file", str(saved)])
    assert complete.exit_code == 0, complete.output
    assert json.loads(complete.output)["inventory"]["status"] == "complete"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "a.txt").write_text("source payload", encoding="utf-8")
    bind_project_source(home, "sample", foreign)
    rejected = runner.invoke(app, [*args, "--resume-file", str(saved)])
    assert rejected.exit_code != 0
    assert root.is_dir()


def test_inventory_checkpoint_input_is_bounded_and_requires_binding(tmp_path: Path) -> None:
    home, _ = _registered(tmp_path)
    runner = CliRunner()
    saved = tmp_path / "checkpoint.json"
    for payload in ('["a.txt"]', '{"inventory":{"resume_checkpoint":null}}', " " * (64 * 1024 + 1)):
        saved.write_text(payload, encoding="utf-8")
        result = runner.invoke(
            app,
            ["project", "inventory", "sample", "--home", str(home), "--resume-file", str(saved)],
        )
        assert result.exit_code != 0
