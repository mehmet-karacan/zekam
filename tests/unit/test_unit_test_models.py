"""Selection/plan/composition regressions. No model or Maven execution."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.application.unit_test_agents import VERIFYING, AgentSpecialty
from zekam.application.unit_test_models import batch_model_plan, validate_batch_models
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.application.unit_test_runtime import (
    UnitTestRuntimeBinding,
    compose_agent_gateway,
    unit_test_job_input_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.main import app


@pytest.mark.parametrize(
    "builder,verifier",
    [
        (None, "provider/verifier"),
        ("provider/builder", None),
        ("provider/model", "provider/model"),
        ("provider/model#fast", "provider/model#high"),
        ("provider/model", "other-provider/MODEL"),
    ],
)
def test_missing_or_same_model_cannot_be_independent(builder, verifier):
    with pytest.raises(PolicyViolation):
        validate_batch_models(builder, verifier)


def test_model_selection_never_asserts_independent_execution_without_evidence():
    plan = batch_model_plan("provider/builder", "provider/verifier", 12)
    assert plan["binding_status"] == "ready-for-runtime-check"
    assert plan["independent_verification_proven"] is False
    assert (
        batch_model_plan("provider/builder", "provider/verifier", 0)["binding_status"]
        == "incomplete"
    )


def test_invalid_secret_model_is_not_echoed():
    model_ref = "provider/" + "password" + '="actual-private-value"'
    plan = batch_model_plan(model_ref, "provider/verifier", 10)
    assert plan["models"]["builder"] is None
    assert model_ref not in json.dumps(plan)


def test_all_verifying_specialties_use_distinct_model_and_job_identity_binds_it(
    tmp_path, monkeypatch
):
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"fixture only")
    project_id = uuid4()
    binding = UnitTestRuntimeBinding(
        realm_id=uuid4(),
        project_id=project_id,
        work_item_id=uuid4(),
        coordinator_assignment_id=uuid4(),
        project_root=tmp_path,
        object_store_root=tmp_path / "cas",
        lock_dir=tmp_path / "locks",
        opencode_executable=executable,
        approved_maven_plan_digest=digest("maven"),
        model_id="provider/builder",
        verifier_model_id="provider/verifier",
        remote_model=True,
        allow_network=True,
    )
    request = UnitTestRequest.with_defaults(
        project_id=str(project_id),
        source_binding_id="binding",
        source_revision="revision",
        source_files=["src/main/java/Example.java"],
        percent="80",
        budget=UnitTestBudget(3, 60, 180),
    )
    gateway = compose_agent_gateway(
        binding,
        request=request,
        index_objects=LocalContentAddressedStore(binding.object_store_root),
        artifact_registrar=lambda *args, **kwargs: None,
    )
    for specialty in AgentSpecialty:
        expected = "provider/verifier" if specialty in VERIFYING else "provider/builder"
        assert gateway.adapters[specialty].model_id == expected
    assert gateway.remote
    approvals, limits = UnitTestApprovals(), LoopLimits(max_remote_calls=10)
    first = unit_test_job_input_digest(request, binding, approvals, limits)
    assert first != unit_test_job_input_digest(
        request, replace(binding, verifier_model_id="provider/second-verifier"), approvals, limits
    )
    assert first != unit_test_job_input_digest(
        request, binding, approvals, replace(limits, max_remote_calls=11)
    )
    with pytest.raises(PolicyViolation):
        compose_agent_gateway(
            replace(binding, verifier_model_id=binding.model_id),
            request=request,
            index_objects=gateway.objects,
            artifact_registrar=lambda *a, **kw: None,
        )
    from zekam.application.unit_test_agents import AgentDispatchFailure, AgentTask
    from zekam.domain.clients import DispatchOutcome, DispatchResult
    from zekam.infrastructure.clients.opencode_batch import OpenCodeBatchAdapter

    calls = []

    def fail_dispatch(adapter, request, *, permit, cwd):
        permit.assert_valid(request)
        calls.append(request)
        return DispatchResult(
            request.assignment_id,
            request.invocation_id,
            request.client_id,
            request.role,
            DispatchOutcome.FAILED,
            1,
            {},
            "fixture",
        )

    monkeypatch.setattr(OpenCodeBatchAdapter, "dispatch", fail_dispatch)
    other = compose_agent_gateway(
        replace(binding, verifier_model_id="provider/other"),
        request=request,
        index_objects=gateway.objects,
        artifact_registrar=lambda *a, **kw: None,
    )
    task = AgentTask(AgentSpecialty.VERIFIER, request.request_digest, 1, 0, {})
    for current in (gateway, other):
        with pytest.raises(AgentDispatchFailure):
            current.invoke(task)
    assert calls[0].assignment_id != calls[1].assignment_id
    assert calls[0].invocation_id != calls[1].invocation_id
    assert calls[0].context_manifest_digest != calls[1].context_manifest_digest


def test_cli_plan_binds_verifier_and_common_call_budget_without_calls():
    runner = CliRunner()
    args = [
        "test",
        "plan",
        "--project-id",
        "project",
        "--source-binding-id",
        "binding",
        "--source-revision",
        "revision",
        "--source",
        "src/main/java/Example.java",
        "--percent",
        "80",
        "--model",
        "provider/builder",
    ]
    plans = []
    for verifier, budget in [
        ("provider/verifier", "10"),
        ("provider/other", "10"),
        ("provider/verifier", "11"),
    ]:
        result = runner.invoke(
            app, [*args, "--verifier-model", verifier, "--max-remote-calls", budget]
        )
        assert result.exit_code == 0, result.stdout
        document = json.loads(result.stdout)
        assert document["provider_calls"] == 0 and document["authorization"] == "not-granted"
        assert document["agent_execution"]["models"]["verifier"] == verifier
        plans.append(document["plan_digest"])
    assert len(set(plans)) == 3
    native = runner.invoke(app, [*args, "--native", "--verifier-model", "provider/verifier"])
    assert native.exit_code == 0, native.stdout
    assert "agent_execution" not in json.loads(native.stdout)
