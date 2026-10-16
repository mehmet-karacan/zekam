"""Native verifier/reviewer composition fixtures; no real provider is called."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_opencode_batch import _events, _output, _stream
from tests.unit.test_skill_trial_aggregation import batch  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import skill_audits as subject
from zekam.domain.canonical import canonical_bytes
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.clients import opencode_batch as native
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.application.config import load_settings


@pytest.fixture
def audits(world, batch, tmp_path, monkeypatch):
    run_trial, _aggregate, case, _builder_calls = batch
    trials = [run_trial(repetition=i) for i in range(5)]
    controls = {"accepted": True, "preference": "unavailable", "session": None, "malformed": False}
    calls = []

    def transport(spec, *, cwd, stdin=None, cancel=None):
        assert cancel is not None
        if spec.argv[-1] == "--version":
            return _output(spec, native.SUPPORTED_VERSION.encode())
        context = json.loads(stdin)["context"]
        assert context["limits"]["preference_evidence"] == "not-collected"
        assert context["skill_instructions"]
        calls.append(context["role"])
        decision = {
            "accepted": controls["accepted"],
            "aggregate_digest": context["aggregate_digest"],
            "checks": {
                "artifact": "pass",
                "process": "pass",
                "safety": "pass",
                "efficiency": "unavailable",
                "preference": controls["preference"],
            },
            "reason_codes": ["consistent", "insufficient_evidence"],
        }
        if controls["malformed"]:
            decision["accepted"] = 1
        events = _events({"result": {}, "body": {"decision": decision}})
        for event in events:
            event["sessionID"] = event["part"]["sessionID"] = (
                controls["session"] or f"ses_audit_{len(calls)}"
            )
        return _output(spec, _stream(events))

    monkeypatch.setattr(native.runner, "run", transport)

    def plan(role="verifier", model="provider/verifier", prior=None):
        return subject.build_skill_audit_plan(
            world.home,
            references=tuple(item["canonical_job_id"] for item in trials),
            cases=(case,),
            project_id=world.binding.project_id,
            revision_digest=trials[0]["revision_digest"],
            package_digest=trials[0]["package_digest"],
            root=tmp_path / "source",
            executable=tmp_path / "opencode.exe",
            model_id=model,
            role=role,
            prior_reference=prior,
        )

    def run(plan_and_context, *, authorize=True, binding=None):
        planned, context = plan_and_context
        return subject.run_skill_audit(
            world.home,
            binding=binding or _binding(world, planned),
            plan=planned,
            context=context,
            root=tmp_path / "source",
            executable=tmp_path / "opencode.exe",
            realm_id=uuid4(),
            coordinator_id=uuid4(),
            validate_inputs=lambda: None,
            authorize_remote=authorize,
        )

    return plan, run, controls, calls, trials


def test_distinct_verifier_and_reviewer_have_actual_scoped_terminal_evidence(world, audits):
    plan, run, controls, calls, trials = audits
    planned = plan()
    assert calls == []
    assert len(canonical_bytes(planned[1])) <= 180_000
    verification = run(planned)
    assert calls == ["verifier"]
    assert verification["audit_integrity_accepted"] is True
    assert verification["independence_proven"] is False
    assert verification["quality_evaluation_complete"] is False
    assert verification["activatable"] is False
    stored = subject.read_skill_audit(
        world.home, verification["canonical_job_id"], project_id=world.binding.project_id
    )
    assert stored["decision"] == verification["decision"]
    review_plan = plan("reviewer", "provider/reviewer", verification["canonical_job_id"])
    assert (
        review_plan[1]["prior_verification"]["receipt_evidence_digest"]
        == stored["receipt_evidence_digest"]
    )
    review = run(review_plan)
    assert calls == ["verifier", "reviewer"]
    assert review["audit_integrity_accepted"] is True
    assert review["native_identity"] != verification["native_identity"]
    reread = subject.read_skill_audit(
        world.home, review["canonical_job_id"], project_id=world.binding.project_id
    )
    assert reread["decision_valid"] is True
    assert reread["activatable"] is False
    with pytest.raises(PolicyViolation, match="foreign"):
        subject.read_skill_audit(world.home, review["canonical_job_id"], project_id=str(uuid4()))


def test_same_model_variants_and_reviewer_without_verifier_rejected_before_calls(audits):
    plan, _run, _controls, calls, _trials = audits
    for model in ("provider/model", "another-provider/model#high"):
        with pytest.raises(PolicyViolation, match="ayni model"):
            plan(model=model)
    with pytest.raises(PolicyViolation, match="canonical verifier"):
        plan(role="reviewer", model="provider/reviewer")
    assert calls == []


def test_remote_authorization_precedes_audit_dispatch(audits):
    plan, run, _controls, calls, _trials = audits
    with pytest.raises(PolicyViolation, match="authorization"):
        run(plan(), authorize=False)
    assert calls == []


def test_reviewer_cannot_override_rejected_verifier_or_reuse_its_model(audits):
    plan, run, controls, calls, _trials = audits
    controls["accepted"] = False
    verification = run(plan())
    controls["accepted"] = True
    with pytest.raises(PolicyViolation, match="ayni model"):
        plan("reviewer", "provider/verifier#another", verification["canonical_job_id"])
    review = run(plan("reviewer", "provider/reviewer", verification["canonical_job_id"]))
    assert review["decision"]["accepted"] is True
    assert review["audit_integrity_accepted"] is False


@pytest.mark.parametrize("field,value", [("preference", "pass"), ("malformed", True)])
def test_unsupported_preference_or_invalid_decision_never_grants_acceptance(audits, field, value):
    plan, run, controls, _calls, _trials = audits
    controls[field] = value
    report = run(plan())
    assert report["audit_integrity_accepted"] is False
    assert report["quality_evaluation_complete"] is False
    assert report["activatable"] is False


def test_reused_builder_session_requires_recovery(world, audits):
    plan, run, controls, calls, trials = audits
    native_identity = trials[0]["observations"]["candidate"]["native_identity"]
    controls["session"] = native_identity.split(":")[1]
    with pytest.raises(PolicyViolation, match="session reused"):
        run(plan())
    assert world.runtime.status().recovery_jobs == 1


def test_missing_native_verifier_artifact_blocks_reviewer_before_disclosure(world, audits):
    plan, run, controls, calls, _trials = audits
    verification = run(plan())
    objects = LocalContentAddressedStore(
        world.home / load_settings(home=world.home).object_store_relative
    )
    objects.delete(verification["dispatch_result"]["payload"]["artifact_digest"])
    with pytest.raises(ZekamError):
        plan("reviewer", "provider/reviewer", verification["canonical_job_id"])
    assert calls == ["verifier"]
