from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.test_portable_benchmark import _fixture as portable_fixture
from typer.testing import CliRunner

from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e


def _invoke(*args: str):
    return CliRunner().invoke(app, ["model", "campaign", *args])


def test_native_campaign_plan_run_status_report_and_zero_call_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_adapter_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native campaign must not launch a benchmark process")

    monkeypatch.setattr(
        "zekam.application.model_benchmark_service._run_json_process",
        forbidden_adapter_process,
    )
    plan_result = _invoke("plan", "--json")
    assert plan_result.exit_code == 0, plan_result.output
    plan = json.loads(plan_result.output)
    assert plan["campaign_kind"] == "pipeline-acceptance"
    assert plan["exact_call_budget"] == 10
    assert plan["provider_calls"] == 0
    assert plan["foreign_code_execution"] is False
    assert plan["qualifies_production_models"] is False

    missing_apply = _invoke(
        "run",
        "--plan-digest",
        plan["plan_digest"],
        "--home",
        str(tmp_path),
    )
    assert missing_apply.exit_code == 70
    assert "--uygula" in missing_apply.stderr

    stale = _invoke(
        "run",
        "--plan-digest",
        "sha256:" + "0" * 64,
        "--uygula",
        "--home",
        str(tmp_path),
    )
    assert stale.exit_code == 70
    assert "stale" in stale.stderr

    first = _invoke(
        "run",
        "--plan-digest",
        plan["plan_digest"],
        "--uygula",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert first.exit_code == 0, first.output
    run = json.loads(first.output)
    assert run["state"] == "completed"
    assert run["approved"] is True
    assert run["trial_count"] == 5
    assert run["new_claims"] == 10
    assert run["new_receipts"] == 10
    assert run["provider_calls"] == 0

    status_result = _invoke("status", "--home", str(tmp_path), "--json")
    assert status_result.exit_code == 0, status_result.output
    status = json.loads(status_result.output)
    assert status["state"] == "completed"
    assert status["claim_count"] == status["receipt_count"] == 10
    assert status["trial_count"] == status["expected_trials"] == 5
    assert status["failure_count"] == 0
    assert status["aggregate"]["approved"] is True
    assert status["read_only"] is True

    report_result = _invoke("report", "--home", str(tmp_path), "--json")
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(report_result.output)
    assert report["report_ready"] is True
    assert report["aggregate"]["pass_rate"] == 1.0
    assert report["qualifies_production_models"] is False

    replay_result = _invoke(
        "run",
        "--plan-digest",
        plan["plan_digest"],
        "--uygula",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert replay_result.exit_code == 0, replay_result.output
    replay = json.loads(replay_result.output)
    assert replay["plan_id"] == run["plan_id"]
    assert replay["new_claims"] == 0
    assert replay["new_receipts"] == 0
    assert replay["ledger_counts"] == run["ledger_counts"]


def test_native_campaign_status_is_read_only_when_plan_is_absent(tmp_path: Path) -> None:
    result = _invoke("status", "--home", str(tmp_path), "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["state"] == "not-found"
    assert document["read_only"] is True
    assert not (tmp_path / "benchmarklar" / "benchmark.db").exists()


def test_native_campaign_plan_is_digest_bound_to_portable_design_source(
    tmp_path: Path,
) -> None:
    portable_fixture(tmp_path)
    plain_result = _invoke("plan", "--json")
    bound_result = _invoke("plan", "--portable-root", str(tmp_path), "--json")
    assert plain_result.exit_code == bound_result.exit_code == 0
    plain = json.loads(plain_result.output)
    bound = json.loads(bound_result.output)
    assert plain["plan_digest"] != bound["plan_digest"]
    assert bound["portable_source"]["inspection_digest"].startswith("sha256:")
    assert bound["portable_source"]["design_input_only"] is True
    assert bound["portable_source"]["executed"] is False
    assert bound["provider_calls"] == 0

    for plan, extra in ((plain, ()), (bound, ("--portable-root", str(tmp_path)))):
        run = _invoke(
            "run",
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(tmp_path / "home"),
            *extra,
            "--json",
        )
        assert run.exit_code == 0, run.output
        assert json.loads(run.output)["state"] == "completed"
