from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.errors import PolicyViolation
from zekam.domain.hook_runtime import (
    CompiledHookEntry,
    CompiledHookSet,
    HookAdapterResult,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookResultKind,
    HookRunOutcome,
    HookRuntimeRevision,
    HookSpecRevision,
)
from zekam.infrastructure.postgres.config_provenance_repository import (
    ConfigProvenanceRepository,
)
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 25, 8, tzinfo=dt.UTC)
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


def _contracts(realm_id: UUID):  # type: ignore[no-untyped-def]
    profile = PermissionProfileRevision.from_flags(
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
    spec = HookSpecRevision.create(
        realm_id=realm_id,
        hook_id="turn-observer",
        revision=1,
        event_type=HookEventType.TURN_START,
        required=True,
        source_layer="managed-policy",
        timeout_ms=1000,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=HookFailurePolicy.ABORT,
        created_at=NOW,
    )
    runtime = HookRuntimeRevision.create(
        realm_id=realm_id,
        hook_id=spec.hook_id,
        hook_revision=spec.revision,
        adapter_ref="internal-observer",
        adapter_digest=digest("internal-observer"),
        permission_capabilities=("filesystem.read",),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    process = HookRuntime()
    compiled = process.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("config"),
        specs=(spec,),
        runtimes=(runtime,),
        profiles=(profile,),
        adapters=(
            LoadedHookAdapter(
                runtime.adapter_ref,
                runtime.adapter_digest,
                HookExecutionMode.INTERNAL,
                lambda payload: HookAdapterResult(HookResultKind.OBSERVATION, payload),
            ),
        ),
        now=NOW,
    )
    process.shutdown(timeout_seconds=0)
    return profile, spec, runtime, compiled


def _bootstrap_spec(
    profile: PermissionProfileRevision,
    *,
    hook_id: str,
    revision: int,
    timeout_ms: int = 1_000,
) -> HookSpecRevision:
    assert profile.realm_id is not None
    return HookSpecRevision.create(
        realm_id=profile.realm_id,
        hook_id=hook_id,
        revision=revision,
        event_type=HookEventType.TURN_START,
        required=True,
        source_layer="managed-policy",
        timeout_ms=timeout_ms,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=HookFailurePolicy.ABORT,
        created_at=NOW,
    )


def test_hook_registry_roundtrip_compiled_set_is_immutable_and_realm_scoped(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, runtime, compiled = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    assert repository.store_spec(spec, permission_profile_revision_id=profile_id)[1] is True
    assert repository.store_spec(spec, permission_profile_revision_id=profile_id)[1] is False
    assert repository.store_runtime(runtime)[1] is True
    assert repository.store_runtime(runtime)[1] is False
    set_id, created = repository.store_compiled_set(compiled, created_at=NOW)
    assert created is True
    assert repository.store_compiled_set(compiled, created_at=NOW) == (set_id, False)
    with connection.cursor() as cursor:
        cursor.execute(
            "select hook_set_digest,required_load_errors,grants_authority"
            " from hooks.compiled_set where id=%s",
            (set_id,),
        )
        assert cursor.fetchone() == (compiled.hook_set_digest, [], False)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute("update hooks.spec_revision set required=false where id=%s", (spec.id,))
    connection.rollback()


def test_fresh_hook_revision_bootstrap_and_exact_replay_are_fail_closed(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, _, _, _ = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    revision_two = _bootstrap_spec(profile, hook_id="fresh-revision-observer", revision=2)

    assert repository.store_spec(
        revision_two,
        permission_profile_revision_id=profile_id,
    ) == (revision_two.id, True)
    assert repository.store_spec(
        revision_two,
        permission_profile_revision_id=profile_id,
    ) == (revision_two.id, False)
    with pytest.raises(PolicyViolation, match="exact replay binding mismatch"):
        repository.store_spec(
            revision_two,
            permission_profile_revision_id=uuid4(),
        )

    divergent_revision_two = _bootstrap_spec(
        profile,
        hook_id=revision_two.hook_id,
        revision=2,
        timeout_ms=2_000,
    )
    with pytest.raises(PsycopgError, match="revision monotonic") as rejected:
        repository.store_spec(
            divergent_revision_two,
            permission_profile_revision_id=profile_id,
        )
    assert rejected.value.sqlstate == "23514"

    revision_three = _bootstrap_spec(
        profile,
        hook_id=revision_two.hook_id,
        revision=3,
    )
    assert repository.store_spec(
        revision_three,
        permission_profile_revision_id=profile_id,
    ) == (revision_three.id, True)
    with connection.cursor() as cursor:
        cursor.execute(
            "select revision,hook_digest from hooks.spec_revision"
            " where realm_id=%s and hook_id=%s order by revision",
            (realm.id, revision_two.hook_id),
        )
        assert cursor.fetchall() == [
            (2, revision_two.hook_digest),
            (3, revision_three.hook_digest),
        ]


def test_database_rejects_forged_spec_schema_and_compiled_set_digest(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, runtime, compiled = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.spec_revision"
            "(id,realm_id,hook_id,revision,event_type,required,source_layer,timeout_ms,"
            "execution_mode,input_schema,output_schema,input_schema_digest,output_schema_digest,"
            "permission_profile_revision_id,permission_profile_name,permission_profile_digest,"
            "failure_policy,created_at,hook_digest,hook_body,grants_authority)"
            " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s::jsonb,false)",
            (
                spec.id,
                realm.id,
                spec.hook_id,
                spec.revision,
                spec.event_type.value,
                spec.required,
                spec.source_layer,
                spec.timeout_ms,
                spec.execution_mode.value,
                json.dumps({"type": "string"}),
                json.dumps(spec.output_schema),
                spec.input_schema_digest,
                spec.output_schema_digest,
                profile_id,
                spec.permission_profile_name,
                spec.permission_profile_digest,
                spec.failure_policy.value,
                spec.created_at,
                spec.hook_digest,
                json.dumps(spec.body(), default=str),
            ),
        )
    connection.rollback()

    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    repository.store_runtime(runtime)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.compiled_set"
            "(id,realm_id,generation,config_effective_digest,required_load_errors,"
            "hook_set_digest,set_body,created_at,grants_authority)"
            " values(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,false)",
            (
                uuid4(),
                realm.id,
                compiled.generation,
                compiled.config_effective_digest,
                [],
                "sha256:" + "0" * 64,
                json.dumps(compiled.body(), default=str),
                NOW,
            ),
        )
        cursor.execute("set constraints hooks.compiled_hook_set_guard immediate")
    connection.rollback()


def test_database_rejects_compiled_runtime_outside_permission_profile(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, _, _ = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    unsafe_runtime = HookRuntimeRevision.create(
        realm_id=realm.id,
        hook_id=spec.hook_id,
        hook_revision=spec.revision,
        adapter_ref="unsafe-network",
        adapter_digest=digest("unsafe-network"),
        permission_capabilities=("network.access",),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    repository.store_runtime(unsafe_runtime)
    unsafe_set = CompiledHookSet.create(
        realm_id=realm.id,
        generation=1,
        config_effective_digest=digest("config"),
        entries=(CompiledHookEntry(1, spec, unsafe_runtime, None),),
    )
    with pytest.raises(PsycopgError, match="profile/load binding"):
        repository.store_compiled_set(unsafe_set, created_at=NOW)
    connection.rollback()


def test_required_load_errors_cannot_create_session(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, runtime, compiled = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    repository.store_runtime(runtime)
    valid_set_id, _ = repository.store_compiled_set(compiled, created_at=NOW)
    repository.activate(valid_set_id)
    blocked = type(compiled).create(
        realm_id=realm.id,
        generation=compiled.generation + 1,
        config_effective_digest=compiled.config_effective_digest,
        entries=(type(compiled.entries[0])(1, spec, None, "adapter-unavailable"),),
        required_load_errors=("turn-observer:adapter-unavailable",),
    )
    set_id, _ = repository.store_compiled_set(blocked, created_at=NOW)
    with pytest.raises(PsycopgError):
        repository.activate(set_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select generation,hook_set_digest from hooks.current_generation where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone() == (compiled.generation, compiled.hook_set_digest)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.session_binding"
            "(id,realm_id,session_ref,compiled_set_id,generation,hook_set_digest,"
            "config_effective_digest,state,started_at) values(%s,%s,%s,%s,%s,%s,%s,'active',%s)",
            (
                uuid4(),
                realm.id,
                "blocked-session",
                set_id,
                blocked.generation,
                blocked.hook_set_digest,
                blocked.config_effective_digest,
                NOW,
            ),
        )
    connection.rollback()


def test_only_narrow_functions_can_activate_pin_and_record_hook_evidence(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, runtime, first = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    repository.store_runtime(runtime)
    first_id, _ = repository.store_compiled_set(first, created_at=NOW)
    repository.activate(first_id)
    second = CompiledHookSet.create(
        realm_id=realm.id,
        generation=2,
        config_effective_digest=digest("config-2"),
        entries=first.entries,
    )
    second_id, _ = repository.store_compiled_set(
        second,
        created_at=NOW + dt.timedelta(seconds=1),
    )
    repository.activate(second_id)

    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.current_generation"
            "(realm_id,compiled_set_id,generation,hook_set_digest,updated_at)"
            " values(%s,%s,%s,%s,%s)",
            (realm.id, first_id, 1, first.hook_set_digest, NOW),
        )
    connection.rollback()

    session_id = repository.start_session(session_ref="current-only")
    with connection.cursor() as cursor:
        cursor.execute(
            "select compiled_set_id,generation from hooks.session_binding where id=%s",
            (session_id,),
        )
        assert cursor.fetchone() == (second_id, 2)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.session_binding"
            "(id,realm_id,session_ref,compiled_set_id,generation,hook_set_digest,"
            "config_effective_digest,state,started_at)"
            " values(%s,%s,%s,%s,%s,%s,%s,'active',%s)",
            (
                uuid4(),
                realm.id,
                "forged-old-pin",
                first_id,
                1,
                first.hook_set_digest,
                first.config_effective_digest,
                NOW,
            ),
        )
    connection.rollback()

    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into hooks.invocation"
            "(id,realm_id,session_binding_id,generation,event_type,spec_revision_id,"
            "runtime_revision_id,input_body,input_digest,deadline_at,created_at)"
            " values(%s,%s,%s,2,%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (
                uuid4(),
                realm.id,
                session_id,
                spec.event_type.value,
                spec.id,
                runtime.id,
                json.dumps({"forged": True}),
                digest({"forged": True}),
                NOW + dt.timedelta(seconds=1),
                NOW,
            ),
        )
    connection.rollback()

    with (
        pytest.raises(PsycopgError, match="pinned generation/spec/runtime mismatch"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "select * from hooks.admit_invocation(%s,%s,%s,%s,%s,%s::jsonb)",
            (
                uuid4(),
                session_id,
                spec.event_type.value,
                spec.id,
                runtime.id,
                json.dumps({"wrong": True}),
            ),
        )
    connection.rollback()

    valid_invocation_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "select * from hooks.admit_invocation(%s,%s,%s,%s,%s,%s::jsonb)",
            (
                valid_invocation_id,
                session_id,
                spec.event_type.value,
                spec.id,
                runtime.id,
                json.dumps({"value": "valid"}),
            ),
        )
    with (
        pytest.raises(PsycopgError, match="terminal shape mismatch"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "select * from hooks.complete_invocation(%s,%s,'completed','observation',"
            "%s::jsonb,null)",
            (uuid4(), valid_invocation_id, json.dumps({"wrong": True})),
        )
    connection.rollback()


def test_proposal_receipt_is_authority_free_and_creates_no_effect_claim(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    profile, spec, runtime, compiled = _contracts(realm.id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm.id).store_profile(profile)
    repository = HookRuntimeRepository(connection, realm.id)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    repository.store_runtime(runtime)
    set_id, _ = repository.store_compiled_set(compiled, created_at=NOW)
    assert repository.activate(set_id) == (
        compiled.generation,
        compiled.hook_set_digest,
    )
    session_id = repository.start_session(session_ref="proposal-session")
    proposal = {"message": "governed change request"}
    outcome = HookRunOutcome(
        spec.hook_id,
        spec.revision,
        HookResultKind.PROPOSAL,
        "completed",
        digest({"value": "value"}),
        digest(proposal),
        digest(proposal),
        None,
        True,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.effect_claim where realm_id=%s",
            (realm.id,),
        )
        claims_before = cursor.fetchone()[0]
    repository.record_outcome(
        session_binding_id=session_id,
        entry=compiled.entries[0],
        outcome=outcome,
        input_body={"value": "value"},
        output_body=proposal,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select status,grants_authority from hooks.proposal where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone() == ("pending-governance", False)
        cursor.execute(
            "select effect_performed,grants_authority from hooks.result_receipt where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            "select count(*) from runtime.effect_claim where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == claims_before
    repository.close_session(session_id)
