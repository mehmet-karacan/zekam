"""Codex/Claude/OpenCode transcript-free continuity E2E."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.cross_client_resume import CrossClientResumeDispatchService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.clients import (
    ClientDescriptor,
    ClientKind,
    ClientPermissionManifest,
    DispatchOutcome,
)
from zekam.domain.context_continuity import (
    Checkpoint,
    ContinuitySnapshot,
    EvidenceReference,
    TargetRouteBinding,
)
from zekam.infrastructure.clients.adapters import SubprocessClientAdapter

pytestmark = pytest.mark.e2e
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_client.py"


class RouteGuard:
    def __init__(self) -> None:
        self.observed_times: list[dt.datetime | None] = []

    def require_current(
        self,
        decision_id,  # type: ignore[no-untyped-def]
        *,
        target_model_ref: str,
        target_client_id: str,
        project_id: str,
        at: dt.datetime | None,
    ) -> TargetRouteBinding:
        del target_client_id, project_id
        self.observed_times.append(at)
        moment = at or NOW
        return TargetRouteBinding(
            decision_id,
            digest((str(decision_id), target_model_ref)),
            target_model_ref,
            moment + dt.timedelta(minutes=5),
            moment,
        )


class RecordingStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    def create(self, assignment):  # type: ignore[no-untyped-def]
        self.events.append("assignment")
        return assignment.id, True

    def record_invocation(self, invocation):  # type: ignore[no-untyped-def]
        self.events.append("invocation")
        return invocation.id, True

    def store_result(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.events.append("receipt")


def test_codex_claude_opencode_resume_without_transcript_or_inherited_authority(
    tmp_path: Path,
) -> None:
    realm_id = uuid4()
    project_id = uuid4()
    work_item_id = uuid4()
    checkpoint = Checkpoint(
        "checkpoint-1",
        str(project_id),
        str(work_item_id),
        "plan-1",
        "revision-1",
        ("inspect", "implement", "verify"),
        ("inspect",),
        ("implement", "verify"),
        (("inspect", digest("inspection")),),
        digest("context-manifest"),
        digest("journal-head"),
        "reacquire-work-and-implement",
        NOW,
    )
    snapshot = ContinuitySnapshot(
        str(project_id),
        str(work_item_id),
        checkpoint.checkpoint_digest,
        checkpoint.journal_head_digest,
        checkpoint.context_manifest_digest,
        "revision-1",
        ("docs/CONTEXT_COMPILER_VE_CONTINUITY.md",),
        ("reacquire-work-and-implement",),
        (EvidenceReference("benchmark", "model-decision:latest", digest("decision")),),
        NOW,
    )
    route_guard = RouteGuard()
    service = ContextContinuityService(route_guard=route_guard)
    store = RecordingStore()
    dispatcher = CrossClientResumeDispatchService(service, store)
    adapters = {
        client_id: SubprocessClientAdapter(
            ClientDescriptor(
                kind=kind,
                client_id=client_id,
                executable=str(FIXTURE),
                version="test-v1",
                capabilities=frozenset({"chat", "code", "structured-result", "cancellation"}),
                permission_manifest=ClientPermissionManifest(
                    f"{client_id}-test",
                    ("filesystem.read", "process.run"),
                    managed=True,
                ),
            ),
            launcher=(sys.executable,),
            env=(("ZEKAM_FAKE_CLIENT_MODE", "success"),),
        )
        for client_id, kind in (
            ("codex", ClientKind.CODEX),
            ("claude-code", ClientKind.CLAUDE_CODE),
            ("opencode", ClientKind.OPENCODE),
        )
    }
    current_client = "codex"
    for next_client in ("claude-code", "opencode", "codex"):
        source = adapters[current_client]
        target = adapters[next_client]
        route_id = uuid4()
        handoff = service.finalize_cross_client_handoff(
            source_client=source.descriptor,
            target_client=target.descriptor,
            source_model_ref=f"model-ref-{current_client}",
            target_model_ref=f"model-ref-{next_client}",
            snapshot=snapshot,
            checkpoint=checkpoint,
            required_capabilities=("code", "structured-result"),
            required_permissions=("filesystem.read", "process.run"),
            target_route_decision_id=route_id,
            created_at=NOW,
        )
        instructions = service.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-1",
            target_client=target.descriptor,
            observed_at=NOW,
        )
        assert instructions.client == next_client
        assert instructions.reacquire_work
        assert not instructions.transcript_used
        assert not instructions.grants_authority
        lifecycle = target.lifecycle_event(
            session_id=f"resume-{next_client}",
            sequence=1,
            previous_digest=None,
            event_type="resume.prepared",
            payload_digest=handoff.handoff_digest,
            occurred_at=NOW,
        )
        assert lifecycle.client_id == next_client
        assert lifecycle.as_dict()["transcript_included"] is False
        assignment_id = uuid4()
        parent_assignment_id = uuid4()
        assignment_body = {
            "id": str(assignment_id),
            "realm_id": str(realm_id),
            "project_id": str(project_id),
            "work_item_id": str(work_item_id),
            "plan_id": None,
            "step_id": "implement",
            "parent_assignment_id": str(parent_assignment_id),
            "role": "builder",
            "agent_ref": "cross-client-builder",
            "risk": "medium",
            "instruction_digest": digest(instructions.next_safe_actions),
            "context_manifest_digest": checkpoint.context_manifest_digest,
            "read_resources": [],
            "write_resources": [],
        }
        assignment = AgentAssignment(
            id=assignment_id,
            realm_id=realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            role=AssignmentRole.BUILDER,
            agent_ref="cross-client-builder",
            instruction_digest=assignment_body["instruction_digest"],
            context_manifest_digest=checkpoint.context_manifest_digest,
            assignment_digest=digest(assignment_body),
            parent_assignment_id=parent_assignment_id,
            step_id="implement",
            created_at=NOW,
        )
        invocation_id = uuid4()
        execution_identity = f"cross-client:{next_client}:{invocation_id}"
        invocation = AgentInvocation(
            id=invocation_id,
            realm_id=realm_id,
            assignment_id=assignment_id,
            client_id=next_client,
            execution_identity=execution_identity,
            invocation_digest=digest(
                {
                    "id": str(invocation_id),
                    "realm_id": str(realm_id),
                    "assignment_id": str(assignment_id),
                    "client_id": next_client,
                    "execution_identity": execution_identity,
                }
            ),
            created_at=NOW,
        )
        result = dispatcher.dispatch(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-1",
            assignment=assignment,
            invocation=invocation,
            adapter=target,
            cwd=tmp_path,
            timeout_seconds=30,
        )
        assert result.outcome is DispatchOutcome.SUCCESS
        assert result.client_id == next_client
        assert result.payload["context_digest"] == checkpoint.context_manifest_digest
        current_client = next_client
    assert store.events == ["assignment", "invocation", "receipt"] * 3
    assert route_guard.observed_times.count(None) == 3
