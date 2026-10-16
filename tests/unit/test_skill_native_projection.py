"""Lifecycle projection fixtures; native transport is offline, not acceptance evidence."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.test_skill_lifecycle_v2 import _origins, NOW
from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_opencode_batch import _events, _output, _stream
from tests.unit.test_skill_trial_aggregation import batch  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import skill_native_projection as subject
from zekam.application.skill_audits import build_skill_audit_plan, run_skill_audit
from zekam.application.skill_measured_aggregate import (
    build_measured_aggregate_plan,
    run_measured_aggregate,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.canonical import digest
from zekam.domain.personal_skill import PersonalSkillRevision, SkillScopeKind
from zekam.infrastructure.clients import opencode_batch as native
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle


@pytest.fixture
def chain(world, batch, tmp_path, monkeypatch):
    run_trial, _aggregate, case, _calls = batch
    learning = world.home / "state" / "learning.db"
    SQLiteLocalLearning(learning, operational_path=world.database).bootstrap()
    lifecycle = SQLiteSkillLifecycle(learning, world.database)
    revision = lifecycle.propose_revision(
        PersonalSkillRevision(
            skill_id="paired-skill",
            name="paired-skill",
            description="Structured result skill.",
            version=1,
            scope_kind=SkillScopeKind.PROJECT,
            scope_ref="demo",
            package_digest=run_trial.package_digest,
            author_ref="provider/model",
            trigger_terms=("paired",),
        ),
        _origins(world.database),
        now=NOW,
    )
    trials = [run_trial(repetition=i, revision_digest=revision) for i in range(5)]
    aggregate_plan, _result = build_measured_aggregate_plan(
        world.home,
        references=tuple(item["canonical_job_id"] for item in trials),
        cases=(case,),
        project_id=world.binding.project_id,
        revision_digest=revision,
        package_digest=run_trial.package_digest,
    )
    evaluator = run_measured_aggregate(
        world.home,
        binding=_binding(world, aggregate_plan),
        plan=aggregate_plan,
        cases=(case,),
        validate_inputs=lambda: None,
    )
    count = []

    def transport(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, native.SUPPORTED_VERSION.encode())
        context = json.loads(stdin)["context"]
        count.append(context["role"])
        decision = {
            "accepted": True,
            "aggregate_digest": context["aggregate_digest"],
            "checks": {
                "artifact": "pass",
                "process": "pass",
                "safety": "pass",
                "efficiency": "unavailable",
                "preference": "unavailable",
            },
            "reason_codes": ["consistent", "insufficient_evidence"],
        }
        events = _events({"result": {}, "body": {"decision": decision}})
        for event in events:
            event["sessionID"] = event["part"]["sessionID"] = f"ses_projection_audit_{len(count)}"
        return _output(spec, _stream(events))

    monkeypatch.setattr(native.runner, "run", transport)

    def audit(role, model, prior=None):
        plan, context = build_skill_audit_plan(
            world.home,
            references=tuple(item["canonical_job_id"] for item in trials),
            cases=(case,),
            project_id=world.binding.project_id,
            revision_digest=revision,
            package_digest=run_trial.package_digest,
            root=tmp_path / "source",
            executable=tmp_path / "opencode.exe",
            role=role,
            model_id=model,
            prior_reference=prior,
        )
        return run_skill_audit(
            world.home,
            binding=_binding(world, plan),
            plan=plan,
            context=context,
            root=tmp_path / "source",
            executable=tmp_path / "opencode.exe",
            realm_id=uuid4(),
            coordinator_id=uuid4(),
            validate_inputs=lambda: None,
            authorize_remote=True,
        )

    verification = audit("verifier", "provider/verifier")
    review = audit("reviewer", "provider/reviewer", verification["canonical_job_id"])
    plan, evaluation, review_input = subject.build_native_projection_plan(
        world.home,
        project_id=world.binding.project_id,
        evaluator_ref=evaluator["canonical_job_id"],
        verifier_ref=verification["canonical_job_id"],
        reviewer_ref=review["canonical_job_id"],
    )
    return lifecycle, plan, evaluation, review_input, ((SkillScopeKind.PROJECT, "demo"),), count


def test_native_projection_is_atomic_receipt_bound_and_not_promotable(world, chain):
    lifecycle, plan, evaluation, review_input, scopes, calls = chain
    assert evaluation.state == "insufficient-evidence"
    assert evaluation.trials == 5
    assert evaluation.metrics
    assert evaluation.activatable is False
    binding = _binding(world, plan)
    result = subject.run_native_projection(
        world.home,
        lifecycle=lifecycle,
        plan=plan,
        binding=binding,
        allowed_scopes=scopes,
        validate_inputs=lambda: None,
    )
    assert result["state"] == "insufficient-evidence"
    assert result["approved"] is False
    assert result["activatable"] is False
    assert (
        lifecycle.recorded_evidence_digest(evaluation.body(), kind="evaluation")
        == result["evaluation_digest"]
    )
    assert (
        lifecycle.recorded_evidence_digest(
            review_input | {"evaluation_digest": result["evaluation_digest"]}, kind="review"
        )
        == result["review_digest"]
    )
    replay = subject.run_native_projection(
        world.home,
        lifecycle=lifecycle,
        plan=plan,
        binding=binding,
        allowed_scopes=scopes,
        validate_inputs=lambda: None,
    )
    assert replay["canonical_replayed"] is True
    assert replay["evaluation_digest"] == result["evaluation_digest"]
    stored = subject.read_native_projection(
        world.home,
        result["canonical_job_id"],
        lifecycle=lifecycle,
        project_id=world.binding.project_id,
        allowed_scopes=scopes,
    )
    assert stored["review_digest"] == result["review_digest"]
    with pytest.raises(PolicyViolation, match="passing"):
        lifecycle.activation_plan(plan["revision_digest"], action="active", allowed_scopes=scopes)
    # Native receipts retain their real operation; generic legacy import cannot relabel them.
    with pytest.raises(PolicyViolation, match="terminal receipt"):
        lifecycle.record_evaluation(evaluation, now=NOW)
    assert calls == ["verifier", "reviewer"]


def test_native_ledger_write_without_projection_claim_is_rejected(world, chain):
    lifecycle, plan, _evaluation, _review, scopes, _calls = chain
    binding = _binding(world, plan)
    with pytest.raises(PolicyViolation, match="own canonical claim"):
        lifecycle.record_native_projection(
            home=world.home, plan=plan, binding=binding, allowed_scopes=scopes, now=NOW
        )
    with lifecycle._connect(read_only=True) as db:
        assert db.execute("select count(*) from skill_evaluation_v2").fetchone()[0] == 0
        assert db.execute("select count(*) from skill_review_v2").fetchone()[0] == 0


def test_native_ledger_pair_rolls_back_when_review_write_fails(world, chain, monkeypatch):
    lifecycle, plan, _evaluation, _review, scopes, _calls = chain

    def fail_review(*_args, **_kwargs):
        raise PolicyViolation("fixture review write interruption")

    monkeypatch.setattr(lifecycle, "_record_review_tx", fail_review)
    with pytest.raises(PolicyViolation, match="interruption"):
        subject.run_native_projection(
            world.home,
            lifecycle=lifecycle,
            plan=plan,
            binding=_binding(world, plan),
            allowed_scopes=scopes,
            validate_inputs=lambda: None,
        )
    with lifecycle._connect(read_only=True) as db:
        assert db.execute("select count(*) from skill_evaluation_v2").fetchone()[0] == 0
        assert db.execute("select count(*) from skill_review_v2").fetchone()[0] == 0
    assert world.runtime.status().recovery_jobs == 1


def test_completed_projection_does_not_reconstruct_missing_ledger_rows(world, chain, monkeypatch):
    lifecycle, plan, _evaluation, _review, scopes, _calls = chain
    binding = _binding(world, plan)
    subject.run_native_projection(
        world.home,
        lifecycle=lifecycle,
        plan=plan,
        binding=binding,
        allowed_scopes=scopes,
        validate_inputs=lambda: None,
    )

    def missing(*_args, **_kwargs):
        raise PolicyViolation("fixture missing recorded row")

    def forbid_write(*_args, **_kwargs):
        pytest.fail("completed projection must not reconstruct the ledger")

    monkeypatch.setattr(lifecycle, "recorded_evidence_digest", missing)
    monkeypatch.setattr(lifecycle, "record_native_projection", forbid_write)
    with pytest.raises(PolicyViolation, match="missing recorded row"):
        subject.run_native_projection(
            world.home,
            lifecycle=lifecycle,
            plan=plan,
            binding=binding,
            allowed_scopes=scopes,
            validate_inputs=lambda: None,
        )


def test_cli_projection_preview_and_wrong_digest_do_not_claim_or_write(world, chain, monkeypatch):
    from typer.testing import CliRunner
    from zekam.interfaces.cli import skill as cli

    lifecycle, plan, _evaluation, _review, scopes, calls = chain
    monkeypatch.setattr(cli, "_paths", lambda _: (None, lifecycle, world.home))
    monkeypatch.setattr(cli, "_cli_scope", lambda *_args: scopes)
    args = [
        "evaluation-from-audits",
        "--evaluator",
        plan["evaluator_job_id"],
        "--verifier",
        plan["verifier_job_id"],
        "--reviewer",
        plan["reviewer_job_id"],
        "--project",
        "demo",
    ]
    runner = CliRunner()
    preview = runner.invoke(cli.app, args)
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["plan_digest"] == plan["plan_digest"]
    wrong = runner.invoke(cli.app, args + ["--uygula", "--plan-digest", digest("wrong")])
    assert wrong.exit_code != 0
    with lifecycle._connect(read_only=True) as db:
        assert db.execute("select count(*) from skill_evaluation_v2").fetchone()[0] == 0
    assert calls == ["verifier", "reviewer"]
