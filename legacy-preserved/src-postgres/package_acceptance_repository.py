"""PostgreSQL ledger for shipped artifact package acceptance evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import canonical_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.package_acceptance import PackageAcceptanceRun, PackageManifestV2


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


@dataclass(frozen=True, slots=True)
class AcceptancePersistenceProvenance:
    authorization_id: UUID
    claim_id: UUID
    receipt_id: UUID


@dataclass(frozen=True, slots=True)
class PackageAcceptanceRepository:
    connection: Any
    realm_id: UUID

    def record(
        self,
        *,
        manifest_id: UUID,
        manifest: PackageManifestV2,
        run: PackageAcceptanceRun,
        provenance: AcceptancePersistenceProvenance,
        created_at: Any,
    ) -> tuple[UUID, bool]:
        """Append the complete ledger in one transaction even on autocommit sessions."""

        with self.connection.transaction():
            return self._record(
                manifest_id=manifest_id,
                manifest=manifest,
                run=run,
                provenance=provenance,
                created_at=created_at,
            )

    def _record(
        self,
        *,
        manifest_id: UUID,
        manifest: PackageManifestV2,
        run: PackageAcceptanceRun,
        provenance: AcceptancePersistenceProvenance,
        created_at: Any,
    ) -> tuple[UUID, bool]:
        """Atomically append manifest, terminal run and its exact result set."""

        with self.connection.cursor() as cursor:
            cursor.execute("set constraints all deferred")
            cursor.execute(
                "insert into release.package_manifest"
                "(id,realm_id,artifact_kind,artifact_digest,source_revision,manifest_body,"
                "manifest_digest,created_at,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,false)"
                " on conflict(realm_id,artifact_kind,artifact_digest) do nothing returning id",
                (
                    manifest_id,
                    self.realm_id,
                    run.artifact_kind,
                    run.artifact_digest,
                    run.source_revision,
                    _json(manifest.body()),
                    manifest.manifest_digest,
                    created_at,
                ),
            )
            row = cursor.fetchone()
            created = row is not None
            if row is None:
                cursor.execute(
                    "select id,manifest_digest,source_revision from release.package_manifest"
                    " where realm_id=%s and artifact_kind=%s and artifact_digest=%s",
                    (self.realm_id, run.artifact_kind, run.artifact_digest),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or str(row[1]) != manifest.manifest_digest
                    or str(row[2]) != run.source_revision
                ):
                    raise PolicyViolation("Package manifest artifact idempotency drift")
                manifest_id = UUID(str(row[0]))
            cursor.execute(
                "insert into release.acceptance_run"
                "(id,realm_id,package_manifest_id,run_body,run_digest,status,platform,"
                "python_version,builder_identity,verifier_identity,verifier_provenance_digest,"
                "builder_assignment_id,builder_invocation_id,builder_execution_identity,"
                "builder_envelope_digest,verifier_assignment_id,verifier_invocation_id,"
                "verifier_execution_identity,verifier_envelope_digest,verifier_source_digest,"
                "authorization_id,claim_id,receipt_id,started_at,completed_at,"
                "isolated_environment,grants_authority)"
                " values(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,true,false)"
                " on conflict(realm_id,run_digest) do nothing returning id",
                (
                    run.id,
                    self.realm_id,
                    manifest_id,
                    _json(run.body()),
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
                    provenance.authorization_id,
                    provenance.claim_id,
                    provenance.receipt_id,
                    run.started_at,
                    run.completed_at,
                ),
            )
            run_row = cursor.fetchone()
            if run_row is None:
                cursor.execute(
                    "select id from release.acceptance_run where realm_id=%s and run_digest=%s",
                    (self.realm_id, run.run_digest),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise PolicyViolation("Package acceptance idempotency lookup failed")
                return UUID(str(existing[0])), False
            for result in run.results:
                cursor.execute(
                    "insert into release.acceptance_result"
                    "(id,realm_id,acceptance_run_id,check_id,status,result_body,result_digest,"
                    "command_digest,stdout_digest,stderr_digest,duration_ms,grants_authority)"
                    " values(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,false)",
                    (
                        uuid4(),
                        self.realm_id,
                        run.id,
                        result.check_id,
                        result.status.value,
                        _json(result.body()),
                        result.result_digest,
                        result.command_digest,
                        result.stdout_digest,
                        result.stderr_digest,
                        result.duration_ms,
                    ),
                )
            return run.id, created

    def latest(self, artifact_digest: str) -> dict[str, Any] | None:
        """Return a sanitized read projection; raw command output is never stored."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.id,r.run_digest,r.status,r.platform,r.python_version,"
                "r.builder_identity,r.verifier_identity,r.verifier_provenance_digest,"
                "r.started_at,r.completed_at,m.manifest_digest,m.source_revision,m.artifact_kind,"
                "count(x.id) from release.acceptance_run r"
                " join release.package_manifest m on m.realm_id=r.realm_id"
                " and m.id=r.package_manifest_id"
                " join release.acceptance_result x on x.realm_id=r.realm_id"
                " and x.acceptance_run_id=r.id"
                " where r.realm_id=%s and m.artifact_digest=%s"
                " group by r.id,m.manifest_digest,m.source_revision,m.artifact_kind"
                " order by r.completed_at desc,r.id desc limit 1",
                (self.realm_id, artifact_digest),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "schema": "zekam-package-acceptance-projection/v1",
            "run_id": str(row[0]),
            "run_digest": str(row[1]),
            "status": str(row[2]),
            "platform": str(row[3]),
            "python_version": str(row[4]),
            "builder_identity": str(row[5]),
            "verifier_identity": str(row[6]),
            "verifier_provenance_digest": str(row[7]),
            "started_at": row[8],
            "completed_at": row[9],
            "manifest_digest": str(row[10]),
            "source_revision": str(row[11]),
            "artifact_kind": str(row[12]),
            "result_count": int(row[13]),
            "read_only": True,
            "grants_authority": False,
        }
