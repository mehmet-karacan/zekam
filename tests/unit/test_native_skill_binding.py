"""Managed binding fixtures; active-pointer port doubles are not acceptance evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.integration.test_skill_lifecycle_v2 import _learning, _operational
from zekam.application import native_skill_binding as subject
from zekam.application.skill_packages import (
    apply_projection_plan,
    build_projection_plan,
    projection_receipt,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillScopeKind
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle
from zekam.interfaces.cli.skill import _run_claimed_effect


@pytest.fixture
def bound(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    operational = _operational(state / "operational.db")
    lifecycle = SQLiteSkillLifecycle(_learning(state, operational), operational)
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: root)
    policy = ClientIntegrationPolicy(opencode=True, codex=True)
    package = SkillPackage.parse(
        "native-example",
        {
            "SKILL.md": b"---\nname: native-example\ndescription: Fixture managed skill.\nallowed-tools: Bash\n---\nFixture skill body.\n",
            "references/policy.md": b"Fixture reference.",
        },
    )
    activation = {
        "revision_digest": digest("active-fixture-revision"),
        "skill_id": package.name,
        "scope_kind": "project",
        "scope_ref": "project-a",
        "activation_event_digest": digest("active-fixture-event"),
    }
    monkeypatch.setattr(
        lifecycle, "require_active_package", lambda *_args, **_kwargs: dict(activation)
    )
    plan = build_projection_plan(root, package, policy=policy)
    exported = _run_claimed_effect(
        lifecycle,
        home,
        operation="skill.export-v1",
        effect={
            "schema": "zekam-skill-export-effect/v1",
            "plan": plan.as_dict(),
            "active_revision": activation,
        },
        idempotency_key=f"skill-export-v1-runtime-v2:{plan.plan_digest}",
        apply_effect=lambda: apply_projection_plan(plan, authorized_plan_digest=plan.plan_digest),
        replay_effect=lambda: projection_receipt(plan, authorized_plan_digest=plan.plan_digest),
    )
    observation = {
        "schema": "zekam-native-skill-load-query/v1",
        "skill_id": package.name,
        "relative_path": f".agents/skills/{package.name}/SKILL.md",
        "instruction_digest": digest_of_bytes(package.instructions.strip().encode()),
    }
    args = dict(
        home=home,
        project_root=root,
        policy=policy,
        allowed_scopes=((SkillScopeKind.PROJECT, "project-a"),),
        observation=observation,
    )
    return lifecycle, args, activation, exported


def test_exact_managed_body_active_pointer_and_actual_export_receipt_bind_without_writes(bound):
    lifecycle, args, activation, exported = bound
    result = subject.resolve_native_skill_binding(lifecycle, **args)
    assert result["revision_digest"] == activation["revision_digest"]
    assert result["export_job_id"] == exported["operational_job_id"]
    assert result["export_evidence_digest"] == exported["operational_terminal_evidence_digest"]
    assert result["invocation_observed"] is False
    assert result["quality_verified"] is False
    assert result["grants_authority"] is False
    assert str(args["project_root"]) not in json.dumps(result)
    assert subject.resolve_native_skill_binding(lifecycle, **args) == result
    with lifecycle._connect(read_only=True) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation", ["body", "artifact", "scope-root", "disabled", "path", "authority"]
)
def test_drift_disabled_and_foreign_queries_do_not_bind(bound, tmp_path, mutation):
    lifecycle, args, _activation, _exported = bound
    args = dict(args)
    args["observation"] = dict(args["observation"])
    target = args["project_root"] / ".agents/skills/native-example"
    if mutation == "body":
        args["observation"]["instruction_digest"] = digest("different SDK content")
    elif mutation == "artifact":
        (target / "references/policy.md").write_text("Changed reference.", encoding="utf-8")
    elif mutation == "scope-root":
        args["project_root"] = tmp_path
    elif mutation == "disabled":
        args["policy"] = ClientIntegrationPolicy(codex=True)
    elif mutation == "path":
        args["observation"]["relative_path"] = ".agents/skills/native-example/../SKILL.md"
    else:
        path = target / ".zekam-managed.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(meta | {"grants_authority": True}), encoding="utf-8")
    with pytest.raises(PolicyViolation):
        subject.resolve_native_skill_binding(lifecycle, **args)


def test_copied_owned_files_without_this_root_export_receipt_are_not_bound(
    bound, tmp_path, monkeypatch
):
    lifecycle, args, _activation, _exported = bound
    root = tmp_path / "other-source"
    root.mkdir()
    shutil.copytree(args["project_root"] / ".agents", root / ".agents")
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: root)
    with pytest.raises(PolicyViolation, match="terminal export"):
        subject.resolve_native_skill_binding(lifecycle, **(args | {"project_root": root}))


def test_terminal_receipt_gate_is_required(bound, monkeypatch):
    lifecycle, args, _activation, _exported = bound

    def missing_receipt(*_args, **_kwargs):
        raise PolicyViolation("Missing terminal receipt")

    monkeypatch.setattr(lifecycle, "_completed_terminal_evidence", missing_receipt)
    with pytest.raises(PolicyViolation, match="Missing terminal receipt"):
        subject.resolve_native_skill_binding(lifecycle, **args)


def test_active_pointer_drift_during_binding_is_rejected(bound, monkeypatch):
    lifecycle, args, activation, _exported = bound
    calls = 0

    def current(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return (
            dict(activation)
            if calls == 1
            else activation | {"activation_event_digest": digest("new-head")}
        )

    monkeypatch.setattr(lifecycle, "require_active_package", current)
    with pytest.raises(PolicyViolation, match="changed during"):
        subject.resolve_native_skill_binding(lifecycle, **args)
