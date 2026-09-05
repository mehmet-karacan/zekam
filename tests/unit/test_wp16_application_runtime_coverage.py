from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

from zekam.application.doctor_repair import (
    DatabaseMigrationPlan,
    DatabaseRoutinePlan,
    DoctorRepairPlan,
    GitFastForwardPlan,
    GitRepositoryState,
    apply_git_fast_forward,
    build_doctor_repair_plan,
)
from zekam.application.doctor_repair_runtime import apply_doctor_repair_with_runtime
from zekam.application.governance import (
    EffectRequest,
    GovernanceService,
    GovernanceVerdict,
    ProviderGate,
    default_capabilities,
    registered_capability_names,
)
from zekam.application.measured_loop_runtime import (
    LocalMeasuredLoopDriver,
    PinnedLocalDriverSpec,
    load_local_driver_config,
    measured_loop_driver_digest,
)
from zekam.application.worker import (
    ShutdownSignal,
    TickResult,
    Worker,
    WorkerSettings,
    resolve_handlers,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.policy import (
    GateDecision,
    GateOutcome,
    GateResult,
    PolicyDocument,
    PolicyRule,
    RiskAssessment,
    RiskLevel,
)
from zekam.domain.realm import Realm
from zekam.domain.runtime import AttemptOutcome
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    OutboundRequest,
    OutboundState,
)
from zekam.domain.work import EffectKind

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 12))


def _driver(path: Path) -> PinnedLocalDriverSpec:
    return PinnedLocalDriverSpec((str(path), "--mode", "test"), digest_of_bytes(path.read_bytes()))


def test_measured_loop_pinned_driver_real_file_and_digest_contract(tmp_path: Path) -> None:
    executable = tmp_path / "native-driver"
    executable.write_bytes(b"native-driver-v1")
    builder = _driver(executable)
    verifier = PinnedLocalDriverSpec((str(executable), "--verify"), builder.executable_digest)
    assert measured_loop_driver_digest(builder, verifier) == measured_loop_driver_digest(
        builder, verifier
    )
    runtime = LocalMeasuredLoopDriver(object(), IDS[0], builder, verifier, timeout_seconds=2.5)
    assert runtime.lease_seconds == 35
    with pytest.raises(ValidationFailed, match="exact argv"):
        replace(builder, argv=())
    with pytest.raises(ValidationFailed, match="exact argv"):
        replace(builder, argv=(str(executable), ""))
    with pytest.raises(PolicyViolation, match="absolute path"):
        replace(builder, argv=("relative-driver",))
    with pytest.raises(PolicyViolation, match="SHA-256 drift"):
        replace(builder, executable_digest=digest("wrong"))
    with pytest.raises(PolicyViolation, match="okunamadi"):
        PinnedLocalDriverSpec((str(tmp_path / "missing"),), digest("missing"))


def test_measured_loop_rejects_shell_and_versioned_interpreter(tmp_path: Path) -> None:
    for name in ("bash", "python3.12", "driver.py"):
        executable = tmp_path / name
        executable.write_bytes(b"not-an-accepted-native")
        with pytest.raises(PolicyViolation, match="shell/interpreter"):
            _driver(executable)


def _driver_config(builder: Path, verifier: Path, **changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-measured-loop-local-drivers/v2",
        "builder_argv": [str(builder), "build"],
        "builder_executable_sha256": digest_of_bytes(builder.read_bytes()),
        "verifier_argv": [str(verifier), "verify"],
        "verifier_executable_sha256": digest_of_bytes(verifier.read_bytes()),
        "timeout_seconds": 12,
        "network_allowed": False,
    }
    body.update(changes)
    return body


def test_measured_loop_driver_config_happy_and_boundary_matrix(tmp_path: Path) -> None:
    builder = tmp_path / "builder-native"
    verifier = tmp_path / "verifier-native"
    builder.write_bytes(b"builder")
    verifier.write_bytes(b"verifier")
    config = tmp_path / "drivers.json"
    config.write_text(json.dumps(_driver_config(builder, verifier)), encoding="utf-8")
    built, verified, timeout = load_local_driver_config(config)
    assert built.argv[-1] == "build" and verified.argv[-1] == "verify" and timeout == 12

    invalid_documents: tuple[object, ...] = (
        [],
        _driver_config(builder, verifier, extra=True),
        _driver_config(builder, verifier, schema="wrong"),
        _driver_config(builder, verifier, network_allowed=True),
        _driver_config(builder, verifier, builder_argv=[]),
        _driver_config(builder, verifier, verifier_argv=[1]),
        _driver_config(builder, verifier, timeout_seconds="bad"),
        _driver_config(builder, verifier, timeout_seconds=0),
        _driver_config(builder, verifier, timeout_seconds=301),
        _driver_config(
            builder,
            verifier,
            verifier_argv=[str(builder), "build"],
            verifier_executable_sha256=digest_of_bytes(builder.read_bytes()),
        ),
    )
    for index, body in enumerate(invalid_documents):
        candidate = tmp_path / f"invalid-{index}.json"
        candidate.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises((PolicyViolation, ValidationFailed)):
            load_local_driver_config(candidate)
    for raw in ("{", "\ud800"):
        candidate = tmp_path / f"bad-{len(raw)}.json"
        candidate.write_text(raw, encoding="utf-8", errors="surrogatepass")
        with pytest.raises(ValidationFailed, match="config okunamadi"):
            load_local_driver_config(candidate)
    with pytest.raises(ValidationFailed, match="config okunamadi"):
        load_local_driver_config(tmp_path / "missing.json")


def _effect() -> EffectRequest:
    return EffectRequest(
        "provider-call",
        (EffectKind.NETWORK_CALL,),
        ("provider:endpoint:invoke",),
        (DataClassification.INTERNAL,),
        ("provider",),
        touches_external_system=True,
        required_capabilities=("provider.call",),
    )


def test_governance_effect_and_verdict_public_contracts() -> None:
    request = _effect()
    assert request.effect_digest == _effect().effect_digest
    assert request.body()["provider_refs"] == ["provider"]
    risk = RiskAssessment(RiskLevel.MEDIUM, ("external",), True, False)
    allowed = GateResult((GateDecision("policy", GateOutcome.ALLOW, "ok"),))
    denied = GateResult((GateDecision("policy", GateOutcome.DENY, "blocked"),))
    allow_verdict = GovernanceVerdict(request, risk, allowed, False)
    deny_verdict = GovernanceVerdict(request, risk, denied, False)
    assert allow_verdict.allowed and allow_verdict.denial_reason is None
    assert not deny_verdict.allowed and deny_verdict.denial_reason == "blocked"
    assert deny_verdict.as_dict()["authorization_id"] is None
    capabilities = default_capabilities(IDS[0])
    names = registered_capability_names(tuple(reversed(capabilities)))
    assert names == tuple(sorted(names)) and len(names) == 7


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def record(self, row: object = None, **kwargs: object) -> None:
        self.rows.append(row if row is not None else kwargs)


class _CurrentRepository(_Recorder):
    def __init__(self, values: dict[str, object] | None = None) -> None:
        super().__init__()
        self.values = values or {}

    def current(self, name: str) -> object | None:
        return self.values.get(name)


class _TestGovernance(GovernanceService):
    def __init__(self, capability_values: dict[str, object]) -> None:
        super().__init__(object(), Realm.create(now=NOW), IDS[1], IDS[2])
        object.__setattr__(self, "_capability_repo", _CurrentRepository(capability_values))
        object.__setattr__(self, "_audit_repo", _Recorder())

    @property
    def capabilities(self) -> _CurrentRepository:
        return self._capability_repo  # type: ignore[attr-defined,no-any-return]

    @property
    def audit(self) -> _Recorder:
        return self._audit_repo  # type: ignore[attr-defined,no-any-return]


def _policy(*, allow: bool = True, max_risk: RiskLevel = RiskLevel.CRITICAL) -> PolicyDocument:
    return PolicyDocument.create(
        realm_id=IDS[0],
        name="coverage",
        revision=1,
        rules=(
            PolicyRule(
                "network",
                (EffectKind.NETWORK_CALL,),
                allow,
                max_risk,
                ("provider:*",),
            ),
        ),
        now=NOW,
    )


def test_governance_public_evaluate_full_gate_matrix() -> None:
    request = _effect()
    missing = _TestGovernance({}).evaluate(request, policy=_policy(), now=NOW)
    assert missing.denial_reason == "capability-missing:provider.call"
    service = _TestGovernance({"provider.call": object()})
    denied = service.evaluate(request, policy=_policy(allow=False), now=NOW)
    assert denied.denial_reason is not None and denied.denial_reason.startswith("policy-denies")
    outside = replace(request, resources=("other",))
    assert "resource-out-of-policy" in str(
        service.evaluate(outside, policy=_policy(), now=NOW).denial_reason
    )
    low = service.evaluate(request, policy=_policy(max_risk=RiskLevel.LOW), now=NOW)
    assert low.denial_reason is not None and low.denial_reason.startswith("risk-above")
    no_authority = service.evaluate(request, policy=_policy(), now=NOW)
    assert no_authority.denial_reason == "authorization-required"
    authorization = Authorization.issue(
        realm_id=service.realm.id,
        actor_id=IDS[1],
        plan_digest=digest("plan"),
        effect_digest=request.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=request.resources,
            allowed_effects=(EffectKind.NETWORK_CALL.value,),
            provider_refs=request.provider_refs,
            data_classifications=request.data_classifications,
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=1),
        now=NOW,
    )
    allowed = service.evaluate(request, authorization=authorization, policy=_policy(), now=NOW)
    assert (
        allowed.allowed and [item.gate for item in allowed.gates.decisions][-1] == "authorization"
    )
    expired = service.evaluate(
        request,
        authorization=authorization,
        policy=_policy(),
        now=NOW + dt.timedelta(minutes=2),
    )
    assert expired.denial_reason == "authorization-expired"
    assert len(service.audit.rows) == 6


def _governance() -> SimpleNamespace:
    return SimpleNamespace(
        outbound=_Recorder(),
        audit=_Recorder(),
        actor_id=IDS[1],
        correlation_id=IDS[2],
    )


def _outbound(*categories: DataClassification) -> OutboundRequest:
    return OutboundRequest.prepare(
        realm_id=IDS[0],
        provider_ref="provider",
        endpoint_ref="endpoint",
        operation="invoke",
        payload_digest=digest("payload"),
        request_identity="request/one",
        data_categories=categories,
        now=NOW,
    )


def _authorization(request: OutboundRequest) -> Authorization:
    return Authorization.issue(
        realm_id=IDS[0],
        actor_id=IDS[1],
        plan_digest=digest("plan"),
        effect_digest=digest("effect"),
        scope=AuthorizationScope(
            allowed_resources=(request.target,),
            allowed_effects=(EffectKind.NETWORK_CALL.value,),
            provider_refs=(request.provider_ref,),
            data_classifications=(DataClassification.INTERNAL,),
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )


def test_provider_gate_prepare_apply_and_failure_paths() -> None:
    governance = _governance()
    gate = ProviderGate(governance)  # type: ignore[arg-type]
    request = _outbound()
    assert gate.prepare(request, now=NOW).state is OutboundState.PREPARED
    approved = gate.apply(request, authorization=_authorization(request), now=NOW)
    assert approved.state is OutboundState.APPROVED and approved.authorization_id is not None
    blocked = _outbound(DataClassification.SECRET)
    denied = gate.prepare(blocked, now=NOW)
    assert denied.state is OutboundState.DENIED
    with pytest.raises(PolicyViolation, match="forbidden-data-class"):
        gate.apply(blocked, authorization=_authorization(request), now=NOW)
    wrong_provider = replace(request, provider_ref="other")
    with pytest.raises(PolicyViolation, match="provider-out-of-scope"):
        gate.apply(wrong_provider, authorization=_authorization(request), now=NOW)
    wrong_endpoint = replace(request, endpoint_ref="other")
    with pytest.raises(PolicyViolation, match="endpoint-out-of-scope"):
        gate.apply(wrong_endpoint, authorization=_authorization(request), now=NOW)
    review = _outbound(DataClassification.RESTRICTED)
    authorization = _authorization(request)
    empty_scope = replace(
        authorization,
        scope=replace(authorization.scope, data_classifications=()),
    )
    with pytest.raises(PolicyViolation, match="data-class-not-reviewed"):
        gate.apply(review, authorization=empty_scope, now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_label": ""},
        {"capabilities": ()},
        {"poll_seconds": 0},
        {"lease_seconds": 0},
    ],
)
def test_worker_settings_fail_closed(changes: dict[str, object]) -> None:
    settings = WorkerSettings("worker", ("read",))
    with pytest.raises(PolicyViolation):
        replace(settings, **changes)  # type: ignore[arg-type]


def test_worker_result_shutdown_and_registry_replay_contracts() -> None:
    result = TickResult(True, ("scheduled",), None, IDS[3], AttemptOutcome.SUCCEEDED)
    assert result.as_dict()["outcome"] == "succeeded"
    empty = TickResult(False, skipped_reason="empty")
    assert empty.as_dict()["job_id"] is None
    shutdown = ShutdownSignal()
    shutdown.request("test")
    assert shutdown.requested and shutdown.reason == "test"

    def handler(work: object) -> str:
        return str(work)

    assert resolve_handlers(("read", "read"), registry={"read": handler}) == {"read": handler}
    with pytest.raises(PolicyViolation, match="handler tanimsiz"):
        resolve_handlers(("missing",), registry={"read": handler})


def test_worker_dry_run_capacity_and_schedule_are_non_mutating() -> None:
    worker = Worker(Mock(), WorkerSettings("coverage", ("read",), max_queue_depth=1, max_workers=1))
    assert "kuyruk derinligi" in str(worker.plan(now=NOW, queue_depth=2).skipped_reason)
    worker._active = 1
    assert "worker kapasitesi" in str(worker.plan(now=NOW).skipped_reason)
    worker._active = 0
    worker.consume_queue = False
    assert worker.plan(now=NOW, queue_depth=999).skipped_reason == "dry-run: hicbir sey yazilmadi"
    worker.cancel(IDS[4], now=NOW, force=True)
    assert worker.cancellations[IDS[4]].force


def _git_state(*, behind: int = 0, dirty: tuple[str, ...] = ()) -> GitRepositoryState:
    return GitRepositoryState(
        Path("/tmp/repository"),
        "main",
        "a" * 40,
        "origin/main",
        "refs/remotes/origin/main",
        "b" * 40,
        "origin",
        "main",
        0,
        behind,
        dirty,
    )


@dataclass(frozen=True)
class _Migration:
    version: int = 4
    name: str = "local"
    checksum: str = digest("migration")
    has_down: bool = False

    @property
    def label(self) -> str:
        return f"{self.version}:{self.name}"


@dataclass(frozen=True)
class _MigrationStatus:
    head: int = 3
    applied: tuple[object, ...] = ()
    pending: tuple[_Migration, ...] = (_Migration(),)
    drift: tuple[object, ...] = ()


@dataclass(frozen=True)
class _RoutineStatus:
    missing: tuple[object, ...] = ("routine",)
    migration_drift: tuple[object, ...] = ()
    migration_pending: tuple[object, ...] = ()
    repair_plan_digest: str = digest("routine-plan")
    migration_head: int = 3

    def as_dict(self) -> dict[str, object]:
        return {"missing": list(self.missing), "head": self.migration_head}


def _doctor_plan(step: str | None) -> DoctorRepairPlan:
    git = GitFastForwardPlan(_git_state(behind=1), (), step == "git-fast-forward")
    migrations = (
        DatabaseMigrationPlan(cast(Any, _MigrationStatus()), ())
        if step == "postgres-migration-upgrade"
        else None
    )
    routines = (
        DatabaseRoutinePlan(cast(Any, _RoutineStatus()), ())
        if step == "postgres-routine-repair"
        else None
    )
    return DoctorRepairPlan(git, migrations, routines)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ("git-fast-forward", ("git-fast-forward",)),
        ("postgres-migration-upgrade", ("postgres-migration-upgrade",)),
        ("postgres-routine-repair", ("postgres-routine-repair",)),
        (None, ()),
    ],
)
def test_doctor_plan_public_step_matrix(step: str | None, expected: tuple[str, ...]) -> None:
    plan = _doctor_plan(step)
    assert plan.required_steps == expected
    assert plan.next_step == step
    assert plan.as_dict()["applicable"] is bool(expected)


def test_doctor_plan_blocking_noop_and_runtime_early_guards() -> None:
    state = _git_state()
    noop = GitFastForwardPlan(state, (), False)
    result = apply_git_fast_forward(state.root, plan=noop, plan_digest=noop.plan_digest)
    assert not result.changed and result.as_dict()["verified"]
    blocked = DoctorRepairPlan(
        GitFastForwardPlan(_git_state(behind=1), ("dirty", "dirty"), True),
        None,
        None,
    )
    assert blocked.blocked_reasons == ("dirty",)
    with pytest.raises(PolicyViolation, match="digest exact"):
        apply_doctor_repair_with_runtime(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            repair_plan=blocked,
            plan_digest=digest("wrong"),
            actor_id=IDS[0],
            project_id=IDS[1],
        )


def test_doctor_real_temp_git_observation_and_database_plan_branches(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "coverage@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Coverage"], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    plan = build_doctor_repair_plan(core_path=tmp_path)
    assert not plan.git.required and plan.git.blocked_reasons == (
        "upstream-missing",
        "upstream-remote-unresolved",
    )
    tracked.write_text("dirty\n", encoding="utf-8")
    dirty = build_doctor_repair_plan(core_path=tmp_path)
    assert "worktree-dirty" in dirty.git.blocked_reasons

    status = _MigrationStatus(drift=(SimpleNamespace(),))
    routine = _RoutineStatus(
        migration_drift=(SimpleNamespace(),),
        migration_pending=(SimpleNamespace(),),
    )
    database = build_doctor_repair_plan(
        core_path=tmp_path,
        connection=object(),
        migration_status_reader=lambda *_: status,  # type: ignore[arg-type]
        routine_status_reader=lambda *_: routine,  # type: ignore[arg-type]
    )
    assert database.migrations is not None
    assert "migration-drift" in database.migrations.blocked_reasons
    assert database.routines is not None
    assert set(database.routines.blocked_reasons) == {"migration-drift", "migration-pending"}
    with pytest.raises(PolicyViolation, match="status adapterlari"):
        build_doctor_repair_plan(core_path=tmp_path, connection=object())
    blocked = DoctorRepairPlan(
        GitFastForwardPlan(_git_state(behind=1), ("dirty",), True),
        None,
        None,
    )
    empty = _doctor_plan(None)
    with pytest.raises(PolicyViolation, match="uygulanacak adim yok"):
        apply_doctor_repair_with_runtime(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            repair_plan=empty,
            plan_digest=empty.plan_digest,
            actor_id=IDS[0],
            project_id=IDS[1],
        )
    with pytest.raises(PolicyViolation, match="Doctor repair bloke"):
        apply_doctor_repair_with_runtime(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            repair_plan=blocked,
            plan_digest=blocked.plan_digest,
            actor_id=IDS[0],
            project_id=IDS[1],
        )
