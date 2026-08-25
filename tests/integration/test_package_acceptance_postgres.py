from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.execution import ExecutionHost
from zekam.application.governance import EffectRequest, GovernanceService
from zekam.application.package_acceptance import build_package_manifest
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.package_acceptance import (
    AcceptanceStatus,
    PackageAcceptanceResult,
    PackageAcceptanceRun,
    PackageVerifierProvenance,
)
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import Job, JobKind
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.package_acceptance_repository import (
    AcceptancePersistenceProvenance,
    PackageAcceptanceRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)
ROOT = Path(__file__).resolve().parents[2]


def _result() -> PackageAcceptanceResult:
    return PackageAcceptanceResult(
        check_id="cli.version",
        status=AcceptanceStatus.PASSED,
        command_digest=digest("command"),
        stdout_digest=digest("stdout"),
        stderr_digest=digest("stderr"),
        duration_ms=4,
    )


def _run() -> PackageAcceptanceRun:
    manifest = build_package_manifest(ROOT / "src" / "zekam")
    provenance = PackageVerifierProvenance(
        builder_assignment_id=uuid4(),
        builder_invocation_id=uuid4(),
        builder_execution_identity="package-builder-execution",
        builder_envelope_digest=digest("builder-envelope"),
        verifier_assignment_id=uuid4(),
        verifier_invocation_id=uuid4(),
        verifier_execution_identity="package-verifier-execution",
        verifier_envelope_digest=digest("verifier-envelope"),
        verifier_source_digest=digest("verifier-source"),
    )
    return PackageAcceptanceRun(
        id=uuid4(),
        manifest_digest=manifest.manifest_digest,
        artifact_digest=digest("wheel-artifact"),
        artifact_kind="wheel",
        source_revision="source-revision-1",
        suite_digest=digest("package-suite"),
        platform="linux-x86_64",
        python_version="3.12.9",
        builder_identity="builder-agent",
        verifier_identity="independent-verifier-agent",
        verifier_provenance=provenance,
        started_at=NOW,
        completed_at=NOW + dt.timedelta(seconds=2),
        results=(_result(),),
    )


def _effect_chain(
    connection: Any,
    realm: Any,
    tmp_path: Path,
    run: PackageAcceptanceRun,
    *,
    materialize_provenance: bool = True,
) -> AcceptancePersistenceProvenance:
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug=f"release-{uuid4().hex[:8]}")
    )
    resource = f"artifact:release:{run.artifact_digest}"
    request = EffectRequest(
        action="package-acceptance",
        effects=(EffectKind.PROCESS_RUN,),
        resources=(resource,),
        required_capabilities=("sandbox.execute",),
    )
    governance = GovernanceService(connection, realm, actor_id=actor.id)
    authorization = governance.issue_authorization(request=request, actor_id=actor.id, now=NOW)
    consumed = governance.consume_authorization(
        authorization.id,
        request=request,
        consumed_by="package-smoke",
        now=NOW + dt.timedelta(milliseconds=1),
    )
    assert consumed.consumed

    source = tmp_path / "release-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    if materialize_provenance:
        _materialize_agent_provenance(connection, realm.id, project.id, run)
    host = ExecutionHost(connection, realm.id, worker_label="package-smoke")
    job, _ = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=project.id,
            kind=JobKind.MUTATION,
            idempotency_key=f"package-{uuid4()}",
            resources=parse_requests(write=(resource,)),
            required_capabilities=("sandbox.execute",),
        )
    )
    work = host.acquire_work(capabilities=("sandbox.execute",))
    assert work is not None and work.job.id == job.id
    claim = host.claim_effect(
        work,
        operation="package-acceptance",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        resources=parse_requests(write=(resource,)),
        adapter_digest=digest("package-smoke-adapter"),
        now=NOW + dt.timedelta(milliseconds=2),
    )
    receipt = host.record_success(
        claim,
        result_digest=run.run_digest,
        adapter_evidence_digest=digest("package-smoke-evidence"),
        now=NOW + dt.timedelta(milliseconds=3),
    )
    return AcceptancePersistenceProvenance(authorization.id, claim.id, receipt.id)


def _materialize_agent_provenance(
    connection: Any, realm_id: Any, project_id: Any, run: PackageAcceptanceRun
) -> None:
    work_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item"
            " (id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','package acceptance provenance',%s)",
            (work_id, realm_id, project_id, digest("package-work")),
        )
    repository = AgentAssignmentRepository(connection, realm_id)

    def assignment(
        *, assignment_id: Any, role: AssignmentRole, agent_ref: str, parent: Any
    ) -> AgentAssignment:
        draft = AgentAssignment(
            id=assignment_id,
            realm_id=realm_id,
            project_id=project_id,
            work_item_id=work_id,
            role=role,
            agent_ref=agent_ref,
            parent_assignment_id=parent,
            instruction_digest=digest("package-instruction"),
            context_manifest_digest=digest("package-context"),
            assignment_digest=digest("draft"),
            created_at=NOW,
        )
        return AgentAssignment(
            **{
                **{field: getattr(draft, field) for field in draft.__dataclass_fields__},
                "assignment_digest": digest(draft.identity_body()),
            }
        )

    coordinator = assignment(
        assignment_id=uuid4(),
        role=AssignmentRole.COORDINATOR,
        agent_ref="package-coordinator",
        parent=None,
    )
    builder = assignment(
        assignment_id=run.verifier_provenance.builder_assignment_id,
        role=AssignmentRole.BUILDER,
        agent_ref=run.builder_identity,
        parent=coordinator.id,
    )
    verifier = assignment(
        assignment_id=run.verifier_provenance.verifier_assignment_id,
        role=AssignmentRole.VERIFIER,
        agent_ref=run.verifier_identity,
        parent=coordinator.id,
    )
    for item in (coordinator, builder, verifier):
        repository.create(item)

    def invocation(
        assignment_item: AgentAssignment, invocation_id: Any, execution_identity: str
    ) -> AgentInvocation:
        body = {
            "id": str(invocation_id),
            "realm_id": str(realm_id),
            "assignment_id": str(assignment_item.id),
            "client_id": "package-acceptance",
            "execution_identity": execution_identity,
        }
        return AgentInvocation(
            invocation_id,
            realm_id,
            assignment_item.id,
            "package-acceptance",
            execution_identity,
            digest(body),
            NOW,
        )

    builder_invocation = invocation(
        builder,
        run.verifier_provenance.builder_invocation_id,
        run.verifier_provenance.builder_execution_identity,
    )
    verifier_invocation = invocation(
        verifier,
        run.verifier_provenance.verifier_invocation_id,
        run.verifier_provenance.verifier_execution_identity,
    )
    repository.record_invocation(builder_invocation)
    repository.record_invocation(verifier_invocation)
    repository.store_result(
        assignment_id=builder.id,
        invocation_id=builder_invocation.id,
        envelope_digest=run.verifier_provenance.builder_envelope_digest,
    )
    repository.store_result(
        assignment_id=verifier.id,
        invocation_id=verifier_invocation.id,
        envelope_digest=run.verifier_provenance.verifier_envelope_digest,
    )


def test_package_acceptance_round_trip_append_only_and_independent(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    manifest = build_package_manifest(ROOT / "src" / "zekam")
    run = _run()
    provenance = _effect_chain(connection, realm, tmp_path, run)
    repository = PackageAcceptanceRepository(connection, realm.id)

    assert (
        repository.record(
            manifest_id=uuid4(),
            manifest=manifest,
            run=run,
            provenance=provenance,
            created_at=NOW,
        )[0]
        == run.id
    )
    with connection.cursor() as cursor:
        cursor.execute("set constraints all immediate")
    projection = repository.latest(run.artifact_digest)

    assert projection is not None
    assert projection["run_digest"] == run.run_digest
    assert projection["status"] == "passed"
    assert projection["result_count"] == 1
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update release.acceptance_run set status='failed' where id=%s",
            (run.id,),
        )
    connection.rollback()


def test_package_run_without_authorization_receipt_is_rejected(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    manifest = build_package_manifest(ROOT / "src" / "zekam")
    run = _run()
    manifest_id = uuid4()
    source = tmp_path / "missing-authorization-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    _materialize_agent_provenance(connection, realm.id, project.id, run)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into release.package_manifest"
            "(id,realm_id,artifact_kind,artifact_digest,source_revision,manifest_body,"
            "manifest_digest,created_at,grants_authority)"
            " values(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,false)",
            (
                manifest_id,
                realm.id,
                run.artifact_kind,
                run.artifact_digest,
                run.source_revision,
                json.dumps(manifest.body()),
                manifest.manifest_digest,
                NOW,
            ),
        )
    with (
        pytest.raises(PsycopgError, match="authorization claim receipt"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into release.acceptance_run"
            "(id,realm_id,package_manifest_id,run_body,run_digest,status,platform,python_version,"
            "builder_identity,verifier_identity,verifier_provenance_digest,"
            "builder_assignment_id,builder_invocation_id,builder_execution_identity,"
            "builder_envelope_digest,verifier_assignment_id,verifier_invocation_id,"
            "verifier_execution_identity,verifier_envelope_digest,verifier_source_digest,authorization_id,"
            "claim_id,receipt_id,started_at,completed_at,isolated_environment,grants_authority)"
            " values(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,true,false)",
            (
                run.id,
                realm.id,
                manifest_id,
                json.dumps(run.body()),
                run.run_digest,
                run.status.value,
                run.platform,
                run.python_version,
                run.builder_identity,
                run.verifier_identity,
                run.verifier_provenance_digest,
                run.verifier_provenance.builder_assignment_id,
                run.verifier_provenance.builder_invocation_id,
                run.verifier_provenance.builder_execution_identity,
                run.verifier_provenance.builder_envelope_digest,
                run.verifier_provenance.verifier_assignment_id,
                run.verifier_provenance.verifier_invocation_id,
                run.verifier_provenance.verifier_execution_identity,
                run.verifier_provenance.verifier_envelope_digest,
                run.verifier_provenance.verifier_source_digest,
                uuid4(),
                uuid4(),
                uuid4(),
                run.started_at,
                run.completed_at,
            ),
        )
    connection.rollback()


def test_package_run_with_forged_verifier_envelope_is_rejected(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    forged = _run()
    canonical = replace(
        forged,
        verifier_provenance=replace(
            forged.verifier_provenance,
            verifier_envelope_digest=digest("actual-verifier-envelope"),
        ),
    )
    source = tmp_path / "canonical-agent-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    _materialize_agent_provenance(connection, realm.id, project.id, canonical)
    effect_tmp = tmp_path / "forged-effect"
    effect_tmp.mkdir()
    persistence = _effect_chain(
        connection,
        realm,
        effect_tmp,
        forged,
        materialize_provenance=False,
    )
    with pytest.raises(PsycopgError, match="canonical verifier receipt drift"):
        PackageAcceptanceRepository(connection, realm.id).record(
            manifest_id=uuid4(),
            manifest=build_package_manifest(ROOT / "src" / "zekam"),
            run=forged,
            provenance=persistence,
            created_at=NOW,
        )
    connection.rollback()
