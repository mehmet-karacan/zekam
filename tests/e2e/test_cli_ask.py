"""`zekam ask` ve `zekam research` uctan uca akisi."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.interfaces.cli.main import app
from zekam.interfaces.cli.session import EXIT_AMBIGUOUS

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

runner = CliRunner()


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path / "zekam-home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZEKAM_HOME", str(home))
    runner.invoke(app, ["init", "--home", str(home)])
    return home


@pytest.fixture
def realm_flags() -> list[str]:
    return ["--realm", f"ask-{secrets.token_hex(4)}"]


@pytest.fixture
def registered_project(cli_home: Path, realm_flags: list[str], tmp_path: Path) -> str:
    root = tmp_path / "gpu"
    root.mkdir()
    (root / "README.md").write_text("# gpu\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "project",
            "add",
            str(root),
            "--slug",
            "gpu",
            "--home",
            str(cli_home),
            *realm_flags,
            "--uygula",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return "gpu"


def _run(cli_home: Path, realm_flags: list[str], *arguments: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*arguments, "--home", str(cli_home), *realm_flags])


def test_ask_arastirma_istegini_cozer(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    result = _run(
        cli_home,
        realm_flags,
        "ask",
        "gpu projesindeki 123 numarali defectin kok nedenini arastir",
        "--json",
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    resolution = payload["resolution"]
    assert resolution["request_class"] == "research"
    assert resolution["project_ref"] == registered_project
    assert [item["value"] for item in resolution["exact_identifiers"]] == ["123"]
    assert resolution["grants_authority"] is False
    assert payload["clarifications"] == []


def test_ask_belirsizlikte_mutation_baslatmaz(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    result = _run(cli_home, realm_flags, "ask", "bunu arastir", "--json")
    assert result.exit_code == EXIT_AMBIGUOUS
    payload = json.loads(result.stdout)
    assert payload["may_start_work"] is False
    kinds = {item["kind"] for item in payload["clarifications"]}
    assert "anaphora-unresolved" in kinds


def test_research_dag_koordinatoru_saymaz(cli_home: Path, realm_flags: list[str]) -> None:
    result = runner.invoke(app, ["research", "dag", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["subagent_count"] == 5
    assert payload["parallel_groups"][0] == ["coordinator"]
    assert payload["parallel_groups"][1] == ["critic", "domain-reviewer", "researcher"]


def test_research_start_uygula_olmadan_yazmaz(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    listing = _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Arastirma isi",
        "--tur",
        "research",
        "--uygula",
    )
    assert listing.exit_code == 0, listing.stdout
    items = json.loads(_run(cli_home, realm_flags, "work", "list", "--json").stdout)
    work_id = items[0]["id"]

    from zekam.domain.canonical import digest

    result = _run(
        cli_home,
        realm_flags,
        "research",
        "start",
        registered_project,
        work_id,
        "Filtreli recall nasil olculur?",
        "--intent-digest",
        digest("intent"),
        "--kaynak-revizyon",
        "revision-1",
    )
    assert result.exit_code == 0, result.stdout
    assert "Dry-run" in result.stdout

    applied = _run(
        cli_home,
        realm_flags,
        "research",
        "start",
        registered_project,
        work_id,
        "Filtreli recall nasil olculur?",
        "--intent-digest",
        digest("intent"),
        "--kaynak-revizyon",
        "revision-1",
        "--uygula",
    )
    assert applied.exit_code == 0, applied.stdout
    assert "kaydedildi" in applied.stdout
