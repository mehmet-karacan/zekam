"""Real canonical SQLite/CAS/signatures; review/current source readers are fixtures."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.unit.test_canonical_job_runtime import world as world
from zekam.application import git_publication_verifier_producer as subject
from zekam.application.canonical_job_runtime import run_canonical_job, canonical_job_key
from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget
from zekam.application.git_publication_trust import PublicationTrust
from zekam.application.local_attestation import Ed25519ReceiptSigner, Ed25519ReceiptVerifier
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


@pytest.fixture
def verifier_world(world, monkeypatch):
    with world.store.unit_of_work() as uow:
        run = uow.create_run(
            work_item_id=world.binding.work_item_id,
            config_revision_id=world.binding.config_revision_id,
            plan_digest=digest("fixture-review-plan"),
            budget={"seconds": 120},
            source_snapshot_id=world.binding.source_snapshot_id,
        )
        uow.commit()
    review_binding = replace(world.binding, run_id=run.id, plan_digest=run.plan_digest)
    result = run_canonical_job(
        world.home,
        binding=review_binding,
        operation="publication.review",
        input_digest=digest("synthetic-review-input"),
        execute=lambda _cancel: {"fixture": True},
        validate_inputs=lambda: None,
    )
    source = world.runtime.job_snapshot(result["canonical_job_id"])
    producer_id, realm = uuid4(), uuid4()
    key = Ed25519PrivateKey.generate()
    trust = PublicationTrust(
        realm,
        world.binding.config_revision_id,
        world.binding.config_digest,
        digest("fixture-registry"),
        {"verification": {producer_id: Ed25519ReceiptVerifier(key.public_key())}},
    )
    plan = GitPublicationPlan(
        realm,
        uuid4(),
        world.binding,
        GitPublicationTarget(
            digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
        ),
        digest("test-evidence"),
        digest("pending-verifier"),
    )
    review = {
        "job_id": source["job_id"],
        "source_evidence_digest": source["terminal_evidence_digest"],
        "execution_id": run.id,
        "subject_digest": plan.subject_digest,
        "publication_binding_digest": world.binding.binding_digest,
        "realm_id": str(realm),
        "project_id": world.binding.project_id,
        "work_item_id": world.binding.work_item_id,
        "source_snapshot_id": world.binding.source_snapshot_id,
        "config_revision_id": world.binding.config_revision_id,
        "config_digest": world.binding.config_digest,
        "verdict": "accepted",
        "canonical_receipt_verified": True,
        "unit_test_change_range_verified": True,
        "execution_independence": "distinct-native-executions",
        "model_independence": "configured-upstream-distinct",
        "review_scope": "selected-production-and-accepted-unit-test-changes",
        "target": plan.target.body(),
        "test_job_id": str(uuid4()),
        "test_source_evidence_digest": digest("test-source"),
        "test_execution_id": str(uuid4()),
        "test_request_digest": digest("test-request"),
        "test_measurement_digest": digest("measurement"),
        "test_source_report_digest": digest("test-source-report"),
    }
    review["report_digest"] = digest(review)
    current = {
        "schema": "zekam-publication-test-source/v1",
        "subject_digest": plan.subject_digest,
        "publication_binding_digest": world.binding.binding_digest,
        "target": plan.target.body(),
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
    current["report_digest"] = digest(current)
    reads, seals, drift = [], [], [False]
    monkeypatch.setattr(subject, "load_publication_trust", lambda _home: trust)
    monkeypatch.setattr(
        subject,
        "load_settings",
        lambda **_kw: SimpleNamespace(
            git_publication=SimpleNamespace(trust_digest=trust.trust_digest),
            object_store_relative="artifacts/sha256",
        ),
    )
    monkeypatch.setattr(subject, "read_publication_review", lambda *_args: review)

    def read_current(*_args, **_kwargs):
        reads.append(True)
        if drift[0]:
            raise PolicyViolation("fixture current source drift")
        return current.copy()

    monkeypatch.setattr(subject, "read_publication_test_source", read_current)
    job_key = canonical_job_key(
        world.binding.binding_digest,
        subject.OPERATION,
        subject._input(plan.subject_digest, source["job_id"], producer_id, trust.trust_digest),
    )
    original = Ed25519ReceiptSigner.seal_digest

    def seal(signer, body):
        snapshot = world.runtime.job_snapshot(job_key)
        assert snapshot is not None and snapshot["effects"][0]["receipt_id"] is None
        assert snapshot["effects"][0]["operation"] == subject.OPERATION
        seals.append(body)
        return original(signer, body)

    monkeypatch.setattr(Ed25519ReceiptSigner, "seal_digest", seal)
    producer = subject.PublicationVerifierEvidenceProducer(
        world.home, producer_id, Ed25519ReceiptSigner(key)
    )
    return SimpleNamespace(
        world=world,
        plan=plan,
        review=review,
        current=current,
        trust=trust,
        producer=producer,
        job_key=job_key,
        reads=reads,
        seals=seals,
        drift=drift,
    )


def produce(w):
    return w.producer.produce(plan=w.plan, review_reference=w.review["job_id"])


def test_verifier_signs_after_claim_and_replays_without_signing_or_git(verifier_world):
    w = verifier_world
    export = produce(w)
    assert len(w.seals) == 1
    assert export["envelope"]["evidence"]["kind"] == "verification"
    assert export["envelope"]["evidence"]["execution_id"] == w.review["execution_id"]
    assert export["envelope"]["source_job_id"] == w.review["job_id"]
    assert export["envelope"]["source_evidence_digest"] == w.review["source_evidence_digest"]
    assert export["historical_source_proof"] is True
    assert export["current_source_rechecked"] is False and export["publication_authorized"] is False
    before = len(w.reads)
    w.drift[0] = True
    assert produce(w) == export
    assert len(w.seals) == 1 and len(w.reads) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("verdict", "rejected"),
        ("verdict", "unavailable"),
        ("subject_digest", digest("different")),
        ("publication_binding_digest", digest("different")),
        ("unit_test_change_range_verified", False),
        ("model_independence", "unknown"),
        ("execution_independence", "replayed"),
        ("config_digest", digest("different")),
    ],
)
def test_verifier_rejects_unqualified_review_before_signing(verifier_world, field, value):
    w = verifier_world
    w.review[field] = value
    with pytest.raises(PolicyViolation, match="exact accepted independent review"):
        produce(w)
    assert not w.seals


def test_verifier_wrong_key_leaves_recovery_without_completed_export(verifier_world):
    w = verifier_world
    wrong = replace(w.producer, signer=Ed25519ReceiptSigner(Ed25519PrivateKey.generate()))
    with pytest.raises(PolicyViolation, match="does not own admitted key"):
        wrong.produce(plan=w.plan, review_reference=w.review["job_id"])
    with pytest.raises(PolicyViolation, match="completed receipt"):
        subject.read_verifier_producer_result(w.world.home, w.job_key)
    with pytest.raises(PolicyViolation, match="recovery required"):
        produce(w)
    assert len(w.seals) == 1


def test_verifier_post_sign_source_drift_cannot_publish_completed_receipt(
    verifier_world, monkeypatch
):
    w = verifier_world
    original = Ed25519ReceiptSigner.seal_digest

    def seal(signer, body):
        signature = original(signer, body)
        w.drift[0] = True
        return signature

    monkeypatch.setattr(Ed25519ReceiptSigner, "seal_digest", seal)
    with pytest.raises(PolicyViolation, match="source drift"):
        produce(w)
    with pytest.raises(PolicyViolation, match="completed receipt"):
        subject.read_verifier_producer_result(w.world.home, w.job_key)
    with pytest.raises(PolicyViolation, match="recovery required|source drift"):
        produce(w)
    assert len(w.seals) == 1


def test_verifier_publisher_identity_and_test_role_cannot_sign(verifier_world):
    w = verifier_world
    with pytest.raises(PolicyViolation, match="distinct typed host"):
        w.producer.produce(
            plan=replace(w.plan, executor_id=w.producer.producer_id),
            review_reference=w.review["job_id"],
        )
    w.trust.verifiers["test"] = dict(w.trust.verifiers["verification"])
    with pytest.raises(PolicyViolation, match="active role"):
        produce(w)
    assert not w.seals


def test_cli_import_rechecks_actual_export_and_refuses_tampering(
    verifier_world, monkeypatch, tmp_path
):
    import json
    from typer.testing import CliRunner
    from zekam.interfaces.cli import git_publication as cli

    w = verifier_world
    export = produce(w)
    writes = []
    monkeypatch.setattr(cli, "_context", lambda _home: SimpleNamespace(home=w.world.home))
    monkeypatch.setattr(
        cli,
        "_components",
        lambda _home: (
            None,
            None,
            SimpleNamespace(record_evidence=lambda *args, **kwargs: writes.append((args, kwargs))),
        ),
    )
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import-evidence", str(path), "--uygula"])
    assert result.exit_code == 0 and len(writes) == 1
    assert writes[0][1]["source_job_id"] == w.review["job_id"]
    export["producer_receipt_digest"] = digest("tampered")
    path.write_text(json.dumps(export), encoding="utf-8")
    result = runner.invoke(cli.app, ["import-evidence", str(path), "--uygula"])
    assert result.exit_code != 0 and len(writes) == 1
    assert len(w.seals) == 1
