"""`zekam sandbox` ve `zekam git` CLI akisi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.interfaces.cli.main import app
from zekam.interfaces.cli.session import EXIT_POLICY_VIOLATION

pytestmark = pytest.mark.e2e

runner = CliRunner()

GOOD_MESSAGE = """ozellik: sandbox teslim akisini ekle

Neden:
- Builder yalniz bagli gercek source rootuna yazmali.

Degisiklik:
- Direct-source mutation eklendi.

Kanit:
- Kabul testleri gecti.

Risk:
- Yanlis source binding riski vardir.

Geri donus:
- Git diff ile geri alinir.
"""


def test_sandbox_policy_default_deny_gosterir() -> None:
    result = runner.invoke(app, ["sandbox", "policy", "--yol", "src/zekam", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["network"]["default_deny"] is True
    assert payload["main_tree_read_only"] is False
    assert payload["direct_source_write"] is True
    assert payload["project_copy"] is False
    assert payload["policy_digest"].startswith("sha256:")


def test_sandbox_policy_bos_allowlist_reddeder() -> None:
    result = runner.invoke(app, ["sandbox", "policy", "--yol", "/etc", "--json"])
    assert result.exit_code != 0


def test_commit_check_gecerli_mesaji_kabul_eder(tmp_path: Path) -> None:
    target = tmp_path / "mesaj.txt"
    target.write_text(GOOD_MESSAGE, encoding="utf-8", newline="\n")
    result = runner.invoke(app, ["git", "commit-check", "--dosya", str(target), "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["accepted"] is True


def test_commit_check_non_ascii_reddeder(tmp_path: Path) -> None:
    target = tmp_path / "mesaj.txt"
    target.write_text(GOOD_MESSAGE.replace("ekle", "ekleç"), encoding="utf-8", newline="\n")
    result = runner.invoke(app, ["git", "commit-check", "--dosya", str(target), "--json"])
    assert result.exit_code == EXIT_POLICY_VIOLATION
    codes = {item["code"] for item in json.loads(result.stdout)["violations"]}
    assert "non-ascii" in codes


def test_push_check_varsayilan_reddeder() -> None:
    result = runner.invoke(app, ["git", "push-check", "origin", "main", "abc1234", "--json"])
    assert result.exit_code == EXIT_POLICY_VIOLATION
    assert json.loads(result.stdout)["allowed"] is False


def test_push_check_tam_kanitla_izin_verir() -> None:
    result = runner.invoke(
        app,
        [
            "git",
            "push-check",
            "origin",
            "main",
            "abc1234",
            "--kullanici-istedi",
            "--yetki-digest",
            digest("auth"),
            "--test-gecti",
            "--verifier-gecti",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["allowed"] is True
