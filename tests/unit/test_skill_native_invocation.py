"""Offline native-load fixtures; active-effect port double is not host acceptance."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.integration.test_skill_lifecycle_v2 import _origins, NOW
from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import native_skill_binding as binding_module
from zekam.application import project_rag_runtime
from zekam.application import skill_native_invocation as subject
from zekam.application.opencode_lifecycle import record_event
from zekam.application.skill_packages import (
    apply_projection_plan,
    build_projection_plan,
    projection_receipt,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.domain.personal_skill import PersonalSkillRevision, SkillScopeKind
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle
from zekam.interfaces.cli.skill import _run_claimed_effect


@pytest.fixture
def native_load(world, tmp_path, monkeypatch):
    learning = world.home / "state" / "learning.db"
    SQLiteLocalLearning(learning, operational_path=world.database).bootstrap()
    lifecycle = SQLiteSkillLifecycle(learning, world.database)
    root = tmp_path / "source"
    root.mkdir()
    for module in (project_rag_runtime, binding_module):
        monkeypatch.setattr(module, "resolve_project_source", lambda *_args: root)
    policy = ClientIntegrationPolicy(opencode=True)
    settings = subject.load_settings(home=world.home)
    monkeypatch.setattr(
        subject,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(
            cli=SimpleNamespace(integrations=policy),
            object_store_relative=settings.object_store_relative,
        ),
    )
    package = SkillPackage.parse(
        "native-example",
        {
            "SKILL.md": b"---\nname: native-example\ndescription: Offline native load.\n---\nFixture instructions.\n"
        },
    )
    revision = lifecycle.propose_revision(
        PersonalSkillRevision(
            skill_id=package.name,
            name=package.name,
            description=package.description,
            version=1,
            scope_kind=SkillScopeKind.PROJECT,
            scope_ref="demo",
            package_digest=package.package_digest,
            author_ref="fixture-builder",
            trigger_terms=("native",),
        ),
        _origins(world.database),
        now=NOW,
    )
    # Only activation acceptance is doubled. Operational recording claim, CAS,
    # terminal receipt, native event chain and invocation transaction are real.
    activation = digest("fixture-active-pointer")
    with closing(lifecycle._connect()) as db:
        db.execute(
            "insert into skill_activation_event_v2 values(?,?,?,?,?,?,?,?,?)",
            (
                activation,
                revision,
                package.name,
                "project",
                "demo",
                "active",
                None,
                NOW.isoformat(),
                canonical_json({"fixture": "active-pointer"}),
            ),
        )
        db.commit()
    monkeypatch.setattr(lifecycle, "_activation_event_effective", lambda _row: True)
    projection = build_projection_plan(root, package, policy=policy)
    active = lifecycle.require_active_package(
        package.package_digest, allowed_scopes=((SkillScopeKind.PROJECT, "demo"),)
    )
    exported = _run_claimed_effect(
        lifecycle,
        world.home,
        operation="skill.export-v1",
        effect={
            "schema": "zekam-skill-export-effect/v1",
            "plan": projection.as_dict(),
            "active_revision": active,
        },
        idempotency_key="fixture-native-invocation-export",
        apply_effect=lambda: apply_projection_plan(
            projection, authorized_plan_digest=projection.plan_digest
        ),
        replay_effect=lambda: projection_receipt(
            projection, authorized_plan_digest=projection.plan_digest
        ),
    )
    fields = {
        "j": exported["operational_job_id"],
        "r": revision[7:],
        "p": package.package_digest[7:],
        "b": digest_of_bytes(package.instructions.strip().encode())[7:],
        "c": digest_of_bytes(b"native-call-one")[7:],
        "m": digest_of_bytes(b"native-message-one")[7:],
    }

    def emit(**changes):
        values = dict(
            event_type="tool.execute.after",
            session_id="fixture-native-session",
            tool="skill",
            status="completed",
            resource=".agents/skills/native-example/SKILL.md",
            task_label="native-skill-load/v1:" + json.dumps(fields, separators=(",", ":")),
            now=dt.datetime(2026, 10, 16, 10, 0, 0, 123456, tzinfo=dt.UTC),
        )
        return record_event(world.home, **(values | changes))

    event = emit()
    scopes = ((SkillScopeKind.PROJECT, "demo"),)
    plan = subject.build_native_invocation_plan(
        world.home,
        lifecycle=lifecycle,
        event_id=event.event_id,
        project_root=root,
        allowed_scopes=scopes,
    )
    return SimpleNamespace(
        lifecycle=lifecycle,
        root=root,
        scopes=scopes,
        event=event,
        emit=emit,
        plan=plan,
        package=package,
    )


def test_native_observation_records_atomic_unverified_chain_and_exact_read_only_replay(
    world, native_load
):
    fixture = native_load
    binding = _binding(world, fixture.plan)
    args = dict(
        lifecycle=fixture.lifecycle,
        plan=fixture.plan,
        binding=binding,
        project_root=fixture.root,
        allowed_scopes=fixture.scopes,
    )
    result = subject.run_native_invocation(world.home, **args)
    replay = subject.run_native_invocation(world.home, **args)
    assert replay["canonical_replayed"] is True
    assert result["invocation_digests"] == replay["invocation_digests"]
    assert result["state"] == "unverified"
    assert result["skill_effect_verified"] is False
    assert result["host_attested"] is False
    assert result["provider_calls"] == 0
    assert "fixture-native-session" not in json.dumps(result)
    report = subject.read_native_invocation(
        world.home,
        result["canonical_job_id"],
        lifecycle=fixture.lifecycle,
        project_id=binding.project_id,
        allowed_scopes=fixture.scopes,
    )
    assert report["invocation_digests"] == result["invocation_digests"]
    with closing(fixture.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 6
        assert db.execute("select count(*) from skill_outcome_v2").fetchone()[0] == 0


def test_duplicate_delivery_cannot_reassociate_native_call_with_another_run(world, native_load):
    f = native_load
    subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=f.plan,
        binding=_binding(world, f.plan),
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    duplicate = f.emit()
    plan = subject.build_native_invocation_plan(
        world.home,
        lifecycle=f.lifecycle,
        event_id=duplicate.event_id,
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    with pytest.raises(ConcurrencyConflict, match="already recorded"):
        subject.run_native_invocation(
            world.home,
            lifecycle=f.lifecycle,
            plan=plan,
            binding=_binding(world, plan),
            project_root=f.root,
            allowed_scopes=f.scopes,
        )
    with closing(f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 6


def test_partial_learning_write_rolls_back_as_one_chain(world, native_load, monkeypatch):
    f = native_load
    original = f.lifecycle._record_invocation_state_tx
    count = 0

    def fail(db, *args, **kwargs):
        nonlocal count
        value = original(db, *args, **kwargs)
        count += 1
        if count == 3:
            raise RuntimeError("fixture interruption")
        return value

    monkeypatch.setattr(f.lifecycle, "_record_invocation_state_tx", fail)
    with pytest.raises(RuntimeError, match="fixture interruption"):
        subject.run_native_invocation(
            world.home,
            lifecycle=f.lifecycle,
            plan=f.plan,
            binding=_binding(world, f.plan),
            project_root=f.root,
            allowed_scopes=f.scopes,
        )
    with closing(f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 0
    assert world.runtime.status().recovery_jobs == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed"},
        {"tool": "read"},
        {"task_label": "model says skill used"},
        {"resource": "foreign/path.md"},
        {"error_category": "native-tool-error"},
    ],
)
def test_unbound_failed_and_model_reported_events_are_rejected(world, native_load, changes):
    f = native_load
    event = f.emit(**changes)
    with pytest.raises((PolicyViolation, ValueError)):
        subject.build_native_invocation_plan(
            world.home,
            lifecycle=f.lifecycle,
            event_id=event.event_id,
            project_root=f.root,
            allowed_scopes=f.scopes,
        )


def test_direct_record_without_live_claim_is_rejected(world, native_load):
    f = native_load
    with pytest.raises(PolicyViolation, match="own canonical recording claim"):
        f.lifecycle.record_native_invocation(
            home=world.home, plan=f.plan, binding=_binding(world, f.plan), allowed_scopes=f.scopes
        )


def test_distinct_calls_in_same_native_session_do_not_collide(world, native_load):
    f = native_load
    first = subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=f.plan,
        binding=_binding(world, f.plan),
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    fields = json.loads(f.event.task_label.removeprefix("native-skill-load/v1:"))
    fields["c"] = digest_of_bytes(b"native-call-two")[7:]
    other = f.emit(task_label="native-skill-load/v1:" + json.dumps(fields, separators=(",", ":")))
    plan = subject.build_native_invocation_plan(
        world.home,
        lifecycle=f.lifecycle,
        event_id=other.event_id,
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    second = subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    assert first["plan"]["agent_ref"] != second["plan"]["agent_ref"]
    with closing(f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 12


def test_report_retains_historical_observation_after_deactivation_without_rewriting_it(
    world, native_load, monkeypatch
):
    f = native_load
    binding = _binding(world, f.plan)
    result = subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=f.plan,
        binding=binding,
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    monkeypatch.setattr(f.lifecycle, "_activation_event_effective", lambda _row: False)
    report = subject.read_native_invocation(
        world.home,
        result["canonical_job_id"],
        lifecycle=f.lifecycle,
        project_id=binding.project_id,
        allowed_scopes=f.scopes,
    )
    assert report["state"] == "unverified"
    with pytest.raises(PolicyViolation, match="active authorized package"):
        subject.build_native_invocation_plan(
            world.home,
            lifecycle=f.lifecycle,
            event_id=f.event.event_id,
            project_root=f.root,
            allowed_scopes=f.scopes,
        )


def test_exact_native_step_model_is_pinned_without_claiming_independence(world, native_load):
    f = native_load
    model = "provider/builder#extra-high"
    fields = json.loads(f.event.task_label.removeprefix("native-skill-load/v1:"))
    fields["u"] = digest_of_bytes(model.encode())[7:]
    event = f.emit(
        model_ref=model,
        task_label="native-skill-load/v2:" + json.dumps(fields, separators=(",", ":")),
    )
    plan = subject.build_native_invocation_plan(
        world.home,
        lifecycle=f.lifecycle,
        event_id=event.event_id,
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    assert plan["invoking_model_ref"] == model
    assert plan["invoking_model_provenance"] == "native-step-message"
    assert plan["host_attested"] is False
    result = subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    assert result["state"] == "unverified"
    assert result["skill_effect_verified"] is False


@pytest.mark.parametrize("model", [None, "provider/different#extra-high", "provider/builder#"])
def test_native_model_v2_label_rejects_missing_changed_and_invalid_model(world, native_load, model):
    f = native_load
    fields = json.loads(f.event.task_label.removeprefix("native-skill-load/v1:"))
    fields["u"] = digest_of_bytes(b"provider/builder#extra-high")[7:]
    event = f.emit(
        model_ref=model,
        task_label="native-skill-load/v2:" + json.dumps(fields, separators=(",", ":")),
    )
    with pytest.raises(PolicyViolation):
        subject.build_native_invocation_plan(
            world.home,
            lifecycle=f.lifecycle,
            event_id=event.event_id,
            project_root=f.root,
            allowed_scopes=f.scopes,
        )


def test_legacy_label_does_not_promote_unbound_model_metadata(world, native_load):
    f = native_load
    event = f.emit(model_ref="provider/session-default#extra-high")
    plan = subject.build_native_invocation_plan(
        world.home,
        lifecycle=f.lifecycle,
        event_id=event.event_id,
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    assert plan["invoking_model_ref"] is None
    assert plan["invoking_model_provenance"] == "unavailable"


def test_foreign_project_cannot_read_recording_receipt(world, native_load):
    from uuid import uuid4

    f = native_load
    result = subject.run_native_invocation(
        world.home,
        lifecycle=f.lifecycle,
        plan=f.plan,
        binding=_binding(world, f.plan),
        project_root=f.root,
        allowed_scopes=f.scopes,
    )
    with pytest.raises(PolicyViolation, match="project scope mismatch"):
        subject.read_native_invocation(
            world.home,
            result["canonical_job_id"],
            lifecycle=f.lifecycle,
            project_id=str(uuid4()),
            allowed_scopes=f.scopes,
        )
