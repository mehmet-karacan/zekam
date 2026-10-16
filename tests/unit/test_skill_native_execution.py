"""Canonical native execution with offline transport/active-policy port doubles."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_native_invocation import native_load  # noqa: F401
from tests.unit.test_skill_trials import _binding
from tests.unit.test_opencode_batch import _output, _stream
from tests.unit.test_opencode_skill_protocol import _events
from zekam.application import skill_native_execution as subject
from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients import opencode_batch as batch
from zekam.infrastructure.clients.opencode_skill_protocol import native_skill_render
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def execution(world, native_load, tmp_path, monkeypatch):
    f = native_load
    runtime = tmp_path.parent / f"{tmp_path.name}-native-runtime"
    runtime.mkdir()
    monkeypatch.setattr(batch, "_native_runtime_root", lambda root: root.resolve())
    monkeypatch.setattr(batch, "resolve_opencode_executable", lambda path: path)
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: f.root)
    case = {
        "schema": "zekam-skill-trial-case/v1",
        "case_id": "native-case",
        "partition": "holdout",
        "input": {"question": "Return seven."},
        "expected_output": 7,
    }
    plan = subject.build_native_execution_plan(
        world.home,
        lifecycle=f.lifecycle,
        package=f.package,
        case=case,
        project_id=world.binding.project_id,
        model_id="provider/native-builder",
        executable=tmp_path / "opencode.exe",
        root=f.root,
        runtime_root=runtime,
        allowed_scopes=f.scopes,
    )
    binding = _binding(world, plan)
    calls = []
    answer = [7]
    process_exit = [0]

    def transport(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, batch.SUPPORTED_VERSION.encode())
        assert cancel is not None
        snapshot = world.runtime.job_snapshot(
            canonical_job_key(
                binding.binding_digest,
                subject.OPERATION,
                digest(
                    {
                        "plan": plan["plan_digest"],
                        "realm_id": str(realm),
                        "coordinator_id": str(coordinator),
                    }
                ),
            )
        )
        assert snapshot["state"] == "running" and snapshot["effects"]
        context = json.loads(stdin)["context"]
        assert set(context) == {"schema", "input", "native_skill_id"}
        assert "expected_output" not in context and "skill_instructions" not in context
        calls.append(context)
        directory = (
            Path(json.loads(dict(spec.env)["OPENCODE_CONFIG_CONTENT"])["skills"][0])
            / f.package.name
        )
        events = _events(directory)
        events[3]["part"]["state"]["output"] = native_skill_render(f.package, directory)
        events[-2]["part"]["text"] = json.dumps({"result": {}, "body": {"output": answer[0]}})
        return _output(spec, _stream(events), exit_code=process_exit[0])

    monkeypatch.setattr(batch.runner, "run", transport)
    realm, coordinator = uuid4(), uuid4()

    def run():
        return subject.run_native_execution(
            world.home,
            lifecycle=f.lifecycle,
            binding=binding,
            package=f.package,
            case=case,
            plan=plan,
            root=f.root,
            runtime_root=runtime,
            executable=tmp_path / "opencode.exe",
            allowed_scopes=f.scopes,
            realm_id=realm,
            coordinator_id=coordinator,
            validate_inputs=lambda: None,
            authorize_remote=True,
        )

    return SimpleNamespace(
        f=f,
        run=run,
        plan=plan,
        binding=binding,
        calls=calls,
        case=case,
        answer=answer,
        process_exit=process_exit,
        realm=realm,
        coordinator=coordinator,
    )


def test_native_execution_has_real_claim_and_receipt_and_regrades_terminal_sources(
    world, execution
):
    x = execution
    result = x.run()
    replay = x.run()
    assert result["canonical_job_id"] == replay["canonical_job_id"]
    assert replay["canonical_replayed"] is True and len(x.calls) == 1
    assert result["state"] == "measured-exact-match"
    assert result["independent_verification"] is False
    assert result["activatable"] is False and result["skill_effect_verified"] is False
    report = subject.read_native_execution(
        world.home,
        result["canonical_job_id"],
        lifecycle=x.f.lifecycle,
        project_id=x.binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    assert report["observation"]["exact_match"] is True
    assert report["receipt_evidence_digest"]
    assert len(x.calls) == 1


@pytest.mark.parametrize(
    "mode,state", [("mismatch", "measured-mismatch"), ("failed-process", "failed")]
)
def test_failed_and_mismatched_executions_are_retained_without_quality_success(
    world, execution, mode, state
):
    x = execution
    if mode == "mismatch":
        x.answer[0] = 8
    else:
        x.process_exit[0] = 4
    result = x.run()
    assert result["state"] == state
    assert result["observation"]["exact_match"] is False
    assert result["skill_effect_verified"] is False
    assert (
        subject.read_native_execution(
            world.home,
            result["canonical_job_id"],
            lifecycle=x.f.lifecycle,
            project_id=x.binding.project_id,
            allowed_scopes=x.f.scopes,
        )["state"]
        == state
    )


def test_report_rejects_foreign_project_and_missing_dispatch_artifact(world, execution):
    from zekam.domain.errors import NotFound

    x = execution
    result = x.run()
    with pytest.raises(PolicyViolation, match="foreign project"):
        subject.read_native_execution(
            world.home,
            result["canonical_job_id"],
            lifecycle=x.f.lifecycle,
            project_id=str(uuid4()),
            allowed_scopes=x.f.scopes,
        )
    objects = LocalContentAddressedStore(
        world.home / subject.load_settings(home=world.home).object_store_relative
    )
    objects._paths(result["observation"]["artifact_digest"])[0].unlink()
    with pytest.raises(NotFound):
        subject.read_native_execution(
            world.home,
            result["canonical_job_id"],
            lifecycle=x.f.lifecycle,
            project_id=x.binding.project_id,
            allowed_scopes=x.f.scopes,
        )
    assert len(x.calls) == 1


def test_regrading_rejects_changed_oracle_and_native_subject(world, execution):
    x = execution
    result = x.run()
    objects = LocalContentAddressedStore(
        world.home / subject.load_settings(home=world.home).object_store_relative
    )
    input_digest = digest(
        {
            "plan": x.plan["plan_digest"],
            "realm_id": str(x.realm),
            "coordinator_id": str(x.coordinator),
        }
    )
    with pytest.raises(PolicyViolation, match="source/plan/authority drift"):
        subject._validate_result(
            result | {"case_snapshot": x.case | {"expected_output": 999}},
            x.binding,
            objects,
            input_digest,
        )
    with pytest.raises(PolicyViolation, match="source/plan/authority drift"):
        subject._validate_result(
            result | {"skill_effect_verified": True}, x.binding, objects, input_digest
        )
