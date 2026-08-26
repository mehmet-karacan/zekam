from __future__ import annotations

from uuid import uuid4

from zekam.application.memory_hooks import MEMORY_HOOK_EVENTS, memory_hook_bundle
from zekam.domain.hook_runtime import HookExecutionMode, HookResultKind


def test_memory_hook_bundle_is_deterministic_exact_and_authority_free() -> None:
    realm_id = uuid4()

    first = memory_hook_bundle(realm_id)
    second = memory_hook_bundle(realm_id)

    assert len(MEMORY_HOOK_EVENTS) == 17
    assert first.bundle_digest == second.bundle_digest
    assert first.specs == second.specs
    assert first.runtimes == second.runtimes
    assert len({item.event_type for item in first.specs}) == 17
    assert all(item.required for item in first.specs)
    assert all(item.execution_mode is HookExecutionMode.INTERNAL for item in first.specs)
    assert all(item.execution_mode is HookExecutionMode.INTERNAL for item in first.adapters)
    assert all(not item.effect_capable for item in first.adapters)
    assert first.profile.allowed_capabilities == ()
    assert set(first.profile.denied_capabilities) == {
        "filesystem.read",
        "filesystem.write",
        "network.access",
        "process.run",
    }

    results = tuple(adapter.invoke({}) for adapter in first.adapters)
    assert all(item.kind is HookResultKind.OBSERVATION for item in results)
    assert all(not item.effect_performed and not item.grants_authority for item in results)
    assert all(item.payload["grants_authority"] is False for item in results)
