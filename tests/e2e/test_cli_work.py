"""`zekam work` uctan uca akisi: create, transition, history, next, resume."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.application.config import DatabaseSettings
from zekam.application.realm_context import attach_realm
from zekam.application.work_graph import WorkGraphService
from zekam.domain.work import WorkState
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.postgres.project_repository import ProjectResolver
from zekam.interfaces.cli.main import app

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
    return ["--realm", f"work-{secrets.token_hex(4)}"]


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


def _fixture_transition(
    cli_home: Path,
    realm_flags: list[str],
    reference: str,
    target: WorkState,
) -> None:
    """Build graph state directly; CLI hydration denial is covered separately."""

    context = build_context(home=str(cli_home))
    with connect(context.settings.database) as connection:
        realm_context = attach_realm(connection, slug=realm_flags[1])
        project = ProjectResolver(connection, realm_context.realm_id).resolve("gpu").resolved
        assert project is not None
        service = WorkGraphService(connection, realm_context.realm)
        item = service.find_exact(project_id=project.project_id, external_number=reference)
        service.transition(item.id, target)


def test_create_requires_apply_flag(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    result = _run(cli_home, realm_flags, "work", "create", registered_project, "Deneme isi")
    assert result.exit_code == 0
    assert "Dry-run" in result.stdout

    listing = _run(cli_home, realm_flags, "work", "list", "--json")
    assert json.loads(listing.stdout) == []


def test_create_and_list(cli_home: Path, realm_flags: list[str], registered_project: str) -> None:
    created = _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Kok neden analizi",
        "--tur",
        "defect",
        "--numara",
        "123",
        "--uygula",
    )
    assert created.exit_code == 0, created.stdout

    listing = _run(cli_home, realm_flags, "work", "list", "--json")
    rows = json.loads(listing.stdout)
    assert len(rows) == 1
    assert rows[0]["external_number"] == "123"
    assert rows[0]["type"] == "defect"
    assert rows[0]["state"] == "proposed"


def test_lifecycle_through_cli(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Akis",
        "--numara",
        "200",
        "--uygula",
    )
    without_hydration = _run(
        cli_home,
        realm_flags,
        "work",
        "transition",
        registered_project,
        "200",
        "ready",
        "--uygula",
    )
    assert without_hydration.exit_code == 6, without_hydration.stdout

    raw_completed = _run(
        cli_home,
        realm_flags,
        "work",
        "transition",
        registered_project,
        "200",
        "completed",
        "--uygula",
    )
    assert raw_completed.exit_code == 64

    with_evidence = _run(
        cli_home,
        realm_flags,
        "work",
        "transition",
        registered_project,
        "200",
        "completed",
        "--kanit",
        "test=pytest",
        "--uygula",
    )
    assert with_evidence.exit_code == 64

    history = _run(cli_home, realm_flags, "work", "history", registered_project, "200")
    document = json.loads(history.stdout)
    assert document["chain_valid"] is True
    assert [item["state"] for item in document["revisions"]] == [
        "proposed",
    ]


def test_forbidden_transition_exits_with_policy_code(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Atlama",
        "--numara",
        "300",
        "--uygula",
    )
    result = _run(
        cli_home,
        realm_flags,
        "work",
        "transition",
        registered_project,
        "300",
        "completed",
        "--uygula",
    )
    assert result.exit_code == 64


def test_relate_blocks_next_actionable(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    for number, title in (("400", "Blocker"), ("401", "Bloklu"), ("402", "Serbest")):
        _run(
            cli_home,
            realm_flags,
            "work",
            "create",
            registered_project,
            title,
            "--numara",
            number,
            "--uygula",
        )
    _run(
        cli_home,
        realm_flags,
        "work",
        "relate",
        registered_project,
        "400",
        "blocks",
        "401",
        "--uygula",
    )
    for number in ("401", "402"):
        _fixture_transition(cli_home, realm_flags, number, WorkState.READY)

    result = _run(cli_home, realm_flags, "work", "next")
    document = json.loads(result.stdout)
    assert document["next"]["title"] == "Serbest"


def test_resume_answers_from_work_graph(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Devam eden",
        "--numara",
        "500",
        "--uygula",
    )
    _fixture_transition(cli_home, realm_flags, "500", WorkState.READY)
    _fixture_transition(cli_home, realm_flags, "500", WorkState.ACTIVE)

    result = _run(cli_home, realm_flags, "work", "resume", "--json")
    document = json.loads(result.stdout)
    assert document["source"] == "work-graph"
    assert document["next_actionable"]["title"] == "Devam eden"
    assert "Devam eden" in document["next_safe_action"]


def test_show_reports_snapshot(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    _run(
        cli_home,
        realm_flags,
        "work",
        "create",
        registered_project,
        "Goruntulenecek",
        "--numara",
        "600",
        "--kriter",
        "testler gecer",
        "--uygula",
    )
    result = _run(cli_home, realm_flags, "work", "show", registered_project, "600")
    document = json.loads(result.stdout)
    assert document["work_item"]["title"] == "Goruntulenecek"
    assert document["work_item"]["acceptance_criteria"][0]["text"] == "testler gecer"
    assert document["is_actionable"] is True
    assert document["intent"] is None


def test_unknown_external_number_exits_with_not_found(
    cli_home: Path, realm_flags: list[str], registered_project: str
) -> None:
    result = _run(cli_home, realm_flags, "work", "show", registered_project, "9999")
    assert result.exit_code == 4
