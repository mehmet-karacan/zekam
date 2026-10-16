"""Canonical local evaluator fixtures; tests are authored for the final phase."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_trial_aggregation import batch  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import skill_measured_aggregate as subject
from zekam.application.config import load_settings
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.personal_skill import SkillScopeKind
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _plan(world, trials, cases):
    return subject.build_measured_aggregate_plan(
        world.home,
        references=tuple(item["canonical_job_id"] for item in trials),
        cases=tuple(cases),
        project_id=world.binding.project_id,
        revision_digest=trials[0]["revision_digest"],
        package_digest=trials[0]["package_digest"],
    )


def test_real_evaluator_receipt_retains_cases_and_replays_without_new_measurement(world, batch):
    run_trial, _aggregate, case, calls = batch
    trials = [run_trial(repetition=i) for i in range(5)]
    plan, preview = _plan(world, trials, [case])
    assert preview["aggregate"]["comparison_state"] == "improved"
    binding = _binding(world, plan)
    first = subject.run_measured_aggregate(
        world.home, binding=binding, plan=plan, cases=(case,), validate_inputs=lambda: None
    )
    assert first["canonical_replayed"] is False
    snapshot = world.runtime.job_snapshot(first["canonical_job_id"])
    assert snapshot["effects"][0]["operation"] == subject.OPERATION
    assert snapshot["effects"][0]["receipt_status"] == "completed"
    assert first["quality_evaluation_complete"] is False
    assert first["activatable"] is False
    replay = subject.run_measured_aggregate(
        world.home, binding=binding, plan=plan, cases=(case,), validate_inputs=lambda: None
    )
    assert replay["canonical_replayed"] is True
    assert replay["aggregate"] == first["aggregate"]
    assert len(calls) == 10
    # The reader receives no case-file path or user/model report JSON.
    stored = subject.read_measured_aggregate(
        world.home, first["canonical_job_id"], project_id=world.binding.project_id
    )
    assert stored["cases"] == [case]
    assert stored["receipt_evidence_digest"] == snapshot["terminal_evidence_digest"]
    assert stored["receipt_evidence_digest"] != stored["aggregate"]["aggregate_digest"]
    assert len(calls) == 10
    with pytest.raises(PolicyViolation, match="foreign"):
        subject.read_measured_aggregate(
            world.home, first["canonical_job_id"], project_id=str(uuid4())
        )


def test_failed_trial_measurement_is_retained_without_quality_promotion(world, batch):
    run_trial, _aggregate, case, _calls = batch
    failed_case = case | {"input": {"fixture_candidate": "process-failure"}}
    trials = [run_trial(case=failed_case)]
    plan, _preview = _plan(world, trials, [failed_case])
    measured = subject.run_measured_aggregate(
        world.home,
        binding=_binding(world, plan),
        plan=plan,
        cases=(failed_case,),
        validate_inputs=lambda: None,
    )
    assert measured["aggregate"]["comparison_state"] == "failed"
    assert measured["aggregate"]["sample_size"] == 1
    assert measured["activatable"] is False


def test_changed_oracle_or_source_artifact_prevents_new_evaluator_claim(world, batch):
    run_trial, _aggregate, case, _calls = batch
    trials = [run_trial()]
    plan, _preview = _plan(world, trials, [case])
    binding = _binding(world, plan)
    with pytest.raises(PolicyViolation):
        subject.run_measured_aggregate(
            world.home,
            binding=binding,
            plan=plan,
            cases=(case | {"expected_output": 9},),
            validate_inputs=lambda: None,
        )
    objects = LocalContentAddressedStore(
        world.home / load_settings(home=world.home).object_store_relative
    )
    objects.delete(trials[0]["observations"]["candidate"]["artifact_digest"])
    with pytest.raises(ZekamError):
        subject.run_measured_aggregate(
            world.home, binding=binding, plan=plan, cases=(case,), validate_inputs=lambda: None
        )
    with world.runtime._connect() as db:
        assert (
            db.execute(
                "select count(*) from local_effect_claim where operation=?", (subject.OPERATION,)
            ).fetchone()[0]
            == 0
        )


def test_terminal_evaluator_summary_cannot_hide_missing_source_evidence(world, batch):
    run_trial, _aggregate, case, _calls = batch
    trials = [run_trial()]
    plan, _preview = _plan(world, trials, [case])
    result = subject.run_measured_aggregate(
        world.home,
        binding=_binding(world, plan),
        plan=plan,
        cases=(case,),
        validate_inputs=lambda: None,
    )
    objects = LocalContentAddressedStore(
        world.home / load_settings(home=world.home).object_store_relative
    )
    objects.delete(trials[0]["observations"]["candidate"]["artifact_digest"])
    with pytest.raises(ZekamError):
        subject.read_measured_aggregate(
            world.home, result["canonical_job_id"], project_id=world.binding.project_id
        )


def test_cli_preview_and_wrong_digest_make_no_evaluator_claim(world, batch, tmp_path, monkeypatch):
    from zekam.interfaces.cli import skill as cli

    run_trial, _aggregate, case, calls = batch
    trials = [run_trial()]
    lifecycle = SimpleNamespace(
        operational_path=world.database,
        evaluation_revision=lambda *_args, **_kwargs: {
            "package_digest": trials[0]["package_digest"]
        },
    )
    monkeypatch.setattr(cli, "_paths", lambda _: (None, lifecycle, world.home))
    monkeypatch.setattr(cli, "_cli_scope", lambda *_args: ((SkillScopeKind.PROJECT, "demo"),))
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    args = [
        "aggregate-record",
        trials[0]["revision_digest"],
        "--trial",
        trials[0]["canonical_job_id"],
        "--case-file",
        str(path),
        "--project",
        "demo",
    ]
    runner = CliRunner()
    preview = runner.invoke(cli.app, args)
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["schema"] == "zekam-skill-measured-aggregate-plan/v1"
    wrong = runner.invoke(cli.app, args + ["--uygula", "--plan-digest", digest("wrong")])
    assert wrong.exit_code != 0
    assert len(calls) == 2
    with world.runtime._connect() as db:
        assert (
            db.execute(
                "select count(*) from local_effect_claim where operation=?", (subject.OPERATION,)
            ).fetchone()[0]
            == 0
        )
