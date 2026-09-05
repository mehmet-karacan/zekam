"""`zekam policy`, `zekam secret` ve `zekam auth` uctan uca akisi."""

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
SECRET_VALUE = "Kx7pQm2ZrT9wLb4Nc1Vd"


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
    return ["--realm", f"gov-{secrets.token_hex(4)}"]


def _run(cli_home: Path, realm_flags: list[str], *arguments: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*arguments, "--home", str(cli_home), *realm_flags])


@pytest.fixture
def initialized(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "policy", "init", "--uygula")
    assert result.exit_code == 0, result.stdout


def test_policy_init_is_idempotent(cli_home: Path, realm_flags: list[str]) -> None:
    first = _run(cli_home, realm_flags, "policy", "init", "--uygula")
    second = _run(cli_home, realm_flags, "policy", "init", "--uygula")
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "0 yeni capability" in second.stdout


def test_policy_init_requires_apply(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "policy", "init")
    assert result.exit_code == 0
    assert "Dry-run" in result.stdout


def test_policy_show_reports_deny_defaults(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    result = _run(cli_home, realm_flags, "policy", "show")
    document = json.loads(result.stdout)
    assert document["network_default_deny"] is True
    assert document["push_default_deny"] is True
    denied = {rule["name"] for rule in document["rules"] if not rule["allow"]}
    assert "ag-varsayilan-kapali" in denied
    assert "push-varsayilan-kapali" in denied


def test_capabilities_are_listed(cli_home: Path, realm_flags: list[str], initialized: None) -> None:
    result = _run(cli_home, realm_flags, "policy", "capabilities")
    assert result.exit_code == 0
    assert "sandbox.write" in result.stdout
    assert "yetki degil" in result.stdout


def test_secret_add_stores_locator_not_value(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    added = _run(
        cli_home,
        realm_flags,
        "secret",
        "add",
        "anthropic-api",
        "--provider",
        "anthropic",
        "--amac",
        "chat",
        "--locator",
        "ANTHROPIC_API_KEY",
        "--operasyon",
        "chat",
        "--uygula",
    )
    assert added.exit_code == 0, added.stdout

    listing = _run(cli_home, realm_flags, "secret", "list", "--json")
    rows = json.loads(listing.stdout)
    assert rows[0]["store_locator"] == "ANTHROPIC_API_KEY"
    assert "value" not in rows[0]
    assert SECRET_VALUE not in listing.stdout


def test_secret_add_rejects_a_value_shaped_locator(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    result = _run(
        cli_home,
        realm_flags,
        "secret",
        "add",
        "kotu",
        "--provider",
        "anthropic",
        "--amac",
        "chat",
        "--locator",
        f"ANTHROPIC_API_KEY={SECRET_VALUE}",
        "--operasyon",
        "chat",
        "--uygula",
    )
    assert result.exit_code != 0


def test_secret_revoke_marks_reference(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    _run(
        cli_home,
        realm_flags,
        "secret",
        "add",
        "gecici",
        "--provider",
        "test",
        "--amac",
        "test",
        "--locator",
        "GECICI",
        "--operasyon",
        "chat",
        "--uygula",
    )
    revoked = _run(cli_home, realm_flags, "secret", "revoke", "gecici", "--uygula")
    assert revoked.exit_code == 0

    listing = _run(cli_home, realm_flags, "secret", "list", "--json")
    rows = json.loads(listing.stdout)
    assert rows[0]["status"] == "revoked"


def test_auth_list_is_empty_before_any_authorization(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    result = _run(cli_home, realm_flags, "auth", "list", "--json")
    assert json.loads(result.stdout) == []


def test_auth_show_rejects_invalid_identifier(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    result = _run(cli_home, realm_flags, "auth", "show", "gecersiz")
    assert result.exit_code == 4


def test_auth_revoke_of_unknown_identifier_fails(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    from uuid import uuid4

    result = _run(
        cli_home, realm_flags, "auth", "revoke", str(uuid4()), "--gerekce", "test", "--uygula"
    )
    assert result.exit_code == 6


def test_audit_records_are_readable(
    cli_home: Path, realm_flags: list[str], initialized: None
) -> None:
    _run(cli_home, realm_flags, "policy", "show")
    result = _run(cli_home, realm_flags, "auth", "audit", "--adet", "5")
    assert result.exit_code == 0
    assert isinstance(json.loads(result.stdout), list)
