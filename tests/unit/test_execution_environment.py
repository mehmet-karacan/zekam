from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.environment_snapshot_service import (
    BoundEnvironmentEffectGuard,
    EnvironmentSnapshotService,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    EnvironmentDriftDimension,
    ExecutionEnvironmentSnapshot,
    ShellSnapshot,
    TurnExecutionSnapshot,
    detect_environment_drift,
)


class Store:
    def __init__(self) -> None:
        self.items: list[ExecutionEnvironmentSnapshot] = []

    def create_environment_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        self.items.append(snapshot)
        return snapshot.id, True

    def record_environment_probe(self, report):  # type: ignore[no-untyped-def]
        return uuid4(), True

    def environment_for_envelope(self, _envelope_id):  # type: ignore[no-untyped-def]
        return self.items[0]


class Probe:
    def __init__(self, snapshots: list[ExecutionEnvironmentSnapshot]) -> None:
        self.snapshots = snapshots
        self.forces: list[bool] = []

    def probe(self, execution_identity: str, *, force: bool) -> ExecutionEnvironmentSnapshot:
        self.forces.append(force)
        snapshot = self.snapshots.pop(0)
        assert snapshot.execution_identity == execution_identity
        return snapshot


def environment(**changes: object) -> ExecutionEnvironmentSnapshot:
    now = dt.datetime.now(dt.UTC)
    values: dict[str, object] = {
        "realm_id": uuid4(),
        "environment_id": "env-local-1",
        "execution_identity": "executor-1",
        "provider": "local-process",
        "platform": "windows-amd64",
        "executor_protocol_version": "zekam-exec/v1",
        "cwd_locator": "workspace:zekam/root",
        "workspace_roots": ("workspace:zekam/root",),
        "shell": ShellSnapshot("powershell", digest("pwsh"), digest("profile")),
        "permission_profile_id": "workspace-write-no-network",
        "permission_profile_digest": digest("permission"),
        "filesystem_policy_digest": digest("filesystem"),
        "network_policy_digest": digest("network"),
        "tool_runtime_digest": digest("tools"),
        "capability_digest": digest("capability"),
        "config_effective_digest": digest("config"),
        "source_revision": "abc123",
        "captured_at": now,
        "expires_at": now + dt.timedelta(minutes=10),
    }
    values.update(changes)
    return ExecutionEnvironmentSnapshot.create(**values)


def test_snapshot_rejects_host_path_and_digest_tamper() -> None:
    item = environment()
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, tool_runtime_digest=digest("forged"))
    with pytest.raises(ValidationFailed, match="environment-native"):
        environment(cwd_locator="C:\\Users\\mkaracan\\zekam")
    for locator in ("workspace:../../secret", "workspace:C:/repo", "workspace:root//child"):
        with pytest.raises(ValidationFailed, match="environment-native"):
            environment(cwd_locator=locator, workspace_roots=(locator,))


def test_drift_has_separate_canonical_reason_codes() -> None:
    sticky = environment()
    current = environment(
        realm_id=sticky.realm_id,
        permission_profile_digest=digest("permission-v2"),
        network_policy_digest=digest("network-v2"),
        tool_runtime_digest=digest("tools-v2"),
    )
    report = detect_environment_drift(sticky, current, checked_at=dt.datetime.now(dt.UTC))
    assert report.dimensions == (
        EnvironmentDriftDimension.NETWORK_POLICY,
        EnvironmentDriftDimension.PERMISSION_PROFILE,
        EnvironmentDriftDimension.TOOL_RUNTIME,
    )
    with pytest.raises(PolicyViolation, match="network-policy-drift"):
        report.assert_current()


def test_initialize_is_sticky_but_force_probe_bypasses_cache() -> None:
    sticky = environment()
    current = environment(realm_id=sticky.realm_id)
    probe = Probe([sticky, current])
    store = Store()
    service = EnvironmentSnapshotService(probe, store)
    assert service.initialize("executor-1") is sticky
    assert service.initialize("executor-1") is sticky
    _probed, report = service.force_probe("executor-1", now=dt.datetime.now(dt.UTC))
    assert report.is_current
    assert probe.forces == [False, True]
    assert store.items == [sticky, current]


def test_dispatch_probe_fails_closed_on_drift() -> None:
    sticky = environment()
    drifted = environment(realm_id=sticky.realm_id, capability_digest=digest("changed"))
    service = EnvironmentSnapshotService(Probe([sticky, drifted]), Store())
    service.initialize("executor-1")
    with pytest.raises(PolicyViolation, match="capability-drift"):
        service.assert_dispatch_current("executor-1", now=dt.datetime.now(dt.UTC))


def test_bound_effect_guard_loads_sticky_and_forces_live_probe() -> None:
    sticky = environment()
    current = environment(realm_id=sticky.realm_id)
    store = Store()
    store.items.append(sticky)
    probe = Probe([current])
    result = BoundEnvironmentEffectGuard(probe, store).assert_envelope_current(
        uuid4(), now=dt.datetime.now(dt.UTC)
    )
    assert result is sticky
    assert probe.forces == [True]
    assert store.items == [sticky, current]


def test_turn_snapshot_binds_all_effect_dimensions() -> None:
    item = TurnExecutionSnapshot.create(
        realm_id=uuid4(),
        assignment_id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        client_session_id="session-1",
        turn_id="turn-1",
        model_id="provider/model",
        provider_id="provider",
        route_decision_digest=digest("route"),
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=digest("environment"),
        context_manifest_digest=digest("context"),
        exposed_tool_set_digest=digest("tools"),
        hook_set_digest=digest("hooks"),
        config_effective_digest=digest("config"),
        created_at=dt.datetime.now(dt.UTC),
    )
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, hook_set_digest=digest("forged"))


def test_assignment_environment_binding_is_immutable_and_authority_free() -> None:
    item = AssignmentEnvironmentBinding.create(
        realm_id=uuid4(),
        assignment_id=uuid4(),
        execution_environment_snapshot_digest=digest("environment"),
        bound_at=dt.datetime.now(dt.UTC),
    )
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(item, execution_environment_snapshot_digest=digest("forged"))
    with pytest.raises(PolicyViolation, match="authority"):
        replace(item, grants_authority=True, binding_digest="")
