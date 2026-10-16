"""Trusted host verification signer over completed current-subject model review."""

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
from zekam.application.git_publication_review import read_publication_review
from zekam.application.git_publication_test_source import read_publication_test_source
from zekam.application.git_publication_trust import load_publication_trust
from zekam.application.git_publication_wire import publication_evidence_from_body
from zekam.application.local_attestation import Ed25519ReceiptSigner
from zekam.application.unit_test_source_evidence import _object
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.git_publication import evidence_attestation_body
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "publication.verification.attest"


def _input(subject, review_job, producer, trust):
    return digest(
        {
            "schema": "zekam-publication-verification-producer-input/v1",
            "subject_digest": subject,
            "review_job_id": review_job,
            "producer_id": str(producer),
            "trust_digest": trust,
        }
    )


def _trust(home, *, binding, realm_id, producer_id):
    trust = load_publication_trust(home)
    if (
        trust.realm_id != realm_id
        or trust.config_revision_id != binding.config_revision_id
        or trust.config_digest != binding.config_digest
        or load_settings(home=home).git_publication.trust_digest != trust.trust_digest
        or producer_id not in trust.verifiers.get("verification", {})
        or producer_id in trust.verifiers.get("test", {})
    ):
        raise PolicyViolation("Publication verifier producer active role/config/realm drift")
    return trust


def _review_matches(review, *, binding, realm_id, subject):
    if (
        review["subject_digest"] != subject
        or review["publication_binding_digest"] != binding.binding_digest
        or review["realm_id"] != str(realm_id)
        or any(
            review[field] != getattr(binding, field)
            for field in (
                "project_id",
                "work_item_id",
                "source_snapshot_id",
                "config_revision_id",
                "config_digest",
            )
        )
        or review["verdict"] != "accepted"
        or review["canonical_receipt_verified"] is not True
        or review["unit_test_change_range_verified"] is not True
        or review["execution_independence"] != "distinct-native-executions"
        or review["model_independence"] != "configured-upstream-distinct"
        or review["review_scope"] != "selected-production-and-accepted-unit-test-changes"
        or review["execution_id"] == binding.run_id
    ):
        raise PolicyViolation(
            "Publication verifier producer exact accepted independent review required"
        )


def _proof_matches(proof, review, binding):
    if not isinstance(proof, dict) or proof.get("report_digest") != digest(
        {k: v for k, v in proof.items() if k != "report_digest"}
    ):
        raise PolicyViolation("Publication verifier producer current proof digest drift")
    target = GitPublicationTarget(**review["target"])
    if (
        proof.get("schema") != "zekam-publication-test-source/v1"
        or proof.get("subject_digest") != review["subject_digest"]
        or proof.get("publication_binding_digest") != binding.binding_digest
        or canonical_bytes(proof.get("target")) != canonical_bytes(target.body())
        or digest({"binding": binding.body(), "target": target.body()}) != review["subject_digest"]
        or proof.get("source_job_id") != review["test_job_id"]
        or proof.get("source_evidence_digest") != review["test_source_evidence_digest"]
        or proof.get("execution_id") != review["test_execution_id"]
        or proof.get("request_digest") != review["test_request_digest"]
        or proof.get("measurement_record_digest") != review["test_measurement_digest"]
        or proof.get("source_report_digest") != review["test_source_report_digest"]
        or proof.get("execution_id") == review["execution_id"]
        or proof.get("current_source_verified") is not True
        or proof.get("raw_reports_verified") is not True
        or proof.get("grants_authority") is not False
    ):
        raise PolicyViolation("Publication verifier producer current test/subject proof drift")


def read_verifier_producer_result(home: Path, reference: str):
    """Read actual completed signer receipt and upstream review without signing or Git."""
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Publication verifier producer completed receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Publication verifier producer canonical binding missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    stored = _object(objects, snapshot["terminal_evidence_digest"])
    trust = _trust(
        home,
        binding=binding,
        realm_id=UUID(stored["realm_id"]),
        producer_id=UUID(stored["envelope"]["producer_id"]),
    )
    result = _completed_result(
        runtime, objects, snapshot, binding, OPERATION, snapshot["payload"]["input_digest"]
    )
    body = {k: v for k, v in result.items() if k not in {"canonical_job_id", "canonical_replayed"}}
    if set(body) != {
        "schema",
        "realm_id",
        "subject_digest",
        "envelope",
        "review_source",
        "current_source",
        "trust_digest",
        "publication_executor_id",
        "grants_authority",
    }:
        raise PolicyViolation("Publication verifier producer exact result required")
    envelope = body["envelope"]
    producer = UUID(envelope["producer_id"])
    evidence = publication_evidence_from_body(envelope["evidence"])
    review = read_publication_review(home, envelope["source_job_id"])
    unsigned = evidence_attestation_body(
        evidence,
        source_job_id=review["job_id"],
        source_evidence_digest=review["source_evidence_digest"],
        producer_id=producer,
    )
    if (
        body["schema"] != "zekam-publication-verification-producer-result/v1"
        or body["grants_authority"] is not False
        or body["trust_digest"] != trust.trust_digest
        or body["realm_id"] != str(trust.realm_id)
        or UUID(body["publication_executor_id"]) == producer
        or evidence.kind != "verification"
        or evidence.passed is not True
        or evidence.executor_id != producer
        or evidence.subject_digest != body["subject_digest"]
        or str(evidence.execution_id) != review["execution_id"]
        or canonical_bytes(body["review_source"]) != canonical_bytes(review)
        or set(envelope) != set(unsigned) | {"attestation"}
        or canonical_bytes({k: v for k, v in envelope.items() if k != "attestation"})
        != canonical_bytes(unsigned)
        or not trust.verifiers["verification"][producer].matches_receipt_digest(
            unsigned, envelope["attestation"]
        )
        or snapshot["payload"]["input_digest"]
        != _input(evidence.subject_digest, review["job_id"], producer, trust.trust_digest)
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, snapshot["payload"]["input_digest"])
    ):
        raise PolicyViolation("Publication verifier producer signature/input/review drift")
    _review_matches(
        review, binding=binding, realm_id=trust.realm_id, subject=evidence.subject_digest
    )
    _proof_matches(body["current_source"], review, binding)
    claimed = dt.datetime.fromisoformat(snapshot["effects"][0]["claimed_at"])
    completed = dt.datetime.fromisoformat(snapshot["effects"][0]["receipt_created_at"])
    if not claimed <= evidence.recorded_at <= completed:
        raise PolicyViolation("Publication verifier producer signing time outside actual claim")
    return {
        "schema": "zekam-publication-verification-producer-export/v1",
        "envelope": envelope,
        "subject_digest": evidence.subject_digest,
        "producer_job_id": snapshot["job_id"],
        "producer_receipt_digest": snapshot["terminal_evidence_digest"],
        "review_report_digest": review["report_digest"],
        "historical_source_proof": True,
        "current_source_rechecked": False,
        "review_scope": review["review_scope"],
        "native_model_identity_observed": False,
        "independent_quality_verified": False,
        "publication_authorized": False,
        "grants_authority": False,
    }


@dataclass(frozen=True, slots=True)
class PublicationVerifierEvidenceProducer:
    """Inject the verification-role key only into an isolated trusted host worker."""

    home: Path
    producer_id: UUID
    signer: Ed25519ReceiptSigner

    def produce(self, *, plan: GitPublicationPlan, review_reference: str):
        if (
            type(plan) is not GitPublicationPlan
            or type(self.producer_id) is not UUID
            or type(self.signer) is not Ed25519ReceiptSigner
            or self.producer_id == plan.executor_id
        ):
            raise PolicyViolation(
                "Publication verifier producer distinct typed host capability required"
            )
        plan.__post_init__()
        trust = _trust(
            self.home, binding=plan.binding, realm_id=plan.realm_id, producer_id=self.producer_id
        )
        runtime = SQLiteLocalRuntimeStore(self.home / "state/operational.db", existing_only=True)
        source = runtime.job_snapshot(review_reference)
        if (
            source is None
            or source["state"] != "completed"
            or source["payload"].get("operation") != "publication.review"
        ):
            raise PolicyViolation("Publication verifier producer actual completed review required")
        input_digest = _input(
            plan.subject_digest, source["job_id"], self.producer_id, trust.trust_digest
        )
        previous = runtime.job_snapshot(
            canonical_job_key(plan.binding.binding_digest, OPERATION, input_digest)
        )
        if previous is not None and previous["state"] == "completed":
            return read_verifier_producer_result(self.home, previous["job_id"])

        def validate():
            actual = _trust(
                self.home,
                binding=plan.binding,
                realm_id=plan.realm_id,
                producer_id=self.producer_id,
            )
            if actual.trust_digest != trust.trust_digest:
                raise PolicyViolation("Publication verifier producer trust changed during signing")
            review = read_publication_review(self.home, source["job_id"])
            _review_matches(
                review, binding=plan.binding, realm_id=plan.realm_id, subject=plan.subject_digest
            )
            proof = read_publication_test_source(self.home, review["test_job_id"], plan=plan)
            _proof_matches(proof, review, plan.binding)
            return review, proof

        def execute(cancel):
            review, proof = validate()
            if cancel.is_set():
                raise PolicyViolation("Publication verifier producer cancelled before signing")
            evidence = GitPublicationEvidence(
                "verification",
                plan.subject_digest,
                UUID(review["execution_id"]),
                self.producer_id,
                True,
                dt.datetime.now(dt.UTC),
            )
            unsigned = evidence_attestation_body(
                evidence,
                source_job_id=review["job_id"],
                source_evidence_digest=review["source_evidence_digest"],
                producer_id=self.producer_id,
            )
            attestation = self.signer.seal_digest(unsigned)
            if not trust.verifiers["verification"][self.producer_id].matches_receipt_digest(
                unsigned, attestation
            ):
                raise PolicyViolation(
                    "Publication verifier producer signer does not own admitted key"
                )
            return {
                "schema": "zekam-publication-verification-producer-result/v1",
                "realm_id": str(plan.realm_id),
                "subject_digest": plan.subject_digest,
                "envelope": unsigned | {"attestation": attestation},
                "review_source": review,
                "current_source": proof,
                "trust_digest": trust.trust_digest,
                "publication_executor_id": str(plan.executor_id),
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
        return read_verifier_producer_result(self.home, result["canonical_job_id"])
