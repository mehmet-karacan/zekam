"""Immutable execution environment and per-turn binding contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class EnvironmentDriftDimension(StrEnum):
    WORKSPACE_ROOTS = "environment.workspace-roots-drift"
    SHELL = "environment.shell-drift"
    PERMISSION_PROFILE = "environment.permission-profile-drift"
    FILESYSTEM_POLICY = "environment.filesystem-policy-drift"
    NETWORK_POLICY = "environment.network-policy-drift"
    TOOL_RUNTIME = "environment.tool-runtime-drift"
    CAPABILITY = "environment.capability-drift"
    CONFIG = "environment.config-drift"
    SOURCE_REVISION = "environment.source-revision-drift"


@dataclass(frozen=True, slots=True)
class ShellSnapshot:
    kind: str
    binary_digest: str
    startup_profile_digest: str

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValidationFailed("Shell kind bos olamaz")
        parse_digest(self.binary_digest)
        parse_digest(self.startup_profile_digest)

    def body(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "binary_digest": self.binary_digest,
            "startup_profile_digest": self.startup_profile_digest,
        }


@dataclass(frozen=True, slots=True)
class ExecutionEnvironmentSnapshot:
    id: UUID
    realm_id: UUID
    environment_id: str
    execution_identity: str
    provider: str
    platform: str
    executor_protocol_version: str
    cwd_locator: str
    workspace_roots: tuple[str, ...]
    shell: ShellSnapshot
    permission_profile_id: str
    permission_profile_digest: str
    filesystem_policy_digest: str
    network_policy_digest: str
    tool_runtime_digest: str
    capability_digest: str
    config_effective_digest: str
    source_revision: str
    captured_at: dt.datetime
    expires_at: dt.datetime
    snapshot_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Environment snapshot authority uretemez")
        if self.captured_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValidationFailed("Environment snapshot zamanlari timezone-aware olmali")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Environment snapshot expiry capture sonrasinda olmali")
        text_fields = (
            self.environment_id,
            self.execution_identity,
            self.provider,
            self.platform,
            self.executor_protocol_version,
            self.cwd_locator,
            self.permission_profile_id,
            self.source_revision,
        )
        if any(not value.strip() for value in text_fields):
            raise ValidationFailed("Environment snapshot kimlikleri bos olamaz")
        if not self.workspace_roots or len(set(self.workspace_roots)) != len(self.workspace_roots):
            raise ValidationFailed("Workspace roots bos veya tekrarli olamaz")
        if tuple(sorted(self.workspace_roots)) != self.workspace_roots:
            raise ValidationFailed("Workspace roots canonical sirali olmali")
        for locator in (self.cwd_locator, *self.workspace_roots):
            _assert_native_locator(locator)
        if self.cwd_locator not in self.workspace_roots:
            raise ValidationFailed("cwd locator workspace roots icinde olmali")
        for value in (
            self.permission_profile_digest,
            self.filesystem_policy_digest,
            self.network_policy_digest,
            self.tool_runtime_digest,
            self.capability_digest,
            self.config_effective_digest,
        ):
            parse_digest(value)
        if self.snapshot_digest:
            parse_digest(self.snapshot_digest)
            if self.snapshot_digest != self.computed_digest:
                raise PolicyViolation("Environment snapshot supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-execution-environment-snapshot/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "environment_id": self.environment_id,
            "execution_identity": self.execution_identity,
            "provider": self.provider,
            "platform": self.platform,
            "executor_protocol_version": self.executor_protocol_version,
            "cwd_locator": self.cwd_locator,
            "workspace_roots": list(self.workspace_roots),
            "shell": self.shell.body(),
            "permission_profile_id": self.permission_profile_id,
            "permission_profile_digest": self.permission_profile_digest,
            "filesystem_policy_digest": self.filesystem_policy_digest,
            "network_policy_digest": self.network_policy_digest,
            "tool_runtime_digest": self.tool_runtime_digest,
            "capability_digest": self.capability_digest,
            "config_effective_digest": self.config_effective_digest,
            "source_revision": self.source_revision,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ExecutionEnvironmentSnapshot:
        return cast(ExecutionEnvironmentSnapshot, _create_bound(cls, "snapshot_digest", values))


@dataclass(frozen=True, slots=True)
class EnvironmentDriftReport:
    sticky_snapshot_digest: str
    current_snapshot_digest: str
    dimensions: tuple[EnvironmentDriftDimension, ...]
    checked_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.sticky_snapshot_digest)
        parse_digest(self.current_snapshot_digest)
        if self.checked_at.tzinfo is None:
            raise ValidationFailed("Drift report zamani timezone-aware olmali")
        if tuple(sorted(set(self.dimensions), key=str)) != self.dimensions:
            raise ValidationFailed("Drift dimensions unique ve canonical sirali olmali")

    @property
    def is_current(self) -> bool:
        return not self.dimensions

    def assert_current(self) -> None:
        if self.dimensions:
            reasons = ",".join(item.value for item in self.dimensions)
            raise PolicyViolation(f"Execution environment stale: {reasons}")


@dataclass(frozen=True, slots=True)
class AssignmentEnvironmentBinding:
    id: UUID
    realm_id: UUID
    assignment_id: UUID
    execution_environment_snapshot_digest: str
    bound_at: dt.datetime
    binding_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Assignment environment binding authority uretemez")
        if self.bound_at.tzinfo is None:
            raise ValidationFailed("Assignment environment binding zamani timezone-aware olmali")
        parse_digest(self.execution_environment_snapshot_digest)
        if self.binding_digest:
            parse_digest(self.binding_digest)
            if self.binding_digest != self.computed_digest:
                raise PolicyViolation("Assignment environment binding supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-assignment-environment-binding/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "assignment_id": str(self.assignment_id),
            "execution_environment_snapshot_digest": self.execution_environment_snapshot_digest,
            "bound_at": self.bound_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> AssignmentEnvironmentBinding:
        return cast(AssignmentEnvironmentBinding, _create_bound(cls, "binding_digest", values))


@dataclass(frozen=True, slots=True)
class TurnExecutionSnapshot:
    id: UUID
    realm_id: UUID
    assignment_id: UUID
    run_id: UUID
    attempt_id: UUID
    client_session_id: str
    turn_id: str
    model_id: str
    provider_id: str
    route_decision_digest: str
    reasoning_profile_digest: str
    execution_environment_snapshot_digest: str
    context_manifest_digest: str
    exposed_tool_set_digest: str
    hook_set_digest: str
    config_effective_digest: str
    created_at: dt.datetime
    turn_snapshot_digest: str
    trace_id: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Turn execution snapshot authority uretemez")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Turn execution snapshot zamani timezone-aware olmali")
        if any(
            not value.strip()
            for value in (
                self.client_session_id,
                self.turn_id,
                self.model_id,
                self.provider_id,
            )
        ):
            raise ValidationFailed("Turn execution snapshot kimlikleri bos olamaz")
        if self.trace_id is not None and not self.trace_id.strip():
            raise ValidationFailed("Trace id bos olamaz")
        for value in (
            self.route_decision_digest,
            self.reasoning_profile_digest,
            self.execution_environment_snapshot_digest,
            self.context_manifest_digest,
            self.exposed_tool_set_digest,
            self.hook_set_digest,
            self.config_effective_digest,
        ):
            parse_digest(value)
        if self.turn_snapshot_digest:
            parse_digest(self.turn_snapshot_digest)
            if self.turn_snapshot_digest != self.computed_digest:
                raise PolicyViolation("Turn execution snapshot supplied digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-turn-execution-snapshot/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "assignment_id": str(self.assignment_id),
            "run_id": str(self.run_id),
            "attempt_id": str(self.attempt_id),
            "client_session_id": self.client_session_id,
            "turn_id": self.turn_id,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "route_decision_digest": self.route_decision_digest,
            "reasoning_profile_digest": self.reasoning_profile_digest,
            "execution_environment_snapshot_digest": self.execution_environment_snapshot_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "exposed_tool_set_digest": self.exposed_tool_set_digest,
            "hook_set_digest": self.hook_set_digest,
            "config_effective_digest": self.config_effective_digest,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> TurnExecutionSnapshot:
        return cast(TurnExecutionSnapshot, _create_bound(cls, "turn_snapshot_digest", values))


def detect_environment_drift(
    sticky: ExecutionEnvironmentSnapshot,
    current: ExecutionEnvironmentSnapshot,
    *,
    checked_at: dt.datetime,
) -> EnvironmentDriftReport:
    if (
        sticky.realm_id != current.realm_id
        or sticky.execution_identity != current.execution_identity
    ):
        raise PolicyViolation("Probe farkli realm/execution identity dondurdu")
    if (
        sticky.id == current.id
        or current.captured_at > checked_at
        or current.expires_at <= checked_at
        or current.captured_at < checked_at - dt.timedelta(minutes=5)
    ):
        raise PolicyViolation("Force probe current snapshot temporal provenance gecersiz")
    checks = (
        (
            EnvironmentDriftDimension.WORKSPACE_ROOTS,
            sticky.workspace_roots,
            current.workspace_roots,
        ),
        (EnvironmentDriftDimension.SHELL, sticky.shell, current.shell),
        (
            EnvironmentDriftDimension.PERMISSION_PROFILE,
            sticky.permission_profile_digest,
            current.permission_profile_digest,
        ),
        (
            EnvironmentDriftDimension.FILESYSTEM_POLICY,
            sticky.filesystem_policy_digest,
            current.filesystem_policy_digest,
        ),
        (
            EnvironmentDriftDimension.NETWORK_POLICY,
            sticky.network_policy_digest,
            current.network_policy_digest,
        ),
        (
            EnvironmentDriftDimension.TOOL_RUNTIME,
            sticky.tool_runtime_digest,
            current.tool_runtime_digest,
        ),
        (
            EnvironmentDriftDimension.CAPABILITY,
            (
                sticky.capability_digest,
                sticky.environment_id,
                sticky.provider,
                sticky.platform,
                sticky.executor_protocol_version,
                sticky.cwd_locator,
            ),
            (
                current.capability_digest,
                current.environment_id,
                current.provider,
                current.platform,
                current.executor_protocol_version,
                current.cwd_locator,
            ),
        ),
        (
            EnvironmentDriftDimension.CONFIG,
            sticky.config_effective_digest,
            current.config_effective_digest,
        ),
        (
            EnvironmentDriftDimension.SOURCE_REVISION,
            sticky.source_revision,
            current.source_revision,
        ),
    )
    dimensions = tuple(sorted((kind for kind, old, new in checks if old != new), key=str))
    return EnvironmentDriftReport(
        sticky.snapshot_digest, current.snapshot_digest, dimensions, checked_at
    )


def reprobe_snapshot(
    sticky: ExecutionEnvironmentSnapshot,
    *,
    captured_at: dt.datetime,
    expires_at: dt.datetime,
    capability_digest: str | None = None,
) -> ExecutionEnvironmentSnapshot:
    """Create a distinct current observation with the same semantic dimensions."""

    return ExecutionEnvironmentSnapshot.create(
        realm_id=sticky.realm_id,
        environment_id=sticky.environment_id,
        execution_identity=sticky.execution_identity,
        provider=sticky.provider,
        platform=sticky.platform,
        executor_protocol_version=sticky.executor_protocol_version,
        cwd_locator=sticky.cwd_locator,
        workspace_roots=sticky.workspace_roots,
        shell=sticky.shell,
        permission_profile_id=sticky.permission_profile_id,
        permission_profile_digest=sticky.permission_profile_digest,
        filesystem_policy_digest=sticky.filesystem_policy_digest,
        network_policy_digest=sticky.network_policy_digest,
        tool_runtime_digest=sticky.tool_runtime_digest,
        capability_digest=capability_digest or sticky.capability_digest,
        config_effective_digest=sticky.config_effective_digest,
        source_revision=sticky.source_revision,
        captured_at=captured_at,
        expires_at=expires_at,
    )


def _assert_native_locator(value: str) -> None:
    if not value.startswith("workspace:") or "\\" in value:
        raise ValidationFailed("Path kanonik environment-native workspace locator olmali")
    logical = value.removeprefix("workspace:")
    segments = logical.split("/")
    if (
        not logical
        or logical.startswith("/")
        or any(not segment or segment in {".", ".."} or ":" in segment for segment in segments)
        or any(not all(char.isalnum() or char in "._-" for char in segment) for segment in segments)
    ):
        raise ValidationFailed("Path kanonik environment-native workspace locator olmali")


def _create_bound(cls: Any, digest_field: str, values: dict[str, Any]) -> Any:
    item = cls(id=values.pop("id", uuid4()), **{digest_field: ""}, **values)
    fields = {
        name: getattr(item, name) for name in item.__dataclass_fields__ if name != digest_field
    }
    return cls(**fields, **{digest_field: item.computed_digest})
