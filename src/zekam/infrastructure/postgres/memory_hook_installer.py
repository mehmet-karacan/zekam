"""Idempotent installer with exact managed Memory Continuity revision upgrade."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.governance import EffectRequest
from zekam.application.memory_hooks import (
    MEMORY_HOOK_EVENTS,
    memory_hook_bundle,
    memory_hook_v1_identities,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.security import DataClassification
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.config_provenance_repository import (
    ConfigProvenanceRepository,
)
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository


@dataclass(frozen=True, slots=True)
class MemoryHookInstallReceipt:
    created: bool
    generation: int
    hook_set_digest: str
    bundle_digest: str


@dataclass(frozen=True, slots=True)
class MemoryHookUpgradePlan:
    realm_id: UUID
    current_generation: int
    current_hook_set_digest: str
    expected_bundle_digest: str
    resource: str
    effect_digest: str
    plan_digest: str

    @property
    def effect_request(self) -> EffectRequest:
        return EffectRequest(
            action="memory-hook-upgrade",
            effects=(EffectKind.DATABASE_WRITE,),
            resources=(self.resource,),
            data_classifications=(DataClassification.LOCAL_ONLY,),
            required_capabilities=(),
        )

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-hook-upgrade-plan/v1",
            "realm_id": str(self.realm_id),
            "current_generation": self.current_generation,
            "current_hook_set_digest": self.current_hook_set_digest,
            "expected_bundle_digest": self.expected_bundle_digest,
            "resource": self.resource,
            "effect_digest": self.effect_digest,
            "plan_digest": self.plan_digest,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class PostgresMemoryHookInstaller:
    connection: Any
    realm_id: UUID

    def plan_upgrade(self) -> MemoryHookUpgradePlan:
        _, generation, current_digest = self._current()
        if current_digest is None:
            raise PolicyViolation("Memory hook upgrade current generation ister")
        bundle_digest = memory_hook_bundle(self.realm_id).bundle_digest
        resource = f"db-object:memory-hook-generation:{self.realm_id}"
        effect_digest = EffectRequest(
            action="memory-hook-upgrade",
            effects=(EffectKind.DATABASE_WRITE,),
            resources=(resource,),
            data_classifications=(DataClassification.LOCAL_ONLY,),
            required_capabilities=(),
        ).effect_digest
        plan_digest = digest(
            {
                "schema": "zekam-memory-hook-upgrade-plan/v1",
                "realm_id": str(self.realm_id),
                "current_generation": generation,
                "current_hook_set_digest": current_digest,
                "expected_bundle_digest": bundle_digest,
                "resource": resource,
                "effect_digest": effect_digest,
                "grants_authority": False,
            }
        )
        return MemoryHookUpgradePlan(
            self.realm_id,
            generation,
            current_digest,
            bundle_digest,
            resource,
            effect_digest,
            plan_digest,
        )

    def ensure(self, *, installed_at: dt.datetime) -> MemoryHookInstallReceipt:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("select core.current_realm_id()")
                row = cursor.fetchone()
                if row is None or row[0] is None or UUID(str(row[0])) != self.realm_id:
                    raise PolicyViolation("Memory hook installer realm session binding mismatch")
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"{self.realm_id}:hook-generation",),
                )
            return self._ensure_locked(installed_at=installed_at)

    def _ensure_locked(self, *, installed_at: dt.datetime) -> MemoryHookInstallReceipt:
        bundle = memory_hook_bundle(self.realm_id)
        current_id, current_generation, current_digest = self._current()
        current_entries = self._current_entries(current_id)
        handlers = self._effective_handlers(current_id)
        predecessors = self._upgradeable_predecessors(current_id)
        conflicts: list[str] = []
        for event, spec, runtime in zip(
            MEMORY_HOOK_EVENTS, bundle.specs, bundle.runtimes, strict=True
        ):
            allowed = {(), ((spec.hook_digest, runtime.runtime_digest),)}
            predecessor = predecessors.get(event.value)
            if predecessor is not None:
                allowed.add(predecessor[0])
            if handlers.get(event.value, ()) not in allowed:
                conflicts.append(event.value)
        if conflicts:
            raise PolicyViolation(
                "Required lifecycle handler conflict; existing generation preserved: "
                + ",".join(conflicts)
            )
        handlers_current = all(
            handlers.get(event.value) == ((spec.hook_digest, runtime.runtime_digest),)
            for event, spec, runtime in zip(
                MEMORY_HOOK_EVENTS, bundle.specs, bundle.runtimes, strict=True
            )
        )
        canonical_current_order = current_entries == sorted(
            current_entries,
            key=lambda item: (item["event_type"], item["hook_id"]),
        )
        if handlers_current and canonical_current_order:
            if current_id is None or current_digest is None:
                raise PolicyViolation("Hook exact-one count current generation olmadan olusamaz")
            return MemoryHookInstallReceipt(
                False,
                current_generation,
                current_digest,
                bundle.bundle_digest,
            )

        profile_id, _ = ConfigProvenanceRepository(self.connection, self.realm_id).store_profile(
            bundle.profile
        )
        hook_repository = HookRuntimeRepository(self.connection, self.realm_id)
        for spec, runtime in zip(bundle.specs, bundle.runtimes, strict=True):
            hook_repository.store_spec(spec, permission_profile_revision_id=profile_id)
            hook_repository.store_runtime(runtime)

        managed_spec_ids = {value[1] for value in predecessors.values()} | {
            spec.id for spec in bundle.specs
        }
        entries = [item for item in current_entries if item["spec_id"] not in managed_spec_ids]
        for event, spec, runtime in zip(
            MEMORY_HOOK_EVENTS, bundle.specs, bundle.runtimes, strict=True
        ):
            entries.append(
                {
                    "ordinal": 0,
                    "spec_id": spec.id,
                    "runtime_id": runtime.id,
                    "hook_digest": spec.hook_digest,
                    "runtime_digest": runtime.runtime_digest,
                    "disabled_reason": None,
                    "event_type": event.value,
                    "hook_id": spec.hook_id,
                }
            )
        entries.sort(key=lambda item: (item["event_type"], item["hook_id"]))
        for ordinal, item in enumerate(entries, start=1):
            item["ordinal"] = ordinal
        generation = current_generation + 1
        config_digest = digest(
            {
                "previous_hook_set_digest": current_digest,
                "memory_hook_bundle_digest": bundle.bundle_digest,
                "generation": generation,
            }
        )
        body = {
            "schema": "zekam-compiled-hook-set/v1",
            "realm_id": str(self.realm_id),
            "generation": generation,
            "config_effective_digest": config_digest,
            "entries": [
                {
                    "ordinal": item["ordinal"],
                    "hook_digest": item["hook_digest"],
                    "runtime_digest": item["runtime_digest"],
                    "disabled_reason": item["disabled_reason"],
                }
                for item in entries
            ],
            "required_load_errors": [],
            "grants_authority": False,
        }
        hook_set_digest = digest(body)
        set_id = new_uuid7(now=installed_at)
        with self.connection.cursor() as cursor:
            cursor.execute("set constraints hooks.compiled_hook_set_guard deferred")
            cursor.execute("set constraints hooks.compiled_hook_entry_guard deferred")
            cursor.execute(
                "insert into hooks.compiled_set"
                " (id,realm_id,generation,config_effective_digest,required_load_errors,"
                " hook_set_digest,set_body,created_at,grants_authority)"
                " values(%s,%s,%s,%s,'{}'::text[],%s,%s::jsonb,%s,false)",
                (
                    set_id,
                    self.realm_id,
                    generation,
                    config_digest,
                    hook_set_digest,
                    canonical_json(body),
                    installed_at,
                ),
            )
            for item in entries:
                cursor.execute(
                    "insert into hooks.compiled_set_entry"
                    " (realm_id,compiled_set_id,ordinal,spec_revision_id,runtime_revision_id,"
                    " disabled_reason) values(%s,%s,%s,%s,%s,%s)",
                    (
                        self.realm_id,
                        set_id,
                        item["ordinal"],
                        item["spec_id"],
                        item["runtime_id"],
                        item["disabled_reason"],
                    ),
                )
            cursor.execute(
                "select generation,hook_set_digest from hooks.activate_compiled_set(%s)", (set_id,)
            )
            activated = cursor.fetchone()
            if activated is None or (int(activated[0]), str(activated[1])) != (
                generation,
                hook_set_digest,
            ):
                raise PolicyViolation("Memory hook generation activation receipt mismatch")
        return MemoryHookInstallReceipt(True, generation, hook_set_digest, bundle.bundle_digest)

    def _current(self) -> tuple[UUID | None, int, str | None]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select compiled_set_id,generation,hook_set_digest"
                " from hooks.current_generation where realm_id=%s",
                (self.realm_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None, 0, None
        return UUID(str(row[0])), int(row[1]), str(row[2])

    def _effective_handlers(
        self, current_id: UUID | None
    ) -> dict[str, tuple[tuple[str, str], ...]]:
        if current_id is None:
            return {}
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select spec.event_type,spec.hook_digest,runtime.runtime_digest"
                " from hooks.compiled_set_entry entry join hooks.spec_revision spec"
                " on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id"
                " join hooks.runtime_revision runtime on runtime.realm_id=entry.realm_id"
                " and runtime.id=entry.runtime_revision_id"
                " where entry.realm_id=%s and entry.compiled_set_id=%s"
                " and spec.required and entry.disabled_reason is null"
                " and spec.event_type=any(%s) order by spec.event_type,spec.hook_digest",
                (self.realm_id, current_id, [item.value for item in MEMORY_HOOK_EVENTS]),
            )
            result: dict[str, list[tuple[str, str]]] = {}
            for row in cursor.fetchall():
                result.setdefault(str(row[0]), []).append((str(row[1]), str(row[2])))
        return {key: tuple(value) for key, value in result.items()}

    def _upgradeable_predecessors(
        self, current_id: UUID | None
    ) -> dict[str, tuple[tuple[tuple[str, str], ...], UUID]]:
        """Resolve only exact older managed bundle entries as replaceable."""

        if current_id is None:
            return {}
        expected = memory_hook_v1_identities(self.realm_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select spec.event_type,spec.id,spec.hook_id,spec.revision,spec.hook_digest,"
                " runtime.runtime_digest,runtime.adapter_ref"
                " from hooks.compiled_set_entry entry"
                " join hooks.spec_revision spec on spec.realm_id=entry.realm_id"
                " and spec.id=entry.spec_revision_id"
                " join hooks.runtime_revision runtime on runtime.realm_id=entry.realm_id"
                " and runtime.id=entry.runtime_revision_id"
                " where entry.realm_id=%s and entry.compiled_set_id=%s"
                " and entry.disabled_reason is null and spec.required"
                " and spec.source_layer='memory-continuity'"
                " and spec.permission_profile_name='memory-continuity-internal'"
                " and spec.execution_mode='internal'"
                " and cardinality(runtime.permission_capabilities)=0"
                " and spec.event_type=any(%s)"
                " order by spec.event_type,spec.revision",
                (self.realm_id, current_id, [item.value for item in MEMORY_HOOK_EVENTS]),
            )
            grouped: dict[str, list[tuple[Any, ...]]] = {}
            for row in cursor.fetchall():
                grouped.setdefault(str(row[0]), []).append(tuple(row))
        resolved: dict[str, tuple[tuple[tuple[str, str], ...], UUID]] = {}
        for event in MEMORY_HOOK_EVENTS:
            rows = grouped.get(event.value, [])
            if len(rows) != 1:
                continue
            row = rows[0]
            revision = int(row[3])
            expected_spec_id, expected_hook_digest, expected_runtime_digest = expected[event]
            if (
                UUID(str(row[1])) != expected_spec_id
                or str(row[2]) != f"memory-continuity-{event.value}"
                or revision != 1
                or str(row[4]) != expected_hook_digest
                or str(row[5]) != expected_runtime_digest
                or str(row[6]) != f"memory-continuity-{event.value}-v1"
            ):
                continue
            resolved[event.value] = (
                ((str(row[4]), str(row[5])),),
                UUID(str(row[1])),
            )
        return resolved

    def _current_entries(self, current_id: UUID | None) -> list[dict[str, Any]]:
        if current_id is None:
            return []
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select entry.ordinal,entry.spec_revision_id,entry.runtime_revision_id,"
                " spec.hook_digest,runtime.runtime_digest,entry.disabled_reason,"
                " spec.event_type,spec.hook_id"
                " from hooks.compiled_set_entry entry join hooks.spec_revision spec"
                " on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id"
                " left join hooks.runtime_revision runtime"
                " on runtime.realm_id=entry.realm_id and runtime.id=entry.runtime_revision_id"
                " where entry.realm_id=%s and entry.compiled_set_id=%s order by entry.ordinal",
                (self.realm_id, current_id),
            )
            return [
                {
                    "ordinal": int(row[0]),
                    "spec_id": UUID(str(row[1])),
                    "runtime_id": None if row[2] is None else UUID(str(row[2])),
                    "hook_digest": str(row[3]),
                    "runtime_digest": None if row[4] is None else str(row[4]),
                    "disabled_reason": None if row[5] is None else str(row[5]),
                    "event_type": str(row[6]),
                    "hook_id": str(row[7]),
                }
                for row in cursor.fetchall()
            ]
