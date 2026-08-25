"""Fail-closed tool spec/runtime revision gate at the executable boundary."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from zekam.domain.errors import PolicyViolation
from zekam.domain.tool_registry import (
    CompiledToolSet,
    DeferredToolMatch,
    ToolDispatchBinding,
    ToolDispatchPlan,
    ToolDispatchWave,
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

    def bind_loop_dispatch(self, attempt_id: UUID, dispatch_id: UUID) -> None: ...


class ToolRuntimeAdapter(Protocol[ResultT]):
    def runtime_binding(self) -> tuple[str, int, str]: ...

    def execute(self, binding: ToolDispatchBinding, *, permit: ToolExecutionPermit) -> ResultT: ...


class ToolExposureStore(Protocol):
    def deferred_catalog(
        self,
        *,
        tool_set_digest: str,
        role: str,
        permission_profile_digest: str,
        now: dt.datetime,
    ) -> tuple[
        CompiledToolSet,
        tuple[tuple[ToolSpecRevision, ToolRuntimeRevision], ...],
    ]: ...


class ToolWaveStore(Protocol):
    def locked_wave_bundles(
        self, bindings: tuple[ToolDispatchBinding, ...]
    ) -> AbstractContextManager[
        tuple[tuple[CompiledToolSet, ToolSpecRevision, ToolRuntimeRevision], ...]
    ]: ...


@dataclass(frozen=True, slots=True)
class DeferredToolSearchService:
    repository: ToolExposureStore

    def search(
        self,
        *,
        tool_set_digest: str,
        role: str,
        permission_profile_digest: str,
        query: str,
        now: dt.datetime,
        limit: int = 10,
    ) -> tuple[DeferredToolMatch, ...]:
        normalized = " ".join(query.casefold().split())
        wall_clock = dt.datetime.now(dt.UTC)
        if (
            not normalized
            or not 1 <= limit <= 50
            or now.tzinfo is None
            or now < wall_clock - dt.timedelta(seconds=30)
            or now > wall_clock + dt.timedelta(seconds=1)
        ):
            raise PolicyViolation("Deferred tool search query/limit/time gecersiz")
        compiled, catalog = self.repository.deferred_catalog(
            tool_set_digest=tool_set_digest,
            role=role,
            permission_profile_digest=permission_profile_digest,
            now=now,
        )
        compiled.assert_digest()
        if (
            compiled.tool_set_digest != tool_set_digest
            or compiled.role != role
            or compiled.permission_profile_digest != permission_profile_digest
        ):
            raise PolicyViolation("Deferred search compiled role/permission binding drift")
        terms = tuple(dict.fromkeys(normalized.split()))
        matches: list[DeferredToolMatch] = []
        for spec, runtime in catalog:
            spec.assert_digest()
            runtime.assert_digest()
            entry = compiled.entry(spec.tool_id)
            if entry.exposure.value != "deferred-search":
                raise PolicyViolation("Deferred catalog exposure disina cikti")
            if (
                entry.revision,
                entry.spec_digest,
                entry.runtime_digest,
                entry.tool_id,
            ) != (
                spec.revision,
                spec.spec_digest,
                runtime.runtime_digest,
                runtime.tool_id,
            ) or runtime.revision != entry.revision:
                raise PolicyViolation("Deferred catalog spec/runtime binding drift")
            if runtime.captured_at > now or runtime.expires_at <= now:
                raise PolicyViolation("Deferred catalog runtime stale")
            searchable = f"{spec.tool_id} {spec.name} {spec.description}".casefold()
            score = sum(1 for term in terms if term in searchable)
            if normalized in searchable:
                score += len(terms) + 1
            if score:
                matches.append(
                    DeferredToolMatch(
                        spec.tool_id,
                        spec.revision,
                        spec.name,
                        spec.description,
                        spec.spec_digest,
                        runtime.runtime_digest,
                        runtime.permission_capabilities,
                        score,
                    )
                )
        return tuple(sorted(matches, key=lambda item: (-item.score, item.tool_id))[:limit])


@dataclass(frozen=True, slots=True)
class ToolDispatchWavePlanner:
    repository: ToolWaveStore

    def plan(
        self,
        bindings: tuple[ToolDispatchBinding, ...],
        *,
        max_parallelism: int,
        now: dt.datetime,
    ) -> ToolDispatchPlan:
        if not bindings or max_parallelism < 1 or now.tzinfo is None:
            raise PolicyViolation("Tool dispatch wave input/parallelism/time gecersiz")
        ordered = tuple(
            sorted(
                bindings,
                key=lambda binding: (
                    binding.tool_id,
                    str(binding.effect_claim_id),
                    binding.input_digest,
                ),
            )
        )
        identities = tuple(
            (binding.effect_claim_id, binding.tool_id, binding.input_digest) for binding in bindings
        )
        if len(set(identities)) != len(identities):
            raise PolicyViolation("Tool dispatch wave duplicate binding iceremez")
        if (
            len({binding.turn_execution_snapshot_digest for binding in bindings}) != 1
            or len({binding.tool_set_digest for binding in bindings}) != 1
        ):
            raise PolicyViolation("Tool dispatch wave exact turn/compiled set ister")
        with self.repository.locked_wave_bundles(ordered) as bundles:
            if len(bundles) != len(ordered):
                raise PolicyViolation("Tool dispatch wave bundle sayisi drift")
            parallel: list[ToolDispatchBinding] = []
            waves: list[tuple[ToolDispatchBinding, ...]] = []
            for binding, (compiled, spec, runtime) in zip(ordered, bundles, strict=True):
                assert_tool_dispatch_binding(binding, compiled, spec, runtime, now=now)
                if not runtime.parallel_supported:
                    if parallel:
                        waves.append(tuple(parallel))
                        parallel.clear()
                    waves.append((binding,))
                    continue
                parallel.append(binding)
                if len(parallel) == max_parallelism:
                    waves.append(tuple(parallel))
                    parallel.clear()
            if parallel:
                waves.append(tuple(parallel))
        planned_waves = tuple(ToolDispatchWave(index, wave) for index, wave in enumerate(waves, 1))
        return ToolDispatchPlan.create(
            turn_execution_snapshot_digest=ordered[0].turn_execution_snapshot_digest,
            tool_set_digest=ordered[0].tool_set_digest,
            max_parallelism=max_parallelism,
            waves=planned_waves,
            grants_authority=False,
        )


@dataclass(frozen=True, slots=True)
class ToolDispatchService:
    repository: ToolRegistryStore

    def dispatch(
        self,
        binding: ToolDispatchBinding,
        adapter: ToolRuntimeAdapter[ResultT],
        *,
        now: dt.datetime | None = None,
        loop_attempt_id: UUID | None = None,
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
            if loop_attempt_id is not None:
                self.repository.bind_loop_dispatch(loop_attempt_id, binding.effect_claim_id)
            self.repository.record_dispatch_gate(binding, disposition="passed", checked_at=moment)
            permit = _issue_tool_execution_permit(binding)
            return adapter.execute(binding, permit=permit)
