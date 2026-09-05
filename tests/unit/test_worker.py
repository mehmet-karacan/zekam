"""Worker handler registry ve scheduled-only fail-closed kurallari."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest

from zekam.application.execution import ExecutionHost
from zekam.application.worker import (
    SchedulerGateway,
    Worker,
    WorkerSettings,
    build_worker,
    noop_handler,
    resolve_handlers,
    run_codex_lifecycle_once,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import AttemptOutcome, FailureCategory, JobKind, JobState
from zekam.domain.scheduler import JobDefinition, Schedule

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 28, 18, tzinfo=dt.UTC)


def test_unknown_handler_is_rejected_at_registry_resolution() -> None:
    with pytest.raises(PolicyViolation, match="handler tanimsiz"):
        resolve_handlers([str(JobKind.READ_ONLY)])


def test_explicit_test_handler_can_be_resolved() -> None:
    name = str(JobKind.READ_ONLY)
    assert resolve_handlers([name], registry={name: noop_handler}) == {name: noop_handler}


def test_unrequested_registry_entries_are_not_exposed() -> None:
    requested = str(JobKind.READ_ONLY)
    extra = str(JobKind.MUTATION)
    resolved = resolve_handlers(
        [requested], registry={requested: noop_handler, extra: noop_handler}
    )
    assert set(resolved) == {requested}


def test_worker_cannot_start_without_an_explicit_handler() -> None:
    settings = WorkerSettings(worker_label="test", capabilities=("read",))
    with pytest.raises(PolicyViolation, match="explicit handler"):
        build_worker(object(), uuid4(), settings=settings, handlers={})


def test_scheduled_only_worker_starts_without_queue_handler() -> None:
    settings = WorkerSettings(
        worker_label="scheduled",
        capabilities=("read",),
        poll_seconds=0.001,
        max_iterations=1,
    )

    worker = build_worker(
        object(),
        uuid4(),
        settings=settings,
        handlers={},
        scheduled_handlers={},
        with_scheduler=False,
        consume_queue=False,
    )

    assert worker.consume_queue is False
    queue_depth = Mock(return_value=10_000)
    results = worker.run(queue_depth=queue_depth)
    assert len(results) == 1
    queue_depth.assert_not_called()


def test_scheduled_only_worker_rejects_unused_queue_handler() -> None:
    settings = WorkerSettings(worker_label="scheduled", capabilities=("read",))

    with pytest.raises(PolicyViolation, match="Scheduled-only"):
        build_worker(
            object(),
            uuid4(),
            settings=settings,
            handlers={str(JobKind.READ_ONLY): noop_handler},
            with_scheduler=False,
            consume_queue=False,
        )


def test_scheduled_only_tick_runs_handler_without_claiming_queue() -> None:
    settings = WorkerSettings(worker_label="scheduled", capabilities=("read",))
    host_mock = Mock(spec=ExecutionHost)
    scheduler_mock = Mock(spec=SchedulerGateway)
    definition_id = uuid4()
    run_id = uuid4()
    definition = JobDefinition(
        job_name="memory-candidate-compile",
        schedule=Schedule(interval="5m"),
    )
    scheduler_mock.definitions.return_value = ((definition_id, definition, None),)
    scheduler_mock.is_running.return_value = False
    scheduler_mock.known_keys.return_value = frozenset()
    scheduler_mock.record_trigger.return_value = run_id
    handled_at: list[dt.datetime] = []

    def compile_candidates(moment: dt.datetime) -> str:
        handled_at.append(moment)
        return "compiler completed"

    worker = Worker(
        host=cast(ExecutionHost, host_mock),
        settings=settings,
        scheduler=cast(SchedulerGateway, scheduler_mock),
        scheduled_handlers={"memory-candidate-compile": compile_candidates},
        consume_queue=False,
    )

    result = worker.tick(now=NOW, queue_depth=10_000)

    assert result.accepted_work is False
    assert result.triggered_jobs == ("memory-candidate-compile",)
    assert result.skipped_reason == "scheduled-only: queue claim disabled"
    assert handled_at == [NOW]
    host_mock.acquire_work.assert_not_called()
    scheduler_mock.finish_run.assert_called_once_with(
        run_id,
        state="succeeded",
        detail="compiler completed",
        now=NOW,
    )


def test_normal_worker_still_claims_queue_by_default() -> None:
    settings = WorkerSettings(worker_label="queue", capabilities=("read",))
    host_mock = Mock(spec=ExecutionHost)
    host_mock.acquire_work.return_value = None
    worker = Worker(
        host=cast(ExecutionHost, host_mock),
        settings=settings,
        handlers={str(JobKind.READ_ONLY): noop_handler},
    )

    result = worker.tick(now=NOW)

    assert worker.consume_queue is True
    assert result.skipped_reason == "kuyruk bos"
    host_mock.acquire_work.assert_called_once_with(
        capabilities=("read",),
        lease_seconds=60,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("claims", "expected_outcome", "expected_failure_category"),
    [
        ((), AttemptOutcome.FAILED, FailureCategory.ADAPTER),
        ((object(),), AttemptOutcome.RECOVERY_REQUIRED, None),
    ],
)
def test_codex_child_bind_failure_is_terminalized_without_silent_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    claims: tuple[object, ...],
    expected_outcome: AttemptOutcome,
    expected_failure_category: FailureCategory | None,
) -> None:
    realm_id = uuid4()
    job_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
        step_id="client-lifecycle-drain",
        assignment_id=uuid4(),
        run_id=uuid4(),
        state=JobState.RUNNING,
    )
    work = SimpleNamespace(job=job)
    jobs = Mock()
    jobs.get.return_value = job
    jobs.claim_exact.return_value = work
    ledger = Mock()
    ledger.claims_for_job.return_value = claims
    host = Mock(jobs=jobs, ledger=ledger)
    host.finish.return_value = True
    repository = Mock()
    repository.committed_admission_exists.return_value = False
    repository.next_codex_lifecycle_job_id.return_value = job_id
    spool = Mock()
    spool.pending.return_value = (SimpleNamespace(entry_digest="sha256:" + "a" * 64),)
    binder = Mock()
    binder.bind_child_envelope.side_effect = RuntimeError("injected bind failure")

    monkeypatch.setattr("zekam.application.worker.ExecutionHost", lambda *args, **kwargs: host)
    monkeypatch.setattr(
        "zekam.application.worker.legacy_repository",
        lambda kind, *args, **kwargs: (
            repository
            if kind == "client_lifecycle"
            else pytest.fail(f"unexpected repository kind: {kind}")
        ),
    )
    monkeypatch.setattr(
        "zekam.application.client_lifecycle_spool.ClientLifecycleSpool",
        lambda *args, **kwargs: spool,
    )
    monkeypatch.setattr(
        "zekam.application.client_runtime_bootstrap.ClaimedLifecycleBootstrapService",
        lambda *args, **kwargs: binder,
    )

    with pytest.raises(RuntimeError, match="injected bind failure"):
        run_codex_lifecycle_once(
            object(),
            realm_id,
            home=tmp_path,
            settings=WorkerSettings(
                worker_label="codex-lifecycle-worker",
                capabilities=("client.lifecycle.codex-drain",),
            ),
        )

    host.finish.assert_called_once()
    assert host.finish.call_args.kwargs["outcome"] is expected_outcome
    assert host.finish.call_args.kwargs["failure_category"] is expected_failure_category
    assert jobs.get.call_count == 2
