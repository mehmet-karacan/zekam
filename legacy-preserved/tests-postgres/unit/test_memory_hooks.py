from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.memory_hooks import (
    MEMORY_HOOK_EVENTS,
    MEMORY_HOOK_REVISION,
    memory_hook_bundle,
    memory_hook_v1_identities,
)
from zekam.domain.canonical import digest
from zekam.domain.hook_runtime import HookExecutionMode, HookResultKind
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller


def test_memory_hook_bundle_is_deterministic_exact_and_authority_free() -> None:
    realm_id = uuid4()

    first = memory_hook_bundle(realm_id)
    second = memory_hook_bundle(realm_id)

    assert len(MEMORY_HOOK_EVENTS) == 17
    assert first.bundle_digest == second.bundle_digest
    assert first.specs == second.specs
    assert first.runtimes == second.runtimes
    legacy = memory_hook_v1_identities(realm_id)
    assert legacy == memory_hook_v1_identities(realm_id)
    assert len({item.event_type for item in first.specs}) == 17
    assert all(item.required for item in first.specs)
    assert all(item.execution_mode is HookExecutionMode.INTERNAL for item in first.specs)
    assert all(item.revision == MEMORY_HOOK_REVISION for item in first.specs)
    assert all(item.execution_mode is HookExecutionMode.INTERNAL for item in first.adapters)
    assert all(not item.effect_capable for item in first.adapters)
    for event, spec, runtime in zip(MEMORY_HOOK_EVENTS, first.specs, first.runtimes, strict=True):
        legacy_spec_id, legacy_hook_digest, legacy_runtime_digest = legacy[event]
        assert legacy_spec_id != spec.id
        assert legacy_hook_digest != spec.hook_digest
        assert legacy_runtime_digest != runtime.runtime_digest
    assert first.profile.allowed_capabilities == ()
    assert set(first.profile.denied_capabilities) == {
        "filesystem.read",
        "filesystem.write",
        "network.access",
        "process.run",
    }

    moment = dt.datetime(2026, 8, 28, tzinfo=dt.UTC)
    results = tuple(
        adapter.invoke(
            {
                "lifecycle": {
                    "schema": "zekam-session-lifecycle-event/v1",
                    "event_id": str(uuid4()),
                    "event_type": event.value,
                    "occurred_at": moment.isoformat(),
                    "grants_authority": False,
                },
                "data": {},
            }
        )
        for event, adapter in zip(MEMORY_HOOK_EVENTS, first.adapters, strict=True)
    )
    assert all(item.kind is HookResultKind.OBSERVATION for item in results)
    assert all(not item.effect_performed and not item.grants_authority for item in results)
    assert all(item.payload["grants_authority"] is False for item in results)
    assert all(item.payload["command"]["provider_calls"] == 0 for item in results)
    assert all(item.payload["command"]["direct_promotion"] is False for item in results)
    assert all(
        item.payload["command_digest"] == digest(item.payload["command"]) for item in results
    )


def test_memory_hook_upgrade_plan_binds_current_generation_and_code_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm_id = uuid4()
    current_digest = digest("current-hooks")

    # The plan is read-only; a DB snapshot supplies only the immutable current identity.
    monkeypatch.setattr(
        PostgresMemoryHookInstaller,
        "_current",
        lambda _self: (uuid4(), 7, current_digest),
    )
    installer = PostgresMemoryHookInstaller(object(), realm_id)

    first = installer.plan_upgrade()
    second = installer.plan_upgrade()

    assert first == second
    assert first.current_generation == 7
    assert first.current_hook_set_digest == current_digest
    assert first.expected_bundle_digest == memory_hook_bundle(realm_id).bundle_digest
    assert first.resource == f"db-object:memory-hook-generation:{realm_id}"
    assert tuple(item.value for item in first.effect_request.data_classifications) == (
        "local-only",
    )
    assert first.body()["grants_authority"] is False
    monkeypatch.setattr(
        PostgresMemoryHookInstaller,
        "_current",
        lambda _self: (uuid4(), 8, digest("new-current-hooks")),
    )
    assert installer.plan_upgrade().plan_digest != first.plan_digest
