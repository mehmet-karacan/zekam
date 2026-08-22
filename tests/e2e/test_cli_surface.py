"""`zekam surface` ve komut yuzeyi tutarliligi."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zekam.domain.observability import CANONICAL_COMMANDS, command_names
from zekam.interfaces.cli.main import app
from zekam.interfaces.cli.surface import registered_commands

pytestmark = pytest.mark.e2e

runner = CliRunner()


def test_sozlesmedeki_her_komut_gercekten_kayitli() -> None:
    """Belge ile kod arasindaki sapma burada yakalanir."""

    result = runner.invoke(app, ["surface", "check", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["missing"] == []
    assert payload["contract_count"] == len(CANONICAL_COMMANDS)


def test_kayitli_komutlar_sozlesmeyi_kapsar() -> None:
    available = registered_commands(app)
    for name in command_names():
        assert name in available, f"{name} kayitli degil"


def test_sozlesme_ciktisi_mutasyon_bilgisini_tasir() -> None:
    result = runner.invoke(app, ["surface", "contract", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    mutating = {item["name"] for item in payload if item["mutating"]}
    assert "knowledge ingest" in mutating
    assert "ask" not in mutating
    for item in payload:
        if item["mutating"]:
            assert item["requires_apply_flag"] is True


def test_yardim_metni_butun_gruplari_gosterir() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("project", "work", "knowledge", "scheduler", "report", "surface"):
        assert group in result.stdout
