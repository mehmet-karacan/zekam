"""`zekam scheduler init` uctan uca akisi.

Komut mutasyon yapar; acik `--uygula` bayragi olmadan hicbir tanim yazmaz.
ZEKAM-DEF-004 bu komutun yoklugunda taze kurulumun `doctor` healthy durumuna
ulasamadigini kayit altina almisti.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.domain.scheduler import REQUIRED_JOB_INTERVALS, REQUIRED_JOBS
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
    return ["--realm", f"scheduler-{secrets.token_hex(4)}"]


def _run(cli_home: Path, realm_flags: list[str], *arguments: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*arguments, "--home", str(cli_home), *realm_flags])


def _defined(cli_home: Path, realm_flags: list[str]) -> dict[str, str]:
    listing = _run(cli_home, realm_flags, "scheduler", "list", "--json")
    assert listing.exit_code == 0, listing.stdout
    payload = json.loads(listing.stdout)
    return {item["job_name"]: item["interval"] for item in payload["definitions"]}


def test_uygula_bayragi_olmadan_tanim_yazilmaz(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "scheduler", "init")
    assert result.exit_code == 0, result.stdout
    assert "Dry-run" in result.stdout
    assert _defined(cli_home, realm_flags) == {}


def test_uygula_zorunlu_isleri_tanimlar(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "scheduler", "init", "--uygula")
    assert result.exit_code == 0, result.stdout

    assert _defined(cli_home, realm_flags) == dict(REQUIRED_JOB_INTERVALS)

    listing = _run(cli_home, realm_flags, "scheduler", "list", "--json")
    assert json.loads(listing.stdout)["missing_required"] == []


def test_ikinci_calistirma_yeni_tanim_uretmez(cli_home: Path, realm_flags: list[str]) -> None:
    _run(cli_home, realm_flags, "scheduler", "init", "--uygula")
    result = _run(cli_home, realm_flags, "scheduler", "init", "--uygula")
    assert result.exit_code == 0, result.stdout
    assert "0 yeni tanim" in result.stdout
    assert len(_defined(cli_home, realm_flags)) == len(REQUIRED_JOBS)
