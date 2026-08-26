"""Deterministic, authority-free built-in lifecycle handler bundle."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID, uuid5

from zekam.application.hook_runtime import LoadedHookAdapter
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
_CREATED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
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


def _adapter(event_type: HookEventType) -> LoadedHookAdapter:
    adapter_ref = f"memory-continuity-{event_type.value}-v1"
    adapter_digest = digest(
        {"adapter_ref": adapter_ref, "event_type": event_type.value, "version": 1}
    )

    def invoke(_payload: object) -> HookAdapterResult:
        return HookAdapterResult(
            HookResultKind.OBSERVATION,
            {
                "event_type": event_type.value,
                "accepted": True,
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
    profile = PermissionProfileRevision.create(
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
        created_at=_CREATED_AT,
    )
    specs: list[HookSpecRevision] = []
    runtimes: list[HookRuntimeRevision] = []
    adapters: list[LoadedHookAdapter] = []
    for event_type in MEMORY_HOOK_EVENTS:
        hook_id = f"memory-continuity-{event_type.value}"
        adapter = _adapter(event_type)
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
            output_schema=_OUTPUT_SCHEMA,
            permission_profile_name=profile.name,
            permission_profile_digest=profile.profile_digest,
            failure_policy=HookFailurePolicy.ABORT,
            created_at=_CREATED_AT,
        )
        runtime = HookRuntimeRevision.create(
            id=uuid5(_NAMESPACE, f"{realm_id}:runtime:{event_type.value}"),
            realm_id=realm_id,
            hook_id=hook_id,
            hook_revision=1,
            adapter_ref=adapter.adapter_ref,
            adapter_digest=adapter.adapter_digest,
            permission_capabilities=(),
            load_state=HookLoadState.READY,
            captured_at=_CREATED_AT,
            expires_at=_EXPIRES_AT,
        )
        specs.append(spec)
        runtimes.append(runtime)
        adapters.append(adapter)
    body = {
        "schema": "zekam-memory-hook-bundle/v1",
        "realm_id": str(realm_id),
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
