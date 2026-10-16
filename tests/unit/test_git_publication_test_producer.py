"""Real SQLite/CAS/Ed25519 producer protocol; current-source adapter is a fixture."""

from types import SimpleNamespace
from uuid import uuid4
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.unit.test_canonical_job_runtime import world as world
from tests.unit.test_canonical_job_runtime import _native_source_fixture
from zekam.application import git_publication_test_producer as subject
from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget
from zekam.application.git_publication_trust import PublicationTrust
from zekam.application.local_attestation import Ed25519ReceiptSigner, Ed25519ReceiptVerifier
from zekam.application.unit_test_source_evidence import read_native_measurement_source
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, NotFound
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def producer_world(world, monkeypatch):
    from zekam.application.unit_test_runtime import native_source_content_digest

    # Use a full commit-shaped revision for publication rather than the generic
    # runtime fixture's human-readable revision label.
    world.observed.source_revision = "a" * 40
    with world.store.unit_of_work() as uow:
        snapshot = uow.capture_source_snapshot(
            source_binding_id=world.source.id,
            revision_ref=world.observed.source_revision,
            tree_digest=digest("tree"),
            content_digest=native_source_content_digest(world.observed),
            config_digest=world.binding.config_digest,
        )
        work = uow.create_work(
            project_id=world.binding.project_id, kind="task", title="Producer", state="ready"
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=world.binding.config_revision_id,
            plan_digest=world.binding.plan_digest,
            budget={"seconds": 120},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    world.binding = replace(
        world.binding,
        work_item_id=work.id,
        work_revision=work.revision,
        run_id=run.id,
        source_snapshot_id=snapshot.id,
    )
    native = _native_source_fixture(world, with_report_facts=True)
    key = Ed25519PrivateKey.generate()
    producer_id, realm_id = uuid4(), uuid4()
    trust = PublicationTrust(
        realm_id,
        world.binding.config_revision_id,
        world.binding.config_digest,
        digest("fixture public registry"),
        {"test": {producer_id: Ed25519ReceiptVerifier(key.public_key())}},
    )
    # Registry/config/file ownership contracts have their own fixtures. No key
    # file is created and this is not real native host acceptance evidence.
    monkeypatch.setattr(subject, "load_publication_trust", lambda _home: trust)
    monkeypatch.setattr(
        subject,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(
            git_publication=SimpleNamespace(trust_digest=trust.trust_digest),
            object_store_relative="artifacts/sha256",
        ),
    )
    calls, seals = [], []
    drift = [False]

    def current_source(home, reference, *, plan):
        if drift[0]:
            raise PolicyViolation("fixture source changed")
        source = read_native_measurement_source(home, reference)
        calls.append(reference)
        proof = {
            "schema": "zekam-publication-test-source/v1",
            "subject_digest": plan.subject_digest,
            "publication_binding_digest": plan.binding.binding_digest,
            "target": plan.target.body(),
            "source_job_id": source["job_id"],
            "source_evidence_digest": source["source_evidence_digest"],
            "execution_id": source["execution_id"],
            "request_digest": source["request_digest"],
            "measurement_record_digest": source["measurement_record_digest"],
            "source_report_digest": source["report_digest"],
            "current_source_verified": True,
            "raw_reports_verified": True,
            "independent_quality_verified": False,
            "publication_authorized": False,
            "grants_authority": False,
        }
        return proof | {"report_digest": digest(proof)}

    monkeypatch.setattr(subject, "read_publication_test_source", current_source)
    plan = GitPublicationPlan(
        realm_id,
        uuid4(),
        world.binding,
        GitPublicationTarget(
            digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
        ),
        digest("pending-test"),
        digest("pending-review"),
    )
    input_digest = subject._input(
        plan.subject_digest, native["canonical_job_id"], producer_id, trust.trust_digest
    )
    job_key = canonical_job_key(world.binding.binding_digest, subject.OPERATION, input_digest)
    original = Ed25519ReceiptSigner.seal_digest

    def seal(signer, body):
        snapshot = world.runtime.job_snapshot(job_key)
        assert snapshot is not None
        assert snapshot["effects"][0]["receipt_id"] is None
        assert snapshot["effects"][0]["operation"] == subject.OPERATION
        seals.append(body)
        return original(signer, body)

    monkeypatch.setattr(Ed25519ReceiptSigner, "seal_digest", seal)
    producer = subject.PublicationTestEvidenceProducer(
        world.home, producer_id, Ed25519ReceiptSigner(key)
    )
    return SimpleNamespace(
        world=world,
        native=native,
        producer=producer,
        plan=plan,
        trust=trust,
        seals=seals,
        calls=calls,
        drift=drift,
        job_key=job_key,
    )


def test_producer_signs_only_after_claim_and_replays_without_signing_or_current_source(
    producer_world,
):
    w = producer_world
    result = w.producer.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    assert len(w.seals) == 1
    envelope = result["envelope"]
    assert envelope["source_job_id"] == w.native["canonical_job_id"]
    assert envelope["evidence"]["kind"] == "test"
    assert envelope["evidence"]["execution_id"] == w.world.binding.run_id
    assert envelope["evidence"]["executor_id"] == str(w.producer.producer_id)
    assert result["historical_source_proof"] is True
    assert result["current_source_rechecked"] is False
    assert result["independent_quality_verified"] is False
    assert result["publication_authorized"] is False
    before = len(w.calls)
    w.drift[0] = True
    assert w.producer.produce(plan=w.plan, source_reference=w.native["canonical_job_id"]) == result
    assert len(w.calls) == before and len(w.seals) == 1


def test_producer_wrong_private_key_has_no_completed_export_or_silent_retry(producer_world):
    w = producer_world
    wrong = subject.PublicationTestEvidenceProducer(
        w.world.home, w.producer.producer_id, Ed25519ReceiptSigner(Ed25519PrivateKey.generate())
    )
    with pytest.raises(PolicyViolation, match="does not own admitted key"):
        wrong.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    with pytest.raises(PolicyViolation, match="completed receipt"):
        subject.read_test_producer_result(w.world.home, w.job_key)
    with pytest.raises(PolicyViolation, match="recovery required"):
        w.producer.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    assert len(w.seals) == 1


def test_producer_post_sign_source_drift_never_exports_completed_signature(
    producer_world, monkeypatch
):
    w = producer_world
    original = Ed25519ReceiptSigner.seal_digest

    def seal(signer, body):
        result = original(signer, body)
        w.drift[0] = True
        return result

    monkeypatch.setattr(Ed25519ReceiptSigner, "seal_digest", seal)
    with pytest.raises(PolicyViolation, match="source changed"):
        w.producer.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    with pytest.raises(PolicyViolation, match="completed receipt"):
        subject.read_test_producer_result(w.world.home, w.job_key)


def test_producer_missing_original_measurement_cas_invalidates_export(producer_world):
    w = producer_world
    result = w.producer.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    objects = LocalContentAddressedStore(w.world.home / "artifacts/sha256")
    objects._paths(w.native["evidence_digest"])[0].unlink()
    with pytest.raises((NotFound, OSError)):
        subject.read_test_producer_result(w.world.home, result["producer_job_id"])


def test_producer_unknown_role_cannot_sign_or_create_job(producer_world):
    w = producer_world
    wrong = subject.PublicationTestEvidenceProducer(w.world.home, uuid4(), w.producer.signer)
    with pytest.raises(PolicyViolation, match="active role"):
        wrong.produce(plan=w.plan, source_reference=w.native["canonical_job_id"])
    assert not w.seals
    assert w.world.runtime.job_snapshot(w.job_key) is None
