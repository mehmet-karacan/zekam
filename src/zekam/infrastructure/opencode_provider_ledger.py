"""OpenCode BGE-M3 calls backed by a durable local claim/receipt ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.embedding_provider import (
    EmbeddingProbeFixture,
    EmbeddingProfile,
    EmbeddingPurpose,
)
from zekam.application.model_benchmark_service import default_fixture_file, load_fixture_registry
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_benchmark_campaign import (
    CampaignCallKind,
    PreparedCampaignManifest,
    normalize_provider_response,
)
from zekam.application.opencode_embedding import (
    OpenCodeCredentialStore,
    OpenCodeEndpointResolver,
    default_opencode_config_file,
    load_opencode_embedding_configuration,
)
from zekam.application.opencode_remote_benchmark import (
    DeterministicProviderNeutralVerifier,
    load_remote_fixture,
)
from zekam.application.provider_adapter import (
    ProviderCall,
    ProviderCallResult,
    reviewed_endpoint_digest,
)
from zekam.application.provider_contract_execution import PreparedProviderContractCall
from zekam.application.provider_contract_runner import (
    ProviderExecutionHost,
    RuntimeProviderContractRunner,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_invocation import GatewayTransportProvenance
from zekam.domain.resources import LockMode, ResourceRequest
from zekam.domain.runtime import (
    ClaimedWork,
    EffectClaim,
    EffectReceipt,
    FailureCategory,
    Job,
    JobKind,
    JobState,
    Lease,
    ReceiptStatus,
    new_owner_token,
    owner_digest,
)
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind
from zekam.infrastructure.embedding.opencode_remote import (
    MAX_BATCH_DELTA,
    MAX_REPEAT_DELTA,
    MIN_BATCH_COSINE,
    MIN_REPEAT_COSINE,
    MIN_SEMANTIC_MARGIN,
    OpenCodeEmbeddingExecution,
    OpenCodeRemoteEmbeddingProvider,
    OpenCodeRuntimeInvocation,
    RecoveredDocumentVectors,
    RuntimeOpenCodeEmbeddingExecutor,
)
from zekam.infrastructure.local_file_security import private_directory, private_regular
from zekam.infrastructure.process.capability_worker import ProcessIsolatedJsonProviderTransport

SCHEMA = """
pragma foreign_keys=on;
create table if not exists provider_job(
  id text primary key,
  realm_id text not null,
  project_id text not null,
  attempt_id text not null,
  created_at text not null
) strict;
create table if not exists provider_effect_claim(
  id text primary key,
  realm_id text not null,
  job_id text not null references provider_job(id),
  attempt_id text not null,
  operation text not null,
  effect_digest text not null,
  authorization_digest text not null,
  idempotency_key text not null unique,
  resources_json text not null,
  execution_identity text not null,
  fencing_token integer not null,
  adapter_digest text not null,
  claimed_at text not null
) strict;
create index if not exists provider_effect_claim_reconcile
on provider_effect_claim(realm_id,effect_digest,operation);
create table if not exists provider_effect_receipt(
  id text primary key,
  realm_id text not null,
  claim_id text not null unique references provider_effect_claim(id),
  status text not null check(status in ('completed','failed')),
  result_digest text,
  failure_category text,
  failure_digest text,
  adapter_evidence_digest text,
  token_count integer not null,
  cost_micros integer not null,
  latency_ms integer not null,
  completed_at text not null
) strict;
create table if not exists provider_recovery(
  job_id text primary key references provider_job(id),
  reason_digest text not null,
  created_at text not null
) strict;
create trigger if not exists provider_effect_claim_no_update
before update on provider_effect_claim begin
  select raise(abort,'provider_effect_claim append-only');
end;
create trigger if not exists provider_effect_claim_no_delete
before delete on provider_effect_claim begin
  select raise(abort,'provider_effect_claim append-only');
end;
create trigger if not exists provider_effect_receipt_no_update
before update on provider_effect_receipt begin
  select raise(abort,'provider_effect_receipt append-only');
end;
create trigger if not exists provider_effect_receipt_no_delete
before delete on provider_effect_receipt begin
  select raise(abort,'provider_effect_receipt append-only');
end;
create table if not exists provider_document_vector_result(
  realm_id text not null,
  project_id text not null,
  effect_digest text not null,
  profile_digest text not null,
  claim_id text not null references provider_effect_claim(id),
  receipt_id text not null references provider_effect_receipt(id),
  vectors_json text not null,
  vectors_digest text not null,
  primary key(realm_id,project_id,effect_digest,profile_digest),
  check(json_valid(vectors_json) and length(cast(vectors_json as blob)) between 2 and 8388608)
) strict;
create trigger if not exists provider_document_vector_result_no_update
before update on provider_document_vector_result begin
  select raise(abort,'provider_document_vector_result append-only');
end;
create trigger if not exists provider_document_vector_result_no_delete
before delete on provider_document_vector_result begin
  select raise(abort,'provider_document_vector_result append-only');
end;
create table if not exists provider_document_input_manifest(
  job_id text primary key references provider_job(id),
  effect_digest text not null,
  operation text not null,
  profile_digest text not null,
  input_digests_json text not null check(json_valid(input_digests_json)),
  binding_digest text not null
) strict;
create table if not exists provider_document_input_slot(
  job_id text not null references provider_document_input_manifest(job_id),
  slot integer not null check(slot between 0 and 63),
  input_digest text not null,
  primary key(job_id,slot)
) strict;
create index if not exists provider_document_input_lookup
on provider_document_input_slot(input_digest,job_id);
create table if not exists provider_document_input_reconciliation(
  realm_id text not null,
  project_id text not null,
  profile_digest text not null,
  request_digest text not null,
  body_json text not null check(json_valid(body_json)),
  body_digest text not null,
  created_at text not null,
  primary key(realm_id,project_id,profile_digest,request_digest)
) strict;
create index if not exists provider_document_input_reconciliation_lookup
on provider_document_input_reconciliation(realm_id,project_id,profile_digest,request_digest);
create trigger if not exists provider_document_input_reconciliation_no_update
before update on provider_document_input_reconciliation begin
  select raise(abort,'provider_document_input_reconciliation append-only');
end;
create trigger if not exists provider_document_input_reconciliation_no_delete
before delete on provider_document_input_reconciliation begin
  select raise(abort,'provider_document_input_reconciliation append-only');
end;
create trigger if not exists provider_document_input_manifest_no_update
before update on provider_document_input_manifest begin
  select raise(abort,'provider_document_input_manifest append-only');
end;
create trigger if not exists provider_document_input_manifest_no_delete
before delete on provider_document_input_manifest begin
  select raise(abort,'provider_document_input_manifest append-only');
end;
create trigger if not exists provider_document_input_slot_no_update
before update on provider_document_input_slot begin
  select raise(abort,'provider_document_input_slot append-only');
end;
create trigger if not exists provider_document_input_slot_no_delete
before delete on provider_document_input_slot begin
  select raise(abort,'provider_document_input_slot append-only');
end;
"""


def _document_input_digests(
    prepared: PreparedProviderContractCall,
    profile: EmbeddingProfile,
) -> tuple[str, ...]:
    if (
        not isinstance(prepared, PreparedProviderContractCall)
        or not isinstance(profile, EmbeddingProfile)
        or not prepared.plan.call_id.startswith("opencode-embedding-document-")
        or prepared.call.payload.get("model") != profile.exact_model_id
    ):
        raise PolicyViolation("Document input manifest typed document/profile binding gerekli")
    inputs = prepared.call.payload.get("input")
    if (
        not isinstance(inputs, list | tuple)
        or not 1 <= len(inputs) <= 64
        or any(
            not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 256 * 1024
            for text in inputs
        )
    ):
        raise PolicyViolation("Document input manifest bounded input gerekli")
    classifications = prepared.plan.data_classifications
    if (
        len(classifications) != 1
        or prepared.plan.call_id
        != f"opencode-embedding-document-{prepared.plan.fixture_digest[7:27]}"
        or prepared.call.request_identity != prepared.plan.call_id
        or prepared.plan.payload_digest != digest(dict(prepared.call.payload))
        or prepared.plan.fixture_digest
        != digest(
            {
                "purpose": "document",
                "classification": classifications[0].value,
                "texts": list(inputs),
                "profile_digest": profile.profile_digest,
            }
        )
    ):
        raise PolicyViolation("Document input manifest prepared payload/profile drift")
    return tuple(
        digest(
            {
                "text_digest": digest(text),
                "profile_digest": profile.profile_digest,
                "endpoint_binding_digest": prepared.plan.endpoint_binding_digest,
                "classifications": [item.value for item in prepared.plan.data_classifications],
            }
        )
        for text in inputs
    )


def _time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValidationFailed("Durable provider ledger timestamp timezone ister")
    return parsed.astimezone(dt.UTC)


def _resources(value: str) -> tuple[ResourceRequest, ...]:
    document = json.loads(value)
    if not isinstance(document, list):
        raise ValidationFailed("Durable provider ledger resource listesi gecersiz")
    return tuple(
        ResourceRequest.parse(str(item["resource"]), LockMode(str(item["mode"])))
        for item in document
    )


class SQLiteProviderLedgerHost:
    """Small structural host used to prove the production runner against disk."""

    def __init__(self, path: Path, realm_id: UUID) -> None:
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValidationFailed("Durable provider ledger absolute bounded path ister")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not private_directory(path.parent):
            raise PolicyViolation("Durable provider ledger parent private olmali")
        self.path = path
        self.realm_id = realm_id
        self.worker_label = "windows-opencode-durable-e2e"
        self.ledger = self
        self.jobs = self
        with self._connect() as connection:
            mode = str(connection.execute("pragma journal_mode=delete").fetchone()[0]).lower()
            if mode != "delete":
                raise PolicyViolation("Durable provider ledger DELETE journal ister")
            connection.execute("pragma synchronous=full")
            connection.executescript(SCHEMA)
        if not private_regular(path):
            raise PolicyViolation("Durable provider ledger private regular file olmali")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        connection.execute("pragma busy_timeout=30000")
        return connection

    def register(
        self,
        work: ClaimedWork,
        *,
        document_call: PreparedProviderContractCall | None = None,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        if work.job.realm_id != self.realm_id:
            raise PolicyViolation("Provider job realm drift")
        if (document_call is None) != (profile is None):
            raise PolicyViolation("Document input manifest call/profile birlikte gerekli")
        manifest = None
        if document_call is not None and profile is not None:
            inputs = _document_input_digests(document_call, profile)
            manifest = {
                "job_id": str(work.job.id),
                "effect_digest": document_call.plan.effect_request.effect_digest,
                "operation": f"provider-contract:{document_call.plan.call_id}",
                "profile_digest": profile.profile_digest,
                "input_digests": inputs,
            }
        with self._connect() as connection:
            connection.execute("begin immediate")
            connection.execute(
                "insert into provider_job values(?,?,?,?,?)",
                (
                    str(work.job.id),
                    str(work.job.realm_id),
                    str(work.job.project_id),
                    str(work.attempt_id),
                    work.job.created_at.isoformat(),
                ),
            )
            if manifest is not None:
                connection.execute(
                    "insert into provider_document_input_manifest values(?,?,?,?,?,?)",
                    (
                        manifest["job_id"],
                        manifest["effect_digest"],
                        manifest["operation"],
                        manifest["profile_digest"],
                        canonical_json(manifest["input_digests"]),
                        digest(manifest),
                    ),
                )
                connection.executemany(
                    "insert into provider_document_input_slot values(?,?,?)",
                    [
                        (manifest["job_id"], slot, value)
                        for slot, value in enumerate(manifest["input_digests"])
                    ],
                )
            connection.commit()

    def claim_effect(self, work: ClaimedWork, **kwargs: Any) -> EffectClaim:
        claim = EffectClaim.create(
            realm_id=self.realm_id,
            job_id=work.job.id,
            attempt_id=work.attempt_id,
            operation=str(kwargs["operation"]),
            effect_digest=str(kwargs["effect_digest"]),
            authorization_digest=str(kwargs["authorization_digest"]),
            idempotency_key=str(kwargs["idempotency_key"]),
            resources=tuple(kwargs["resources"]),
            execution_identity=self.worker_label,
            fencing_token=work.lease.fencing_token,
            adapter_digest=str(kwargs["adapter_digest"]),
        )
        with self._connect() as connection:
            connection.execute("begin immediate")
            if claim.operation.startswith("provider-contract:opencode-embedding-document-"):
                manifest = connection.execute(
                    "select * from provider_document_input_manifest where job_id=?",
                    (str(work.job.id),),
                ).fetchone()
                if manifest is not None:
                    body = {
                        "job_id": manifest["job_id"],
                        "effect_digest": manifest["effect_digest"],
                        "operation": manifest["operation"],
                        "profile_digest": manifest["profile_digest"],
                        "input_digests": json.loads(manifest["input_digests_json"]),
                    }
                    slots = connection.execute(
                        "select input_digest from provider_document_input_slot where job_id=? order by slot",
                        (str(work.job.id),),
                    ).fetchall()
                    if (
                        manifest["binding_digest"] != digest(body)
                        or body["effect_digest"] != claim.effect_digest
                        or body["operation"] != claim.operation
                        or [row[0] for row in slots] != body["input_digests"]
                    ):
                        raise PolicyViolation("Document input manifest binding drift")
                    # Claims and input overlap are checked in the same write transaction.
                    # A legacy claim without slot evidence cannot prove disjoint inputs.
                    overlap = connection.execute(
                        "select c.id from provider_effect_claim c "
                        "join provider_job j on j.id=c.job_id "
                        "left join provider_effect_receipt r on r.claim_id=c.id "
                        "left join provider_document_input_manifest m on m.job_id=c.job_id "
                        "where c.realm_id=? and j.project_id=? "
                        "and c.operation like 'provider-contract:opencode-embedding-document-%' "
                        "and (r.id is null or r.status='completed' or r.failure_category='unknown-effect') "
                        "and (m.job_id is null or exists (select 1 from provider_document_input_slot old "
                        "join provider_document_input_slot new on old.input_digest=new.input_digest "
                        "where old.job_id=c.job_id and new.job_id=?)) limit 1",
                        (str(claim.realm_id), str(work.job.project_id), str(work.job.id)),
                    ).fetchone()
                    if overlap is not None:
                        raise PolicyViolation(
                            "Document embedding girdileri onceki claim/receipt ile uzlastirilmali"
                        )
                elif (
                    connection.execute(
                        "select 1 from provider_document_input_manifest m join provider_job j on j.id=m.job_id "
                        "where j.realm_id=? and j.project_id=? limit 1",
                        (str(claim.realm_id), str(work.job.project_id)),
                    ).fetchone()
                    is not None
                ):
                    raise PolicyViolation("Document embedding input manifest gerekli")
                prior = connection.execute(
                    "select c.id from provider_effect_claim c "
                    "join provider_job j on j.id=c.job_id "
                    "left join provider_effect_receipt r on r.claim_id=c.id "
                    "where c.realm_id=? and j.project_id=? and c.effect_digest=? "
                    "and c.operation like 'provider-contract:opencode-embedding-document-%' "
                    "and (r.id is null or r.status='completed' "
                    "or r.failure_category='unknown-effect') limit 1",
                    (str(claim.realm_id), str(work.job.project_id), claim.effect_digest),
                ).fetchone()
                if prior is not None:
                    raise PolicyViolation(
                        "Document embedding effect onceki claim/receipt ile uzlastirilmali"
                    )
            connection.execute(
                "insert into provider_effect_claim values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(claim.id),
                    str(claim.realm_id),
                    str(claim.job_id),
                    str(claim.attempt_id),
                    claim.operation,
                    claim.effect_digest,
                    claim.authorization_digest,
                    claim.idempotency_key,
                    json.dumps([item.as_dict() for item in claim.resources], sort_keys=True),
                    claim.execution_identity,
                    claim.fencing_token,
                    claim.adapter_digest,
                    claim.claimed_at.isoformat(),
                ),
            )
            connection.commit()
        return claim

    def record_success(self, claim: EffectClaim, **kwargs: Any) -> EffectReceipt:
        receipt = EffectReceipt.completed(
            realm_id=self.realm_id,
            claim=claim,
            result_digest=str(kwargs["result_digest"]),
            adapter_evidence_digest=str(kwargs["adapter_evidence_digest"]),
        )
        self._record_receipt(receipt)
        return receipt

    def record_failure(self, claim: EffectClaim, **kwargs: Any) -> EffectReceipt:
        receipt = EffectReceipt.failed(
            realm_id=self.realm_id,
            claim=claim,
            category=kwargs["category"],
            failure_digest=str(kwargs["failure_digest"]),
        )
        self._record_receipt(receipt)
        return receipt

    def _record_receipt(self, receipt: EffectReceipt) -> None:
        with self._connect() as connection:
            connection.execute("begin immediate")
            connection.execute(
                "insert into provider_effect_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(receipt.id),
                    str(receipt.realm_id),
                    str(receipt.claim_id),
                    receipt.status.value,
                    receipt.result_digest,
                    None if receipt.failure_category is None else receipt.failure_category.value,
                    receipt.failure_digest,
                    receipt.adapter_evidence_digest,
                    receipt.token_count,
                    receipt.cost_micros,
                    receipt.latency_ms,
                    receipt.completed_at.isoformat(),
                ),
            )
            connection.commit()

    def claims_for_job(self, job_id: UUID) -> tuple[EffectClaim, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from provider_effect_claim where job_id=? order by claimed_at,id",
                (str(job_id),),
            ).fetchall()
        return tuple(
            EffectClaim(
                id=UUID(row["id"]),
                realm_id=UUID(row["realm_id"]),
                job_id=UUID(row["job_id"]),
                attempt_id=UUID(row["attempt_id"]),
                operation=row["operation"],
                effect_digest=row["effect_digest"],
                authorization_digest=row["authorization_digest"],
                idempotency_key=row["idempotency_key"],
                resources=_resources(row["resources_json"]),
                execution_identity=row["execution_identity"],
                fencing_token=row["fencing_token"],
                adapter_digest=row["adapter_digest"],
                claimed_at=_time(row["claimed_at"]),
            )
            for row in rows
        )

    def receipt_for_claim(self, claim_id: UUID) -> EffectReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from provider_effect_receipt where claim_id=?", (str(claim_id),)
            ).fetchone()
        if row is None:
            return None
        category = row["failure_category"]
        return EffectReceipt(
            id=UUID(row["id"]),
            realm_id=UUID(row["realm_id"]),
            claim_id=UUID(row["claim_id"]),
            status=ReceiptStatus(row["status"]),
            result_digest=row["result_digest"],
            failure_category=None if category is None else FailureCategory(category),
            failure_digest=row["failure_digest"],
            adapter_evidence_digest=row["adapter_evidence_digest"],
            token_count=row["token_count"],
            cost_micros=row["cost_micros"],
            latency_ms=row["latency_ms"],
            completed_at=_time(row["completed_at"]),
        )

    def mark_recovery_required(self, job_id: UUID, reason: str) -> None:
        with self._connect() as connection:
            connection.execute("begin immediate")
            connection.execute(
                "insert or ignore into provider_recovery values(?,?,?)",
                (str(job_id), digest(reason), dt.datetime.now(dt.UTC).isoformat()),
            )
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            claims = connection.execute("select count(*) from provider_effect_claim").fetchone()[0]
            receipts = connection.execute(
                "select count(*) from provider_effect_receipt"
            ).fetchone()[0]
            recoveries = connection.execute("select count(*) from provider_recovery").fetchone()[0]
            mode = str(connection.execute("pragma journal_mode").fetchone()[0]).lower()
            integrity = str(connection.execute("pragma integrity_check").fetchone()[0]).lower()
        return {
            "claims": claims,
            "receipts": receipts,
            "recoveries": recoveries,
            "journal_mode": mode,
            "integrity_check": integrity,
        }


@dataclass(frozen=True, slots=True)
class LiveProcessClient:
    configuration: Any
    transport: ProcessIsolatedJsonProviderTransport
    endpoint_mapping: Mapping[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def invoke(
        self,
        call: ProviderCall,
        *,
        secret_ref: SecretRef,
        authorization: Authorization,
        consumed_by: str,
    ) -> ProviderCallResult:
        del consumed_by
        endpoint = self.endpoint_mapping.get((call.endpoint_ref, call.operation))
        if endpoint is None:
            endpoint = OpenCodeEndpointResolver(
                self.configuration.provider_id,
                f"opencode:{self.configuration.provider_id}:embeddings",
                self.configuration.embedding_endpoint,
            ).resolve(call.endpoint_ref, call.operation)
        if (
            call.endpoint_path_hint is None
            or call.endpoint_binding_digest
            != reviewed_endpoint_digest(endpoint, path_hint=urlsplit(endpoint).path)
        ):
            raise PolicyViolation("Live provider endpoint reviewed binding drift")
        store = OpenCodeCredentialStore(
            self.configuration.provider_id, self.configuration.credential_locator
        )
        credential = store.resolve(secret_ref)
        outbound_id = uuid4()
        try:
            response = self.transport.post_json(
                endpoint,
                call.payload,
                credential,
                gateway_provenance=GatewayTransportProvenance(
                    digest(
                        {
                            "call_id": call.request_identity,
                            "authorization_id": str(authorization.id),
                        }
                    ),
                    uuid4(),
                    outbound_id,
                ),
            )
        finally:
            credential.clear()
        return ProviderCallResult(response, digest(dict(response)), outbound_id, authorization.id)


class SQLiteDocumentEmbeddingJournal:
    """Recover exact completed document vectors without retaining source or raw responses."""

    def __init__(self, host: SQLiteProviderLedgerHost, project_id: UUID) -> None:
        self.host = host
        self.project_id = project_id

    def _key(
        self,
        prepared: PreparedProviderContractCall,
        profile: EmbeddingProfile,
    ) -> tuple[str, str, str, str]:
        _document_input_digests(prepared, profile)
        if not prepared.plan.call_id.startswith("opencode-embedding-document-"):
            raise PolicyViolation("Document vector journal document effect required")
        if prepared.call.payload.get("model") != profile.exact_model_id:
            raise PolicyViolation("Document vector journal model binding drift")
        return (
            str(self.host.realm_id),
            str(self.project_id),
            prepared.plan.effect_request.effect_digest,
            profile.profile_digest,
        )

    @staticmethod
    def _validate_vectors(
        prepared: PreparedProviderContractCall,
        profile: EmbeddingProfile,
        vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        inputs = prepared.call.payload.get("input")
        if not isinstance(inputs, list | tuple) or len(vectors) != len(inputs):
            raise PolicyViolation("Document vector journal input slot binding drift")
        for vector in vectors:
            profile.validate_vector(vector)

    @staticmethod
    def _reconciliation_request_digest(
        key: tuple[str, str, str, str], inputs: tuple[str, ...]
    ) -> str:
        return digest(
            {
                "schema": "zekam-provider-document-input-reconciliation-request/v1",
                "realm_id": key[0],
                "project_id": key[1],
                "effect_digest": key[2],
                "profile_digest": key[3],
                "input_digests": list(inputs),
            }
        )

    def _validate_reconciliation_slot(
        self,
        key: tuple[str, str, str, str],
        profile: EmbeddingProfile,
        item: Mapping[str, Any],
        input_digest: str,
    ) -> tuple[str, str, tuple[tuple[float, ...], ...]]:
        required = {
            "input_digest",
            "slot",
            "parent_effect_digest",
            "manifest_digest",
            "claim_id",
            "receipt_id",
            "result_digest",
            "operation",
        }
        if set(item) != required or item.get("input_digest") != input_digest:
            raise PolicyViolation("Document reconciliation input kaniti drift")
        slot = item.get("slot")
        parent_effect_digest = item.get("parent_effect_digest")
        operation = item.get("operation")
        if (
            type(slot) is not int
            or not 0 <= slot < 64
            or not isinstance(parent_effect_digest, str)
            or not isinstance(operation, str)
            or not operation.startswith("provider-contract:opencode-embedding-document-")
            or not all(
                isinstance(item.get(name), str)
                for name in ("manifest_digest", "claim_id", "receipt_id", "result_digest")
            )
        ):
            raise PolicyViolation("Document reconciliation slot typed evidence invalid")
        parent_key = (key[0], key[1], parent_effect_digest, key[3])
        parent = self._load_key(parent_key, profile, operation)
        if parent is None or slot >= len(parent.vectors):
            raise PolicyViolation("Document reconciliation parent vector kaniti eksik")
        with self.host._connect() as db:
            source = db.execute(
                "select v.claim_id,v.receipt_id,c.operation,r.status,v.vectors_digest,"
                "m.binding_digest,s.input_digest "
                "from provider_document_vector_result v "
                "join provider_effect_claim c on c.id=v.claim_id "
                "join provider_effect_receipt r on r.id=v.receipt_id and r.claim_id=c.id "
                "join provider_document_input_manifest m on m.job_id=c.job_id "
                "join provider_document_input_slot s on s.job_id=m.job_id "
                "where v.realm_id=? and v.project_id=? and v.effect_digest=? "
                "and v.profile_digest=? and s.input_digest=? and s.slot=?",
                (*parent_key, input_digest, slot),
            ).fetchone()
        if source is None or tuple(source) != (
            item["claim_id"],
            item["receipt_id"],
            operation,
            "completed",
            item["result_digest"],
            item["manifest_digest"],
            input_digest,
        ):
            raise PolicyViolation("Document reconciliation claim/receipt kaniti drift")
        if parent.evidence_digest != item["result_digest"]:
            raise PolicyViolation("Document reconciliation result kaniti drift")
        return parent_effect_digest, operation, parent.vectors

    def _load_reconciliation(
        self,
        key: tuple[str, str, str, str],
        profile: EmbeddingProfile,
        inputs: tuple[str, ...],
    ) -> RecoveredDocumentVectors | None:
        request_digest = self._reconciliation_request_digest(key, inputs)
        with self.host._connect() as db:
            row = db.execute(
                "select body_json,body_digest from provider_document_input_reconciliation "
                "where realm_id=? and project_id=? and profile_digest=? and request_digest=?",
                (*key[:2], key[3], request_digest),
            ).fetchone()
        if row is None:
            return None
        try:
            body = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PolicyViolation("Document reconciliation body JSON gecersiz") from exc
        if (
            not isinstance(body, dict)
            or canonical_json(body) != str(row[0])
            or digest(body) != str(row[1])
            or body.get("schema") != "zekam-provider-document-input-reconciliation/v1"
            or body.get("realm_id") != key[0]
            or body.get("project_id") != key[1]
            or body.get("profile_digest") != key[3]
            or body.get("request_digest") != request_digest
            or body.get("requested_key") != list(key)
            or body.get("input_digests") != list(inputs)
        ):
            raise PolicyViolation("Document reconciliation request binding drift")
        slots = body.get("slots")
        if not isinstance(slots, list) or len(slots) != len(inputs) or not slots:
            raise PolicyViolation("Document reconciliation slot listesi gecersiz")
        vectors: list[tuple[float, ...]] = []
        used: set[tuple[str, int]] = set()
        for input_digest, item in zip(inputs, slots, strict=True):
            if not isinstance(item, dict):
                raise PolicyViolation("Document reconciliation slot nesnesi gecersiz")
            parent_effect_digest, _operation, parent_vectors = self._validate_reconciliation_slot(
                key, profile, item, input_digest
            )
            slot = item["slot"]
            marker = (parent_effect_digest, slot)
            if marker in used:
                raise PolicyViolation("Document reconciliation slot duplicate")
            used.add(marker)
            vectors.append(parent_vectors[slot])
        evidence = digest({"requested_key": key, "slots": slots})
        if body.get("evidence_digest") != evidence:
            raise PolicyViolation("Document reconciliation evidence digest drift")
        for vector in vectors:
            profile.validate_vector(vector)
        return RecoveredDocumentVectors(tuple(vectors), evidence)

    def _store_reconciliation(
        self,
        key: tuple[str, str, str, str],
        inputs: tuple[str, ...],
        slots: list[dict[str, Any]],
    ) -> str:
        request_digest = self._reconciliation_request_digest(key, inputs)
        evidence_digest = digest({"requested_key": key, "slots": slots})
        body = {
            "schema": "zekam-provider-document-input-reconciliation/v1",
            "realm_id": key[0],
            "project_id": key[1],
            "profile_digest": key[3],
            "request_digest": request_digest,
            "requested_key": list(key),
            "input_digests": list(inputs),
            "slots": slots,
            "evidence_digest": evidence_digest,
        }
        body_json = canonical_json(body)
        body_digest = digest(body)
        with self.host._connect() as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select body_json,body_digest from provider_document_input_reconciliation "
                "where realm_id=? and project_id=? and profile_digest=? and request_digest=?",
                (*key[:2], key[3], request_digest),
            ).fetchone()
            if existing is None:
                db.execute(
                    "insert into provider_document_input_reconciliation values(?,?,?,?,?,?,?)",
                    (
                        key[0],
                        key[1],
                        key[3],
                        request_digest,
                        body_json,
                        body_digest,
                        dt.datetime.now(dt.UTC).isoformat(),
                    ),
                )
            elif tuple(existing) != (body_json, body_digest):
                db.rollback()
                raise PolicyViolation("Document reconciliation immutable drift")
            db.commit()
        return evidence_digest

    def load(
        self,
        prepared: PreparedProviderContractCall,
        profile: EmbeddingProfile,
    ) -> RecoveredDocumentVectors | None:
        key = self._key(prepared, profile)
        exact = self._load_key(key, profile, f"provider-contract:{prepared.plan.call_id}")
        if exact is not None:
            self._validate_vectors(prepared, profile, exact.vectors)
            return exact
        inputs = _document_input_digests(prepared, profile)
        reconciled = self._load_reconciliation(key, profile, inputs)
        if reconciled is not None:
            self._validate_vectors(prepared, profile, reconciled.vectors)
            return reconciled
        vectors = []
        evidence = []
        parents = {}
        used_slots: dict[tuple[str, str, str, str], set[int]] = {}
        with self.host._connect() as db:
            candidates: dict[
                str,
                tuple[tuple[tuple[str, str, str, str], int, str, str, str, str, int], ...],
            ] = {}
            for input_digest in dict.fromkeys(inputs):
                rows = db.execute(
                    "select m.*,s.slot,v.claim_id,v.receipt_id from provider_document_input_slot s "
                    "join provider_document_input_manifest m on m.job_id=s.job_id "
                    "join provider_job j on j.id=m.job_id "
                    "join provider_effect_claim c on c.job_id=j.id and c.effect_digest=m.effect_digest and c.operation=m.operation "
                    "join provider_document_vector_result v on v.claim_id=c.id and v.profile_digest=m.profile_digest "
                    "where j.realm_id=? and j.project_id=? and m.profile_digest=? and s.input_digest=? "
                    "order by j.created_at,j.id,s.slot limit 65",
                    (key[0], key[1], key[3], input_digest),
                ).fetchall()
                if not rows:
                    return None
                if len(rows) > 64:
                    raise PolicyViolation("Document regroup input claim adayi siniri asildi")
                normalized = []
                for row in rows:
                    try:
                        input_digests = json.loads(row["input_digests_json"])
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise PolicyViolation("Document regroup input manifest gecersiz") from exc
                    body = {
                        "job_id": row["job_id"],
                        "effect_digest": row["effect_digest"],
                        "operation": row["operation"],
                        "profile_digest": row["profile_digest"],
                        "input_digests": input_digests,
                    }
                    slot = row["slot"]
                    if (
                        digest(body) != row["binding_digest"]
                        or not isinstance(input_digests, list)
                        or len(input_digests) < 1
                        or len(input_digests) > 64
                        or any(not isinstance(value, str) for value in input_digests)
                        or not 0 <= slot < len(input_digests)
                        or input_digests[slot] != input_digest
                    ):
                        raise PolicyViolation("Document regroup input binding drift")
                    parent_key = (key[0], key[1], row["effect_digest"], key[3])
                    normalized.append(
                        (
                            parent_key,
                            slot,
                            str(row["binding_digest"]),
                            str(row["claim_id"]),
                            str(row["receipt_id"]),
                            str(row["operation"]),
                            len(input_digests),
                        )
                    )
                candidates[input_digest] = tuple(normalized)

            for position, input_digest in enumerate(inputs):
                remaining_needed = sum(value == input_digest for value in inputs[position:])
                available = [
                    item
                    for item in candidates[input_digest]
                    if item[1] not in used_slots.setdefault(item[0], set())
                ]
                capacity: dict[tuple[str, str, str, str], int] = {}
                for item in available:
                    capacity[item[0]] = capacity.get(item[0], 0) + 1
                capable = {
                    parent_key
                    for parent_key, count in capacity.items()
                    if count >= remaining_needed
                }
                if len(capable) != 1:
                    raise PolicyViolation("Document regroup input claim belirsiz")
                parent_key = next(iter(capable))
                row = min(
                    (item for item in available if item[0] == parent_key),
                    key=lambda item: item[1],
                )
                _, slot, manifest_digest, claim_id, receipt_id, operation, input_count = row
                used_slots[parent_key].add(slot)
                if parent_key not in parents:
                    parents[parent_key] = self._load_key(parent_key, profile, operation)
                parent = parents[parent_key]
                if parent is None or len(parent.vectors) != input_count:
                    raise PolicyViolation("Document regroup terminal vector evidence missing")
                vectors.append(parent.vectors[slot])
                evidence.append(
                    {
                        "input_digest": input_digest,
                        "slot": slot,
                        "parent_effect_digest": parent_key[2],
                        "manifest_digest": manifest_digest,
                        "claim_id": claim_id,
                        "receipt_id": receipt_id,
                        "operation": operation,
                        "result_digest": parent.evidence_digest,
                    }
                )
        evidence_digest = self._store_reconciliation(key, inputs, evidence)
        return RecoveredDocumentVectors(tuple(vectors), evidence_digest)

    def _load_key(
        self,
        key: tuple[str, str, str, str],
        profile: EmbeddingProfile,
        operation: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> RecoveredDocumentVectors | None:
        with self.host._connect() if connection is None else nullcontext(connection) as db:
            row = db.execute(
                "select v.vectors_json,v.vectors_digest,c.realm_id,j.project_id,"
                "c.effect_digest,c.operation,r.status,r.result_digest,r.id,"
                "r.realm_id,j.realm_id "
                "from provider_document_vector_result v "
                "join provider_effect_claim c on c.id=v.claim_id "
                "join provider_job j on j.id=c.job_id "
                "join provider_effect_receipt r on r.id=v.receipt_id and r.claim_id=c.id "
                "where v.realm_id=? and v.project_id=? "
                "and v.effect_digest=? and v.profile_digest=?",
                key,
            ).fetchone()
            exists = db.execute(
                "select 1 from provider_document_vector_result "
                "where realm_id=? and project_id=? and effect_digest=? and profile_digest=?",
                key,
            ).fetchone()
        if row is None:
            if exists is not None:
                raise PolicyViolation("Document vector journal terminal evidence missing")
            return None
        if (
            tuple(row)[2:5] != key[:3]
            or row[5] != operation
            or row[6] != "completed"
            or not isinstance(row[7], str)
            or row[9] != key[0]
            or row[10] != key[0]
        ):
            raise PolicyViolation("Document vector journal receipt binding drift")
        try:
            values = json.loads(str(row[0]))
            if not isinstance(values, list) or any(
                not isinstance(vector, list) for vector in values
            ):
                raise ValueError("vector shape")
            vectors = tuple(tuple(vector) for vector in values)
            if not 1 <= len(vectors) <= 64:
                raise ValueError("bounded vector count")
            for vector in vectors:
                profile.validate_vector(vector)
        except (TypeError, ValueError, ValidationFailed) as exc:
            raise PolicyViolation("Document vector journal payload invalid") from exc
        expected = digest(
            {"key": key, "receipt_id": str(row[8]), "response_digest": row[7], "vectors": vectors}
        )
        if row[1] != expected:
            raise PolicyViolation("Document vector journal digest drift")
        return RecoveredDocumentVectors(vectors, expected)

    def bind_completed_inputs(
        self,
        prepared: PreparedProviderContractCall,
        profile: EmbeddingProfile,
    ) -> dict[str, Any] | None:
        """Append slot evidence only for an exact, completed durable batch.

        No claim, receipt, remote reconciliation or success is fabricated. The
        caller must still possess the original ordered inputs and qualified profile.
        """
        key = self._key(prepared, profile)
        operation = f"provider-contract:{prepared.plan.call_id}"
        inputs = _document_input_digests(prepared, profile)
        with self.host._connect() as db:
            db.execute("begin immediate")
            completed = self._load_key(key, profile, operation, connection=db)
            if completed is None:
                db.rollback()
                return None
            self._validate_vectors(prepared, profile, completed.vectors)
            source = db.execute(
                "select c.job_id,v.claim_id,v.receipt_id from provider_document_vector_result v "
                "join provider_effect_claim c on c.id=v.claim_id "
                "where v.realm_id=? and v.project_id=? and v.effect_digest=? and v.profile_digest=?",
                key,
            ).fetchone()
            if source is None:
                raise PolicyViolation("Document input binding completed source missing")
            body = {
                "job_id": source["job_id"],
                "effect_digest": key[2],
                "operation": operation,
                "profile_digest": key[3],
                "input_digests": inputs,
            }
            binding_digest = digest(body)
            existing = db.execute(
                "select * from provider_document_input_manifest where job_id=?", (source["job_id"],)
            ).fetchone()
            if existing is None:
                db.execute(
                    "insert into provider_document_input_manifest values(?,?,?,?,?,?)",
                    (
                        body["job_id"],
                        body["effect_digest"],
                        operation,
                        key[3],
                        canonical_json(inputs),
                        binding_digest,
                    ),
                )
                db.executemany(
                    "insert into provider_document_input_slot values(?,?,?)",
                    [(body["job_id"], slot, value) for slot, value in enumerate(inputs)],
                )
            else:
                slots = db.execute(
                    "select slot,input_digest from provider_document_input_slot where job_id=? order by slot",
                    (source["job_id"],),
                ).fetchall()
                if tuple(existing) != (
                    body["job_id"],
                    key[2],
                    operation,
                    key[3],
                    canonical_json(inputs),
                    binding_digest,
                ) or [tuple(row) for row in slots] != list(enumerate(inputs)):
                    raise PolicyViolation("Document completed input manifest drift")
            db.commit()
        return {
            "status": "replayed" if existing is not None else "bound",
            "input_count": len(inputs),
            "manifest_digest": binding_digest,
            "source_evidence_digest": completed.evidence_digest,
            "claim_id": source["claim_id"],
            "receipt_id": source["receipt_id"],
            "provider_calls": 0,
            "grants_authority": False,
        }

    def has_unbound_completed_inputs(self) -> bool:
        with self.host._connect() as db:
            return (
                db.execute(
                    "select 1 from provider_document_vector_result v "
                    "join provider_effect_claim c on c.id=v.claim_id "
                    "left join provider_document_input_manifest m on m.job_id=c.job_id "
                    "where v.realm_id=? and v.project_id=? and m.job_id is null limit 1",
                    (str(self.host.realm_id), str(self.project_id)),
                ).fetchone()
                is not None
            )

    def record(
        self,
        execution: OpenCodeEmbeddingExecution,
        profile: EmbeddingProfile,
        vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        prepared = execution.prepared
        execution.assert_valid(prepared)
        self._validate_vectors(prepared, profile, vectors)
        key = self._key(prepared, profile)
        response = execution.provider_result.response
        rows = response.get("data")
        if (
            response.get("model") != profile.exact_model_id
            or not isinstance(rows, list)
            or len(rows) != len(vectors)
        ):
            raise PolicyViolation("Document vector journal response binding drift")
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise PolicyViolation("Document vector journal response slot invalid")
            slot = row.get("index")
            raw = row.get("embedding")
            if (
                type(slot) is not int
                or slot in seen
                or not 0 <= slot < len(vectors)
                or not isinstance(raw, list)
                or len(raw) != profile.dimension
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in raw)
            ):
                raise PolicyViolation("Document vector journal response slot invalid")
            raw_vector = tuple(float(value) for value in raw)
            norm = math.sqrt(sum(value * value for value in raw_vector))
            if (
                not math.isfinite(norm)
                or norm == 0.0
                or tuple(value / norm for value in raw_vector) != vectors[slot]
            ):
                raise PolicyViolation("Document vector journal response vector drift")
            seen.add(slot)
        if self.load(prepared, profile) is not None:
            raise PolicyViolation("Document vector journal duplicate result")
        with self.host._connect() as db:
            db.execute("begin immediate")
            receipt = db.execute(
                "select c.realm_id,j.project_id,c.effect_digest,c.operation,"
                "r.status,r.result_digest,r.realm_id,j.realm_id "
                "from provider_effect_claim c join provider_job j on j.id=c.job_id "
                "join provider_effect_receipt r on r.claim_id=c.id where c.id=? and r.id=?",
                (str(execution.claim.id), str(execution.receipt.id)),
            ).fetchone()
            expected = (
                *key[:3],
                f"provider-contract:{prepared.plan.call_id}",
                "completed",
                execution.provider_result.response_digest,
                key[0],
                key[0],
            )
            if receipt is None or tuple(receipt) != expected:
                raise PolicyViolation("Document vector journal persisted receipt required")
            db.execute(
                "insert into provider_document_vector_result values(?,?,?,?,?,?,?,?)",
                (
                    *key,
                    str(execution.claim.id),
                    str(execution.receipt.id),
                    canonical_json(vectors),
                    digest(
                        {
                            "key": key,
                            "receipt_id": str(execution.receipt.id),
                            "response_digest": execution.provider_result.response_digest,
                            "vectors": vectors,
                        }
                    ),
                ),
            )
            db.commit()


def _work(realm_id: UUID, project_id: UUID) -> ClaimedWork:
    attempt_id = uuid4()
    token = new_owner_token()
    job = replace(
        Job.create(
            realm_id=realm_id,
            project_id=project_id,
            kind=JobKind.PROVIDER_CALL,
            idempotency_key=f"windows-opencode-e2e:{uuid4()}",
        ),
        state=JobState.RUNNING,
        attempt_count=1,
        fencing_token=1,
    )
    now = dt.datetime.now(dt.UTC)
    lease = Lease(
        id=uuid4(),
        realm_id=realm_id,
        job_id=job.id,
        attempt_id=attempt_id,
        owner_digest=owner_digest(token),
        fencing_token=1,
        expires_at=now + dt.timedelta(minutes=5),
        heartbeat_at=now,
        worker_label="windows-opencode-durable-e2e",
    )
    return ClaimedWork(job, attempt_id, lease, token)


def execute(database: Path, config_file: Path) -> dict[str, Any]:
    inventory = load_inventory()
    configuration = load_opencode_embedding_configuration(
        config_file,
        provider_id="litellm",
        selected_model_id="openai/BAAI/bge-m3",
        inventory=inventory,
    )
    realm_id, project_id = uuid4(), uuid4()
    host = SQLiteProviderLedgerHost(database, realm_id)
    client = LiveProcessClient(configuration, ProcessIsolatedJsonProviderTransport())

    def invocation(prepared: PreparedProviderContractCall) -> OpenCodeRuntimeInvocation:
        work = _work(realm_id, project_id)
        host.register(work)
        secret_ref = SecretRef.create(
            realm_id=realm_id,
            name="opencode-litellm-embedding",
            provider=prepared.plan.provider_ref,
            purpose="windows durable embedding e2e",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator=configuration.credential_locator,
        )
        authorization = Authorization.issue(
            realm_id=realm_id,
            actor_id=uuid4(),
            plan_digest=prepared.plan.authorization_plan_digest,
            effect_digest=prepared.plan.effect_request.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(prepared.plan.target, prepared.plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(prepared.plan.provider_ref,),
                secret_ref_ids=(secret_ref.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=5),
        )
        return OpenCodeRuntimeInvocation(
            RuntimeProviderContractRunner(
                host=cast(ProviderExecutionHost, host), work=work, client=client
            ),
            secret_ref,
            authorization,
            "windows-opencode-durable-e2e",
        )

    provider = OpenCodeRemoteEmbeddingProvider(
        configuration,
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=1024,
    )
    fixture = EmbeddingProbeFixture(
        query="Which service validates an active product release?",
        positive_passage="The product version service validates whether a release is active.",
        negative_passage="A recipe describes baking a chocolate cake.",
        source_refs=("synthetic:product-version", "synthetic:recipe"),
        source_digests=(digest("product-version"), digest("recipe")),
        classification=DataClassification.PUBLIC,
    )
    batch, _ = provider._vectors(
        (fixture.query, fixture.query, fixture.positive_passage, fixture.negative_passage),
        purpose=EmbeddingPurpose.PROBE,
        classification=fixture.classification,
    )
    single, _ = provider._vectors(
        (fixture.query,),
        purpose=EmbeddingPurpose.PROBE,
        classification=fixture.classification,
    )
    max_repeat_delta = provider._max_delta(batch[0], batch[1])
    max_batch_delta = provider._max_delta(batch[0], single[0])
    repeat_cosine = provider._score(batch[0], batch[1])
    batch_cosine = provider._score(batch[0], single[0])
    positive_score = provider._score(batch[0], batch[2])
    negative_score = provider._score(batch[0], batch[3])
    semantic_margin = positive_score - negative_score
    determinism_passed = (
        max_repeat_delta <= MAX_REPEAT_DELTA
        and max_batch_delta <= MAX_BATCH_DELTA
        and repeat_cosine >= MIN_REPEAT_COSINE
        and batch_cosine >= MIN_BATCH_COSINE
    )
    semantic_passed = semantic_margin > MIN_SEMANTIC_MARGIN
    before_restart = host.summary()
    restarted = SQLiteProviderLedgerHost(database, realm_id)
    after_restart = restarted.summary()
    if before_restart != after_restart or after_restart != {
        "claims": 2,
        "receipts": 2,
        "recoveries": 0,
        "journal_mode": "delete",
        "integrity_check": "ok",
    }:
        raise PolicyViolation("Durable provider ledger restart readback drift")
    database_digest = hashlib.sha256(
        read_bounded_bytes(database, max_bytes=64 * 1024 * 1024)
    ).hexdigest()
    evidence_digest = digest(
        {
            "exact_model_id": configuration.selected_model_id,
            "max_repeat_delta": max_repeat_delta,
            "max_batch_delta": max_batch_delta,
            "repeat_cosine": repeat_cosine,
            "batch_cosine": batch_cosine,
            "semantic_margin": semantic_margin,
            "durable_ledger": after_restart,
            "database_sha256": f"sha256:{database_digest}",
        }
    )
    return {
        "schema": "zekam-windows-opencode-durable-e2e/v1",
        "status": "passed" if determinism_passed and semantic_passed else "partial",
        "durable_e2e_status": "passed",
        "model_qualification_status": (
            "passed" if determinism_passed and semantic_passed else "failed"
        ),
        "platform": "windows-amd64" if os.name == "nt" else os.name,
        "provider_id": configuration.provider_id,
        "provider_identity_digest": configuration.endpoint_identity.identity_digest,
        "exact_model_id": configuration.selected_model_id,
        "canonical_model_id": configuration.canonical_model_id,
        "dimension": len(batch[0]),
        "semantic_margin": semantic_margin,
        "max_repeat_delta": max_repeat_delta,
        "max_batch_delta": max_batch_delta,
        "repeat_cosine": repeat_cosine,
        "batch_cosine": batch_cosine,
        "determinism_thresholds": {
            "max_repeat_delta": MAX_REPEAT_DELTA,
            "max_batch_delta": MAX_BATCH_DELTA,
            "min_repeat_cosine": MIN_REPEAT_COSINE,
            "min_batch_cosine": MIN_BATCH_COSINE,
            "min_semantic_margin": MIN_SEMANTIC_MARGIN,
        },
        "provider_call_count": 2,
        "probe_evidence_digest": evidence_digest,
        "durable_ledger": after_restart,
        "restart_readback": True,
        "database_sha256": f"sha256:{database_digest}",
        "credential_source": "environment",
        "secret_value_recorded": False,
    }


def execute_reviewed_campaign(
    database: Path,
    config_file: Path,
    manifest: PreparedCampaignManifest,
) -> dict[str, Any]:
    """Run the reviewed OpenCode manifest through the durable provider boundary.

    The caller owns the reviewed plan-digest check.  This function only accepts the
    already prepared, exact manifest and never retries a provider effect.  Health is
    evaluated before benchmark calls for each model; a failed health call therefore
    produces an explicit skipped benchmark set rather than a silent partial pass.
    """

    inventory = load_inventory()
    configuration = load_opencode_embedding_configuration(
        config_file,
        provider_id=manifest.discovery.scope.provider_id,
        selected_model_id="openai/BAAI/bge-m3",
        inventory=inventory,
    )
    if (
        configuration.credential_locator not in os.environ
        or not os.environ[configuration.credential_locator].strip()
    ):
        raise PolicyViolation("OpenCode credential locator run oncesi hazir degil")
    if len(manifest.calls) != manifest.discovery.provider_call_budget:
        raise PolicyViolation("Reviewed campaign manifest exact provider budget disinda")

    realm_id = uuid4()
    project_id = uuid4()
    host = SQLiteProviderLedgerHost(database, realm_id)
    client = LiveProcessClient(
        configuration,
        ProcessIsolatedJsonProviderTransport(),
        endpoint_mapping=manifest.endpoint_mapping,
    )
    fixtures = load_fixture_registry()
    fixtures_by_digest = {item.fixture_digest: item for item in fixtures.fixtures}
    fixture_root = default_fixture_file().parent.resolve(strict=True)
    evaluator = DeterministicProviderNeutralVerifier()
    health: dict[str, bool] = {}
    results: list[dict[str, Any]] = []
    skipped = 0

    for item in manifest.calls:
        if item.kind is CampaignCallKind.BENCHMARK and not health.get(
            item.canonical_model_id, False
        ):
            skipped += 1
            results.append(
                {
                    "call_id": item.call_id,
                    "kind": item.kind.value,
                    "model_id": item.canonical_model_id,
                    "status": "skipped-health-failed",
                    "provider_calls": 0,
                }
            )
            continue

        prepared = item.prepared
        work = _work(realm_id, project_id)
        host.register(work)
        secret_ref = SecretRef.create(
            realm_id=realm_id,
            name=prepared.plan.secret_ref_name,
            provider=prepared.plan.provider_ref,
            purpose="reviewed OpenCode AIHub campaign",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator=configuration.credential_locator,
            project_id=project_id,
        )
        authorization = Authorization.issue(
            realm_id=realm_id,
            actor_id=uuid4(),
            plan_digest=prepared.plan.authorization_plan_digest,
            effect_digest=prepared.plan.effect_request.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(prepared.plan.target, prepared.plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(prepared.plan.provider_ref,),
                secret_ref_ids=(secret_ref.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=5),
        )
        runner = RuntimeProviderContractRunner(
            host=cast(ProviderExecutionHost, host), work=work, client=client
        )
        execution = runner.invoke(
            prepared,
            secret_ref=secret_ref,
            authorization=authorization,
            consumed_by=f"cli:model-campaign:{item.kind.value}:{item.call_id}",
        )
        fixture = fixtures_by_digest[item.fixture_digest]
        artifact = load_remote_fixture(fixture, allow_root=fixture_root)
        try:
            normalized = normalize_provider_response(
                item.modality,
                execution.provider_result.response,
                artifact=artifact,
            )
            evaluation = evaluator.verify(artifact, normalized)
            passed = bool(evaluation.approved)
            evaluation_body: Mapping[str, Any] = evaluation.evidence_body()
        except (PolicyViolation, ValidationFailed, ValueError, TypeError) as exc:
            # The provider effect succeeded, but its response contract did not.
            # Keep the durable success receipt and turn this into a model failure so
            # health admission can safely gate the remaining calls.
            passed = False
            evaluation_body = {
                "approved": False,
                "failure_category": "response-contract",
                "error_type": type(exc).__name__,
            }
        if item.kind is CampaignCallKind.HEALTH:
            health[item.canonical_model_id] = passed
        results.append(
            {
                "call_id": item.call_id,
                "kind": item.kind.value,
                "model_id": item.canonical_model_id,
                "status": "passed" if passed else "failed",
                "receipt_id": str(execution.receipt.id),
                "response_digest": execution.provider_result.response_digest,
                "evaluation": dict(evaluation_body),
                "provider_calls": 1,
            }
        )

    completed = all(item["status"] in {"passed", "skipped-health-failed"} for item in results)
    return {
        "schema": "zekam-opencode-benchmark-campaign-run/v1",
        "state": "completed" if completed else "failed-contracts",
        "campaign_kind": "reviewed-opencode-aihub",
        "manifest_digest": manifest.manifest_digest,
        "exact_call_budget": manifest.discovery.provider_call_budget,
        "provider_calls": sum(int(item["provider_calls"]) for item in results),
        "network_calls": sum(int(item["provider_calls"]) for item in results),
        "skipped_health_failed": skipped,
        "health_passed_model_count": sum(health.values()),
        "results": results,
        "durable_ledger": host.summary(),
        "credential_source": "environment",
        "secret_value_recorded": False,
        "qualifies_production_models": False,
        "grants_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=default_opencode_config_file())
    args = parser.parse_args()
    result = execute(args.database.resolve(), args.config.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
