"""Fail-closed tool spec/runtime revision gate at the executable boundary."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, TypeVar

from zekam.domain.errors import PolicyViolation
from zekam.domain.tool_registry import (
    CompiledToolSet,
    ToolDispatchBinding,
    ToolExecutionPermit,
    ToolRuntimeRevision,
    ToolSpecRevision,
    _issue_tool_execution_permit,
    assert_tool_dispatch_binding,
)

ResultT = TypeVar("ResultT", covariant=True)


class ToolRegistryStore(Protocol):
    def locked_dispatch_bundle(
        self, binding: ToolDispatchBinding
    ) -> AbstractContextManager[tuple[CompiledToolSet, ToolSpecRevision, ToolRuntimeRevision]]: ...

    def record_dispatch_gate(
        self,
        binding: ToolDispatchBinding,
        *,
        disposition: str,
        checked_at: dt.datetime,
    ) -> None: ...


class ToolRuntimeAdapter(Protocol[ResultT]):
    def runtime_binding(self) -> tuple[str, int, str]: ...

    def execute(self, binding: ToolDispatchBinding, *, permit: ToolExecutionPermit) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class ToolDispatchService:
    repository: ToolRegistryStore

    def dispatch(
        self,
        binding: ToolDispatchBinding,
        adapter: ToolRuntimeAdapter[ResultT],
        *,
        now: dt.datetime | None = None,
    ) -> ResultT:
        moment = now or dt.datetime.now(dt.UTC)
        with self.repository.locked_dispatch_bundle(binding) as (compiled, spec, runtime):
            assert_tool_dispatch_binding(binding, compiled, spec, runtime, now=moment)
            if adapter.runtime_binding() != (
                binding.tool_id,
                binding.revision,
                binding.runtime_digest,
            ):
                raise PolicyViolation("Executable adapter runtime revision mismatch")
            self.repository.record_dispatch_gate(binding, disposition="passed", checked_at=moment)
            permit = _issue_tool_execution_permit(binding)
            return adapter.execute(binding, permit=permit)
