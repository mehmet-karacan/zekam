"""Real SQLite/CAS atomic admission; upstream host producer readers are fixtures."""

import datetime as dt
import secrets
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tests.unit.test_git_publication import world as world
from zekam.application import git_publication_bundle as subject
from zekam.application.canonical_job_runtime import canonical_job_key, run_canonical_job
from zekam.application.git_publication import GitPublicationEvidence
from zekam.application.git_publication_trust import PublicationTrust
from zekam.application.local_attestation import LocalAttestationSigner, LocalAttestationVerifier
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.sqlite.git_publication import (
    SQLiteGitPublicationLedger,
    evidence_attestation_body,
)
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def pair(world):
    envelopes, verifiers, proofs = [], {}, []
    for kind in ("test", "verification"):
        producer, key = uuid4(), secrets.token_bytes(32)
        signer = LocalAttestationSigner(key)
        verifiers[kind] = {producer: LocalAttestationVerifier(key)}
        with world.store.unit_of_work() as uow:
            run = uow.create_run(
                work_item_id=world.plan.binding.work_item_id,
                config_revision_id=world.plan.binding.config_revision_id,
                plan_digest=digest(kind),
                budget={"seconds": 60},
                source_snapshot_id=world.plan.binding.source_snapshot_id,
            )
            uow.commit()
        binding = replace(world.plan.binding, run_id=run.id, plan_digest=run.plan_digest)
        result = run_canonical_job(
            world.home,
            binding=binding,
            operation="test.measure" if kind == "test" else "publication.review",
            input_digest=digest(kind),
            execute=lambda _cancel: {"fixture": kind},
            validate_inputs=lambda: None,
        )
        source = world.runtime.job_snapshot(result["canonical_job_id"])
        evidence = GitPublicationEvidence(
            kind, world.plan.subject_digest, UUID(run.id), producer, True, dt.datetime.now(dt.UTC)
        )
        envelope = evidence_attestation_body(
            evidence,
            source_job_id=source["job_id"],
            source_evidence_digest=source["terminal_evidence_digest"],
            producer_id=producer,
        )
        envelopes.append(envelope | {"attestation": signer.seal_digest(envelope)})
        proofs.append(evidence)
    plan = replace(
        world.plan,
        test_evidence_digest=proofs[0].evidence_digest,
        verifier_evidence_digest=proofs[1].evidence_digest,
    )
    ledger = SQLiteGitPublicationLedger(
        world.home / "state/operational.db",
        objects=LocalContentAddressedStore(world.home / "artifacts/sha256"),
        trusted_verifiers=verifiers,
    )
    return SimpleNamespace(
        world=world,
        plan=plan,
        ledger=ledger,
        envelopes=tuple(envelopes),
        verifiers=verifiers,
        proofs=proofs,
    )


def row_count(w):
    connection = _connect(w.world.home / "state/operational.db", read_only=True)
    try:
        return connection.execute("select count(*) from git_publication_evidence").fetchone()[0]
    finally:
        connection.close()


def test_ledger_admits_both_or_exactly_replays_pair(pair):
    w = pair
    assert row_count(w) == 0
    w.ledger.record_evidence_bundle(w.plan, w.envelopes)
    assert row_count(w) == 2
    w.ledger.record_evidence_bundle(w.plan, w.envelopes)
    assert row_count(w) == 2
    assert w.ledger.publication_evidence(w.plan.verifier_evidence_digest) == w.proofs[1]


def test_ledger_rolls_back_first_insert_when_second_source_cas_missing(pair):
    w = pair
    original = w.ledger._evidence

    def unavailable(connection, key, verify):
        if key == w.plan.verifier_evidence_digest:
            raise PolicyViolation("fixture second CAS unavailable")
        return original(connection, key, verify)

    w.ledger._evidence = unavailable
    with pytest.raises(PolicyViolation, match="second CAS"):
        w.ledger.record_evidence_bundle(w.plan, w.envelopes)
    assert row_count(w) == 0


@pytest.mark.parametrize("change", ["reversed", "subject", "signature", "extra"])
def test_ledger_rejects_malformed_pair_without_partial_write(pair, change):
    w = pair
    envelopes = deepcopy(w.envelopes)
    plan = w.plan
    if change == "reversed":
        envelopes = tuple(reversed(envelopes))
    elif change == "subject":
        plan = replace(plan, target=replace(plan.target, head="b" * 40))
    elif change == "signature":
        envelopes[1]["attestation"] = digest("invalid")
    else:
        envelopes[1]["extra"] = True
    with pytest.raises(ZekamError):
        w.ledger.record_evidence_bundle(plan, envelopes)
    assert row_count(w) == 0


@pytest.fixture
def bundle_world(pair, monkeypatch):
    w = pair
    test_envelope, verification_envelope = w.envelopes
    review = {
        "job_id": verification_envelope["source_job_id"],
        "source_evidence_digest": verification_envelope["source_evidence_digest"],
        "execution_id": str(w.proofs[1].execution_id),
        "test_execution_id": str(w.proofs[0].execution_id),
        "subject_digest": w.plan.subject_digest,
        "publication_binding_digest": w.plan.binding.binding_digest,
        "realm_id": str(w.plan.realm_id),
        "target": w.plan.target.body(),
        "test_job_id": test_envelope["source_job_id"],
        "test_source_evidence_digest": test_envelope["source_evidence_digest"],
        "test_request_digest": digest("request"),
        "test_measurement_digest": digest("measurement"),
        "test_source_report_digest": digest("source-report"),
    }
    review["report_digest"] = digest(review)
    test_export = {
        "envelope": test_envelope,
        "subject_digest": w.plan.subject_digest,
        "producer_job_id": str(uuid4()),
        "producer_receipt_digest": digest("test-producer"),
    }
    verification_export = {
        "envelope": verification_envelope,
        "subject_digest": w.plan.subject_digest,
        "producer_job_id": str(uuid4()),
        "producer_receipt_digest": digest("verification-producer"),
        "review_report_digest": review["report_digest"],
    }
    proof = {
        "schema": "zekam-publication-test-source/v1",
        "subject_digest": w.plan.subject_digest,
        "publication_binding_digest": w.plan.binding.binding_digest,
        "target": w.plan.target.body(),
        "source_job_id": review["test_job_id"],
        "source_evidence_digest": review["test_source_evidence_digest"],
        "execution_id": review["test_execution_id"],
        "request_digest": review["test_request_digest"],
        "measurement_record_digest": review["test_measurement_digest"],
        "source_report_digest": review["test_source_report_digest"],
        "current_source_verified": True,
        "raw_reports_verified": True,
        "grants_authority": False,
    }
    proof["report_digest"] = digest(proof)
    reads, drift = [], [False]
    trust = PublicationTrust(
        w.plan.realm_id,
        w.plan.binding.config_revision_id,
        w.plan.binding.config_digest,
        digest("registry"),
        w.verifiers,
    )
    monkeypatch.setattr(subject, "read_test_producer_result", lambda *_args: test_export)
    monkeypatch.setattr(
        subject, "read_verifier_producer_result", lambda *_args: verification_export
    )
    monkeypatch.setattr(subject, "read_publication_review", lambda *_args: review)
    monkeypatch.setattr(subject, "_ledger", lambda *_args: (trust, w.ledger))

    def current(*_args, **_kwargs):
        reads.append(True)
        if drift[0]:
            raise PolicyViolation("fixture source drift")
        return proof

    monkeypatch.setattr(subject, "read_publication_test_source", current)
    return SimpleNamespace(
        pair=w,
        review=review,
        test=test_export,
        verification=verification_export,
        reads=reads,
        drift=drift,
    )


def prepare(w):
    return subject.prepare_publication_evidence(
        w.pair.world.home,
        publication=w.pair.plan,
        test_reference=w.test["producer_job_id"],
        verification_reference=w.verification["producer_job_id"],
    )


def admit(w):
    return subject.admit_publication_evidence(
        w.pair.world.home,
        publication=w.pair.plan,
        test_reference=w.test["producer_job_id"],
        verification_reference=w.verification["producer_job_id"],
    )


def test_prepare_is_read_only_and_admission_has_actual_claim_receipt(bundle_world, monkeypatch):
    w = bundle_world
    bundle = prepare(w)
    assert bundle["evidence_bound"] is True and bundle["publication_authorized"] is False
    assert row_count(w.pair) == 0 and not w.reads
    key = canonical_job_key(
        w.pair.plan.binding.binding_digest, subject.OPERATION, subject._input(bundle)
    )
    original = w.pair.ledger.record_evidence_bundle

    def record(plan, envelopes):
        snapshot = w.pair.world.runtime.job_snapshot(key)
        assert snapshot is not None and snapshot["effects"][0]["receipt_id"] is None
        original(plan, envelopes)

    monkeypatch.setattr(w.pair.ledger, "record_evidence_bundle", record)
    result = admit(w)
    assert result["canonical_receipt_verified"] is True and result["atomic_pair_admitted"] is True
    assert row_count(w.pair) == 2
    before = len(w.reads)
    w.drift[0] = True
    assert admit(w) == result and len(w.reads) == before


def test_admission_post_write_drift_requires_recovery_and_never_pushes(bundle_world, monkeypatch):
    w = bundle_world
    bundle = prepare(w)
    key = canonical_job_key(
        w.pair.plan.binding.binding_digest, subject.OPERATION, subject._input(bundle)
    )
    original = w.pair.ledger.record_evidence_bundle

    def drift_after_write(plan, envelopes):
        original(plan, envelopes)
        w.drift[0] = True

    monkeypatch.setattr(w.pair.ledger, "record_evidence_bundle", drift_after_write)
    with pytest.raises(PolicyViolation, match="source drift"):
        admit(w)
    assert row_count(w.pair) == 2
    with pytest.raises(PolicyViolation, match="completed canonical receipt"):
        subject.read_publication_admission(w.pair.world.home, key)
    with pytest.raises(PolicyViolation):
        admit(w)


def test_bundle_refuses_unrelated_native_test_source(bundle_world):
    w = bundle_world
    w.review["test_job_id"] = str(uuid4())
    with pytest.raises(PolicyViolation, match="subject/source/independence"):
        prepare(w)
    assert row_count(w.pair) == 0
