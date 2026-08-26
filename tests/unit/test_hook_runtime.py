from __future__ import annotations

import datetime as dt
import threading
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.errors import PolicyViolation, ValidationFailed
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

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


def _profile(realm_id: UUID) -> PermissionProfileRevision:
    return PermissionProfileRevision.from_flags(
        realm_id=realm_id,
        name="hook-readonly",
        revision=1,
        permission_flags={
            "filesystem.read": True,
            "filesystem.write": False,
            "network.access": False,
            "process.run": False,
        },
        managed=True,
        created_at=NOW,
    )


def _spec(
    realm_id: UUID,
    profile: PermissionProfileRevision,
    *,
    hook_id: str = "observe-turn",
    required: bool = True,
    failure_policy: HookFailurePolicy = HookFailurePolicy.ABORT,
    timeout_ms: int = 500,
    event_type: HookEventType = HookEventType.TURN_START,
) -> HookSpecRevision:
    return HookSpecRevision.create(
        realm_id=realm_id,
        hook_id=hook_id,
        revision=1,
        event_type=event_type,
        required=required,
        source_layer="managed-policy",
        timeout_ms=timeout_ms,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=failure_policy,
        created_at=NOW,
    )


def _runtime(spec: HookSpecRevision, *, adapter_ref: str = "adapter-v1") -> HookRuntimeRevision:
    return HookRuntimeRevision.create(
        realm_id=spec.realm_id,
        hook_id=spec.hook_id,
        hook_revision=spec.revision,
        adapter_ref=adapter_ref,
        adapter_digest=digest(adapter_ref),
        permission_capabilities=("filesystem.read",),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )


def _configure(
    runtime: HookRuntime,
    spec: HookSpecRevision,
    profile: PermissionProfileRevision,
    adapter: LoadedHookAdapter | None,
) -> None:
    runtime.reconfigure(
        realm_id=spec.realm_id,
        config_effective_digest=digest("config"),
        specs=(spec,),
        runtimes=(_runtime(spec),),
        profiles=(profile,),
        adapters=() if adapter is None else (adapter,),
        now=NOW,
    )


def test_preview_is_typed_deterministic_and_never_executes_adapter() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(realm_id, profile)
    calls = 0

    def invoke(payload: Any) -> HookAdapterResult:
        nonlocal calls
        calls += 1
        return HookAdapterResult(HookResultKind.OBSERVATION, {"message": payload["value"]})

    adapter = LoadedHookAdapter(
        "adapter-v1", digest("adapter-v1"), HookExecutionMode.INTERNAL, invoke
    )
    runtime = HookRuntime()
    _configure(runtime, spec, profile, adapter)
    session = runtime.start_session()
    first = runtime.preview(session, HookEventType.TURN_START, {"value": "one"})
    second = runtime.preview(session, HookEventType.TURN_START, {"value": "one"})
    assert first == second and first[0].will_execute
    assert first[0].effect_performed is False and first[0].grants_authority is False
    assert calls == 0
    with pytest.raises(ValidationFailed, match="typed schema"):
        runtime.preview(session, HookEventType.TURN_START, {"wrong": True})
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)

    with pytest.raises(PolicyViolation, match="direct effect"):
        LoadedHookAdapter(
            "unsafe",
            digest("unsafe"),
            HookExecutionMode.COMMAND,
            invoke,
            effect_capable=True,
        )
    with pytest.raises(PolicyViolation, match="inherited environment"):
        LoadedHookAdapter(
            "leaky",
            digest("leaky"),
            HookExecutionMode.PYTHON,
            invoke,
            inherited_environment=("PATH",),
        )


def test_required_load_failure_blocks_first_session_and_failed_reconfigure_keeps_old() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(realm_id, profile)
    blocked = HookRuntime()
    _configure(blocked, spec, profile, None)
    with pytest.raises(PolicyViolation, match="session baslangici reddedildi"):
        blocked.start_session()
    blocked.shutdown(timeout_seconds=0)

    future_runtime = HookRuntimeRevision.create(
        realm_id=realm_id,
        hook_id=spec.hook_id,
        hook_revision=spec.revision,
        adapter_ref="adapter-v1",
        adapter_digest=digest("adapter-v1"),
        permission_capabilities=("filesystem.read",),
        load_state=HookLoadState.READY,
        captured_at=NOW + dt.timedelta(minutes=1),
        expires_at=NOW + dt.timedelta(hours=1),
    )
    future = HookRuntime()
    future.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("config"),
        specs=(spec,),
        runtimes=(future_runtime,),
        profiles=(profile,),
        adapters=(
            LoadedHookAdapter(
                "adapter-v1",
                digest("adapter-v1"),
                HookExecutionMode.INTERNAL,
                lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "future"}),
            ),
        ),
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="runtime-future-dated"):
        future.start_session()
    future.shutdown(timeout_seconds=0)

    duplicate = HookRuntime()
    current_runtime = _runtime(spec)
    with pytest.raises(ValidationFailed, match="runtime identity duplicate"):
        duplicate.reconfigure(
            realm_id=realm_id,
            config_effective_digest=digest("config"),
            specs=(spec,),
            runtimes=(current_runtime, current_runtime),
            profiles=(profile,),
            adapters=(),
            now=NOW,
        )
    duplicate.shutdown(timeout_seconds=0)

    runtime = HookRuntime()
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "old"}),
    )
    _configure(runtime, spec, profile, adapter)
    old_session = runtime.start_session()
    with pytest.raises(PolicyViolation, match="onceki generation korundu"):
        _configure(runtime, replace(spec, hook_digest=spec.hook_digest), profile, None)
    new_session = runtime.start_session()
    assert new_session.compiled_set.generation == old_session.compiled_set.generation == 1
    runtime.close_session(old_session)
    runtime.close_session(new_session)
    runtime.shutdown(timeout_seconds=0)


def test_safe_reconfigure_pins_inflight_old_generation_and_new_session_uses_new() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(realm_id, profile)
    started = threading.Event()
    release = threading.Event()
    result_holder: list[tuple[Any, ...]] = []

    def old_invoke(_: Any) -> HookAdapterResult:
        started.set()
        release.wait(2)
        return HookAdapterResult(HookResultKind.OBSERVATION, {"message": "old"})

    runtime = HookRuntime(max_workers=2)
    old_adapter = LoadedHookAdapter(
        "adapter-v1", digest("adapter-v1"), HookExecutionMode.INTERNAL, old_invoke
    )
    _configure(runtime, spec, profile, old_adapter)
    old_session = runtime.start_session()
    worker = threading.Thread(
        target=lambda: result_holder.append(
            runtime.run(old_session, HookEventType.TURN_START, {"value": "x"})
        )
    )
    worker.start()
    assert started.wait(1)
    new_runtime = _runtime(spec, adapter_ref="adapter-v2")
    new_adapter = LoadedHookAdapter(
        "adapter-v2",
        digest("adapter-v2"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "new"}),
    )
    compiled = runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("config-v2"),
        specs=(spec,),
        runtimes=(new_runtime,),
        profiles=(profile,),
        adapters=(new_adapter,),
        now=NOW,
    )
    assert compiled.generation == 2
    new_session = runtime.start_session()
    new_result = runtime.run(new_session, HookEventType.TURN_START, {"value": "x"})
    assert new_result[0].output_digest == digest({"message": "new"})
    release.set()
    worker.join(2)
    assert result_holder[0][0].output_digest == digest({"message": "old"})
    runtime.close_session(old_session)
    runtime.close_session(new_session)
    runtime.shutdown(timeout_seconds=1)


def test_proposal_is_authority_free_and_optional_failure_policies_are_visible() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    proposal_spec = _spec(realm_id, profile)
    runtime = HookRuntime()
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.PROPOSAL, {"message": "change-request"}),
    )
    _configure(runtime, proposal_spec, profile, adapter)
    session = runtime.start_session()
    result = runtime.run(session, HookEventType.TURN_START, {"value": "x"})[0]
    assert result.requires_governed_effect is True
    assert result.effect_performed is False and result.grants_authority is False
    assert result.proposal_digest == digest({"message": "change-request"})
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_durable_execution_surface_retains_typed_output_for_terminal_receipt() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(realm_id, profile)
    runtime = HookRuntime()
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda payload: HookAdapterResult(
            HookResultKind.OBSERVATION, {"message": payload["value"]}
        ),
    )
    _configure(runtime, spec, profile, adapter)
    session = runtime.start_session()

    records = runtime.run_with_records(session, HookEventType.TURN_START, {"value": "receipt-body"})

    assert records[0].entry.spec == spec
    assert records[0].outcome.status == "completed"
    assert records[0].output_body == {"message": "receipt-body"}
    assert records[0].outcome.output_digest == digest(records[0].output_body)
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)

    for policy, expected in (
        (HookFailurePolicy.WARN, "warning"),
        (HookFailurePolicy.QUARANTINE, "quarantined"),
    ):
        optional = _spec(
            realm_id,
            profile,
            hook_id=f"optional-{policy.value}",
            required=False,
            failure_policy=policy,
        )
        current = HookRuntime()
        failing = LoadedHookAdapter(
            "adapter-v1",
            digest("adapter-v1"),
            HookExecutionMode.INTERNAL,
            lambda _: (_ for _ in ()).throw(RuntimeError("raw secret must not surface")),
        )
        _configure(current, optional, profile, failing)
        active = current.start_session()
        outcome = current.run(active, HookEventType.TURN_START, {"value": "x"})[0]
        assert outcome.status == expected
        assert outcome.warning == "RuntimeError"
        assert "secret" not in repr(outcome).lower()
        current.close_session(active)
        current.shutdown(timeout_seconds=0)


def test_timeout_and_invalid_output_follow_optional_policy_and_shutdown_is_bounded() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(
        realm_id,
        profile,
        required=False,
        failure_policy=HookFailurePolicy.WARN,
        timeout_ms=10,
    )
    release = threading.Event()
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: (
            release.wait(1) and HookAdapterResult(HookResultKind.OBSERVATION, {"message": "late"})
        ),
    )
    runtime = HookRuntime()
    _configure(runtime, spec, profile, adapter)
    session = runtime.start_session()
    timed_out = runtime.run(session, HookEventType.TURN_START, {"value": "x"})[0]
    assert timed_out.status == "warning" and timed_out.warning == "TimeoutError"
    runtime.close_session(session)
    receipt = runtime.shutdown(timeout_seconds=0)
    assert receipt.bounded and receipt.still_running == 1
    release.set()

    invalid = HookRuntime()
    invalid_adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"wrong": True}),
    )
    _configure(invalid, spec, profile, invalid_adapter)
    invalid_session = invalid.start_session()
    bad_output = invalid.run(invalid_session, HookEventType.TURN_START, {"value": "x"})[0]
    assert bad_output.status == "warning" and bad_output.warning == "ValidationFailed"
    invalid.close_session(invalid_session)
    invalid.shutdown(timeout_seconds=0)


def test_concurrent_reconfigure_swaps_generations_atomically() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    spec = _spec(realm_id, profile)
    runtime_revision = _runtime(spec)
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "ok"}),
    )
    runtime = HookRuntime()
    generations: list[int] = []
    failures: list[BaseException] = []

    def reconfigure(index: int) -> None:
        try:
            item = runtime.reconfigure(
                realm_id=realm_id,
                config_effective_digest=digest({"config": index}),
                specs=(spec,),
                runtimes=(runtime_revision,),
                profiles=(profile,),
                adapters=(adapter,),
                now=NOW,
            )
            generations.append(item.generation)
        except BaseException as exc:  # pragma: no cover - assertion evidence
            failures.append(exc)

    workers = [threading.Thread(target=reconfigure, args=(index,)) for index in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
    assert failures == []
    assert sorted(generations) == list(range(1, 13))
    session = runtime.start_session()
    assert session.compiled_set.generation == 12
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_required_lifecycle_event_effective_handler_count_must_be_exactly_one() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    runtime = HookRuntime()

    compiled = runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("config-no-handler"),
        specs=(),
        runtimes=(),
        profiles=(profile,),
        adapters=(),
        now=NOW,
        required_events=(HookEventType.PRE_COMPACTION,),
    )
    assert compiled.required_load_errors == ("event:pre_compaction:effective-handler-count:0",)
    with pytest.raises(PolicyViolation, match="effective-handler-count:0"):
        runtime.start_session()
    runtime.shutdown(timeout_seconds=0)

    first = _spec(
        realm_id,
        profile,
        hook_id="global-pre-compaction",
        event_type=HookEventType.PRE_COMPACTION,
    )
    second = _spec(
        realm_id,
        profile,
        hook_id="project-pre-compaction",
        event_type=HookEventType.PRE_COMPACTION,
    )
    custom = _spec(
        realm_id,
        profile,
        hook_id="unrelated-custom-hook",
        required=False,
        failure_policy=HookFailurePolicy.WARN,
        event_type=HookEventType.POST_TASK,
    )
    duplicate_runtime = HookRuntime()
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "ok"}),
    )
    duplicate = duplicate_runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("global-project-local-merged"),
        specs=(first, second, custom),
        runtimes=(_runtime(first), _runtime(second), _runtime(custom)),
        profiles=(profile,),
        adapters=(adapter,),
        now=NOW,
        required_events=(HookEventType.PRE_COMPACTION,),
    )
    assert "event:pre_compaction:effective-handler-count:2" in duplicate.required_load_errors
    assert any(entry.spec.hook_id == "unrelated-custom-hook" for entry in duplicate.entries)
    with pytest.raises(PolicyViolation, match="effective-handler-count:2"):
        duplicate_runtime.start_session()
    duplicate_runtime.shutdown(timeout_seconds=0)


def test_continuity_event_set_can_be_generation_bound_with_one_handler_each() -> None:
    realm_id = uuid4()
    profile = _profile(realm_id)
    events = (
        HookEventType.CONTINUITY_SESSION_START,
        HookEventType.HYDRATION_REQUIRED,
        HookEventType.HYDRATION_COMPLETED,
        HookEventType.PRE_TASK,
        HookEventType.POST_TASK,
        HookEventType.PRE_COMPACTION,
        HookEventType.POST_COMPACTION,
        HookEventType.PRE_CLOSE,
        HookEventType.POST_CLOSE,
        HookEventType.ON_FAILURE,
        HookEventType.ON_VALIDATION_FAILURE,
        HookEventType.ON_MEMORY_WRITE_FAILURE,
        HookEventType.ON_MEMORY_HYDRATION_FAILURE,
        HookEventType.ON_SKILL_CANDIDATE,
        HookEventType.ON_SKILL_UPDATE,
        HookEventType.ON_STATE_DRIFT,
        HookEventType.UNCLEAN_EXIT,
    )
    specs = tuple(
        _spec(
            realm_id,
            profile,
            hook_id=f"continuity-{event.value}",
            event_type=event,
        )
        for event in events
    )
    adapter = LoadedHookAdapter(
        "adapter-v1",
        digest("adapter-v1"),
        HookExecutionMode.INTERNAL,
        lambda _: HookAdapterResult(HookResultKind.OBSERVATION, {"message": "ok"}),
    )
    runtime = HookRuntime()
    compiled = runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("continuity-event-set"),
        specs=specs,
        runtimes=tuple(_runtime(spec) for spec in specs),
        profiles=(profile,),
        adapters=(adapter,),
        now=NOW,
        required_events=events,
    )
    assert compiled.required_load_errors == ()
    session = runtime.start_session()
    assert len(session.compiled_set.entries) == len(events) == 17
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)
