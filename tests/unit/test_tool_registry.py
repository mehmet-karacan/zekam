from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.tool_dispatch import ToolDispatchService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.tool_registry import (
    CompiledToolSet,
    ToolDispatchBinding,
    ToolExposure,
    ToolRuntimeRevision,
    ToolSetEntry,
    ToolSpecRevision,
)

pytestmark = pytest.mark.unit


def bundle():  # type: ignore[no-untyped-def]
    realm_id = uuid4()
    now = dt.datetime.now(dt.UTC)
    spec = ToolSpecRevision.create(
        realm_id=realm_id,
        tool_id="jira.search",
        revision=3,
        name="Jira search",
        description="Exact Jira issue search",
        input_schema_digest=digest("input-schema-v3"),
        output_schema_digest=digest("output-schema-v3"),
        created_at=now,
    )
    runtime = ToolRuntimeRevision.create(
        realm_id=realm_id,
        tool_id=spec.tool_id,
        revision=spec.revision,
        adapter_ref="mcp:jira/search",
        executable_revision="jira-mcp@3.1.0",
        executable_digest=digest("jira-mcp-binary"),
        permission_capabilities=("jira.read",),
        parallel_supported=True,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    entry = ToolSetEntry(
        spec.tool_id,
        spec.revision,
        ToolExposure.DIRECT,
        spec.spec_digest,
        runtime.runtime_digest,
    )
    compiled = CompiledToolSet.create(
        realm_id=realm_id,
        role="researcher",
        permission_profile_digest=digest("permission"),
        entries=(entry,),
        created_at=now,
    )
    binding = ToolDispatchBinding(
        uuid4(),
        digest("turn"),
        compiled.tool_set_digest,
        spec.tool_id,
        spec.revision,
        spec.spec_digest,
        runtime.runtime_digest,
        digest("input"),
    )
    return now, compiled, spec, runtime, binding


class Store:
    def __init__(self, values):  # type: ignore[no-untyped-def]
        self.values = values
        self.gates: list[str] = []
        self.loop_bindings: list[tuple[object, object]] = []

    @contextmanager
    def locked_dispatch_bundle(self, *_):  # type: ignore[no-untyped-def]
        yield self.values

    def record_dispatch_gate(self, _binding, *, disposition, checked_at):  # type: ignore[no-untyped-def]
        assert checked_at.tzinfo is not None
        self.gates.append(disposition)

    def bind_loop_dispatch(self, attempt_id, dispatch_id):  # type: ignore[no-untyped-def]
        self.loop_bindings.append((attempt_id, dispatch_id))


class Adapter:
    def __init__(self, runtime_binding: tuple[str, int, str] | None = None) -> None:
        self.calls = 0
        self._runtime_binding = runtime_binding

    def runtime_binding(self):  # type: ignore[no-untyped-def]
        assert self._runtime_binding is not None
        return self._runtime_binding

    def execute(self, binding, *, permit):  # type: ignore[no-untyped-def]
        permit.assert_for(binding)
        self.calls += 1
        return "ok"


def test_exact_spec_runtime_binding_dispatches_with_unforgeable_permit() -> None:
    now, compiled, spec, runtime, binding = bundle()
    store = Store((compiled, spec, runtime))
    adapter = Adapter((binding.tool_id, binding.revision, binding.runtime_digest))
    assert ToolDispatchService(store).dispatch(binding, adapter, now=now) == "ok"  # type: ignore[arg-type]
    assert store.gates == ["passed"]
    assert adapter.calls == 1


def test_tool_loop_binding_is_persisted_before_gate_and_effect() -> None:
    now, compiled, spec, runtime, binding = bundle()
    store = Store((compiled, spec, runtime))
    adapter = Adapter((binding.tool_id, binding.revision, binding.runtime_digest))
    attempt_id = uuid4()
    assert (
        ToolDispatchService(store).dispatch(  # type: ignore[arg-type]
            binding, adapter, now=now, loop_attempt_id=attempt_id
        )
        == "ok"
    )
    assert store.loop_bindings == [(attempt_id, binding.effect_claim_id)]
    assert store.gates == ["passed"]


@pytest.mark.parametrize("drift", ("spec", "runtime", "revision", "set"))
def test_model_visible_spec_or_executable_runtime_drift_fails_before_effect(drift: str) -> None:
    now, compiled, spec, runtime, binding = bundle()
    if drift == "spec":
        spec = replace(spec, spec_digest=digest("forged-spec"))
    elif drift == "runtime":
        runtime = replace(runtime, runtime_digest=digest("forged-runtime"))
    elif drift == "revision":
        binding = replace(binding, revision=binding.revision + 1)
    else:
        binding = replace(binding, tool_set_digest=digest("foreign-set"))
    store = Store((compiled, spec, runtime))
    adapter = Adapter((binding.tool_id, binding.revision, binding.runtime_digest))
    with pytest.raises(PolicyViolation):
        ToolDispatchService(store).dispatch(binding, adapter, now=now)  # type: ignore[arg-type]
    assert store.gates == []
    assert adapter.calls == 0


def test_hidden_tool_is_not_model_visible_but_remains_exact_dispatchable() -> None:
    now, compiled, spec, runtime, binding = bundle()
    hidden_entry = replace(compiled.entries[0], exposure=ToolExposure.HIDDEN_DISPATCH)
    hidden = CompiledToolSet.create(
        realm_id=compiled.realm_id,
        role=compiled.role,
        permission_profile_digest=compiled.permission_profile_digest,
        entries=(hidden_entry,),
        created_at=compiled.created_at,
    )
    hidden_binding = replace(binding, tool_set_digest=hidden.tool_set_digest)
    assert hidden.model_visible_entries() == ()
    assert (
        ToolDispatchService(Store((hidden, spec, runtime))).dispatch(  # type: ignore[arg-type]
            hidden_binding,
            Adapter(
                (hidden_binding.tool_id, hidden_binding.revision, hidden_binding.runtime_digest)
            ),
            now=now,
        )
        == "ok"
    )


def test_expired_runtime_and_digest_tamper_fail_closed() -> None:
    now, compiled, spec, runtime, binding = bundle()
    with pytest.raises(PolicyViolation, match="stale"):
        ToolDispatchService(Store((compiled, spec, runtime))).dispatch(  # type: ignore[arg-type]
            binding,
            Adapter((binding.tool_id, binding.revision, binding.runtime_digest)),
            now=now + dt.timedelta(minutes=11),
        )
    with pytest.raises(PolicyViolation, match="supplied digest mismatch"):
        replace(compiled, role="builder").assert_digest()


def test_adapter_runtime_descriptor_mismatch_fails_before_gate_and_effect() -> None:
    now, compiled, spec, runtime, binding = bundle()
    store = Store((compiled, spec, runtime))
    adapter = Adapter((binding.tool_id, binding.revision + 1, digest("runtime-v2")))
    with pytest.raises(PolicyViolation, match="adapter runtime revision mismatch"):
        ToolDispatchService(store).dispatch(binding, adapter, now=now)  # type: ignore[arg-type]
    assert store.gates == []
    assert adapter.calls == 0


def test_initial_and_code_mode_exposure_are_bounded() -> None:
    now, compiled, _spec, _runtime, _binding = bundle()
    base = compiled.entries[0]
    entries = (
        replace(base, tool_id="a.direct", exposure=ToolExposure.DIRECT),
        replace(base, tool_id="b.code", exposure=ToolExposure.CODE_MODE_ONLY),
        replace(base, tool_id="c.search", exposure=ToolExposure.DEFERRED_SEARCH),
        replace(base, tool_id="d.hidden", exposure=ToolExposure.HIDDEN_DISPATCH),
    )
    exposed = CompiledToolSet.create(
        realm_id=compiled.realm_id,
        role=compiled.role,
        permission_profile_digest=compiled.permission_profile_digest,
        entries=entries,
        created_at=now,
    )
    assert tuple(item.tool_id for item in exposed.model_visible_entries()) == ("a.direct",)
    assert tuple(item.tool_id for item in exposed.model_visible_entries(code_mode=True)) == (
        "a.direct",
        "b.code",
    )
    payload = exposed.compile_model_payload(code_mode=True)
    serialized = payload.serialize_request({"input": "provider-request"})
    binding = serialized.binding
    assert binding.ordered_tool_ids == ("a.direct", "b.code")
    assert binding.serialized_tools_digest == digest(list(payload.entries))
    assert serialized.payload["tools"] == list(payload.entries)
    serialized.assert_unchanged()
    with pytest.raises(PolicyViolation, match="kanonik serializer"):
        replace(binding, _token=object()).assert_valid()
    with pytest.raises(PolicyViolation, match="muhru gecersiz"):
        replace(binding, request_payload_digest=digest("forged-request")).assert_valid()
    with pytest.raises(PolicyViolation, match="mutation drift"):
        replace(serialized, payload={**serialized.payload, "tools": []}).assert_unchanged()


def test_model_tool_serializer_rejects_prepopulated_tool_section() -> None:
    _now, compiled, _spec, _runtime, _binding = bundle()
    with pytest.raises(PolicyViolation, match="onceden doldurulmus"):
        compiled.compile_model_payload().serialize_request({"tools": []})
