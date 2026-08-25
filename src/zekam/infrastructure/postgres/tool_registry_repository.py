"""PostgreSQL persistence and current-runtime projection for ToolRegistry v2."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.tool_registry import (
    CompiledToolSet,
    ToolDispatchBinding,
    ToolExposure,
    ToolRuntimeRevision,
    ToolSetEntry,
    ToolSpecRevision,
)


@dataclass(frozen=True, slots=True)
class ToolRegistryRepository:
    connection: Any
    realm_id: UUID

    def bind_loop_dispatch(self, attempt_id: UUID, dispatch_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.bind_loop_dispatch(%s,'tool',%s)",
                (attempt_id, dispatch_id),
            )

    def store_spec(self, item: ToolSpecRevision) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        item.assert_digest()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into tools.spec_revision"
                "(id,realm_id,tool_id,revision,name,description,input_schema_digest,"
                "output_schema_digest,created_at,spec_digest) values"
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(realm_id,spec_digest) do nothing"
                " returning id",
                (
                    item.id,
                    item.realm_id,
                    item.tool_id,
                    item.revision,
                    item.name,
                    item.description,
                    item.input_schema_digest,
                    item.output_schema_digest,
                    item.created_at,
                    item.spec_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from tools.spec_revision where realm_id=%s and spec_digest=%s",
                (self.realm_id, item.spec_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def store_runtime(self, item: ToolRuntimeRevision) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        item.assert_digest()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into tools.runtime_revision"
                "(id,realm_id,tool_id,revision,adapter_ref,executable_revision,"
                "executable_digest,permission_capabilities,parallel_supported,captured_at,"
                "expires_at,runtime_digest) values(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)"
                " on conflict(realm_id,runtime_digest) do nothing returning id",
                (
                    item.id,
                    item.realm_id,
                    item.tool_id,
                    item.revision,
                    item.adapter_ref,
                    item.executable_revision,
                    item.executable_digest,
                    json.dumps(item.permission_capabilities),
                    item.parallel_supported,
                    item.captured_at,
                    item.expires_at,
                    item.runtime_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from tools.runtime_revision where realm_id=%s and runtime_digest=%s",
                (self.realm_id, item.runtime_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def store_compiled_set(self, item: CompiledToolSet) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        item.assert_digest()
        entries = [entry.body() for entry in item.entries]
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into tools.compiled_set"
                "(id,realm_id,role,permission_profile_digest,entries,created_at,"
                "tool_set_digest,grants_authority) values(%s,%s,%s,%s,%s::jsonb,%s,%s,false)"
                " on conflict(realm_id,tool_set_digest) do nothing returning id",
                (
                    item.id,
                    item.realm_id,
                    item.role,
                    item.permission_profile_digest,
                    json.dumps(entries),
                    item.created_at,
                    item.tool_set_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from tools.compiled_set where realm_id=%s and tool_set_digest=%s",
                (self.realm_id, item.tool_set_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    @contextmanager
    def locked_dispatch_bundle(self, binding: ToolDispatchBinding):  # type: ignore[no-untyped-def]
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{self.realm_id}:{binding.tool_id}",),
            )
            yield self._current_dispatch_bundle(binding)

    def _current_dispatch_bundle(
        self, binding: ToolDispatchBinding
    ) -> tuple[CompiledToolSet, ToolSpecRevision, ToolRuntimeRevision]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from runtime.effect_claim c"
                " join runtime.job j on j.realm_id=c.realm_id and j.id=c.job_id"
                " join runtime.turn_execution_snapshot t on t.realm_id=c.realm_id"
                " and t.attempt_id=c.attempt_id and t.run_id=j.run_id"
                " and t.assignment_id=j.assignment_id"
                " join agents.assignment a on a.realm_id=t.realm_id and a.id=t.assignment_id"
                " join runtime.execution_environment_snapshot env on env.realm_id=t.realm_id"
                " and env.snapshot_digest=t.execution_environment_snapshot_digest"
                " join tools.compiled_set tool_set on tool_set.realm_id=t.realm_id"
                " and tool_set.tool_set_digest=t.exposed_tool_set_digest"
                " where c.realm_id=%s and c.id=%s and t.turn_snapshot_digest=%s"
                " and t.exposed_tool_set_digest=%s"
                " and tool_set.role=a.role"
                " and tool_set.permission_profile_digest=env.permission_profile_digest"
                " and not exists(select 1 from runtime.effect_receipt receipt"
                " where receipt.realm_id=c.realm_id and receipt.claim_id=c.id)",
                (
                    self.realm_id,
                    binding.effect_claim_id,
                    binding.turn_execution_snapshot_digest,
                    binding.tool_set_digest,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Tool dispatch exact unreceipted claim/turn binding ister")
            cursor.execute(
                "select id,role,permission_profile_digest,entries,created_at,tool_set_digest"
                " from tools.compiled_set where realm_id=%s and tool_set_digest=%s",
                (self.realm_id, binding.tool_set_digest),
            )
            set_row = cursor.fetchone()
            if set_row is None:
                raise PolicyViolation("Compiled tool set bulunamadi")
            entries = tuple(
                ToolSetEntry(
                    str(value["tool_id"]),
                    int(value["revision"]),
                    ToolExposure(str(value["exposure"])),
                    str(value["spec_digest"]),
                    str(value["runtime_digest"]),
                )
                for value in set_row[3]
            )
            compiled = CompiledToolSet(
                UUID(str(set_row[0])),
                self.realm_id,
                str(set_row[1]),
                str(set_row[2]),
                entries,
                set_row[4],
                str(set_row[5]),
            )
            entry = compiled.entry(binding.tool_id)
            cursor.execute(
                "select id,tool_id,revision,name,description,input_schema_digest,"
                "output_schema_digest,created_at,spec_digest from tools.spec_revision"
                " where realm_id=%s and tool_id=%s and revision=%s and spec_digest=%s",
                (self.realm_id, binding.tool_id, entry.revision, entry.spec_digest),
            )
            spec_row = cursor.fetchone()
            cursor.execute(
                "select id,tool_id,revision,adapter_ref,executable_revision,executable_digest,"
                "permission_capabilities,parallel_supported,captured_at,expires_at,runtime_digest"
                " from tools.runtime_revision where realm_id=%s and tool_id=%s"
                " and captured_at<=statement_timestamp() and expires_at>statement_timestamp()"
                " order by revision desc,captured_at desc,id desc limit 1",
                (self.realm_id, binding.tool_id),
            )
            runtime_row = cursor.fetchone()
        if spec_row is None or runtime_row is None:
            raise PolicyViolation("Tool exact spec veya current runtime bulunamadi")
        spec = ToolSpecRevision(
            UUID(str(spec_row[0])),
            self.realm_id,
            str(spec_row[1]),
            int(spec_row[2]),
            str(spec_row[3]),
            str(spec_row[4]),
            str(spec_row[5]),
            str(spec_row[6]),
            spec_row[7],
            str(spec_row[8]),
        )
        runtime = ToolRuntimeRevision(
            UUID(str(runtime_row[0])),
            self.realm_id,
            str(runtime_row[1]),
            int(runtime_row[2]),
            str(runtime_row[3]),
            str(runtime_row[4]),
            str(runtime_row[5]),
            tuple(str(value) for value in runtime_row[6]),
            bool(runtime_row[7]),
            runtime_row[8],
            runtime_row[9],
            str(runtime_row[10]),
        )
        return compiled, spec, runtime

    def record_dispatch_gate(
        self,
        binding: ToolDispatchBinding,
        *,
        disposition: str,
        checked_at: dt.datetime,
    ) -> None:
        evidence_digest = digest(
            {
                "realm_id": str(self.realm_id),
                "effect_claim_id": str(binding.effect_claim_id),
                "turn_execution_snapshot_digest": binding.turn_execution_snapshot_digest,
                "tool_set_digest": binding.tool_set_digest,
                "tool_id": binding.tool_id,
                "revision": binding.revision,
                "spec_digest": binding.spec_digest,
                "runtime_digest": binding.runtime_digest,
                "input_digest": binding.input_digest,
                "disposition": disposition,
                "checked_at": checked_at,
            }
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into tools.dispatch_gate_evidence"
                "(id,realm_id,effect_claim_id,turn_execution_snapshot_digest,tool_set_digest,"
                "tool_id,revision,spec_digest,runtime_digest,input_digest,disposition,"
                "checked_at,evidence_digest) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    new_uuid7(),
                    self.realm_id,
                    binding.effect_claim_id,
                    binding.turn_execution_snapshot_digest,
                    binding.tool_set_digest,
                    binding.tool_id,
                    binding.revision,
                    binding.spec_digest,
                    binding.runtime_digest,
                    binding.input_digest,
                    disposition,
                    checked_at,
                    evidence_digest,
                ),
            )

    def _realm(self, value: UUID) -> None:
        if value != self.realm_id:
            raise PolicyViolation("Tool registry cross-realm yazma reddedildi")
