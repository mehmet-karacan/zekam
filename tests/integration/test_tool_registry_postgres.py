from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

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
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class Adapter:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, binding, *, permit):  # type: ignore[no-untyped-def]
        permit.assert_for(binding)
        self.calls += 1
        return digest("tool-result")


def test_tool_registry_roundtrip_and_current_runtime_mismatch_fails_closed(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = ToolRegistryRepository(connection, realm.id)
    now = dt.datetime.now(dt.UTC)
    spec = ToolSpecRevision.create(
        realm_id=realm.id,
        tool_id="jira.search",
        revision=1,
        name="Jira search",
        description="Search exact issue",
        input_schema_digest=digest("input-v1"),
        output_schema_digest=digest("output-v1"),
        created_at=now,
    )
    runtime = ToolRuntimeRevision.create(
        realm_id=realm.id,
        tool_id=spec.tool_id,
        revision=1,
        adapter_ref="mcp:jira/search",
        executable_revision="jira@1",
        executable_digest=digest("binary-v1"),
        permission_capabilities=("jira.read",),
        parallel_supported=True,
        captured_at=now - dt.timedelta(seconds=1),
        expires_at=now + dt.timedelta(minutes=10),
    )
    assert repository.store_spec(spec)[1] is True
    assert repository.store_runtime(runtime)[1] is True
    compiled = CompiledToolSet.create(
        realm_id=realm.id,
        role="researcher",
        permission_profile_digest=digest("permission"),
        entries=(
            ToolSetEntry(
                spec.tool_id,
                1,
                ToolExposure.DIRECT,
                spec.spec_digest,
                runtime.runtime_digest,
            ),
        ),
        created_at=now,
    )
    assert repository.store_compiled_set(compiled)[1] is True
    manifest_binding = SimpleNamespace(
        tool_set_digest=compiled.tool_set_digest,
        permission_profile_digest=compiled.permission_profile_digest,
        role=compiled.role,
    )
    ModelInvocationRepository(connection, realm.id).assert_current_tool_set(manifest_binding)  # type: ignore[arg-type]
    binding = ToolDispatchBinding(
        uuid4(),
        digest("turn"),
        compiled.tool_set_digest,
        spec.tool_id,
        1,
        spec.spec_digest,
        runtime.runtime_digest,
        digest("issue-key"),
    )
    runtime_v2 = ToolRuntimeRevision.create(
        realm_id=realm.id,
        tool_id=spec.tool_id,
        revision=2,
        adapter_ref="mcp:jira/search",
        executable_revision="jira@2",
        executable_digest=digest("binary-v2"),
        permission_capabilities=("jira.read",),
        parallel_supported=True,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    repository.store_runtime(runtime_v2)
    with pytest.raises(PolicyViolation, match="runtime drift"):
        ModelInvocationRepository(connection, realm.id).assert_current_tool_set(  # type: ignore[arg-type]
            manifest_binding
        )
    assert binding.runtime_digest == runtime.runtime_digest


def test_database_recomputes_digests_and_rejects_forged_dispatch_evidence(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = ToolRegistryRepository(connection, realm.id)
    now = dt.datetime.now(dt.UTC)
    spec = ToolSpecRevision.create(
        realm_id=realm.id,
        tool_id="shell.read",
        revision=1,
        name="Shell read",
        description="Read-only shell",
        input_schema_digest=digest("input"),
        output_schema_digest=digest("output"),
        created_at=now,
    )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        forged = replace(spec, spec_digest=digest("forged"))
        cursor.execute(
            "insert into tools.spec_revision"
            "(id,realm_id,tool_id,revision,name,description,input_schema_digest,"
            "output_schema_digest,created_at,spec_digest) values"
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                forged.id,
                forged.realm_id,
                forged.tool_id,
                forged.revision,
                forged.name,
                forged.description,
                forged.input_schema_digest,
                forged.output_schema_digest,
                forged.created_at,
                forged.spec_digest,
            ),
        )
    connection.rollback()
    repository.store_spec(spec)
    runtime = ToolRuntimeRevision.create(
        realm_id=realm.id,
        tool_id=spec.tool_id,
        revision=1,
        adapter_ref="native:shell/read",
        executable_revision="shell@1",
        executable_digest=digest("binary"),
        permission_capabilities=("filesystem.read",),
        parallel_supported=False,
        captured_at=now - dt.timedelta(seconds=1),
        expires_at=now + dt.timedelta(minutes=5),
    )
    repository.store_runtime(runtime)
    compiled = CompiledToolSet.create(
        realm_id=realm.id,
        role="builder",
        permission_profile_digest=digest("permission"),
        entries=(
            ToolSetEntry(
                spec.tool_id,
                1,
                ToolExposure.CODE_MODE_ONLY,
                spec.spec_digest,
                runtime.runtime_digest,
            ),
        ),
        created_at=now,
    )
    repository.store_compiled_set(compiled)
    binding = ToolDispatchBinding(
        uuid4(),
        digest("turn"),
        compiled.tool_set_digest,
        spec.tool_id,
        1,
        spec.spec_digest,
        runtime.runtime_digest,
        digest("input"),
    )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into tools.dispatch_gate_evidence"
            "(id,realm_id,tool_set_digest,tool_id,revision,spec_digest,runtime_digest,"
            "input_digest,disposition,checked_at,evidence_digest)"
            " values(gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s,'passed',%s,%s)",
            (
                realm.id,
                binding.tool_set_digest,
                binding.tool_id,
                binding.revision,
                binding.spec_digest,
                binding.runtime_digest,
                binding.input_digest,
                now,
                digest("forged-evidence"),
            ),
        )
    connection.rollback()
