"""CLI binding admission and historical replay; final-stage tests, not yet run."""

from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.interfaces.cli import work_binding as cli_binding


def _binding() -> LocalJobBinding:
    return LocalJobBinding(
        project_id=str(uuid4()),
        work_item_id=str(uuid4()),
        work_revision=1,
        run_id=str(uuid4()),
        config_revision_id=str(uuid4()),
        config_digest=digest("config"),
        plan_digest=digest("plan"),
    )


def _arguments(binding: LocalJobBinding) -> dict:
    return {
        "run_id": binding.run_id,
        "work_revision": binding.work_revision,
        "run_step_id": binding.run_step_id,
        "expected_binding_digest": binding.binding_digest,
        "project_id": binding.project_id,
        "plan_digest": binding.plan_digest,
        "apply": True,
        "replay_job_key": "research:exact-plan",
    }


def test_incomplete_options_fail_without_opening_runtime(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Incomplete binding must fail before runtime access")

    monkeypatch.setattr(cli_binding, "SQLiteLocalRuntimeStore", forbidden)
    binding = _binding()
    args = _arguments(binding)
    args["expected_binding_digest"] = None
    with pytest.raises(PolicyViolation, match="from dry-run"):
        cli_binding.resolve_optional_work_binding(tmp_path, **args)
    args["apply"] = False
    args["run_id"] = None
    with pytest.raises(ValidationFailed, match="require --work-run"):
        cli_binding.resolve_optional_work_binding(tmp_path, **args)
    args["work_revision"] = None
    assert cli_binding.resolve_optional_work_binding(tmp_path, **args) is None


@pytest.mark.parametrize("field", [None, "run_id", "work_revision", "plan_digest", "project_id"])
def test_completed_replay_uses_persisted_binding_without_current_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str | None,
) -> None:
    binding = _binding()

    class HistoricalRuntime:
        def __init__(self, path, *, existing_only):
            assert existing_only

        def job_snapshot(self, key):
            assert key == "research:exact-plan"
            return {"job_id": "existing-job", "state": "completed"}

        def job_binding(self, job_id):
            assert job_id == "existing-job"
            return binding

        def resolve_work_binding(self, **kwargs):
            raise AssertionError("Historical replay must not require open Work/Run")

    monkeypatch.setattr(cli_binding, "SQLiteLocalRuntimeStore", HistoricalRuntime)
    args = _arguments(binding)
    if field is not None:
        args[field] = (
            2
            if field == "work_revision"
            else (digest("different") if field == "plan_digest" else str(uuid4()))
        )
        with pytest.raises(PolicyViolation, match="historical replay tuple drift"):
            cli_binding.resolve_optional_work_binding(tmp_path, **args)
    else:
        assert cli_binding.resolve_optional_work_binding(tmp_path, **args) == binding


@pytest.mark.parametrize("state", ["ready", "running", "recovery-required", "failed"])
def test_nonterminal_or_failed_job_must_revalidate_current_tuple(tmp_path, monkeypatch, state):
    binding = _binding()
    calls = []

    class CurrentRuntime:
        def __init__(self, path, *, existing_only):
            assert existing_only

        def job_snapshot(self, key):
            return {"job_id": "existing-job", "state": state}

        def job_binding(self, job_id):
            raise AssertionError("Only completed jobs may replay historical binding")

        def resolve_work_binding(self, **kwargs):
            calls.append(kwargs)
            raise PolicyViolation("fixture-current-authority-denied")

    monkeypatch.setattr(cli_binding, "SQLiteLocalRuntimeStore", CurrentRuntime)
    with pytest.raises(PolicyViolation, match="fixture-current-authority-denied"):
        cli_binding.resolve_optional_work_binding(tmp_path, **_arguments(binding))
    assert len(calls) == 1
