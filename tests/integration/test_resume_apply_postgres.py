from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.execution import ExecutionHost
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_run import ExecutionRun
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import Job, JobKind
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_exact_reclaim_rejects_expired_attempt_with_terminal_effect(
    realm_session: tuple[Any, Any], migrated_database: Any, tmp_path: Path
) -> None:
    realm, connection = realm_session
    now = dt.datetime.now(dt.UTC)
    source = tmp_path / "resume-exact-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Resume exact")
    policy_digest = digest("resume-policy")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("build", "Build", EffectKind.FILE_WRITE),),
    )
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="codex",
        session_id="resume-exact",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=100,
        deadline=now + dt.timedelta(hours=2),
        created_at=now,
    )
    ExecutionRunRepository(connection, realm.id).create_run(run)
    coordinator_id, assignment_id = uuid4(), uuid4()
    with connection.cursor() as cursor:
        for identity, parent, role in (
            (coordinator_id, None, "coordinator"),
            (assignment_id, coordinator_id, "builder"),
        ):
            cursor.execute(
                "insert into agents.assignment"
                "(id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
                "agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                "(%s,%s,%s,%s,%s,'build',%s,%s,%s,'active','medium',%s,%s,%s,%s)",
                (
                    identity,
                    realm.id,
                    project.id,
                    work.id,
                    plan.id,
                    parent,
                    role,
                    f"agent:{role}",
                    digest(f"instruction:{role}"),
                    digest("context"),
                    digest(f"assignment:{role}"),
                    now,
                ),
            )
        cursor.execute(
            "insert into agents.assignment_resource"
            "(realm_id,assignment_id,resource,mode) values(%s,%s,%s,'write')",
            (realm.id, assignment_id, "project:resume:file:src/app.py"),
        )
    connection.commit()
    job = Job.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        assignment_id=assignment_id,
        run_id=run.id,
        kind=JobKind.MUTATION,
        idempotency_key=f"resume-exact-{uuid4()}",
        resources=parse_requests(write=("project:resume:file:src/app.py",)),
        required_capabilities=("sandbox.write",),
        max_attempts=2,
        available_at=now,
    )
    host = ExecutionHost(connection, realm.id, worker_label="worker-one")
    stored, created = host.jobs.enqueue(job)
    assert created
    connection.commit()

    def race(worker: str) -> Any:
        from zekam.infrastructure.postgres.connection import configure_session, connect

        with connect(migrated_database) as contender:
            configure_session(contender, realm_id=realm.id)
            return ExecutionHost(contender, realm.id, worker_label=worker).jobs.claim_exact(
                stored.id,
                project_id=project.id,
                work_item_id=work.id,
                plan_id=plan.id,
                step_id="build",
                assignment_id=assignment_id,
                run_id=run.id,
                capabilities=("sandbox.write",),
                worker_label=worker,
                lease_seconds=1,
                now=now,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race, worker) for worker in ("worker-one", "worker-two")]
        outcomes: list[Any] = []
        failures: list[Exception] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # exact loser category asserted below
                failures.append(exc)
    assert len(outcomes) == 1
    assert len(failures) == 1
    assert "aktif lease" in str(failures[0]) or "yarisi kaybedildi" in str(failures[0])
    first = outcomes[0]
    claim = host.claim_effect(
        first,
        operation="write",
        effect_digest=digest("effect"),
        authorization_digest=digest("authorization"),
        resources=job.resources,
        adapter_digest=digest("adapter"),
        idempotency_key=f"effect-{uuid4()}",
        now=now,
    )
    host.record_success(claim, result_digest=digest("result"), now=now)
    with pytest.raises(PolicyViolation, match="effect bulunan attempt"):
        host.jobs.claim_exact(
            stored.id,
            project_id=project.id,
            work_item_id=work.id,
            plan_id=plan.id,
            step_id="build",
            assignment_id=assignment_id,
            run_id=run.id,
            capabilities=("sandbox.write",),
            worker_label="worker-two",
            lease_seconds=30,
            now=now + dt.timedelta(seconds=2),
        )
