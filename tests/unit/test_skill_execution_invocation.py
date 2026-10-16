"""Actual-execution projection with offline native/active-policy port doubles."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_native_invocation import native_load  # noqa: F401
from tests.unit.test_skill_native_execution import execution  # noqa: F401
from tests.unit.test_skill_trials import _binding
from zekam.application import skill_execution_invocation as subject
from zekam.application.skill_native_invocation import read_native_invocation
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation


def _source_plan(world, x):
    result = x.run()
    plan = subject.build_execution_invocation_plan(
        world.home,
        lifecycle=x.f.lifecycle,
        execution_ref=result["canonical_job_id"],
        project_id=x.binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    return result, plan


def test_actual_execution_records_original_work_run_and_terminal_receipt_source(world, execution):
    x = execution
    source, plan = _source_plan(world, x)
    recording = _binding(world, plan)
    assert recording.run_id != x.binding.run_id
    args = dict(lifecycle=x.f.lifecycle, plan=plan, binding=recording, allowed_scopes=x.f.scopes)
    result = subject.run_execution_invocation(world.home, **args)
    replay = subject.run_execution_invocation(world.home, **args)
    assert replay["canonical_replayed"] is True and len(x.calls) == 1
    assert (
        result["plan"]["execution_receipt_digest"]
        == world.runtime.job_snapshot(source["canonical_job_id"])["terminal_evidence_digest"]
    )
    assert result["plan"]["observation_time_source"] == "execution-terminal-receipt"
    assert result["plan"]["work_association"] == "canonical-execution"
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        rows = db.execute(
            "select work_ref,run_ref,state from skill_invocation_v2 order by rowid"
        ).fetchall()
    assert len(rows) == 6
    assert {row[0] for row in rows} == {x.binding.work_item_id}
    assert {row[1] for row in rows} == {x.binding.run_id}
    assert rows[-1][2] == "unverified"
    report = read_native_invocation(
        world.home,
        result["canonical_job_id"],
        lifecycle=x.f.lifecycle,
        project_id=x.binding.project_id,
        allowed_scopes=x.f.scopes,
    )
    assert report["invocation_digests"] == result["invocation_digests"]
    assert report["skill_effect_verified"] is False
    assert len(x.calls) == 1


def test_actual_execution_cannot_be_reassociated_with_another_work(world, execution):
    x = execution
    _source, plan = _source_plan(world, x)
    recording = _binding(world, plan)
    with world.store.unit_of_work() as uow:
        other = uow.create_work(
            project_id=x.binding.project_id, kind="task", title="Other Work", state="ready"
        )
        uow.commit()
    with pytest.raises(PolicyViolation, match="cannot reassociate"):
        subject.run_execution_invocation(
            world.home,
            lifecycle=x.f.lifecycle,
            plan=plan,
            binding=replace(recording, work_item_id=other.id),
            allowed_scopes=x.f.scopes,
        )
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 0


def test_second_recording_run_cannot_duplicate_actual_native_call(world, execution):
    x = execution
    _source, plan = _source_plan(world, x)
    subject.run_execution_invocation(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        allowed_scopes=x.f.scopes,
    )
    with pytest.raises(ConcurrencyConflict, match="already recorded"):
        subject.run_execution_invocation(
            world.home,
            lifecycle=x.f.lifecycle,
            plan=plan,
            binding=_binding(world, plan),
            allowed_scopes=x.f.scopes,
        )
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_invocation_v2").fetchone()[0] == 6
    assert len(x.calls) == 1


def test_mismatch_is_an_observed_execution_without_verified_outcome(world, execution):
    x = execution
    x.answer[0] = 8
    source, plan = _source_plan(world, x)
    assert source["state"] == "measured-mismatch"
    result = subject.run_execution_invocation(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        allowed_scopes=x.f.scopes,
    )
    assert result["state"] == "unverified" and result["skill_effect_verified"] is False
    with closing(x.f.lifecycle._connect(read_only=True)) as db:
        assert db.execute("select count(*) from skill_outcome_v2").fetchone()[0] == 0


def test_process_failure_cannot_mint_completed_native_invocation(world, execution):
    x = execution
    x.process_exit[0] = 4
    source = x.run()
    with pytest.raises(PolicyViolation, match="observed completed native skill call"):
        subject.build_execution_invocation_plan(
            world.home,
            lifecycle=x.f.lifecycle,
            execution_ref=source["canonical_job_id"],
            project_id=x.binding.project_id,
            allowed_scopes=x.f.scopes,
        )


def test_historical_recording_survives_deactivation_without_reingestion(
    world, execution, monkeypatch
):
    x = execution
    source, plan = _source_plan(world, x)
    result = subject.run_execution_invocation(
        world.home,
        lifecycle=x.f.lifecycle,
        plan=plan,
        binding=_binding(world, plan),
        allowed_scopes=x.f.scopes,
    )
    monkeypatch.setattr(x.f.lifecycle, "_activation_event_effective", lambda _row: False)
    assert (
        read_native_invocation(
            world.home,
            result["canonical_job_id"],
            lifecycle=x.f.lifecycle,
            project_id=x.binding.project_id,
            allowed_scopes=x.f.scopes,
        )["state"]
        == "unverified"
    )
    with pytest.raises(PolicyViolation):
        subject.build_execution_invocation_plan(
            world.home,
            lifecycle=x.f.lifecycle,
            execution_ref=source["canonical_job_id"],
            project_id=x.binding.project_id,
            allowed_scopes=x.f.scopes,
        )
    assert len(x.calls) == 1
