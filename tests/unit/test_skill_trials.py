"""Paired native fixtures. No actual executable/model calls are made."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401 - shared canonical DB fixture
from tests.unit.test_opencode_batch import _events, _output, _stream
from zekam.application import skill_trials as subject
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.clients import opencode_batch as native
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _inputs(tmp_path: Path, project_id: str):
    root = tmp_path / "source"
    root.mkdir()
    package = SkillPackage.parse(
        "paired-skill",
        {
            "SKILL.md": b"---\nname: paired-skill\ndescription: Return a structured answer.\n---\nReturn the integer 7.\n"
        },
    )
    case = {
        "schema": "zekam-skill-trial-case/v1",
        "case_id": "case-one",
        "partition": "holdout",
        "input": {"question": "Return the requested integer."},
        "expected_output": 7,
    }
    objects = LocalContentAddressedStore(tmp_path / "cas")
    plan = subject.build_skill_trial_plan(
        revision_digest=digest("revision"),
        package=package,
        case=case,
        project_id=project_id,
        model_id="provider/model",
        executable=tmp_path / "opencode.exe",
        objects=objects,
        source_root=root,
    )
    return root, package, case, plan


def test_trial_plan_and_case_pin_oracle_model_and_instruction_bytes(tmp_path):
    root, package, case, plan = _inputs(tmp_path, str(uuid4()))
    assert plan["max_agent_calls"] == 2
    assert plan["activatable"] is False
    assert plan["oracle_disclosed_to_builder"] is False
    assert "expected_output" not in plan
    changed = subject.build_skill_trial_plan(
        revision_digest=digest("revision"),
        package=package,
        case=case | {"expected_output": 8},
        project_id=plan["project_id"],
        model_id="provider/model",
        executable=tmp_path / "opencode.exe",
        objects=LocalContentAddressedStore(tmp_path / "cas"),
        source_root=root,
    )
    assert changed["plan_digest"] != plan["plan_digest"]
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    assert subject.load_trial_case(path) == case
    path.write_text(json.dumps(case | {"unexpected": True}), encoding="utf-8")
    with pytest.raises(ValidationFailed):
        subject.load_trial_case(path)
    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
    with pytest.raises(PolicyViolation, match="duplicate"):
        subject.load_trial_case(path)


def _binding(world, plan):
    with world.store.unit_of_work() as uow:
        run = uow.create_run(
            work_item_id=world.binding.work_item_id,
            config_revision_id=world.binding.config_revision_id,
            plan_digest=plan["plan_digest"],
            budget={"seconds": 400},
            source_snapshot_id=world.binding.source_snapshot_id,
        )
        uow.commit()
    return replace(world.binding, run_id=run.id, plan_digest=plan["plan_digest"])


def test_real_claim_native_pair_oracle_isolation_and_completed_replay(world, tmp_path, monkeypatch):
    root, package, case, plan = _inputs(tmp_path, world.binding.project_id)
    binding = _binding(world, plan)
    monkeypatch.setattr(native, "resolve_opencode_executable", lambda value: Path(value))
    calls = []

    def execute(spec, *, cwd, stdin=None, cancel=None):
        assert cancel is not None
        with world.runtime._connect() as db:
            assert (
                db.execute(
                    "select count(*) from local_effect_claim where operation='skill.trial'"
                ).fetchone()[0]
                == 1
            )
        if spec.argv[-1] == "--version":
            return _output(spec, native.SUPPORTED_VERSION.encode())
        prompt = json.loads(stdin)
        assert "expected_output" not in prompt["context"]
        assert "case_digest" not in prompt["context"]
        calls.append(prompt["context"])
        answer = 7 if prompt["context"]["skill_instructions"] else 0
        events = _events({"result": {}, "body": {"output": answer}})
        for event in events:
            event["sessionID"] = event["part"]["sessionID"] = f"ses_{len(calls)}"
        return _output(spec, _stream(events))

    monkeypatch.setattr(native.runner, "run", execute)
    arguments = dict(
        binding=binding,
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
    result = subject.run_skill_trial(world.home, **arguments)
    assert len(calls) == 2
    assert result["observations"]["baseline"]["exact_match"] is False
    assert result["observations"]["candidate"]["exact_match"] is True
    assert result["observations"]["candidate"]["elapsed_ns"] > 0
    assert result["provider_calls"] is None
    assert result["agent_calls"] == 2
    assert result["independent_verification"] is False
    assert result["activatable"] is False
    replay = subject.run_skill_trial(world.home, **arguments)
    assert len(calls) == 2
    assert replay["canonical_replayed"] is True
    assert replay["observations"] == result["observations"]
    snapshot = world.runtime.job_snapshot(result["canonical_job_id"])
    assert snapshot["state"] == "completed"
    assert snapshot["effects"][0]["receipt_status"] == "completed"


def test_trial_plan_drift_and_remote_authorization_precede_effect(world, tmp_path):
    root, package, case, plan = _inputs(tmp_path, world.binding.project_id)
    binding = _binding(world, plan)
    args = dict(
        binding=binding,
        package=package,
        case=case,
        plan=plan,
        root=root,
        executable=tmp_path / "opencode.exe",
        realm_id=uuid4(),
        coordinator_id=uuid4(),
        validate_inputs=lambda: None,
    )
    with pytest.raises(PolicyViolation, match="authorization"):
        subject.run_skill_trial(world.home, **args)
    with pytest.raises(PolicyViolation, match="drift"):
        subject.run_skill_trial(
            world.home, **(args | {"case": case | {"expected_output": 9}}), authorize_remote=True
        )
    with world.runtime._connect() as db:
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0


def test_reused_native_session_requires_recovery(world, tmp_path, monkeypatch):
    root, package, case, plan = _inputs(tmp_path, world.binding.project_id)
    binding = _binding(world, plan)

    def invoke(**kwargs):
        return {
            "condition": kwargs["condition"],
            "native_identity": "opencode:same:message",
            "valid_output": True,
            "output_digest": digest(7),
        }

    monkeypatch.setattr(subject, "_invoke_trial", invoke)
    with pytest.raises(PolicyViolation, match="reused"):
        subject.run_skill_trial(
            world.home,
            binding=binding,
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
    assert world.runtime.status().recovery_jobs == 1


def test_native_batch_forwards_cancel_to_version_and_dispatch(tmp_path, monkeypatch):
    from tests.unit.test_opencode_batch import _setup

    adapter, request, permit = _setup(tmp_path)
    cancel = threading.Event()
    adapter = replace(adapter, cancel=cancel)
    seen = []
    monkeypatch.setattr(native, "resolve_opencode_executable", lambda value: Path(value))

    def execute(spec, *, cwd, stdin=None, cancel=None):
        seen.append(cancel)
        return _output(
            spec,
            native.SUPPORTED_VERSION.encode()
            if spec.argv[-1] == "--version"
            else _stream(_events()),
        )

    monkeypatch.setattr(native.runner, "run", execute)
    assert adapter.dispatch(request, cwd=tmp_path, permit=permit).is_success
    assert seen == [cancel, cancel]
