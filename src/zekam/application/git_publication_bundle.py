"""Compose actual host-produced test/review receipts into one canonical admission."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from zekam.application.canonical_job_runtime import (
    _completed_result,
    canonical_job_key,
    run_canonical_job,
)
from zekam.application.config import load_settings
from zekam.application.git_publication import GitPublicationPlan
from zekam.application.git_publication_review import read_publication_review
from zekam.application.git_publication_test_producer import read_test_producer_result
from zekam.application.git_publication_test_source import read_publication_test_source
from zekam.application.git_publication_trust import load_publication_trust
from zekam.application.git_publication_verifier_producer import (
    read_verifier_producer_result,
    _proof_matches,
)
from zekam.application.git_publication_wire import (
    publication_evidence_from_body,
    publication_plan_from_body,
)
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.git_publication import SQLiteGitPublicationLedger
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "publication.evidence.bind"


def _sources(home, *, publication, test_reference, verification_reference):
    if type(publication) is not GitPublicationPlan:
        raise PolicyViolation("Publication evidence bundle typed subject required")
    publication.__post_init__()
    test = read_test_producer_result(home, test_reference)
    verification = read_verifier_producer_result(home, verification_reference)
    envelopes = (test["envelope"], verification["envelope"])
    measured, reviewed = (publication_evidence_from_body(item["evidence"]) for item in envelopes)
    review = read_publication_review(home, envelopes[1]["source_job_id"])
    if (
        measured.kind != "test"
        or reviewed.kind != "verification"
        or not measured.passed
        or not reviewed.passed
        or any(item.subject_digest != publication.subject_digest for item in (measured, reviewed))
        or any(
            item["subject_digest"] != publication.subject_digest
            for item in (test, verification, review)
        )
        or measured.execution_id == reviewed.execution_id
        or reviewed.executor_id in {measured.executor_id, publication.executor_id}
        or review["publication_binding_digest"] != publication.binding.binding_digest
        or review["realm_id"] != str(publication.realm_id)
        or review["job_id"] != envelopes[1]["source_job_id"]
        or review["source_evidence_digest"] != envelopes[1]["source_evidence_digest"]
        or verification["review_report_digest"] != review["report_digest"]
        or envelopes[0]["source_job_id"] != review["test_job_id"]
        or envelopes[0]["source_evidence_digest"] != review["test_source_evidence_digest"]
        or str(measured.execution_id) != review["test_execution_id"]
        or str(reviewed.execution_id) != review["execution_id"]
    ):
        raise PolicyViolation("Publication evidence bundle subject/source/independence drift")
    plan = replace(
        publication,
        test_evidence_digest=measured.evidence_digest,
        verifier_evidence_digest=reviewed.evidence_digest,
    )
    body = {
        "schema": "zekam-publication-evidence-bundle/v1",
        "plan": plan.body(),
        "plan_digest": plan.plan_digest,
        "subject_digest": plan.subject_digest,
        "test_producer_job_id": test["producer_job_id"],
        "test_producer_receipt_digest": test["producer_receipt_digest"],
        "verification_producer_job_id": verification["producer_job_id"],
        "verification_producer_receipt_digest": verification["producer_receipt_digest"],
        "review_report_digest": review["report_digest"],
        "evidence_bound": True,
        "current_source_rechecked": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "network_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    return plan, body | {"bundle_digest": digest(body)}, envelopes, review


def prepare_publication_evidence(
    home: Path, *, publication: GitPublicationPlan, test_reference: str, verification_reference: str
):
    """Read real receipt producers and build a full plan; no writes, Git or signing."""
    return _sources(
        home,
        publication=publication,
        test_reference=test_reference,
        verification_reference=verification_reference,
    )[1]


def _ledger(home, plan):
    settings = load_settings(home=home)
    trust = load_publication_trust(home)
    if (
        settings.database.sqlite_path(home).resolve() != (home / "state/operational.db").resolve()
        or settings.git_publication.trust_digest != trust.trust_digest
        or trust.realm_id != plan.realm_id
        or trust.config_revision_id != plan.binding.config_revision_id
        or trust.config_digest != plan.binding.config_digest
    ):
        raise PolicyViolation("Publication evidence bundle active trust/config drift")
    return trust, SQLiteGitPublicationLedger(
        home / "state/operational.db",
        objects=LocalContentAddressedStore(home / settings.object_store_relative),
        trusted_verifiers=trust.verifiers,
    )


def _input(bundle):
    return digest(
        {
            "schema": "zekam-publication-evidence-admission-input/v1",
            "bundle_digest": bundle["bundle_digest"],
        }
    )


def admit_publication_evidence(
    home: Path, *, publication: GitPublicationPlan, test_reference: str, verification_reference: str
):
    """Claim then atomically admit both signed evidence records, without push approval."""
    plan, bundle, envelopes, review = _sources(
        home,
        publication=publication,
        test_reference=test_reference,
        verification_reference=verification_reference,
    )
    trust, ledger = _ledger(home, plan)
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    key = canonical_job_key(plan.binding.binding_digest, OPERATION, _input(bundle))
    previous = runtime.job_snapshot(key)
    if previous is not None and previous["state"] == "completed":
        return read_publication_admission(home, previous["job_id"])

    def validate():
        actual = _sources(
            home,
            publication=plan,
            test_reference=bundle["test_producer_job_id"],
            verification_reference=bundle["verification_producer_job_id"],
        )[1]
        current_trust, _current_ledger = _ledger(home, plan)
        if actual != bundle or current_trust.trust_digest != trust.trust_digest:
            raise PolicyViolation("Publication evidence bundle changed during admission")
        proof = read_publication_test_source(home, review["test_job_id"], plan=plan)
        _proof_matches(proof, review, plan.binding)
        return proof

    def execute(cancel):
        proof = validate()
        if cancel.is_set():
            raise PolicyViolation("Publication evidence bundle cancelled before admission")
        ledger.record_evidence_bundle(plan, envelopes)
        return {
            "schema": "zekam-publication-evidence-admission-result/v1",
            "bundle": bundle,
            "current_source": proof,
            "trust_digest": trust.trust_digest,
            "grants_authority": False,
        }

    result = run_canonical_job(
        home,
        binding=plan.binding,
        operation=OPERATION,
        input_digest=_input(bundle),
        execute=execute,
        validate_inputs=validate,
    )
    return read_publication_admission(home, result["canonical_job_id"])


def read_publication_admission(home: Path, reference: str):
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Publication evidence admission completed canonical receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Publication evidence admission binding missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    from zekam.application.unit_test_source_evidence import _object

    stored = _object(objects, snapshot["terminal_evidence_digest"])
    publication = publication_plan_from_body(stored["bundle"]["plan"])
    plan, bundle, _envelopes, review = _sources(
        home,
        publication=publication,
        test_reference=stored["bundle"]["test_producer_job_id"],
        verification_reference=stored["bundle"]["verification_producer_job_id"],
    )
    trust, ledger = _ledger(home, plan)
    result = _completed_result(runtime, objects, snapshot, binding, OPERATION, _input(bundle))
    body = {k: v for k, v in result.items() if k not in {"canonical_job_id", "canonical_replayed"}}
    if (
        set(body) != {"schema", "bundle", "current_source", "trust_digest", "grants_authority"}
        or body["schema"] != "zekam-publication-evidence-admission-result/v1"
        or body["grants_authority"] is not False
        or binding.binding_digest != plan.binding.binding_digest
        or canonical_bytes(body["bundle"]) != canonical_bytes(bundle)
        or body["trust_digest"] != trust.trust_digest
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, _input(bundle))
    ):
        raise PolicyViolation("Publication evidence admission immutable bundle drift")
    _proof_matches(body["current_source"], review, binding)
    for value, kind in (
        (plan.test_evidence_digest, "test"),
        (plan.verifier_evidence_digest, "verification"),
    ):
        evidence = ledger.publication_evidence(value)
        if (
            evidence.kind != kind
            or evidence.subject_digest != plan.subject_digest
            or evidence.passed is not True
        ):
            raise PolicyViolation("Publication evidence admission ledger readback drift")
    return {
        "schema": "zekam-publication-evidence-admission-source/v1",
        "job_id": snapshot["job_id"],
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "bundle": bundle,
        "atomic_pair_admitted": True,
        "canonical_receipt_verified": True,
        "historical_source_proof": True,
        "current_source_rechecked": False,
        "publication_authorized": False,
        "grants_authority": False,
    }
