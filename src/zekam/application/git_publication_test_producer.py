"""Host-owned test signer over actual canonical measurement and source readback."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from zekam.application.canonical_job_runtime import (
    _completed_result,
    canonical_job_key,
    run_canonical_job,
)
from zekam.application.config import load_settings
from zekam.application.git_publication import (
    GitPublicationEvidence,
    GitPublicationPlan,
    GitPublicationTarget,
)
from zekam.application.git_publication_test_source import read_publication_test_source
from zekam.application.git_publication_trust import load_publication_trust
from zekam.application.git_publication_wire import publication_evidence_from_body
from zekam.application.local_attestation import Ed25519ReceiptSigner
from zekam.application.unit_test_source_evidence import _object, read_native_measurement_source
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.git_publication import evidence_attestation_body
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "publication.test.attest"


def _input(subject_digest, source_job_id, producer_id, trust_digest):
    return digest(
        {
            "schema": "zekam-publication-test-producer-input/v1",
            "subject_digest": subject_digest,
            "source_job_id": source_job_id,
            "producer_id": str(producer_id),
            "trust_digest": trust_digest,
        }
    )


def _trust(home, *, binding, realm_id, producer_id):
    trust = load_publication_trust(home)
    if (
        trust.realm_id != realm_id
        or trust.config_revision_id != binding.config_revision_id
        or trust.config_digest != binding.config_digest
        or load_settings(home=home).git_publication.trust_digest != trust.trust_digest
        or producer_id not in trust.verifiers["test"]
    ):
        raise PolicyViolation("Publication test producer active role/config/realm drift")
    return trust


def _verify_result(home, *, result, snapshot, binding, trust):
    body = {k: v for k, v in result.items() if k not in {"canonical_job_id", "canonical_replayed"}}
    if set(body) != {
        "schema",
        "realm_id",
        "subject_digest",
        "envelope",
        "source_verification",
        "trust_digest",
        "grants_authority",
    }:
        raise PolicyViolation("Publication test producer exact result required")
    envelope = body["envelope"]
    if not isinstance(envelope, dict):
        raise PolicyViolation("Publication test producer signed envelope required")
    evidence = publication_evidence_from_body(envelope["evidence"])
    producer = UUID(envelope["producer_id"])
    if (
        body["schema"] != "zekam-publication-test-producer-result/v1"
        or body["realm_id"] != str(trust.realm_id)
        or body["trust_digest"] != trust.trust_digest
        or body["grants_authority"] is not False
        or evidence.kind != "test"
        or evidence.executor_id != producer
        or evidence.passed is not True
        or evidence.subject_digest != body["subject_digest"]
        or snapshot["payload"]["input_digest"]
        != _input(body["subject_digest"], envelope["source_job_id"], producer, trust.trust_digest)
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, snapshot["payload"]["input_digest"])
    ):
        raise PolicyViolation("Publication test producer identity/input drift")
    unsigned = evidence_attestation_body(
        evidence,
        source_job_id=envelope["source_job_id"],
        source_evidence_digest=envelope["source_evidence_digest"],
        producer_id=producer,
    )
    if set(envelope) != set(unsigned) | {"attestation"} or canonical_bytes(
        unsigned
    ) != canonical_bytes({k: v for k, v in envelope.items() if k != "attestation"}):
        raise PolicyViolation("Publication test producer attestation body drift")
    verifier = trust.verifiers["test"].get(producer)
    if verifier is None or not verifier.matches_receipt_digest(unsigned, envelope["attestation"]):
        raise PolicyViolation("Publication test producer trusted signature missing")
    source = read_native_measurement_source(home, envelope["source_job_id"])
    proof = body["source_verification"]
    if not isinstance(proof, dict) or proof.get("report_digest") != digest(
        {k: v for k, v in proof.items() if k != "report_digest"}
    ):
        raise PolicyViolation("Publication test producer source proof digest drift")
    target = GitPublicationTarget(**proof["target"])
    if (
        source["source_evidence_digest"] != envelope["source_evidence_digest"]
        or source["execution_id"] != str(evidence.execution_id)
        or source["project_id"] != binding.project_id
        or source["work_item_id"] != binding.work_item_id
        or source["source_snapshot_id"] != binding.source_snapshot_id
        or source["config_revision_id"] != binding.config_revision_id
        or source["config_digest"] != binding.config_digest
        or source["source_revision"] != target.head
        or source["outcome"] != "target-met"
        or source["measurement_status"] != "accepted"
        or source["attempt_state"] != "completed"
        or source["build_exit_code"] != 0
        or source["recorded_freshness"] != "fresh"
        or source["recorded_drift"]
        or proof.get("schema") != "zekam-publication-test-source/v1"
        or proof.get("subject_digest") != evidence.subject_digest
        or proof.get("publication_binding_digest") != binding.binding_digest
        or digest({"binding": binding.body(), "target": target.body()}) != evidence.subject_digest
        or proof.get("source_job_id") != source["job_id"]
        or proof.get("source_evidence_digest") != source["source_evidence_digest"]
        or proof.get("source_report_digest") != source["report_digest"]
        or proof.get("execution_id") != str(evidence.execution_id)
        or proof.get("request_digest") != source["request_digest"]
        or proof.get("measurement_record_digest") != source["measurement_record_digest"]
        or proof.get("current_source_verified") is not True
        or proof.get("raw_reports_verified") is not True
        or proof.get("independent_quality_verified") is not False
        or proof.get("publication_authorized") is not False
        or proof.get("grants_authority") is not False
    ):
        raise PolicyViolation("Publication test producer actual source proof drift")
    claimed = dt.datetime.fromisoformat(snapshot["effects"][0]["claimed_at"])
    completed = dt.datetime.fromisoformat(snapshot["effects"][0]["receipt_created_at"])
    if not claimed <= evidence.recorded_at <= completed:
        raise PolicyViolation("Publication test producer signing time outside actual claim")
    return body


def read_test_producer_result(home: Path, reference: str) -> dict:
    """Read an actual completed producer receipt; no signer or source mutation."""
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Publication test producer completed receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Publication test producer canonical binding missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    stored = _object(objects, snapshot["terminal_evidence_digest"])
    if not isinstance(stored.get("envelope"), dict):
        raise PolicyViolation("Publication test producer envelope missing")
    trust = _trust(
        home,
        binding=binding,
        realm_id=UUID(stored["realm_id"]),
        producer_id=UUID(stored["envelope"]["producer_id"]),
    )
    result = _completed_result(
        runtime, objects, snapshot, binding, OPERATION, snapshot["payload"]["input_digest"]
    )
    body = _verify_result(home, result=result, snapshot=snapshot, binding=binding, trust=trust)
    return {
        "schema": "zekam-publication-test-producer-export/v1",
        "envelope": body["envelope"],
        "subject_digest": body["subject_digest"],
        "producer_job_id": snapshot["job_id"],
        "producer_receipt_digest": snapshot["terminal_evidence_digest"],
        "source_verification_report_digest": body["source_verification"]["report_digest"],
        "historical_source_proof": True,
        "current_source_rechecked": False,
        "independent_quality_verified": False,
        "publication_authorized": False,
        "grants_authority": False,
    }


@dataclass(frozen=True, slots=True)
class PublicationTestEvidenceProducer:
    """Inject only into the trusted host worker; never a model-facing sign-JSON tool.

    No private-key file discovery, creation, CLI argument or public registry
    mutation is provided here. Host key confinement is an external acceptance gate.
    """

    home: Path
    producer_id: UUID
    signer: Ed25519ReceiptSigner

    def produce(self, *, plan: GitPublicationPlan, source_reference: str) -> dict:
        if (
            type(plan) is not GitPublicationPlan
            or type(self.producer_id) is not UUID
            or type(self.signer) is not Ed25519ReceiptSigner
        ):
            raise PolicyViolation("Publication test producer typed host capability required")
        plan.__post_init__()
        trust = _trust(
            self.home, binding=plan.binding, realm_id=plan.realm_id, producer_id=self.producer_id
        )
        runtime = SQLiteLocalRuntimeStore(self.home / "state/operational.db", existing_only=True)
        source = runtime.job_snapshot(source_reference)
        if (
            source is None
            or source["state"] != "completed"
            or source["payload"].get("operation") != "test.measure"
        ):
            raise PolicyViolation("Publication test producer actual completed measurement required")
        input_digest = _input(
            plan.subject_digest, source["job_id"], self.producer_id, trust.trust_digest
        )
        key = canonical_job_key(plan.binding.binding_digest, OPERATION, input_digest)
        previous = runtime.job_snapshot(key)
        if previous is not None and previous["state"] == "completed":
            return read_test_producer_result(self.home, previous["job_id"])

        def validate():
            actual = _trust(
                self.home,
                binding=plan.binding,
                realm_id=plan.realm_id,
                producer_id=self.producer_id,
            )
            if actual.trust_digest != trust.trust_digest:
                raise PolicyViolation("Publication test producer trust changed during signing")
            read_publication_test_source(self.home, source["job_id"], plan=plan)

        def execute(cancel):
            proof = read_publication_test_source(self.home, source["job_id"], plan=plan)
            if cancel.is_set():
                raise PolicyViolation("Publication test producer cancelled before signing")
            evidence = GitPublicationEvidence(
                "test",
                plan.subject_digest,
                UUID(proof["execution_id"]),
                self.producer_id,
                True,
                dt.datetime.now(dt.UTC),
            )
            envelope = evidence_attestation_body(
                evidence,
                source_job_id=proof["source_job_id"],
                source_evidence_digest=proof["source_evidence_digest"],
                producer_id=self.producer_id,
            )
            attestation = self.signer.seal_digest(envelope)
            if not trust.verifiers["test"][self.producer_id].matches_receipt_digest(
                envelope, attestation
            ):
                raise PolicyViolation("Publication test producer signer does not own admitted key")
            return {
                "schema": "zekam-publication-test-producer-result/v1",
                "realm_id": str(plan.realm_id),
                "subject_digest": plan.subject_digest,
                "envelope": envelope | {"attestation": attestation},
                "source_verification": proof,
                "trust_digest": trust.trust_digest,
                "grants_authority": False,
            }

        result = run_canonical_job(
            self.home,
            binding=plan.binding,
            operation=OPERATION,
            input_digest=input_digest,
            execute=execute,
            validate_inputs=validate,
        )
        return read_test_producer_result(self.home, result["canonical_job_id"])
