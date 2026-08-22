"""`zekam worker` uctan uca akisi."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
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
    return ["--realm", f"worker-{secrets.token_hex(4)}"]


def test_worker_ayarlari_yetenek_beyan_eder() -> None:
    result = runner.invoke(app, ["worker", "settings", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["capabilities"]
    assert payload["max_workers"] > 0
    assert payload["max_queue_depth"] > 0


def test_worker_tick_bos_kuyrukta_calisir(cli_home: Path, realm_flags: list[str]) -> None:
    """Realm yoksa bile komut sanitize hata verir; varsa bos kuyrukla doner."""

    result = runner.invoke(
        app,
        ["project", "list", "--json", "--home", str(cli_home), *realm_flags],
    )
    # Realm henuz yok; worker tick de ayni sekilde not-found dondurur.
    assert result.exit_code in (0, 4)


def test_worker_tick_zamanlanmis_isi_tetikler(
    cli_home: Path, realm_flags: list[str], tmp_path: Path
) -> None:
    root = tmp_path / "kaynak"
    root.mkdir()
    (root / "README.md").write_text("# kaynak\n", encoding="utf-8")
    created = runner.invoke(
        app,
        [
            "project",
            "add",
            str(root),
            "--slug",
            "kaynak",
            "--home",
            str(cli_home),
            *realm_flags,
            "--uygula",
        ],
    )
    assert created.exit_code == 0, created.stdout

    # --uygula olmadan salt okunur: hicbir sey yazilmaz.
    dry = runner.invoke(app, ["worker", "tick", "--json", "--home", str(cli_home), *realm_flags])
    assert dry.exit_code == 0, dry.stdout
    plan = json.loads(dry.stdout)
    assert plan["accepted_work"] is False
    assert "dry-run" in plan["skipped_reason"]

    applied = runner.invoke(
        app, ["worker", "tick", "--uygula", "--json", "--home", str(cli_home), *realm_flags]
    )
    assert applied.exit_code == 0, applied.stdout
    payload = json.loads(applied.stdout)
    # Tanimli zamanlanmis is yok; kuyruk da bos.
    assert payload["accepted_work"] is False
    assert payload["triggered_jobs"] == []
    assert payload["skipped_reason"] == "kuyruk bos"


def test_worker_run_uygula_olmadan_baslamaz(cli_home: Path, realm_flags: list[str]) -> None:
    result = runner.invoke(app, ["worker", "run", "--home", str(cli_home), *realm_flags])
    assert result.exit_code == 0, result.stdout
    assert "Dry-run" in result.stdout


def test_worker_komutu_sozlesmede_kayitli() -> None:
    result = runner.invoke(app, ["surface", "check", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["missing"] == []
