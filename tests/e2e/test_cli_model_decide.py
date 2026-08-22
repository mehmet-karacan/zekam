"""DB gerektirmeyen model benchmark ve decide CLI contract testleri."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.domain.model_benchmark import HARD_GATE_ORDER
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e
runner = CliRunner()


def test_benchmark_registry_contract_is_secret_free() -> None:
    result = runner.invoke(app, ["model", "benchmark", "--json"])
    assert result.exit_code == 0, result.stdout
    document = json.loads(result.stdout)
    assert document["fixture_count"] >= 5
    assert document["remote_fixture_count"] < document["local_fixture_count"]
    assert "://" not in result.stdout


def test_decide_rejects_cli_supplied_candidates_and_hard_gates(tmp_path: Path) -> None:
    evidence = digest({"evidence": 1})
    gates = {gate.value: True for gate in HARD_GATE_ORDER}
    rejected_gates = gates | {"health-current-passed": False}
    candidate = {
        "quota_pool": "codex",
        "evidence_digests": [evidence],
        "quality": 0.9,
        "reliability": 0.9,
        "project_specialization": 0.8,
        "observed_success": 0.8,
        "latency_efficiency": 0.7,
        "token_efficiency": 0.7,
        "cost_efficiency": 0.7,
        "correction_efficiency": 0.8,
    }
    source = tmp_path / "decision.json"
    source.write_text(
        json.dumps(
            {
                "candidates": [
                    candidate | {"model_id": "bad", "gates": rejected_gates},
                    candidate | {"model_id": "good", "gates": gates},
                ],
                "quota_observations": [],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["model", "decide", "--girdi", str(source), "--json"])
    assert result.exit_code == 6
    assert "hard gate" in result.stderr


def test_benchmark_apply_requires_exact_authorization_runtime_gate() -> None:
    evidence = digest({"evidence": 1})
    result = runner.invoke(
        app,
        [
            "model",
            "benchmark",
            "--model",
            "model-a",
            "--inventory-digest",
            evidence,
            "--policy-digest",
            evidence,
            "--uygula",
        ],
    )
    assert result.exit_code == 6
    assert "exact authorization" in result.stderr
