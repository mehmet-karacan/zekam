"""OpenCode BGE-M3 calls backed by a durable local claim/receipt ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from zekam.application.embedding_provider import EmbeddingProbeFixture, EmbeddingPurpose
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_embedding import (
    OpenCodeCredentialStore,
    OpenCodeEndpointResolver,
    default_opencode_config_file,
    load_opencode_embedding_configuration,
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
from zekam.domain.canonical import digest
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
    OpenCodeRemoteEmbeddingProvider,
    OpenCodeRuntimeInvocation,
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
"""


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

    def register(self, work: ClaimedWork) -> None:
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

    def invoke(
        self,
        call: ProviderCall,
        *,
        secret_ref: SecretRef,
        authorization: Authorization,
        consumed_by: str,
    ) -> ProviderCallResult:
        del consumed_by
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
    database_digest = hashlib.sha256(database.read_bytes()).hexdigest()
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
