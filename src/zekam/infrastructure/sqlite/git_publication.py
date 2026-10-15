"""Signed publication authority in the existing operational SQLite database."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.application.git_publication import GitPublicationEvidence, GitPublicationPlan
from zekam.application.local_attestation import ReceiptVerifier
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, ConfigurationError, NotFound, PolicyViolation
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
)
from zekam.infrastructure.sqlite.operational_schema import _connect, status
from zekam.infrastructure.sqlite.work_dependency_schema import current_binding_query
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def evidence_attestation_body(
    evidence: GitPublicationEvidence,
    *,
    source_job_id: str,
    source_evidence_digest: str,
    producer_id: UUID,
) -> dict:
    return {
        "schema": "zekam-publication-evidence-attestation/v1",
        "evidence": evidence.body(),
        "source_job_id": source_job_id,
        "source_evidence_digest": source_evidence_digest,
        "producer_id": str(producer_id),
    }


def approval_attestation_body(
    plan: GitPublicationPlan, authorization: Authorization, producer_id: UUID
) -> dict:
    return {
        "schema": "zekam-publication-approval/v1",
        "plan": plan.body(),
        "authorization": authorization.as_dict(),
        "realm_id": str(authorization.realm_id),
        "producer_id": str(producer_id),
    }


def revocation_attestation_body(
    authorization_id: UUID, revoked_at: dt.datetime, producer_id: UUID
) -> dict:
    return {
        "schema": "zekam-publication-revocation/v1",
        "authorization_id": str(authorization_id),
        "revoked_at": revoked_at,
        "producer_id": str(producer_id),
    }


class SQLiteGitPublicationLedger:
    """Only trusted external receipt producers can admit evidence or approvals.

    The ledger accepts verifier capabilities, never signer keys. It installs no
    schema and creates no user authority merely by being opened.
    """

    def __init__(
        self,
        database: Path,
        *,
        objects: LocalContentAddressedStore,
        trusted_verifiers: Mapping[str, Mapping[UUID, ReceiptVerifier]],
    ) -> None:
        observed = status(database)
        if observed.schema_version != 9 or not observed.schema_ok or not observed.integrity_ok:
            raise ConfigurationError(
                "Git publication ledger migration-required: exact operational v9"
            )
        self.database = database
        self._objects = objects
        self._verifiers = {role: dict(values) for role, values in trusted_verifiers.items()}
        test_ids = set(self._verifiers.get("test", {}))
        review_ids = set(self._verifiers.get("verification", {}))
        if test_ids & review_ids:
            raise PolicyViolation("Publication test/verifier producer kimlikleri ayri olmali")
        if {id(value) for value in self._verifiers.get("test", {}).values()} & {
            id(value) for value in self._verifiers.get("verification", {}).values()
        }:
            raise PolicyViolation("Publication test/verifier capability ayri olmali")

    def _verify(self, role: str, producer_id: str, body: dict, attestation: str) -> None:
        verifier = self._verifiers.get(role, {}).get(UUID(producer_id))
        if verifier is None or not verifier.matches_receipt_digest(body, attestation):
            raise AuthorizationRequired("Publication trusted producer attestation yok")

    @staticmethod
    def _insert_immutable(connection, table: str, identity: str, values: tuple) -> None:
        if (table, identity) not in {
            ("git_publication_evidence", "evidence_digest"),
            ("git_publication_authorization", "id"),
            ("git_publication_revocation", "authorization_id"),
        }:
            raise PolicyViolation("Publication internal table allowlist disi")
        existing = connection.execute(
            f"select * from {table} where {identity}=?", (values[0],)
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise PolicyViolation("Publication immutable import replay drift")
            return
        connection.execute(f"insert into {table} values({','.join('?' for _ in values)})", values)

    def _evidence(self, connection, evidence_digest: str, verify) -> GitPublicationEvidence:
        row = connection.execute(
            "select * from git_publication_evidence where evidence_digest=?", (evidence_digest,)
        ).fetchone()
        if row is None:
            raise NotFound("Publication evidence bulunamadi")
        body = json.loads(row["body_json"])
        evidence = GitPublicationEvidence(
            body["kind"],
            body["subject_digest"],
            UUID(body["execution_id"]),
            UUID(body["executor_id"]),
            body["passed"],
            dt.datetime.fromisoformat(body["recorded_at"]),
        )
        if evidence.evidence_digest != evidence_digest or digest(body) != evidence_digest:
            raise PolicyViolation("Publication evidence digest drift")
        envelope = evidence_attestation_body(
            evidence,
            source_job_id=row["source_job_id"],
            source_evidence_digest=row["source_evidence_digest"],
            producer_id=UUID(row["producer_id"]),
        )
        verify(evidence.kind, row["producer_id"], envelope, row["attestation"])
        source = connection.execute(
            "select a.size_bytes from local_job j join local_job_binding b on b.local_job_id=j.id"
            " join local_effect_claim c on c.job_id=j.id join local_effect_receipt r on r.claim_id=c.id"
            " join artifact_ref a on a.digest=r.evidence_digest"
            " where j.id=? and j.state='completed' and b.run_id=?"
            " and r.status='completed' and r.evidence_digest=? limit 1",
            (row["source_job_id"], str(evidence.execution_id), row["source_evidence_digest"]),
        ).fetchone()
        if source is None:
            raise PolicyViolation("Publication evidence canonical completed source receipt ister")
        info = self._objects.stat(row["source_evidence_digest"])
        if (
            not 0 <= source["size_bytes"] <= 64 * 1024 * 1024
            or info.size_bytes != source["size_bytes"]
            or len(self._objects.get(row["source_evidence_digest"])) != source["size_bytes"]
        ):
            raise PolicyViolation("Publication evidence CAS boyut/digest drift")
        return evidence

    def publication_evidence(self, evidence_digest: str) -> GitPublicationEvidence:
        parse_digest(evidence_digest)
        connection = _connect(self.database, read_only=True)
        try:
            connection.execute("begin")
            return self._evidence(connection, evidence_digest, self._verify)
        finally:
            connection.close()

    def record_evidence(
        self,
        evidence: GitPublicationEvidence,
        *,
        source_job_id: str,
        source_evidence_digest: str,
        producer_id: UUID,
        attestation: str,
    ) -> None:
        evidence.__post_init__()
        parse_digest(source_evidence_digest)
        UUID(source_job_id)
        envelope = evidence_attestation_body(
            evidence,
            source_job_id=source_job_id,
            source_evidence_digest=source_evidence_digest,
            producer_id=producer_id,
        )
        self._verify(evidence.kind, str(producer_id), envelope, attestation)
        connection = _connect(self.database)
        try:
            connection.execute("begin immediate")
            self._insert_immutable(
                connection,
                "git_publication_evidence",
                "evidence_digest",
                (
                    evidence.evidence_digest,
                    canonical_json(evidence.body()),
                    source_job_id,
                    source_evidence_digest,
                    str(producer_id),
                    attestation,
                ),
            )
            self._evidence(connection, evidence.evidence_digest, self._verify)
            connection.commit()
        finally:
            connection.close()

    def record_authorization(
        self,
        plan: GitPublicationPlan,
        authorization: Authorization,
        *,
        producer_id: UUID,
        attestation: str,
    ) -> None:
        plan.__post_init__()
        if (
            authorization.state != AuthorizationState.ISSUED
            or authorization.rejection_reason(dt.datetime.now(dt.UTC))
            or authorization.realm_id != plan.realm_id
            or authorization.actor_id != plan.executor_id
            or str(authorization.work_item_id) != plan.binding.work_item_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or authorization.scope.allowed_resources != (plan.resource,)
            or authorization.scope.allowed_effects != ("git-push",)
        ):
            raise AuthorizationRequired("Publication approval exact plan/scope ister")
        envelope = approval_attestation_body(plan, authorization, producer_id)
        self._verify("approval", str(producer_id), envelope, attestation)
        input_digest = digest(
            {"plan_digest": plan.plan_digest, "authorization_id": str(authorization.id)}
        )
        connection = _connect(self.database)
        try:
            connection.execute("begin immediate")
            proofs = []
            for ref, kind in (
                (plan.test_evidence_digest, "test"),
                (plan.verifier_evidence_digest, "verification"),
            ):
                evidence = self._evidence(connection, ref, self._verify)
                if (
                    evidence.subject_digest != plan.subject_digest
                    or evidence.passed is not True
                    or evidence.kind != kind
                    or evidence.recorded_at > dt.datetime.now(dt.UTC)
                ):
                    raise PolicyViolation("Publication approval evidence subject drift")
                proofs.append(evidence)
            if proofs[0].execution_id == proofs[1].execution_id or proofs[1].executor_id in {
                plan.executor_id,
                proofs[0].executor_id,
            }:
                raise PolicyViolation("Publication approval independent verifier ister")
            self._insert_immutable(
                connection,
                "git_publication_authorization",
                "id",
                (
                    str(authorization.id),
                    authorization.authorization_digest,
                    plan.binding.project_id,
                    plan.binding.work_item_id,
                    plan.binding.work_revision,
                    plan.binding.binding_digest,
                    plan.effect_digest,
                    input_digest,
                    canonical_job_key(plan.binding.binding_digest, "git.push", input_digest),
                    canonical_json(
                        authorization.as_dict() | {"realm_id": str(authorization.realm_id)}
                    ),
                    canonical_json(plan.body()),
                    str(producer_id),
                    attestation,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _authorization(self, connection, authorization_id: UUID):
        row = connection.execute(
            "select * from git_publication_authorization where id=?", (str(authorization_id),)
        ).fetchone()
        if row is None:
            raise NotFound("Publication authorization bulunamadi")
        body, plan = json.loads(row["body_json"]), json.loads(row["plan_json"])
        scope = body["scope"]
        authorization = Authorization(
            id=UUID(body["id"]),
            realm_id=UUID(body["realm_id"]),
            actor_id=UUID(body["actor_id"]),
            work_item_id=UUID(body["work_item_id"]),
            plan_digest=body["plan_digest"],
            effect_digest=body["effect_digest"],
            scope=AuthorizationScope(
                allowed_resources=tuple(scope["allowed_resources"]),
                allowed_effects=tuple(scope["allowed_effects"]),
                provider_refs=tuple(scope["provider_refs"]),
                secret_ref_ids=tuple(UUID(value) for value in scope["secret_ref_ids"]),
                data_classifications=tuple(
                    DataClassification(value) for value in scope["data_classifications"]
                ),
                allowed_read_resources=tuple(scope.get("allowed_read_resources", ())),
                allowed_write_resources=tuple(scope.get("allowed_write_resources", ())),
            ),
            risk=body["risk"],
            issued_at=dt.datetime.fromisoformat(body["issued_at"]),
            expires_at=dt.datetime.fromisoformat(body["expires_at"]),
        )
        envelope = {
            "schema": "zekam-publication-approval/v1",
            "plan": plan,
            "authorization": authorization.as_dict(),
            "realm_id": str(authorization.realm_id),
            "producer_id": row["producer_id"],
        }
        self._verify("approval", row["producer_id"], envelope, row["attestation"])
        input_digest = digest(
            {"plan_digest": authorization.plan_digest, "authorization_id": str(authorization.id)}
        )
        binding = plan["binding"]
        if (
            authorization.id != authorization_id
            or authorization.authorization_digest != row["authorization_digest"]
            or digest(plan) != authorization.plan_digest
            or digest(binding) != row["binding_digest"]
            or binding["project_id"] != row["project_id"]
            or binding["work_item_id"] != row["work_item_id"]
            or binding["work_revision"] != row["work_revision"]
            or authorization.effect_digest != row["effect_digest"]
            or input_digest != row["input_digest"]
            or canonical_job_key(row["binding_digest"], "git.push", input_digest) != row["job_key"]
        ):
            raise PolicyViolation("Publication authorization canonical binding drift")
        revocation = connection.execute(
            "select * from git_publication_revocation where authorization_id=?",
            (str(authorization_id),),
        ).fetchone()
        if revocation is not None:
            revoked_at = dt.datetime.fromisoformat(revocation["revoked_at"])
            self._verify(
                "approval",
                revocation["producer_id"],
                revocation_attestation_body(
                    authorization_id, revoked_at, UUID(revocation["producer_id"])
                ),
                revocation["attestation"],
            )
            authorization = replace(
                authorization, state=AuthorizationState.REVOKED, revoked_at=revoked_at
            )
        elif connection.execute(
            "select 1 from git_publication_consumption where authorization_id=?",
            (str(authorization_id),),
        ).fetchone():
            authorization = replace(authorization, state=AuthorizationState.CONSUMED)
        return authorization, row

    def get(self, authorization_id: UUID) -> Authorization:
        connection = _connect(self.database, read_only=True)
        try:
            connection.execute("begin")
            return self._authorization(connection, authorization_id)[0]
        finally:
            connection.close()

    def consume(
        self, authorization_id: UUID, *, effect_digest: str, consumed_by: str, now: dt.datetime
    ):
        if (
            now.tzinfo is None
            or abs((dt.datetime.now(dt.UTC) - now).total_seconds()) > 5
            or consumed_by != "git-publication/v1"
        ):
            raise PolicyViolation("Publication consume trusted current time/consumer ister")
        connection = _connect(self.database)
        try:
            connection.execute("begin immediate")
            authorization, row = self._authorization(connection, authorization_id)
            if authorization.rejection_reason(now) or authorization.effect_digest != effect_digest:
                raise AuthorizationRequired("Publication authorization guncel degil")
            claim = connection.execute(
                "select c.id,c.job_id from local_effect_claim c join local_job j on j.id=c.job_id"
                " join local_lease l on l.id=c.lease_id and l.fencing_token=c.fencing_token"
                " where j.idempotency_key=? and j.state='running' and c.operation='git.push' and c.effect_digest=?"
                " and l.job_id=j.id and l.expires_at>? and not exists(select 1 from local_effect_receipt r where r.claim_id=c.id)",
                (
                    row["job_key"],
                    digest(
                        {
                            "operation": "git.push",
                            "binding": row["binding_digest"],
                            "input": row["input_digest"],
                        }
                    ),
                    now.astimezone(dt.UTC).isoformat(),
                ),
            ).fetchone()
            if (
                claim is None
                or connection.execute(
                    current_binding_query(connection).replace("new.job_id", "?"), (claim["job_id"],)
                ).fetchone()
                is None
            ):
                raise AuthorizationRequired(
                    "Publication consume live current canonical claim ister"
                )
            plan = json.loads(row["plan_json"])
            for ref, kind in (
                (plan["test_evidence_digest"], "test"),
                (plan["verifier_evidence_digest"], "verification"),
            ):
                evidence = self._evidence(connection, ref, self._verify)
                if (
                    evidence.kind != kind
                    or evidence.subject_digest != plan["subject_digest"]
                    or evidence.passed is not True
                ):
                    raise PolicyViolation("Publication consume evidence drift")
            now = dt.datetime.now(dt.UTC)
            if authorization.rejection_reason(now):
                raise AuthorizationRequired("Publication authorization consume oncesi suresi doldu")
            connection.execute(
                "insert into git_publication_consumption values(?,?,?,?,?)",
                (
                    str(authorization_id),
                    claim["id"],
                    effect_digest,
                    now.astimezone(dt.UTC).isoformat(),
                    consumed_by,
                ),
            )
            connection.commit()
            return SimpleNamespace(consumed=True)
        finally:
            connection.close()

    def revoke(
        self,
        authorization_id: UUID,
        *,
        revoked_at: dt.datetime,
        producer_id: UUID,
        attestation: str,
    ) -> None:
        if revoked_at.tzinfo is None or revoked_at > dt.datetime.now(dt.UTC):
            raise PolicyViolation("Publication revocation zamani gecersiz")
        self._verify(
            "approval",
            str(producer_id),
            revocation_attestation_body(authorization_id, revoked_at, producer_id),
            attestation,
        )
        connection = _connect(self.database)
        try:
            connection.execute("begin immediate")
            self._authorization(connection, authorization_id)
            self._insert_immutable(
                connection,
                "git_publication_revocation",
                "authorization_id",
                (str(authorization_id), revoked_at.isoformat(), str(producer_id), attestation),
            )
            connection.commit()
        finally:
            connection.close()
