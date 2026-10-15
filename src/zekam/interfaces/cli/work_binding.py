"""Exact canonical Work/Run binding shared by effect CLI entry points."""

from __future__ import annotations

from pathlib import Path

from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


def resolve_optional_work_binding(
    home: Path,
    *,
    run_id: str | None,
    work_revision: int | None,
    run_step_id: str | None,
    expected_binding_digest: str | None,
    project_id: str,
    plan_digest: str,
    apply: bool,
    replay_job_key: str | None = None,
) -> LocalJobBinding | None:
    """A dry-run observes references; apply must pin the observed binding digest."""

    if run_id is None:
        if any(
            value is not None
            for value in (
                work_revision,
                run_step_id,
                expected_binding_digest,
            )
        ):
            raise ValidationFailed("Work binding options require --work-run")
        return None
    if type(work_revision) is not int or work_revision < 1:
        raise ValidationFailed("Work binding requires positive --work-revision")
    if apply and expected_binding_digest is None:
        raise PolicyViolation("Work binding apply requires --work-binding-digest from dry-run")
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    # Only an exact, already completed job may use historical references. New
    # jobs and queued/recovery jobs still resolve against current authority.
    snapshot = (
        runtime.job_snapshot(replay_job_key) if apply and replay_job_key is not None else None
    )
    if snapshot is not None and snapshot["state"] == "completed":
        binding = runtime.job_binding(snapshot["job_id"])
        if binding is None or (
            binding.run_id != run_id
            or binding.work_revision != work_revision
            or binding.project_id != project_id
            or binding.plan_digest != plan_digest
            or binding.run_step_id != run_step_id
        ):
            raise PolicyViolation("Work binding historical replay tuple drift")
    else:
        binding = runtime.resolve_work_binding(
            run_id=run_id,
            work_revision=work_revision,
            project_id=project_id,
            plan_digest=plan_digest,
            run_step_id=run_step_id,
        )
    if expected_binding_digest is not None and expected_binding_digest != binding.binding_digest:
        raise PolicyViolation("Work binding exact digest drift")
    return binding


def work_binding_document(binding: LocalJobBinding) -> dict[str, object]:
    return binding.body() | {"binding_digest": binding.binding_digest, "read_only": True}
