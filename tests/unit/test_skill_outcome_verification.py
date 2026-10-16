"""Offline native verifier fixtures; no actual provider or independent acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_native_invocation import native_load  # noqa: F401
from tests.unit.test_skill_native_execution import execution  # noqa: F401
from tests.unit.test_skill_trials import _binding
from tests.unit.test_opencode_batch import _events as answer_events, _output, _stream
from tests.unit.test_opencode_skill_protocol import _events as tool_events
from zekam.application import skill_native_execution as native
from zekam.application import skill_outcome_verification as subject
from zekam.application.skill_execution_invocation import (
    build_execution_invocation_plan,
    run_execution_invocation,
)
from zekam.application.unit_test_provider_profile import BatchProvider, BatchProviderProfile
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients import opencode_batch as batch
from zekam.infrastructure.clients.opencode_skill_protocol import native_skill_render


@pytest.fixture
def verified_source(world, native_load, tmp_path, monkeypatch):
    f = native_load
    runtime = tmp_path.parent / f"{tmp_path.name}-outcome-runtime"
    runtime.mkdir()
    monkeypatch.setattr(batch, "_native_runtime_root", lambda root: root.resolve())
    monkeypatch.setattr(batch, "resolve_opencode_executable", lambda path: path)
    monkeypatch.setattr(native, "resolve_project_source", lambda *_args: f.root)
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "fixture",
                "http://127.0.0.1:11434/v1",
                "loopback",
                (("builder", "actual-builder"), ("verifier", "actual-verifier")),
            ),
        )
    )
    calls = []
    mode = {"answer": 7, "session": "ses_verifier", "exit": 0, "decision": "consistent"}

    def transport(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, batch.SUPPORTED_VERSION.encode())
        context = json.loads(stdin)["context"]
        calls.append(context)
        if context["schema"] == "zekam-native-skill-task/v1":
            assert "expected_output" not in context
            directory = (
                Path(json.loads(dict(spec.env)["OPENCODE_CONFIG_CONTENT"])["skills"][0])
                / f.package.name
            )
            events = tool_events(directory)
            events[3]["part"]["state"]["output"] = native_skill_render(f.package, directory)
            events[-2]["part"]["text"] = json.dumps(
                {"result": {}, "body": {"output": mode["answer"]}}
            )
            return _output(spec, _stream(events))
        assert context["case"]["expected_output"] == 7
        exact = context["local_grade"]["exact_match"]
        decision = {
            "subject_digest": context["subject_digest"],
            "verdict": "pass" if exact else "fail",
            "checks": {
                "artifact": "pass" if exact else "fail",
                "process": "pass",
                "safety": "pass",
            },
            "reason_codes": ["consistent" if exact else "mismatch"],
        }
        if mode["decision"] == "invalid":
            decision["verdict"] = {"bad": "shape"}
        events = answer_events({"result": {}, "body": {"output": decision}})
        for event in events:
            event["sessionID"] = event["part"]["sessionID"] = mode["session"]
        return _output(spec, _stream(events), exit_code=mode["exit"])

    monkeypatch.setattr(batch.runner, "run", transport)
    case = {
        "schema": "zekam-skill-trial-case/v1",
        "case_id": "outcome-case",
        "partition": "holdout",
        "input": {"question": "Return seven."},
        "expected_output": 7,
    }

    def source():
        plan = native.build_native_execution_plan(
            world.home,
            lifecycle=f.lifecycle,
            package=f.package,
            case=case,
            project_id=world.binding.project_id,
            model_id="fixture/builder",
            executable=tmp_path / "opencode.exe",
            root=f.root,
            runtime_root=runtime,
            allowed_scopes=f.scopes,
            provider_profile=profile,
        )
        result = native.run_native_execution(
            world.home,
            lifecycle=f.lifecycle,
            binding=_binding(world, plan),
            package=f.package,
            case=case,
            plan=plan,
            root=f.root,
            runtime_root=runtime,
            executable=tmp_path / "opencode.exe",
            allowed_scopes=f.scopes,
            realm_id=uuid4(),
            coordinator_id=uuid4(),
            validate_inputs=lambda: None,
            provider_profile=profile,
        )
        ip = build_execution_invocation_plan(
            world.home,
            lifecycle=f.lifecycle,
            execution_ref=result["canonical_job_id"],
            project_id=world.binding.project_id,
            allowed_scopes=f.scopes,
        )
        return run_execution_invocation(
            world.home,
            lifecycle=f.lifecycle,
            plan=ip,
            binding=_binding(world, ip),
            allowed_scopes=f.scopes,
        )

    def prepare(invocation):
        return subject.build_outcome_verification_plan(
            world.home,
            lifecycle=f.lifecycle,
            invocation_ref=invocation["canonical_job_id"],
            project_id=world.binding.project_id,
            allowed_scopes=f.scopes,
            verifier_model="fixture/verifier",
            executable=tmp_path / "opencode.exe",
            root=f.root,
            provider_profile=profile,
        )

    def run(plan, context, binding, realm, coordinator):
        return subject.run_outcome_verification(
            world.home,
            lifecycle=f.lifecycle,
            plan=plan,
            context=context,
            binding=binding,
            allowed_scopes=f.scopes,
            root=f.root,
            executable=tmp_path / "opencode.exe",
            realm_id=realm,
            coordinator_id=coordinator,
            provider_profile=profile,
        )

    return SimpleNamespace(
        f=f, profile=profile, calls=calls, mode=mode, source=source, prepare=prepare, run=run
    )


@pytest.mark.parametrize("answer,status", [(7, "verified-success"), (8, "verified-failure")])
def test_separate_verifier_receipt_covers_one_native_artifact_with_no_promotion(
    world, verified_source, answer, status
):
    x = verified_source
    x.mode["answer"] = answer
    source = x.source()
    plan, context = x.prepare(source)
    binding = _binding(world, plan)
    realm, coordinator = uuid4(), uuid4()
    result = x.run(plan, context, binding, realm, coordinator)
    replay = x.run(plan, context, binding, realm, coordinator)
    assert result["status"] == status
    assert replay["canonical_replayed"] is True and len(x.calls) == 2
    assert result["quality_evaluation_complete"] is False and result["activatable"] is False
    assert result["upstream_identity_attested"] is False
    report = subject.read_outcome_verification(
        world.home,
        result["canonical_job_id"],
        lifecycle=x.f.lifecycle,
        project_id=binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    assert report["status"] == status and report["receipt_evidence_digest"]
    assert len(x.calls) == 2


@pytest.mark.parametrize("mode", ["invalid-decision", "failed-process"])
def test_invalid_and_failed_verifier_result_stays_unverified(world, verified_source, mode):
    x = verified_source
    source = x.source()
    plan, context = x.prepare(source)
    if mode == "invalid-decision":
        x.mode["decision"] = "invalid"
    else:
        x.mode["exit"] = 4
    result = x.run(plan, context, _binding(world, plan), uuid4(), uuid4())
    assert result["status"] == "unverified" and result["decision"] is None


def test_verifier_cannot_reuse_native_builder_session(world, verified_source):
    x = verified_source
    plan, context = x.prepare(x.source())
    x.mode["session"] = "ses_native"
    with pytest.raises(PolicyViolation, match="reused native builder session"):
        x.run(plan, context, _binding(world, plan), uuid4(), uuid4())
    assert world.runtime.status().recovery_jobs == 1


@pytest.mark.parametrize("verifier", ["fixture/builder#other", "different/builder"])
def test_same_requested_model_and_variant_alias_are_rejected(
    world, verified_source, tmp_path, verifier
):
    x = verified_source
    source = x.source()
    with pytest.raises(PolicyViolation):
        subject.build_outcome_verification_plan(
            world.home,
            lifecycle=x.f.lifecycle,
            invocation_ref=source["canonical_job_id"],
            project_id=world.binding.project_id,
            allowed_scopes=x.f.scopes,
            verifier_model=verifier,
            executable=tmp_path / "opencode.exe",
            root=x.f.root,
            provider_profile=x.profile,
        )
    assert len(x.calls) == 1


def test_builtin_unknown_upstream_identity_cannot_be_promoted_by_a_model_pass(
    world, execution, tmp_path
):
    x = execution
    result = x.run()
    ip = build_execution_invocation_plan(
        world.home,
        lifecycle=x.f.lifecycle,
        execution_ref=result["canonical_job_id"],
        project_id=world.binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    invocation = run_execution_invocation(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=ip,
        binding=_binding(world, ip),
        allowed_scopes=x.f.scopes,
    )
    plan, context = subject.build_outcome_verification_plan(
        world.home,
        lifecycle=x.f.lifecycle,
        invocation_ref=invocation["canonical_job_id"],
        project_id=world.binding.project_id,
        allowed_scopes=x.f.scopes,
        verifier_model="provider/different-verifier",
        executable=tmp_path / "opencode.exe",
        root=x.f.root,
    )
    assert plan["model_independence_state"] == "requested-distinct-upstream-unproven"
    decision = {
        "verdict": "pass",
        "checks": {"artifact": "pass", "process": "pass", "safety": "pass"},
    }
    assert subject._status(plan, context, decision) == "unverified"


def test_reviewed_alias_cannot_hide_same_upstream_model(world, verified_source, tmp_path):
    x = verified_source
    source = x.source()
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "fixture",
                "http://127.0.0.1:11434/v1",
                "loopback",
                (("builder", "actual-builder"), ("verifier", "actual-builder")),
            ),
        )
    )
    with pytest.raises(PolicyViolation, match="same|ayni upstream"):
        subject.build_outcome_verification_plan(
            world.home,
            lifecycle=x.f.lifecycle,
            invocation_ref=source["canonical_job_id"],
            project_id=world.binding.project_id,
            allowed_scopes=x.f.scopes,
            verifier_model="fixture/verifier",
            executable=tmp_path / "opencode.exe",
            root=x.f.root,
            provider_profile=profile,
        )
    assert len(x.calls) == 1


def test_ambiguous_reason_code_cannot_turn_pass_into_verified_outcome():
    plan = {"model_independence_state": "reviewed-upstream-config-distinct"}
    context = {"local_grade": {"exact_match": True}}
    decision = {
        "verdict": "pass",
        "checks": {"artifact": "pass", "process": "pass", "safety": "pass"},
        "reason_codes": ["consistent", "insufficient_evidence"],
    }
    assert subject._status(plan, context, decision) == "unverified"
