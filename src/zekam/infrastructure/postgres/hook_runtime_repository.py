"""PostgreSQL persistence for HookRuntime v2 registry snapshots."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.hook_runtime import (
    CompiledHookEntry,
    CompiledHookSet,
    HookResultKind,
    HookRunOutcome,
    HookRuntimeRevision,
    HookSpecRevision,
    validate_payload,
)


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


@dataclass(frozen=True, slots=True)
class HookRuntimeRepository:
    connection: Any
    realm_id: UUID

    def store_spec(
        self,
        spec: HookSpecRevision,
        *,
        permission_profile_revision_id: UUID,
    ) -> tuple[UUID, bool]:
        if spec.realm_id != self.realm_id:
            raise PolicyViolation("Hook spec cross-realm yazma reddedildi")
        spec.assert_integrity()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{self.realm_id}:{spec.hook_id}",),
            )
            replay_id = self._exact_spec_replay(
                cursor,
                spec=spec,
                permission_profile_revision_id=permission_profile_revision_id,
            )
            if replay_id is not None:
                return replay_id, False
            cursor.execute(
                "insert into hooks.spec_revision"
                "(id,realm_id,hook_id,revision,event_type,required,source_layer,timeout_ms,"
                "execution_mode,input_schema,output_schema,input_schema_digest,"
                "output_schema_digest,permission_profile_revision_id,permission_profile_name,"
                "permission_profile_digest,failure_policy,created_at,hook_digest,hook_body,"
                "grants_authority) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,false)"
                " on conflict(realm_id,hook_digest) do nothing returning id",
                (
                    spec.id,
                    self.realm_id,
                    spec.hook_id,
                    spec.revision,
                    spec.event_type.value,
                    spec.required,
                    spec.source_layer,
                    spec.timeout_ms,
                    spec.execution_mode.value,
                    _json(spec.input_schema),
                    _json(spec.output_schema),
                    spec.input_schema_digest,
                    spec.output_schema_digest,
                    permission_profile_revision_id,
                    spec.permission_profile_name,
                    spec.permission_profile_digest,
                    spec.failure_policy.value,
                    spec.created_at,
                    spec.hook_digest,
                    _json(spec.body()),
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            replay_id = self._exact_spec_replay(
                cursor,
                spec=spec,
                permission_profile_revision_id=permission_profile_revision_id,
            )
            if replay_id is None:
                raise PolicyViolation("Hook spec exact replay kaydi bulunamadi")
            return replay_id, False

    def _exact_spec_replay(
        self,
        cursor: Any,
        *,
        spec: HookSpecRevision,
        permission_profile_revision_id: UUID,
    ) -> UUID | None:
        cursor.execute(
            "select id,permission_profile_revision_id,hook_id,revision"
            " from hooks.spec_revision where realm_id=%s and hook_digest=%s",
            (self.realm_id, spec.hook_digest),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        replay_id = UUID(str(row[0]))
        if (
            replay_id != spec.id
            or UUID(str(row[1])) != permission_profile_revision_id
            or str(row[2]) != spec.hook_id
            or int(row[3]) != spec.revision
        ):
            raise PolicyViolation("Hook spec exact replay binding mismatch")
        return replay_id

    def store_runtime(self, runtime: HookRuntimeRevision) -> tuple[UUID, bool]:
        if runtime.realm_id != self.realm_id:
            raise PolicyViolation("Hook runtime cross-realm yazma reddedildi")
        runtime.assert_integrity()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into hooks.runtime_revision"
                "(id,realm_id,hook_id,hook_revision,adapter_ref,adapter_digest,"
                "permission_capabilities,load_state,captured_at,expires_at,runtime_digest,"
                "runtime_body) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
                " on conflict(realm_id,hook_id,hook_revision,runtime_digest) do nothing"
                " returning id",
                (
                    runtime.id,
                    self.realm_id,
                    runtime.hook_id,
                    runtime.hook_revision,
                    runtime.adapter_ref,
                    runtime.adapter_digest,
                    list(runtime.permission_capabilities),
                    runtime.load_state.value,
                    runtime.captured_at,
                    runtime.expires_at,
                    runtime.runtime_digest,
                    _json(runtime.body()),
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from hooks.runtime_revision where realm_id=%s and hook_id=%s"
                " and hook_revision=%s and runtime_digest=%s",
                (
                    self.realm_id,
                    runtime.hook_id,
                    runtime.hook_revision,
                    runtime.runtime_digest,
                ),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def store_compiled_set(
        self,
        compiled: CompiledHookSet,
        *,
        created_at: dt.datetime,
    ) -> tuple[UUID, bool]:
        if compiled.realm_id != self.realm_id:
            raise PolicyViolation("Compiled hook set cross-realm yazma reddedildi")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("set constraints hooks.compiled_hook_set_guard deferred")
            cursor.execute("set constraints hooks.compiled_hook_entry_guard deferred")
            cursor.execute(
                "select id from hooks.compiled_set where realm_id=%s and hook_set_digest=%s",
                (self.realm_id, compiled.hook_set_digest),
            )
            existing = cursor.fetchone()
            if existing is not None:
                return UUID(str(existing[0])), False
            set_id = uuid4()
            cursor.execute(
                "insert into hooks.compiled_set"
                "(id,realm_id,generation,config_effective_digest,required_load_errors,"
                "hook_set_digest,set_body,created_at,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,false)",
                (
                    set_id,
                    self.realm_id,
                    compiled.generation,
                    compiled.config_effective_digest,
                    list(compiled.required_load_errors),
                    compiled.hook_set_digest,
                    _json(compiled.body()),
                    created_at,
                ),
            )
            for entry in compiled.entries:
                cursor.execute(
                    "select id from hooks.spec_revision where realm_id=%s and hook_digest=%s",
                    (self.realm_id, entry.spec.hook_digest),
                )
                spec_row = cursor.fetchone()
                if spec_row is None:
                    raise PolicyViolation("Compiled hook spec registry'de bulunamadi")
                runtime_id = None
                if entry.runtime is not None:
                    cursor.execute(
                        "select id from hooks.runtime_revision"
                        " where realm_id=%s and runtime_digest=%s",
                        (self.realm_id, entry.runtime.runtime_digest),
                    )
                    runtime_row = cursor.fetchone()
                    if runtime_row is None:
                        raise PolicyViolation("Compiled hook runtime registry'de bulunamadi")
                    runtime_id = runtime_row[0]
                cursor.execute(
                    "insert into hooks.compiled_set_entry"
                    "(realm_id,compiled_set_id,ordinal,spec_revision_id,runtime_revision_id,"
                    "disabled_reason) values(%s,%s,%s,%s,%s,%s)",
                    (
                        self.realm_id,
                        set_id,
                        entry.ordinal,
                        spec_row[0],
                        runtime_id,
                        entry.disabled_reason,
                    ),
                )
            cursor.execute("set constraints hooks.compiled_hook_set_guard immediate")
            cursor.execute("set constraints hooks.compiled_hook_entry_guard immediate")
            cursor.execute("set constraints hooks.compiled_hook_set_guard deferred")
            cursor.execute("set constraints hooks.compiled_hook_entry_guard deferred")
            return set_id, True

    def activate(self, compiled_set_id: UUID) -> tuple[int, str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select generation,hook_set_digest from hooks.activate_compiled_set(%s)",
                (compiled_set_id,),
            )
            row = cursor.fetchone()
            return int(row[0]), str(row[1])

    def start_session(
        self,
        *,
        session_ref: str,
    ) -> UUID:
        session_id = uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select session_binding_id from hooks.start_session(%s,%s)",
                (session_id, session_ref),
            )
            row = cursor.fetchone()
            if row is None:
                raise PolicyViolation("Hook current generation session icin bulunamadi")
        return session_id

    def close_session(self, session_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("select hooks.close_session(%s)", (session_id,))
            if cursor.fetchone()[0] is not True:
                raise PolicyViolation("Hook session current degil veya zaten kapali")

    def record_outcome(
        self,
        *,
        session_binding_id: UUID,
        entry: CompiledHookEntry,
        outcome: HookRunOutcome,
        input_body: Any,
        output_body: Any | None,
    ) -> tuple[UUID, UUID]:
        if entry.runtime is None:
            raise PolicyViolation("Disabled hook invocation kaydi uretemez")
        validate_payload(entry.spec.input_schema, input_body, "hook input")
        if digest(input_body) != outcome.input_digest:
            raise PolicyViolation("Hook input body digest mismatch")
        completed = outcome.status == "completed"
        if completed is not (output_body is not None):
            raise PolicyViolation("Hook output body/result status tutarsiz")
        if output_body is not None and digest(output_body) != outcome.output_digest:
            raise PolicyViolation("Hook output body digest mismatch")
        if output_body is not None:
            validate_payload(entry.spec.output_schema, output_body, "hook output")
        if (outcome.kind is HookResultKind.PROPOSAL) is not (
            isinstance(output_body, dict) and outcome.proposal_digest is not None
        ):
            raise PolicyViolation("Hook proposal body/result kind tutarsiz")
        if (
            outcome.kind is HookResultKind.PROPOSAL
            and digest(output_body) != outcome.proposal_digest
        ):
            raise PolicyViolation("Hook proposal body digest mismatch")
        invocation_id = uuid4()
        receipt_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select invocation_id,input_digest from hooks.admit_invocation"
                "(%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    invocation_id,
                    session_binding_id,
                    entry.spec.event_type.value,
                    entry.spec.id,
                    entry.runtime.id,
                    _json(input_body),
                ),
            )
            admitted = cursor.fetchone()
            if admitted is None or str(admitted[1]) != outcome.input_digest:
                raise PolicyViolation("Hook invocation admission digest mismatch")
            cursor.execute(
                "select receipt_id,output_digest,failure_digest from hooks.complete_invocation"
                "(%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    receipt_id,
                    invocation_id,
                    outcome.status,
                    None if outcome.kind is None else outcome.kind.value,
                    None if output_body is None else _json(output_body),
                    outcome.warning,
                ),
            )
            completed_row = cursor.fetchone()
            if completed_row is None:
                raise PolicyViolation("Hook invocation terminal receipt uretemedi")
            if completed and str(completed_row[1]) != outcome.output_digest:
                raise PolicyViolation("Hook terminal output digest mismatch")
            expected_failure = (
                None if outcome.warning is None else digest({"category": outcome.warning})
            )
            if completed_row[2] != expected_failure:
                raise PolicyViolation("Hook terminal failure digest mismatch")
        return invocation_id, receipt_id
