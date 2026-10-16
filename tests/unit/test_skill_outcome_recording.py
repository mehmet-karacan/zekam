"""Native outcome recording over real SQLite/CAS with offline verifier transport."""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_native_invocation import native_load  # noqa: F401
from tests.unit.test_skill_outcome_verification import verified_source  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import skill_outcome_recording as subject
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation


def _verify(world, x):
    plan, context = x.prepare(x.source())
    report = x.run(plan, context, _binding(world, plan), uuid4(), uuid4())
    return report, plan, context


def _plan(world, x, report):
    return subject.build_outcome_record_plan(
        world.home,
        lifecycle=x.f.lifecycle,
        verifier_ref=report["canonical_job_id"],
        project_id=world.binding.project_id,
        allowed_scopes=x.f.scopes,
    )


@pytest.mark.parametrize("answer,status", [(7, "verified-success"), (8, "verified-failure")])
def test_actual_verifier_receipt_is_recorded_once_with_bounded_scope(
    world, verified_source, answer, status
):
    x = verified_source
    x.mode["answer"] = answer
    verified, _vp, _context = _verify(world, x)
    plan = _plan(world, x, verified)
    binding = _binding(world, plan)
    args = dict(lifecycle=x.f.lifecycle, plan=plan, binding=binding, allowed_scopes=x.f.scopes)
    result = subject.run_outcome_recording(world.home, **args)
    replay = subject.run_outcome_recording(world.home, **args)
    assert result["status"] == status and replay["canonical_replayed"] is True
    assert result["verification_scope"] == "one-native-task-artifact"
    assert result["quality_evaluation_complete"] is False and result["activatable"] is False
    assert result["provider_calls"] == 0 and len(x.calls) == 2
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        row = db.execute(
            "select outcome_digest,status,evidence_digest from skill_outcome_v2"
        ).fetchall()
    assert len(row) == 1 and row[0][0] == result["outcome_digest"]
    assert row[0][1] == status
    assert row[0][2] == plan["verifier_receipt_digest"]
    report = subject.read_native_outcome(
        world.home,
        result["canonical_job_id"],
        lifecycle=x.f.lifecycle,
        project_id=world.binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    assert report["outcome_digest"] == result["outcome_digest"] and len(x.calls) == 2


def test_unverified_verifier_journal_does_not_poison_unique_outcome_row(world, verified_source):
    x = verified_source
    vp, context = x.prepare(x.source())
    x.mode["decision"] = "invalid"
    verified = x.run(vp, context, _binding(world, vp), uuid4(), uuid4())
    plan = _plan(world, x, verified)
    assert plan["recordable"] is False and plan["outcome_body"] is None
    assert plan["blockers"] == ["verifier-terminal-status-unverified"]
    with pytest.raises(PolicyViolation, match="recordable"):
        subject.run_outcome_recording(
            world.home,
            lifecycle=x.f.lifecycle,
            plan=plan,
            binding=_binding(world, plan),
            allowed_scopes=x.f.scopes,
        )
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_outcome_v2").fetchone()[0] == 0
    assert len(x.calls) == 2


def test_native_operation_is_not_relabelled_as_legacy_verifier_receipt(world, verified_source):
    x = verified_source
    verified, _vp, _ctx = _verify(world, x)
    plan = _plan(world, x, verified)
    body = plan["outcome_body"]
    with pytest.raises(PolicyViolation, match="terminal receipt"):
        x.f.lifecycle.record_outcome(
            body["invocation_digest"],
            status=body["status"],
            grader_kind="artifact",
            verifier_ref=body["verifier_ref"],
            evidence_digest=body["evidence_digest"],
            now=dt.datetime.now(dt.UTC),
        )
    with pytest.raises(PolicyViolation, match="own canonical recording claim"):
        x.f.lifecycle.record_native_outcome(
            home=world.home, plan=plan, binding=_binding(world, plan), allowed_scopes=x.f.scopes
        )


def test_conflicting_second_verifier_cannot_replace_an_immutable_outcome(world, verified_source):
    x = verified_source
    verified, vp, context = _verify(world, x)
    plan = _plan(world, x, verified)
    first = subject.run_outcome_recording(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        allowed_scopes=x.f.scopes,
    )
    x.mode["session"] = "ses_another_verifier"
    second = x.run(vp, context, _binding(world, vp), uuid4(), uuid4())
    next_plan = _plan(world, x, second)
    with pytest.raises(ConcurrencyConflict, match="immutable decision"):
        subject.run_outcome_recording(
            world.home,
            lifecycle=x.f.lifecycle,
            plan=next_plan,
            binding=_binding(world, next_plan),
            allowed_scopes=x.f.scopes,
        )
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert (
            db.execute("select outcome_digest from skill_outcome_v2").fetchone()[0]
            == first["outcome_digest"]
        )


def test_missing_row_after_completed_receipt_is_not_recreated_on_replay(world, verified_source):
    x = verified_source
    verified, _vp, _context = _verify(world, x)
    plan = _plan(world, x, verified)
    binding = _binding(world, plan)
    args = dict(lifecycle=x.f.lifecycle, plan=plan, binding=binding, allowed_scopes=x.f.scopes)
    result = subject.run_outcome_recording(world.home, **args)
    with closing(x.f.lifecycle._connect()) as db:
        sql = db.execute(
            "select sql from sqlite_master where name='skill_outcome_v2_no_delete'"
        ).fetchone()[0]
        db.execute("drop trigger skill_outcome_v2_no_delete")
        db.execute(
            "delete from skill_outcome_v2 where outcome_digest=?", (result["outcome_digest"],)
        )
        db.execute(sql)
        db.commit()
    with pytest.raises(PolicyViolation, match="immutable row/result/source drift"):
        subject.run_outcome_recording(world.home, **args)
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_outcome_v2").fetchone()[0] == 0
    assert len(x.calls) == 2


def test_native_outcome_report_rejects_foreign_project(world, verified_source):
    x = verified_source
    verified, _vp, _context = _verify(world, x)
    plan = _plan(world, x, verified)
    result = subject.run_outcome_recording(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        allowed_scopes=x.f.scopes,
    )
    with pytest.raises(PolicyViolation, match="foreign project"):
        subject.read_native_outcome(
            world.home,
            result["canonical_job_id"],
            lifecycle=x.f.lifecycle,
            project_id=str(uuid4()),
            allowed_scopes=x.f.scopes,
        )
