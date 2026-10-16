"""Real canonical SQLite/CAS chains with an offline native-event transport fixture."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_opencode_batch import _events, _output, _stream
from tests.unit.test_skill_trials import _binding, _inputs
from zekam.application import skill_trial_aggregation as subject
from zekam.application.skill_trials import build_skill_trial_plan, run_skill_trial
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.clients import opencode_batch as native
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def batch(world, tmp_path, monkeypatch):
    root, package, base_case, _plan = _inputs(tmp_path, world.binding.project_id)
    calls = []
    reuse_sessions = [False]
    monkeypatch.setattr(native, "resolve_opencode_executable", lambda value: Path(value))

    def transport(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, native.SUPPORTED_VERSION.encode())
        prompt = json.loads(stdin)["context"]
        condition = "candidate" if prompt["skill_instructions"] else "baseline"
        calls.append(condition)
        answer = prompt["input"].get(f"fixture_{condition}", 7 if condition == "candidate" else 0)
        if answer == "process-failure":
            return _output(spec, b"", exit_code=1)
        events = _events({"result": {}, "body": {"output": answer}})
        for event in events:
            number = (len(calls) - 1) % 2 + 1 if reuse_sessions[0] else len(calls)
            event["sessionID"] = event["part"]["sessionID"] = f"ses_unique_{number}"
        return _output(spec, _stream(events))

    monkeypatch.setattr(native.runner, "run", transport)

    def execute(repetition=0, case=None, model="provider/model", reuse_session=False):
        reuse_sessions[0] = reuse_session
        case = base_case if case is None else case
        plan = build_skill_trial_plan(
            revision_digest=_plan["revision_digest"],
            package=package,
            case=case,
            project_id=world.binding.project_id,
            model_id=model,
            executable=tmp_path / "opencode.exe",
            objects=LocalContentAddressedStore(tmp_path / "cas"),
            source_root=root,
            repetition=repetition,
        )
        return run_skill_trial(
            world.home,
            binding=_binding(world, plan),
            package=package,
            case=case,
            plan=plan,
            root=root,
            executable=tmp_path / "opencode.exe",
            realm_id=uuid4(),
            coordinator_id=uuid4(),
            validate_inputs=lambda: None,
            authorize_remote=True,
        )

    def aggregate(results, cases=None, project_id=None):
        return subject.aggregate_skill_trials(
            world.home,
            references=tuple(item["canonical_job_id"] for item in results),
            cases=tuple(cases or [base_case]),
            project_id=project_id or world.binding.project_id,
            revision_digest=_plan["revision_digest"],
            package_digest=package.package_digest,
        )

    return execute, aggregate, base_case, calls


def test_five_actual_holdout_pairs_are_regraded_and_order_independent(batch):
    execute, aggregate, case, calls = batch
    results = [execute(repetition=i) for i in range(5)]
    before = len(calls)
    report = aggregate(results)
    assert report["sample_size"] == report["holdout_pair_count"] == 5
    assert report["holdout_case_count"] == 1
    assert report["comparison_state"] == "improved"
    assert report["partition_metrics"]["holdout"]["baseline"]["exact_match_rate"] == 0.0
    assert report["partition_metrics"]["holdout"]["candidate"]["exact_match_rate"] == 1.0
    assert report["independent_verification"] is False
    assert report["activatable"] is False
    assert report["uncertainty"]["unique_case_count"] == 1
    assert aggregate(list(reversed(results)))["aggregate_digest"] == report["aggregate_digest"]
    assert len(calls) == before == 10
    assert {item["plan"]["repetition"] for item in results} == set(range(5))
    assert results[0]["plan"]["order"] == list(reversed(results[1]["plan"]["order"]))


def test_development_pairs_cannot_satisfy_holdout_minimum(batch):
    execute, aggregate, case, _calls = batch
    development = case | {"case_id": "development-one", "partition": "development"}
    results = [execute(repetition=i, case=development) for i in range(5)]
    report = aggregate(results, [development])
    assert report["comparison_state"] == "insufficient-evidence"
    assert report["holdout_pair_count"] == 0
    assert report["partition_metrics"]["holdout"]["candidate"]["exact_match_rate"] is None


def test_duplicate_repetition_from_different_runs_cannot_inflate_sample_size(batch):
    execute, aggregate, _case, _calls = batch
    with pytest.raises(PolicyViolation, match="sample"):
        aggregate([execute(), execute()])


def test_session_reuse_across_distinct_receipts_is_rejected(batch):
    execute, aggregate, _case, _calls = batch
    first = execute()
    second = execute(repetition=1, reuse_session=True)
    with pytest.raises(PolicyViolation, match="session reused"):
        aggregate([first, second])


def test_mixed_models_foreign_scope_and_unused_cases_rejected(batch):
    execute, aggregate, case, _calls = batch
    first = execute()
    with pytest.raises(PolicyViolation, match="mixed"):
        aggregate([first, execute(repetition=1, model="provider/another-model")])
    with pytest.raises(PolicyViolation, match="foreign"):
        aggregate([first], project_id=str(uuid4()))
    with pytest.raises(PolicyViolation, match="unused"):
        aggregate([first], [case, case | {"case_id": "unobserved-case"}])


def test_aliases_of_one_terminal_job_are_not_two_samples(world, batch):
    execute, _aggregate, case, _calls = batch
    first = execute()
    snapshot = world.runtime.job_snapshot(first["canonical_job_id"])
    with pytest.raises(PolicyViolation, match="duplicate"):
        subject.aggregate_skill_trials(
            world.home,
            references=(snapshot["job_id"], snapshot["idempotency_key"]),
            cases=(case,),
            project_id=world.binding.project_id,
            revision_digest=first["revision_digest"],
            package_digest=first["package_digest"],
        )


def test_case_regression_and_native_failures_are_retained(batch):
    execute, aggregate, case, _calls = batch
    regression = case | {
        "case_id": "regression",
        "input": {"fixture_candidate": 0, "fixture_baseline": 7},
    }
    results = [execute(repetition=i) for i in range(5)] + [execute(case=regression)]
    report = aggregate(results, [case, regression])
    assert report["comparison_state"] == "regressed"
    assert report["sample_size"] == 6
    assert report["partition_metrics"]["holdout"]["candidate"]["exact_match_rate"] == 0.5
    assert report["partition_metrics"]["holdout"]["baseline"]["exact_match_rate"] == 0.5
    failure = case | {"case_id": "failed", "input": {"fixture_candidate": "process-failure"}}
    report = aggregate([execute(case=failure)], [failure])
    assert report["comparison_state"] == "failed"
    assert report["case_results"][0]["candidate"]["exact_match_rate"] == 0.0


def test_grade_is_recomputed_from_native_artifact(world, batch, monkeypatch):
    execute, aggregate, _case, _calls = batch
    first = execute()
    original = subject.replay_canonical_job

    def forged_grade(*args, **kwargs):
        report = original(*args, **kwargs)
        report["observations"]["candidate"]["exact_match"] = False
        return report

    monkeypatch.setattr(subject, "replay_canonical_job", forged_grade)
    with pytest.raises(PolicyViolation, match="grade"):
        aggregate([first])


def test_missing_output_artifact_cannot_be_replaced_by_claimed_grade(world, batch):
    execute, aggregate, _case, _calls = batch
    first = execute()
    # Use the configured CAS path, rather than a second evidence store.
    from zekam.application.config import load_settings

    objects = LocalContentAddressedStore(
        world.home / load_settings(home=world.home).object_store_relative
    )
    objects.delete(first["observations"]["candidate"]["artifact_digest"])
    with pytest.raises(ZekamError):
        aggregate([first])
