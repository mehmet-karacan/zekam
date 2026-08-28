"""Deterministic, authority-free built-in lifecycle handler bundle."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid5

from zekam.application.hook_runtime import LoadedHookAdapter
from zekam.application.memory_continuity_orchestrator import plan_memory_hook
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.hook_runtime import (
    HookAdapterResult,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookResultKind,
    HookRuntimeRevision,
    HookSpecRevision,
)

MEMORY_HOOK_EVENTS: tuple[HookEventType, ...] = tuple(
    item for item in HookEventType if "_" in item.value and "." not in item.value
)
_NAMESPACE = UUID("91c54616-4154-4af5-a3a0-1ac455189a2a")
MEMORY_HOOK_REVISION = 2
_PROFILE_CREATED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_HOOK_CREATED_AT = dt.datetime(2026, 8, 28, tzinfo=dt.UTC)
_EXPIRES_AT = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)

_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lifecycle", "data"],
    "properties": {
        "lifecycle": {"type": "object"},
        "data": {},
    },
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_type",
        "accepted",
        "command",
        "command_digest",
        "grants_authority",
    ],
    "properties": {
        "event_type": {"type": "string"},
        "accepted": {"const": True},
        "command": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schema",
                "event_type",
                "event_ref",
                "event_digest",
                "actions",
                "compiler_enqueue",
                "provider_calls",
                "direct_promotion",
                "grants_authority",
            ],
            "properties": {
                "schema": {"const": "zekam-memory-learning-command/v1"},
                "event_type": {"type": "string"},
                "event_ref": {"type": "string"},
                "event_digest": {"type": "string"},
                "actions": {"type": "array", "items": {"type": "string"}},
                "compiler_enqueue": {"type": "boolean"},
                "provider_calls": {"const": 0},
                "direct_promotion": {"const": False},
                "grants_authority": {"const": False},
            },
        },
        "command_digest": {"type": "string"},
        "grants_authority": {"const": False},
    },
}
_V1_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["event_type", "accepted", "grants_authority"],
    "properties": {
        "event_type": {"type": "string"},
        "accepted": {"const": True},
        "grants_authority": {"const": False},
    },
}


@dataclass(frozen=True, slots=True)
class MemoryHookBundle:
    profile: PermissionProfileRevision
    specs: tuple[HookSpecRevision, ...]
    runtimes: tuple[HookRuntimeRevision, ...]
    adapters: tuple[LoadedHookAdapter, ...]
    bundle_digest: str


def _profile(realm_id: UUID) -> PermissionProfileRevision:
    return PermissionProfileRevision.create(
        id=uuid5(_NAMESPACE, f"{realm_id}:profile"),
        realm_id=realm_id,
        name="memory-continuity-internal",
        revision=1,
        allowed_capabilities=(),
        denied_capabilities=(
            "filesystem.read",
            "filesystem.write",
            "network.access",
            "process.run",
        ),
        managed=True,
        created_at=_PROFILE_CREATED_AT,
    )


def memory_hook_v1_identities(
    realm_id: UUID,
) -> dict[HookEventType, tuple[UUID, str, str]]:
    """Return the exact immutable v1 identities accepted for an in-place v2 upgrade."""

    profile = _profile(realm_id)
    identities: dict[HookEventType, tuple[UUID, str, str]] = {}
    for event_type in MEMORY_HOOK_EVENTS:
        hook_id = f"memory-continuity-{event_type.value}"
        adapter_ref = f"{hook_id}-v1"
        adapter_digest = digest(
            {"adapter_ref": adapter_ref, "event_type": event_type.value, "version": 1}
        )
        spec = HookSpecRevision.create(
            id=uuid5(_NAMESPACE, f"{realm_id}:spec:{event_type.value}"),
            realm_id=realm_id,
            hook_id=hook_id,
            revision=1,
            event_type=event_type,
            required=True,
            source_layer="memory-continuity",
            timeout_ms=5_000,
            execution_mode=HookExecutionMode.INTERNAL,
            input_schema=_INPUT_SCHEMA,
            output_schema=_V1_OUTPUT_SCHEMA,
            permission_profile_name=profile.name,
            permission_profile_digest=profile.profile_digest,
            failure_policy=HookFailurePolicy.ABORT,
            created_at=_PROFILE_CREATED_AT,
        )
        runtime = HookRuntimeRevision.create(
            id=uuid5(_NAMESPACE, f"{realm_id}:runtime:{event_type.value}"),
            realm_id=realm_id,
            hook_id=hook_id,
            hook_revision=1,
            adapter_ref=adapter_ref,
            adapter_digest=adapter_digest,
            permission_capabilities=(),
            load_state=HookLoadState.READY,
            captured_at=_PROFILE_CREATED_AT,
            expires_at=_EXPIRES_AT,
        )
        identities[event_type] = (spec.id, spec.hook_digest, runtime.runtime_digest)
    return identities


def _adapter(event_type: HookEventType) -> LoadedHookAdapter:
    adapter_ref = f"memory-continuity-{event_type.value}-v{MEMORY_HOOK_REVISION}"
    adapter_digest = digest(
        {
            "adapter_ref": adapter_ref,
            "event_type": event_type.value,
            "version": MEMORY_HOOK_REVISION,
            "orchestrator": "zekam-memory-learning-command/v1",
        }
    )

    def invoke(payload: object) -> HookAdapterResult:
        if not isinstance(payload, Mapping):
            raise TypeError("Memory hook payload mapping olmali")
        command = plan_memory_hook(event_type, payload)
        return HookAdapterResult(
            HookResultKind.OBSERVATION,
            {
                "event_type": event_type.value,
                "accepted": True,
                "command": command.body(),
                "command_digest": command.command_digest,
                "grants_authority": False,
            },
        )

    return LoadedHookAdapter(
        adapter_ref=adapter_ref,
        adapter_digest=adapter_digest,
        execution_mode=HookExecutionMode.INTERNAL,
        invoke=invoke,
    )


def memory_hook_bundle(realm_id: UUID) -> MemoryHookBundle:
    profile = _profile(realm_id)
    specs: list[HookSpecRevision] = []
    runtimes: list[HookRuntimeRevision] = []
    adapters: list[LoadedHookAdapter] = []
    for event_type in MEMORY_HOOK_EVENTS:
        hook_id = f"memory-continuity-{event_type.value}"
        adapter = _adapter(event_type)
        spec = HookSpecRevision.create(
            id=uuid5(
                _NAMESPACE,
                f"{realm_id}:spec:{event_type.value}:v{MEMORY_HOOK_REVISION}",
            ),
            realm_id=realm_id,
            hook_id=hook_id,
            revision=MEMORY_HOOK_REVISION,
            event_type=event_type,
            required=True,
            source_layer="memory-continuity",
            timeout_ms=5_000,
            execution_mode=HookExecutionMode.INTERNAL,
            input_schema=_INPUT_SCHEMA,
            output_schema=_OUTPUT_SCHEMA,
            permission_profile_name=profile.name,
            permission_profile_digest=profile.profile_digest,
            failure_policy=HookFailurePolicy.ABORT,
            created_at=_HOOK_CREATED_AT,
        )
        runtime = HookRuntimeRevision.create(
            id=uuid5(
                _NAMESPACE,
                f"{realm_id}:runtime:{event_type.value}:v{MEMORY_HOOK_REVISION}",
            ),
            realm_id=realm_id,
            hook_id=hook_id,
            hook_revision=MEMORY_HOOK_REVISION,
            adapter_ref=adapter.adapter_ref,
            adapter_digest=adapter.adapter_digest,
            permission_capabilities=(),
            load_state=HookLoadState.READY,
            captured_at=_HOOK_CREATED_AT,
            expires_at=_EXPIRES_AT,
        )
        specs.append(spec)
        runtimes.append(runtime)
        adapters.append(adapter)
    body = {
        "schema": "zekam-memory-hook-bundle/v2",
        "realm_id": str(realm_id),
        "revision": MEMORY_HOOK_REVISION,
        "profile_digest": profile.profile_digest,
        "hook_digests": [item.hook_digest for item in specs],
        "runtime_digests": [item.runtime_digest for item in runtimes],
        "adapter_digests": [item.adapter_digest for item in adapters],
        "grants_authority": False,
    }
    return MemoryHookBundle(
        profile,
        tuple(specs),
        tuple(runtimes),
        tuple(adapters),
        digest(body),
    )
