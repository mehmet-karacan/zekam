"""Generation-pinned HookRuntime v2 orchestration."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import digest
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import (
    CompiledHookEntry,
    CompiledHookSet,
    HookAdapterResult,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookPreviewEntry,
    HookResultKind,
    HookRunOutcome,
    HookRuntimeRevision,
    HookSpecRevision,
    validate_payload,
)

HookCallable = Callable[[Any], HookAdapterResult]


@dataclass(frozen=True, slots=True)
class LoadedHookAdapter:
    adapter_ref: str
    adapter_digest: str
    execution_mode: HookExecutionMode
    invoke: HookCallable
    effect_capable: bool = False
    inherited_environment: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effect_capable or self.inherited_environment:
            raise PolicyViolation("Hook adapter direct effect veya inherited environment tasiyamaz")


@dataclass(frozen=True, slots=True)
class HookSession:
    session_id: UUID
    compiled_set: CompiledHookSet
    adapters: Mapping[str, LoadedHookAdapter]


@dataclass(frozen=True, slots=True)
class HookShutdownReceipt:
    joined: int
    cancelled: int
    still_running: int
    bounded: bool = True


class HookRuntime:
    """Hook'lari effect yetkisi vermeden preview ve execute eder."""

    def __init__(self, *, max_workers: int = 8) -> None:
        if max_workers < 1:
            raise ValidationFailed("Hook max_workers pozitif olmali")
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="zekam-hook"
        )
        self._current: tuple[CompiledHookSet, dict[str, LoadedHookAdapter]] | None = None
        self._generation = 0
        self._sessions: dict[UUID, HookSession] = {}
        self._futures: set[Future[HookAdapterResult]] = set()
        self._quarantined: set[tuple[int, str]] = set()
        self._closed = False

    def reconfigure(
        self,
        *,
        realm_id: UUID,
        config_effective_digest: str,
        specs: tuple[HookSpecRevision, ...],
        runtimes: tuple[HookRuntimeRevision, ...],
        profiles: tuple[PermissionProfileRevision, ...],
        adapters: tuple[LoadedHookAdapter, ...],
        now: dt.datetime,
    ) -> CompiledHookSet:
        """Yeni generation'i once tam derler, sonra atomik olarak aktif eder."""
        if now.tzinfo is None:
            raise ValidationFailed("Hook reconfigure zamani timezone-aware olmali")
        runtime_by_identity = {(item.hook_id, item.hook_revision): item for item in runtimes}
        profile_by_digest = {item.profile_digest: item for item in profiles}
        adapter_by_ref = {item.adapter_ref: item for item in adapters}
        if len(runtime_by_identity) != len(runtimes):
            raise ValidationFailed("Hook runtime identity duplicate olamaz")
        if len(profile_by_digest) != len(profiles):
            raise ValidationFailed("Hook permission profile digest duplicate olamaz")
        if len(adapter_by_ref) != len(adapters):
            raise ValidationFailed("Hook adapter ref duplicate olamaz")
        ordered_specs = tuple(sorted(specs, key=lambda item: (item.event_type.value, item.hook_id)))
        if len({(item.hook_id, item.revision) for item in ordered_specs}) != len(ordered_specs):
            raise ValidationFailed("Hook reconfigure duplicate spec iceremez")
        if len({item.hook_id for item in ordered_specs}) != len(ordered_specs):
            raise ValidationFailed("Hook set ayni hook_id icin tek revision ister")
        if any(item.realm_id != realm_id for item in ordered_specs + runtimes):
            raise PolicyViolation("Hook reconfigure cross-realm binding reddedildi")
        entries: list[CompiledHookEntry] = []
        required_errors: list[str] = []
        loaded: dict[str, LoadedHookAdapter] = {}
        for ordinal, spec in enumerate(ordered_specs, start=1):
            spec.assert_integrity()
            reason: str | None = None
            runtime = runtime_by_identity.get((spec.hook_id, spec.revision))
            profile = profile_by_digest.get(spec.permission_profile_digest)
            adapter = None if runtime is None else adapter_by_ref.get(runtime.adapter_ref)
            if (
                profile is None
                or profile.name != spec.permission_profile_name
                or profile.realm_id
                not in (
                    None,
                    realm_id,
                )
            ):
                reason = "permission-profile-unavailable"
            elif runtime is None:
                reason = "runtime-unavailable"
            elif runtime.load_state is not HookLoadState.READY:
                reason = f"runtime-{runtime.load_state.value}"
            elif runtime.captured_at > now:
                reason = "runtime-future-dated"
            elif runtime.expires_at <= now:
                reason = "runtime-stale"
            elif (
                adapter is None
                or adapter.adapter_digest != runtime.adapter_digest
                or adapter.execution_mode is not spec.execution_mode
            ):
                reason = "adapter-unavailable-or-drifted"
            else:
                runtime.assert_integrity()
                try:
                    profile.resolve_session(runtime.permission_capabilities)
                except PolicyViolation:
                    reason = "permission-capability-denied"
                else:
                    loaded[spec.hook_id] = adapter
            if reason is not None:
                if spec.required:
                    required_errors.append(f"{spec.hook_id}:{reason}")
                entries.append(CompiledHookEntry(ordinal, spec, None, reason))
            else:
                entries.append(CompiledHookEntry(ordinal, spec, runtime, None))
        with self._lock:
            self._ensure_open()
            generation = self._generation + 1
            compiled = CompiledHookSet.create(
                realm_id=realm_id,
                generation=generation,
                config_effective_digest=config_effective_digest,
                entries=tuple(entries),
                required_load_errors=tuple(required_errors),
            )
            if required_errors and self._current is not None:
                raise PolicyViolation(
                    "Required hook reconfigure basarisiz; onceki generation korundu"
                )
            self._generation = generation
            self._current = (compiled, loaded)
            return compiled

    def start_session(self) -> HookSession:
        with self._lock:
            self._ensure_open()
            if self._current is None:
                raise PolicyViolation("Hook runtime configure edilmeden session baslatilamaz")
            compiled, adapters = self._current
            compiled.assert_session_startable()
            quarantined_required = tuple(
                entry.spec.hook_id
                for entry in compiled.entries
                if entry.spec.required
                and (compiled.generation, entry.spec.hook_id) in self._quarantined
            )
            if quarantined_required:
                raise PolicyViolation(
                    "Required hook quarantined; session baslangici reddedildi: "
                    + ", ".join(quarantined_required)
                )
            session = HookSession(uuid4(), compiled, dict(adapters))
            self._sessions[session.session_id] = session
            return session

    def close_session(self, session: HookSession) -> None:
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current != session:
                raise PolicyViolation("Hook session current degil veya zaten kapali")
            del self._sessions[session.session_id]

    def preview(
        self,
        session: HookSession,
        event_type: HookEventType,
        payload: Any,
    ) -> tuple[HookPreviewEntry, ...]:
        self._assert_session(session)
        input_digest = digest(payload)
        rows: list[HookPreviewEntry] = []
        for entry in session.compiled_set.entries:
            if entry.spec.event_type is not event_type:
                continue
            validate_payload(entry.spec.input_schema, payload, "hook input")
            quarantined = (
                session.compiled_set.generation,
                entry.spec.hook_id,
            ) in self._quarantined
            reason = "quarantined" if quarantined else entry.disabled_reason
            rows.append(
                HookPreviewEntry(
                    entry.spec.hook_id,
                    entry.spec.revision,
                    event_type,
                    input_digest,
                    entry.spec.timeout_ms,
                    entry.spec.permission_profile_digest,
                    entry.spec.failure_policy,
                    entry.spec.hook_digest,
                    reason is None,
                    reason,
                )
            )
        return tuple(rows)

    def run(
        self,
        session: HookSession,
        event_type: HookEventType,
        payload: Any,
    ) -> tuple[HookRunOutcome, ...]:
        previews = self.preview(session, event_type, payload)
        entries = {
            (entry.spec.hook_id, entry.spec.revision): entry
            for entry in session.compiled_set.entries
        }
        outcomes: list[HookRunOutcome] = []
        for preview in previews:
            entry = entries[(preview.hook_id, preview.hook_revision)]
            if not preview.will_execute:
                outcomes.append(
                    self._failed_outcome(entry.spec, payload, preview.disabled_reason or "disabled")
                )
                continue
            adapter = session.adapters[entry.spec.hook_id]
            future = self._executor.submit(adapter.invoke, payload)
            with self._lock:
                self._futures.add(future)
            try:
                result = future.result(timeout=entry.spec.timeout_ms / 1000)
                if not isinstance(result, HookAdapterResult):
                    raise ValidationFailed("Hook adapter typed result dondurmedi")
                validate_payload(entry.spec.output_schema, result.payload, "hook output")
                output_digest = digest(result.payload)
                outcomes.append(
                    HookRunOutcome(
                        entry.spec.hook_id,
                        entry.spec.revision,
                        result.kind,
                        "completed",
                        digest(payload),
                        output_digest,
                        output_digest if result.kind is HookResultKind.PROPOSAL else None,
                        None,
                        result.kind is HookResultKind.PROPOSAL,
                    )
                )
            except Exception as exc:
                outcome = self._handle_failure(
                    entry.spec, payload, exc, session.compiled_set.generation
                )
                outcomes.append(outcome)
            finally:
                with self._lock:
                    if future.done():
                        self._futures.discard(future)
        return tuple(outcomes)

    def shutdown(self, *, timeout_seconds: float) -> HookShutdownReceipt:
        if timeout_seconds < 0:
            raise ValidationFailed("Hook shutdown timeout negatif olamaz")
        with self._lock:
            self._closed = True
            futures = set(self._futures)
        done, pending = wait(futures, timeout=timeout_seconds)
        cancelled = sum(1 for future in pending if future.cancel())
        self._executor.shutdown(wait=False, cancel_futures=True)
        return HookShutdownReceipt(len(done), cancelled, len(pending) - cancelled)

    def _handle_failure(
        self,
        spec: HookSpecRevision,
        payload: Any,
        exc: BaseException,
        generation: int,
    ) -> HookRunOutcome:
        reason = type(exc).__name__
        if spec.failure_policy is HookFailurePolicy.QUARANTINE:
            with self._lock:
                self._quarantined.add((generation, spec.hook_id))
        if spec.required or spec.failure_policy is HookFailurePolicy.ABORT:
            raise PolicyViolation(
                f"Hook calismasi fail-closed durdu: {spec.hook_id}:{reason}"
            ) from exc
        return self._failed_outcome(spec, payload, reason)

    @staticmethod
    def _failed_outcome(
        spec: HookSpecRevision,
        payload: Any,
        reason: str,
    ) -> HookRunOutcome:
        return HookRunOutcome(
            spec.hook_id,
            spec.revision,
            None,
            "warning" if spec.failure_policy is HookFailurePolicy.WARN else "quarantined",
            digest(payload),
            None,
            None,
            reason,
            False,
        )

    def _assert_session(self, session: HookSession) -> None:
        with self._lock:
            if self._sessions.get(session.session_id) != session:
                raise PolicyViolation("Hook session aktif degil")

    def _ensure_open(self) -> None:
        if self._closed:
            raise PolicyViolation("Hook runtime shutdown sonrasinda kullanilamaz")
