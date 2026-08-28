"""SQLite seciminin CLI'dan minimum persistence profile kadar gercek kabul testi."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from typer.testing import CliRunner

from zekam.application.mutation_admission import DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY
from zekam.interfaces.cli import close as close_commands
from zekam.interfaces.cli import session as cli_session
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


def test_sqlite_rejects_full_continuity_mutation_before_postgres_connect(
    home_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "sqlite"])
    assert initialized.exit_code == 0, initialized.output
    input_file = home_root.parent / "hydration.json"
    input_file.write_text("{}", encoding="utf-8")

    def forbidden_connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("SQLite mutation PostgreSQL connect'e ulasmamali")

    monkeypatch.setattr(cli_session, "connect", forbidden_connect)
    result = runner.invoke(
        app,
        [
            "memory",
            "hydration-apply",
            "--girdi",
            str(input_file),
            "--idempotency-key",
            "sqlite-negative-hydration",
            "--uygula",
            "--home",
            str(home_root),
        ],
    )

    assert result.exit_code == 70
    assert "full-continuity destekleyen PostgreSQL" in result.output
    assert "backend ister" in result.output
    assert "backend veya" in result.output
    assert "authorization yetkisi vermez" in result.output
    assert "PostgreSQL'e fallback yok" in result.output


def test_sqlite_rejects_projection_close_mutation_before_postgres_connect(
    home_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "sqlite"])
    assert initialized.exit_code == 0, initialized.output
    input_file = home_root.parent / "close.json"
    input_file.write_text("{}", encoding="utf-8")

    def forbidden_connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("SQLite close PostgreSQL connect'e ulasmamali")

    monkeypatch.setattr(cli_session, "connect", forbidden_connect)
    monkeypatch.setattr(
        close_commands,
        "_close_input_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            input_sha256="sha256:" + "1" * 64,
            receipt=object(),
            invocation=SimpleNamespace(
                admission=DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
                    ("close", "apply"), {"apply": True}
                )
            ),
        ),
    )
    result = runner.invoke(
        app,
        [
            "close",
            "apply",
            "--girdi",
            str(input_file),
            "--idempotency-key",
            "sqlite-negative-close",
            "--plan-digest",
            "sha256:" + "0" * 64,
            "--authorization-id",
            "00000000-0000-0000-0000-000000000001",
            "--claim-id",
            "00000000-0000-0000-0000-000000000002",
            "--uygula",
            "--home",
            str(home_root),
        ],
    )

    assert result.exit_code == 70
    assert "close apply mutation full-continuity" in result.output
    assert "PostgreSQL'e fallback yok" in result.output


def test_sqlite_rejects_canonical_lifecycle_mutation_before_postgres_connect(
    home_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home_root), "--persistence", "sqlite"])
    assert initialized.exit_code == 0, initialized.output

    def forbidden_connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("SQLite lifecycle PostgreSQL connect'e ulasmamali")

    monkeypatch.setattr(cli_session, "connect", forbidden_connect)
    result = runner.invoke(
        app,
        [
            "opencode",
            "forward",
            "--limit",
            "1",
            "--home",
            str(home_root),
        ],
    )

    assert result.exit_code == 70
    assert "opencode forward mutation full-continuity" in result.output
    assert "PostgreSQL'e fallback yok" in result.output
