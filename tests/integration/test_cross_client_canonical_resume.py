"""Canonical local lease reacquisition for cross-client resume."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.cross_client_resume import CrossClientResumeDispatchService
from zekam.application.home import HomeLayout
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.clients import (
    ClientDescriptor,
    ClientKind,
    ClientPermissionManifest,
    DispatchOutcome,
    DispatchResult,
)
from zekam.domain.context_continuity import (
    Checkpoint,
    ContinuitySnapshot,
    EvidenceReference,
    TargetRouteBinding,
)
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v7
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.integration
NOW = dt.datetime(2026, 10, 15, 8, 0, tzinfo=dt.UTC)


class _RouteGuard:
    def require_current(
        self,
        decision_id: UUID,
        *,
        target_model_ref: str,
        target_client_id: str,
        project_id: str,
        at: dt.datetime | None,
    ) -> TargetRouteBinding:
        del target_client_id, project_id
        moment = at or NOW
        return TargetRouteBinding(
            decision_id,
            digest((str(decision_id), target_model_ref)),
            target_model_ref,
            moment + dt.timedelta(minutes=5),
            moment,
        )


class _AssignmentStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    def create(self, assignment: AgentAssignment) -> tuple[UUID, bool]:
        self.events.append("assignment")
        return assignment.id, True

    def record_invocation(self, invocation: AgentInvocation) -> tuple[UUID, bool]:
        self.events.append("invocation")
        return invocation.id, True

    def store_result(self, **_kwargs: object) -> None:
        self.events.append("receipt")


class _Adapter:
    def __init__(self, descriptor: ClientDescriptor) -> None:
        self.descriptor = descriptor
        self.calls = 0

    def dispatch(self, request, *, cwd: Path, permit):  # type: ignore[no-untyped-def]
        del cwd
        permit.assert_valid(request)
        self.calls += 1
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.SUCCESS,
            exit_code=0,
            payload={"step": "implemented"},
        )


def _descriptor(client_id: str, kind: ClientKind) -> ClientDescriptor:
    return ClientDescriptor(
        kind=kind,
        client_id=client_id,
        executable="fixture-client",
        capabilities=frozenset({"chat", "code", "structured-result", "cancellation"}),
        version="fixture-v1",
        permission_manifest=ClientPermissionManifest(
            f"{client_id}-permissions",
            ("filesystem.read", "process.run"),
            managed=True,
        ),
    )


def _world(tmp_path: Path):
    home = HomeLayout(tmp_path / "home").ensure().root
    database = home / "state" / "operational.db"
    bootstrap_v7(database)
    SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("task"), sanitized_config={}
        )
        project = uow.create_project(slug="resume", display_name="Resume")
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:resume", source_kind="directory"
        )
        source_snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref="revision-1",
            tree_digest=digest("tree"),
            content_digest=digest("content"),
            config_digest=config.config_digest,
        )
        work = uow.create_work(project_id=project.id, kind="task", title="Resume", state="ready")
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
            budget={"seconds": 60},
            source_snapshot_id=source_snapshot.id,
        )
        uow.commit()
    binding = LocalJobBinding(
        project.id,
        work.id,
        work.revision,
        run.id,
        config.id,
        config.config_digest,
        run.plan_digest,
        source_snapshot_id=source_snapshot.id,
    )
    return SimpleNamespace(
        home=home,
        database=database,
        binding=binding,
        run_id=run.id,
        project_id=project.id,
        work_item_id=work.id,
    )


def _request_parts(world):
    project_id = UUID(world.project_id)
    work_item_id = UUID(world.work_item_id)
    plan_id = uuid4()
    checkpoint = Checkpoint(
        "checkpoint-1",
        str(project_id),
        str(work_item_id),
        str(plan_id),
        "revision-1",
        ("inspect", "implement"),
        ("inspect",),
        ("implement",),
        (("inspect", digest("inspection")),),
        digest("context"),
        digest("journal"),
        "reacquire-and-implement",
        NOW,
        source_fingerprint=digest("source"),
        test_fingerprint=digest("test"),
        config_fingerprint=digest("config"),
    )
    snapshot = ContinuitySnapshot(
        str(project_id),
        str(work_item_id),
        checkpoint.checkpoint_digest,
        checkpoint.journal_head_digest,
        checkpoint.context_manifest_digest,
        "revision-1",
        ("docs/checkpoint.md",),
        ("reacquire-and-implement",),
        (EvidenceReference("work", "checkpoint-1", digest("evidence")),),
        NOW,
    )
    source_client = _descriptor("codex", ClientKind.CODEX)
    target_client = _descriptor("claude-code", ClientKind.CLAUDE_CODE)
    service = ContextContinuityService(route_guard=_RouteGuard())
    handoff = service.finalize_cross_client_handoff(
        source_client=source_client,
        target_client=target_client,
        source_model_ref="model-codex",
        target_model_ref="model-claude",
        snapshot=snapshot,
        checkpoint=checkpoint,
        required_capabilities=("code", "structured-result"),
        required_permissions=("filesystem.read", "process.run"),
        target_route_decision_id=uuid4(),
        created_at=NOW,
    )
    assignment_id = uuid4()
    parent_assignment_id = uuid4()
    assignment_body = {
        "id": str(assignment_id),
        "realm_id": str(uuid4()),
        "project_id": str(project_id),
        "work_item_id": str(work_item_id),
        "plan_id": str(plan_id),
        "step_id": "implement",
        "parent_assignment_id": str(parent_assignment_id),
        "role": "builder",
        "agent_ref": "cross-client-builder",
        "risk": "medium",
        "instruction_digest": digest("instructions"),
        "context_manifest_digest": checkpoint.context_manifest_digest,
        "read_resources": [],
        "write_resources": [],
    }
    assignment = AgentAssignment(
        id=assignment_id,
        realm_id=UUID(assignment_body["realm_id"]),
        project_id=project_id,
        work_item_id=work_item_id,
        role=AssignmentRole.BUILDER,
        agent_ref="cross-client-builder",
        instruction_digest=digest("instructions"),
        context_manifest_digest=checkpoint.context_manifest_digest,
        assignment_digest=digest(assignment_body),
        parent_assignment_id=parent_assignment_id,
        plan_id=plan_id,
        step_id="implement",
        created_at=NOW,
    )
    invocation_id = uuid4()
    execution_identity = "cross-client:claude-code:fixture"
    invocation = AgentInvocation(
        id=invocation_id,
        realm_id=assignment.realm_id,
        assignment_id=assignment.id,
        client_id=target_client.client_id,
        execution_identity=execution_identity,
        invocation_digest=digest(
            {
                "id": str(invocation_id),
                "realm_id": str(assignment.realm_id),
                "assignment_id": str(assignment.id),
                "client_id": target_client.client_id,
                "execution_identity": execution_identity,
            }
        ),
        created_at=NOW,
    )
    return service, handoff, snapshot, checkpoint, assignment, invocation, _Adapter(target_client)


def test_cross_client_resume_reacquires_bound_lease_and_replays_without_adapter_call(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    service, handoff, snapshot, checkpoint, assignment, invocation, adapter = _request_parts(world)
    assignments = _AssignmentStore()
    dispatcher = CrossClientResumeDispatchService(service, assignments)
    args = {
        "home": world.home,
        "binding": world.binding,
        "handoff": handoff,
        "snapshot": snapshot,
        "checkpoint": checkpoint,
        "current_source_revision": "revision-1",
        "current_source_fingerprint": digest("source"),
        "current_test_fingerprint": digest("test"),
        "current_config_fingerprint": digest("config"),
        "assignment": assignment,
        "invocation": invocation,
        "adapter": adapter,
        "cwd": tmp_path,
        "timeout_seconds": 30,
    }

    first = dispatcher.dispatch_bound(**args)
    second = dispatcher.dispatch_bound(**args)

    assert first.result_digest == second.result_digest
    assert adapter.calls == 1
    assert assignments.events == ["assignment", "invocation", "receipt"]
    runtime = SQLiteLocalRuntimeStore(world.database, existing_only=True)
    input_digest = dispatcher._input_digest(
        handoff=handoff,
        snapshot=snapshot,
        checkpoint=checkpoint,
        current_source_revision="revision-1",
        current_source_fingerprint=digest("source"),
        current_test_fingerprint=digest("test"),
        current_config_fingerprint=digest("config"),
        assignment=assignment,
        invocation=invocation,
        adapter=adapter,
        timeout_seconds=30,
    )
    job = runtime.job_snapshot(
        canonical_job_key(world.binding.binding_digest, "continuity.resume", input_digest)
    )
    assert job is not None
    assert job["state"] == "completed"
    assert job["effects"][0]["receipt_status"] == "completed"
    with SQLiteOperationalStore(world.database).unit_of_work() as uow:
        assert len(uow.list_checkpoints(world.run_id)) == 1
        uow.commit()


def test_cross_client_resume_rejects_source_drift_before_local_job_admission(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    service, handoff, snapshot, checkpoint, assignment, invocation, adapter = _request_parts(world)
    dispatcher = CrossClientResumeDispatchService(service, _AssignmentStore())
    with pytest.raises(PolicyViolation, match="source revision stale"):
        dispatcher.dispatch_bound(
            home=world.home,
            binding=world.binding,
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-2",
            current_source_fingerprint=digest("source"),
            current_test_fingerprint=digest("test"),
            current_config_fingerprint=digest("config"),
            assignment=assignment,
            invocation=invocation,
            adapter=adapter,
            cwd=tmp_path,
            timeout_seconds=30,
        )
    assert SQLiteLocalRuntimeStore(world.database, existing_only=True).status().ready_jobs == 0
