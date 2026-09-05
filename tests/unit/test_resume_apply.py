from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import zekam.application.resume_apply_service as apply_module
from zekam.application.resume_apply_service import ResumeApplyService
from zekam.domain.agents import AssignmentStatus
from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import SandboxBindingV2, SandboxDisposition
from zekam.domain.clients import (
    ClientCapabilityManifest,
    ClientDescriptor,
    ClientKind,
    ClientLifecycleEvent,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.resume import (
    ResumeAction,
    ResumeDisposition,
    ResumePlan,
    RuntimeObservation,
)
from zekam.domain.resume_apply import (
    ResumeApplyEvent,
    ResumeApplyPhase,
    ResumeApplyRequest,
    ResumeApplyState,
)
from zekam.domain.runtime import AttemptOutcome

pytestmark = pytest.mark.unit


def uid(value: int) -> UUID:
    return UUID(int=value)


def plan(now: dt.datetime, **changes: Any) -> ResumePlan:
    value = ResumePlan(
        realm_id=uid(1),
        project_id=uid(2),
        work_item_id=uid(3),
        checkpoint_id=uid(4),
        checkpoint_digest=digest("checkpoint"),
        checkpoint_revision=2,
        selected_checkpoint_reason="latest-valid-v2",
        disposition=ResumeDisposition.SAFE_CONTINUE,
        stale_dimensions=(),
        reconciliation_actions=(),
        reacquire_resources=("authorization", "lease"),
        logical_read_resources=("project:p:source",),
        logical_write_resources=("project:p:file:src/app.py",),
        runtime=RuntimeObservation(
            run_id=uid(5),
            job_id=uid(6),
            attempt_id=uid(7),
            assignment_id=uid(8),
            execution_envelope_id=uid(9),
            execution_envelope_digest=digest("envelope"),
            observed_lease_id=uid(10),
            observed_fencing_token=1,
            job_state="ready",
            lease_expires_at=now - dt.timedelta(seconds=1),
            deadline=now + dt.timedelta(minutes=10),
        ),
        target_client_id="codex",
        next_step_id="build",
        context_recipe="resume:codex:implementer",
        required_route_role="implementer",
        actions=(ResumeAction("dispatch", "dispatch-next-step", (), "build"),),
        blockers=(),
        observed_at=now,
        valid_until=now + dt.timedelta(minutes=5),
    )
    return replace(value, **changes)


class Connection:
    def transaction(self) -> Any:
        return nullcontext()


class Adapter:
    def __init__(self) -> None:
        self.calls = 0
        self.descriptor = ClientDescriptor(
            kind=ClientKind.CODEX,
            client_id="codex",
            executable="codex",
            capabilities=frozenset({"structured-result"}),
        )

    def dispatch(self, *_: Any, **__: Any) -> Any:
        self.calls += 1
        raise AssertionError("stale plan adapter'a ulasmamali")

    @property
    def capability_manifest(self) -> ClientCapabilityManifest:
        return self.descriptor.capability_manifest

    def lifecycle_event(
        self,
        *,
        session_id: str,
        sequence: int,
        previous_digest: str | None,
        event_type: str,
        payload_digest: str,
        occurred_at: dt.datetime,
    ) -> ClientLifecycleEvent:
        raise AssertionError("resume apply lifecycle event uretmemeli")


class Governance:
    def __init__(self) -> None:
        self.authorization_reads = 0
        self.consumptions = 0


class EnvironmentGuard:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, dt.datetime]] = []

    def assert_envelope_current(self, envelope_id: UUID, *, now: dt.datetime):  # type: ignore[no-untyped-def]
        self.calls.append((envelope_id, now))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=envelope_id, captured_at=now)


def test_stale_revalidation_consumes_no_authority_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    original = plan(now)
    stale = replace(original, observed_at=now + dt.timedelta(seconds=1))
    repository_calls = {"lock": 0, "create": 0}

    class Repository:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def lock_work(self, _: UUID) -> None:
            repository_calls["lock"] += 1

        def find_exact(self, *_: Any, **__: Any) -> None:
            return None

        def create(self, *_: Any, **__: Any) -> Any:
            repository_calls["create"] += 1
            raise AssertionError("stale plan apply kaydi uretmemeli")

    class Coordinator:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def prepare(self, *_: Any, **__: Any) -> ResumePlan:
            return stale

    monkeypatch.setattr(
        apply_module,
        "legacy_repository",
        lambda kind, *_args, **_kwargs: Repository() if kind == "resume_apply" else object(),
    )
    monkeypatch.setattr(apply_module, "ResumeCoordinator", Coordinator)
    governance = Governance()
    adapter = Adapter()
    request = ResumeApplyRequest(
        original, original.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="exact plan revalidation drift"):
        ResumeApplyService(Connection(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
            request, adapter, cwd=tmp_path, timeout_seconds=10, now=now
        )
    assert repository_calls == {"lock": 1, "create": 0}
    assert governance.authorization_reads == 0
    assert governance.consumptions == 0
    assert adapter.calls == 0


def test_dirty_sandbox_live_guard_olmadan_apply_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    dirty = SandboxBindingV2(
        SandboxDisposition.DIRTY,
        "workspace-1",
        "revision-1",
        digest("patch"),
        digest("dirty-state"),
    )
    original = plan(now, sandbox=dirty)
    repository_calls = {"create": 0}

    class Repository:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def lock_work(self, _: UUID) -> None:
            pass

        def find_exact(self, *_: Any, **__: Any) -> None:
            return None

        def create(self, *_: Any, **__: Any) -> None:
            repository_calls["create"] += 1

    class Coordinator:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def prepare(self, *_: Any, **__: Any) -> ResumePlan:
            return original

    monkeypatch.setattr(
        apply_module,
        "legacy_repository",
        lambda kind, *_args, **_kwargs: Repository() if kind == "resume_apply" else object(),
    )
    monkeypatch.setattr(apply_module, "ResumeCoordinator", Coordinator)
    request = ResumeApplyRequest(
        original, original.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="sandbox live binding guard"):
        ResumeApplyService(Connection(), Governance(), EnvironmentGuard()).apply(  # type: ignore[arg-type]
            request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now
        )
    assert repository_calls["create"] == 0


def test_dirty_sandbox_live_guard_drifti_effectten_once_reddeder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    dirty = SandboxBindingV2(
        SandboxDisposition.DIRTY,
        "workspace-1",
        "revision-1",
        digest("patch"),
        digest("dirty-state"),
    )
    original = plan(now, sandbox=dirty)
    calls: list[SandboxBindingV2] = []

    class Repository:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def lock_work(self, _: UUID) -> None:
            pass

        def find_exact(self, *_: Any, **__: Any) -> None:
            return None

        def create(self, *_: Any, **__: Any) -> None:
            raise AssertionError("sandbox drift apply kaydi uretmemeli")

    class Coordinator:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def prepare(self, *_: Any, **__: Any) -> ResumePlan:
            return original

    class RejectingGuard:
        def assert_checkpoint_binding(self, binding: SandboxBindingV2) -> None:
            calls.append(binding)
            raise PolicyViolation("resume sandbox patch veya dirty state drift")

        def hold_checkpoint_binding(self, binding: SandboxBindingV2):  # type: ignore[no-untyped-def]
            return nullcontext()

    monkeypatch.setattr(
        apply_module,
        "legacy_repository",
        lambda kind, *_args, **_kwargs: Repository() if kind == "resume_apply" else object(),
    )
    monkeypatch.setattr(apply_module, "ResumeCoordinator", Coordinator)
    request = ResumeApplyRequest(
        original, original.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="dirty state drift"):
        ResumeApplyService(
            Connection(),
            cast(Any, Governance()),
            EnvironmentGuard(),
            sandbox_binding_guard=RejectingGuard(),
        ).apply(request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now)
    assert calls == [dirty]


def test_expired_or_non_continue_plan_stops_before_database_and_dispatch(tmp_path: Path) -> None:
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    governance = Governance()
    adapter = Adapter()
    expired = plan(now, valid_until=now)
    expired_request = ResumeApplyRequest(
        expired, expired.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="penceresi doldu"):
        ResumeApplyService(object(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
            expired_request, adapter, cwd=tmp_path, timeout_seconds=10, now=now
        )
    denied = plan(now, disposition=ResumeDisposition.SAFE_REPLAN)
    denied_request = ResumeApplyRequest(
        denied, denied.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="safe-continue"):
        ResumeApplyService(object(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
            denied_request, adapter, cwd=tmp_path, timeout_seconds=10, now=now
        )
    assert adapter.calls == 0


def test_apply_request_and_events_are_digest_bound_and_authority_free() -> None:
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    prepared = plan(now)
    with pytest.raises(PolicyViolation, match="plan digest"):
        ResumeApplyRequest(
            prepared, digest("forged"), uid(11), uid(12), "worker", ("database.write",)
        )
    claim = ResumeApplyEvent(
        apply_id=uid(20),
        sequence=1,
        phase=ResumeApplyPhase.CLAIM,
        state=ResumeApplyState.CLAIMED,
        reason_code="resume.exact-claim-created",
        occurred_at=now,
        attempt_id=uid(21),
        lease_id=uid(22),
        fencing_token=2,
        claim_id=uid(23),
    )
    dispatch = ResumeApplyEvent(
        apply_id=claim.apply_id,
        sequence=2,
        phase=ResumeApplyPhase.DISPATCH,
        state=ResumeApplyState.DISPATCHED,
        reason_code="resume.adapter-dispatch-started",
        occurred_at=now,
        attempt_id=claim.attempt_id,
        lease_id=claim.lease_id,
        fencing_token=claim.fencing_token,
        claim_id=claim.claim_id,
        previous_digest=claim.event_digest,
    )
    assert claim.event_digest != dispatch.event_digest
    assert dispatch.body()["previous_digest"] == claim.event_digest


def _install_apply_fakes(
    monkeypatch: pytest.MonkeyPatch, prepared: ResumePlan, *, adapter_raises: bool = False
) -> tuple[dict[str, Any], Governance]:
    state: dict[str, Any] = {
        "created": False,
        "events": [],
        "dispatches": 0,
        "claims": 0,
        "finishes": [],
        "lease_live": True,
    }
    apply_id = uid(30)

    class Repository:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def lock_work(self, _: UUID) -> None:
            pass

        def find_exact(self, *_: Any, **__: Any) -> UUID | None:
            return apply_id if state["created"] else None

        def create(self, *_: Any, **__: Any) -> tuple[UUID, bool]:
            if state["created"]:
                return apply_id, False
            state["created"] = True
            return apply_id, True

        def latest_event(self, _: UUID) -> ResumeApplyEvent | None:
            return state["events"][-1] if state["events"] else None

        def append_event(self, event: ResumeApplyEvent) -> tuple[UUID, bool]:
            state["events"].append(event)
            return uid(40 + event.sequence), True

        def lease_is_live(self, _: ResumeApplyEvent, **__: Any) -> bool:
            return bool(state["lease_live"])

        def recover_interrupted(self, event: ResumeApplyEvent, **__: Any) -> ResumeApplyEvent:
            recovery = ResumeApplyEvent(
                apply_id=event.apply_id,
                sequence=event.sequence + 1,
                phase=ResumeApplyPhase.DISPATCH,
                state=ResumeApplyState.RECOVERY_REQUIRED,
                reason_code="resume.nonterminal-replay-lease-expired",
                occurred_at=prepared.observed_at,
                attempt_id=event.attempt_id,
                lease_id=event.lease_id,
                fencing_token=event.fencing_token,
                claim_id=event.claim_id,
                previous_digest=event.event_digest,
            )
            state["events"].append(recovery)
            return recovery

        def clone_envelope(self, *_: Any, **__: Any) -> SimpleNamespace:
            return SimpleNamespace(id=uid(50))

        def store_result_checkpoint(self, *_: Any, **__: Any) -> SimpleNamespace:
            return SimpleNamespace(id=uid(51))

    class Coordinator:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def prepare(self, *_: Any, **__: Any) -> ResumePlan:
            return prepared

    assignment = SimpleNamespace(
        id=prepared.runtime.assignment_id,
        project_id=prepared.project_id,
        work_item_id=prepared.work_item_id,
        plan_id=uid(70),
        step_id=prepared.next_step_id,
        status=AssignmentStatus.ACTIVE,
        is_child=True,
        role=SimpleNamespace(value="builder"),
        instruction_digest=digest("instruction"),
        context_manifest_digest=digest("context"),
    )

    class Assignments:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def get(self, _: UUID) -> Any:
            return assignment

        def record_invocation(self, invocation: Any) -> tuple[UUID, bool]:
            return invocation.id, True

    lease = SimpleNamespace(id=uid(61), fencing_token=2)
    claimed = SimpleNamespace(attempt_id=uid(60), lease=lease, job=SimpleNamespace(plan_id=uid(70)))

    class Jobs:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def claim_exact(self, *_: Any, **__: Any) -> Any:
            state["claims"] += 1
            return claimed

    claim = SimpleNamespace(id=uid(62))

    class Host:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def claim_effect(self, *_: Any, **__: Any) -> Any:
            return claim

        def record_success(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(id=uid(63))

        def finish(self, *_: Any, **__: Any) -> bool:
            state["finishes"].append(__.get("outcome"))
            return True

    class Dispatch:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def dispatch(self, *_: Any, **__: Any) -> Any:
            state["dispatches"] += 1
            if adapter_raises:
                raise RuntimeError("adapter disconnected")
            return SimpleNamespace(is_success=True, result_digest=digest("dispatch-result"))

    governance = Governance()
    authorization = SimpleNamespace(actor_id=uid(11), plan_id=uid(70))
    governance.authorizations = SimpleNamespace(get=lambda _: authorization)  # type: ignore[attr-defined]

    def require(*_: Any, **__: Any) -> Any:
        governance.consumptions += 1
        return SimpleNamespace(
            id=uid(12),
            authorization_digest=digest("authorization"),
            scope=SimpleNamespace(body=lambda: {"scope": "resume"}),
        )

    governance.require_authorized = require  # type: ignore[attr-defined]
    repositories = {
        "resume_apply": Repository,
        "agent_assignment": Assignments,
        "resume": lambda *_args, **_kwargs: object(),
        "job": Jobs,
    }
    monkeypatch.setattr(
        apply_module,
        "legacy_repository",
        lambda kind, *args, **kwargs: repositories[kind](*args, **kwargs),
    )
    monkeypatch.setattr(apply_module, "ResumeCoordinator", Coordinator)
    monkeypatch.setattr(apply_module, "ExecutionHost", Host)
    monkeypatch.setattr(apply_module, "CanonicalAgentDispatchService", Dispatch)
    return state, governance


def test_fresh_apply_dispatches_once_and_replay_never_dispatches_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime.now(dt.UTC)
    prepared = plan(now)
    state, governance = _install_apply_fakes(monkeypatch, prepared)
    adapter = Adapter()
    request = ResumeApplyRequest(
        prepared, prepared.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    service = ResumeApplyService(Connection(), governance, EnvironmentGuard())  # type: ignore[arg-type]
    first = service.apply(request, adapter, cwd=tmp_path, timeout_seconds=10, now=now)
    replay = service.apply(request, adapter, cwd=tmp_path, timeout_seconds=10, now=now)
    assert first.state is ResumeApplyState.COMPLETED
    assert replay.event_digest == first.event_digest
    assert state["claims"] == 1
    assert state["dispatches"] == 1
    assert governance.consumptions == 1
    assert state["finishes"] == [AttemptOutcome.SUCCEEDED]


def test_live_environment_drift_blocks_authority_claim_and_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime.now(dt.UTC)
    prepared = plan(now)
    state, governance = _install_apply_fakes(monkeypatch, prepared)
    guard = EnvironmentGuard(PolicyViolation("environment.network-policy-drift"))
    request = ResumeApplyRequest(
        prepared, prepared.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="network-policy-drift"):
        ResumeApplyService(Connection(), governance, guard).apply(  # type: ignore[arg-type]
            request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now
        )
    assert len(guard.calls) == 1
    assert governance.consumptions == 0
    assert state["claims"] == 0
    assert state["dispatches"] == 0


def test_expired_nonterminal_replay_becomes_recovery_without_redispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime.now(dt.UTC)
    prepared = plan(now)
    state, governance = _install_apply_fakes(monkeypatch, prepared)
    claim = ResumeApplyEvent(
        apply_id=uid(30),
        sequence=1,
        phase=ResumeApplyPhase.CLAIM,
        state=ResumeApplyState.CLAIMED,
        reason_code="resume.exact-claim-created",
        occurred_at=now,
        attempt_id=uid(60),
        lease_id=uid(61),
        fencing_token=2,
        claim_id=uid(62),
    )
    state["created"] = True
    state["events"] = [claim]
    state["lease_live"] = False
    request = ResumeApplyRequest(
        prepared, prepared.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    result = ResumeApplyService(Connection(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
        request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now
    )
    assert result.state is ResumeApplyState.RECOVERY_REQUIRED
    assert result.reprepare_required
    assert state["dispatches"] == 0
    assert state["claims"] == 0
    assert governance.consumptions == 0


def test_live_nonterminal_replay_is_observed_without_redispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime.now(dt.UTC)
    prepared = plan(now)
    state, governance = _install_apply_fakes(monkeypatch, prepared)
    claim = ResumeApplyEvent(
        apply_id=uid(30),
        sequence=1,
        phase=ResumeApplyPhase.CLAIM,
        state=ResumeApplyState.CLAIMED,
        reason_code="resume.exact-claim-created",
        occurred_at=now,
        attempt_id=uid(60),
        lease_id=uid(61),
        fencing_token=2,
        claim_id=uid(62),
    )
    state["created"] = True
    state["events"] = [claim]
    request = ResumeApplyRequest(
        prepared, prepared.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    result = ResumeApplyService(Connection(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
        request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now
    )
    assert result.state is ResumeApplyState.CLAIMED
    assert len(state["events"]) == 1
    assert state["dispatches"] == 0
    assert governance.consumptions == 0


def test_adapter_exception_is_durable_recovery_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = dt.datetime.now(dt.UTC)
    prepared = plan(now)
    state, governance = _install_apply_fakes(monkeypatch, prepared, adapter_raises=True)
    request = ResumeApplyRequest(
        prepared, prepared.plan_digest, uid(11), uid(12), "worker", ("database.write",)
    )
    result = ResumeApplyService(Connection(), governance, EnvironmentGuard()).apply(  # type: ignore[arg-type]
        request, Adapter(), cwd=tmp_path, timeout_seconds=10, now=now
    )
    assert result.state is ResumeApplyState.RECOVERY_REQUIRED
    assert result.reprepare_required
    assert [event.state for event in state["events"]] == [
        ResumeApplyState.CLAIMED,
        ResumeApplyState.DISPATCHED,
        ResumeApplyState.RECOVERY_REQUIRED,
    ]
    assert state["finishes"] == [AttemptOutcome.RECOVERY_REQUIRED]
