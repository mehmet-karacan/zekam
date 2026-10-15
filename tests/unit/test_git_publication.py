"""Typed Git publication on real SQLite/CAS; regression code awaits final test phase."""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zekam.application.canonical_job_runtime import canonical_job_key, run_canonical_job
from zekam.application.local_attestation import LocalAttestationSigner, LocalAttestationVerifier
from zekam.application.git_publication import (
    GitPublicationEvidence,
    GitPublicationPlan,
    GitPublicationService,
    GitPublicationTarget,
)
from zekam.application.home import HomeLayout
from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization, AuthorizationScope, AuthorizationState
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v9
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.sqlite.git_publication import (
    SQLiteGitPublicationLedger,
    evidence_attestation_body,
    approval_attestation_body,
    revocation_attestation_body,
)


@pytest.fixture
def world(tmp_path):
    home = HomeLayout(tmp_path / "home").ensure().root
    database = home / "state" / "operational.db"
    bootstrap_v9(database)
    runtime = SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("task"), sanitized_config={}
        )
        project = uow.create_project(slug="publication", display_name="Publication")
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:publication", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref="a" * 40,
            tree_digest=digest("tree"),
            content_digest=digest("content"),
            config_digest=config.config_digest,
        )
        work = uow.create_work(
            project_id=project.id, kind="task", title="Publication", state="ready"
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("parent-plan"),
            budget={"seconds": 60},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    binding = LocalJobBinding(
        project.id,
        work.id,
        work.revision,
        run.id,
        config.id,
        config.config_digest,
        run.plan_digest,
        source_snapshot_id=snapshot.id,
    )
    target = GitPublicationTarget(
        digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
    )
    executor = uuid4()
    plan = GitPublicationPlan(
        uuid4(), executor, binding, target, digest("pending-test"), digest("pending-review")
    )
    now = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    test = GitPublicationEvidence("test", plan.subject_digest, uuid4(), executor, True, now)
    review = GitPublicationEvidence(
        "verification", plan.subject_digest, uuid4(), uuid4(), True, now
    )
    plan = replace(
        plan,
        test_evidence_digest=test.evidence_digest,
        verifier_evidence_digest=review.evidence_digest,
    )

    class Ledger:
        evidence = {test.evidence_digest: test, review.evidence_digest: review}
        reads = 0
        consumes = 0

        def issue(self, value):
            self.authorization = Authorization.issue(
                realm_id=value.realm_id,
                actor_id=value.executor_id,
                plan_digest=value.plan_digest,
                effect_digest=value.effect_digest,
                scope=AuthorizationScope(
                    allowed_resources=(value.resource,), allowed_effects=("git-push",)
                ),
                risk="high",
                lifetime=dt.timedelta(minutes=5),
                work_item_id=UUID(binding.work_item_id),
            )

        def get(self, authorization_id):
            assert authorization_id == self.authorization.id
            self.reads += 1
            return self.authorization

        def consume(self, authorization_id, **_kwargs):
            assert authorization_id == self.authorization.id
            assert self.authorization.state == AuthorizationState.ISSUED
            self.consumes += 1
            self.authorization = replace(self.authorization, state=AuthorizationState.CONSUMED)
            return SimpleNamespace(consumed=True)

        def publication_evidence(self, evidence_digest):
            return self.evidence[evidence_digest]

    class Transport:
        calls = 0
        current = target
        fail = False

        def inspect(self, **_kwargs):
            return self.current

        def push(self, expected, *, cancel):
            assert not cancel.is_set()
            self.calls += 1
            # A separate writer must be possible while the external effect runs.
            with store.unit_of_work() as uow:
                uow.create_project(slug=f"writer-{self.calls}", display_name="Writer")
                uow.commit()
            if self.fail:
                raise TimeoutError("fixture ambiguous remote outcome")
            return expected.head

    ledger, transport = Ledger(), Transport()
    ledger.issue(plan)
    service = GitPublicationService(home, ledger, transport)
    return SimpleNamespace(
        home=home,
        store=store,
        runtime=runtime,
        ledger=ledger,
        transport=transport,
        plan=plan,
        service=service,
        review=review,
        test=test,
    )


def test_publication_claim_receipt_and_historical_replay(world):
    authorization_id = world.ledger.authorization.id
    first = world.service.apply(world.plan, authorization_id=authorization_id)
    assert first["remote_oid"] == world.plan.target.head
    assert first["canonical_replayed"] is False
    reads = world.ledger.reads
    world.transport.current = replace(world.plan.target, head="b" * 40)
    replay = world.service.apply(world.plan, authorization_id=authorization_id)
    assert replay["canonical_replayed"] is True
    assert world.transport.calls == 1 and world.ledger.consumes == 1
    assert world.ledger.reads == reads
    job = world.runtime.job_snapshot(first["canonical_job_id"])
    assert job["state"] == "completed"
    assert job["effects"][0]["receipt_status"] == "completed"


@pytest.fixture
def signed_world(world):
    signers, verifiers, producers = {}, {}, {}
    for role in ("test", "verification", "approval"):
        key, producer = secrets.token_bytes(32), uuid4()
        signers[role] = LocalAttestationSigner(key)
        verifiers[role] = {producer: LocalAttestationVerifier(key)}
        producers[role] = producer
    database = world.home / "state" / "operational.db"
    ledger = SQLiteGitPublicationLedger(
        database,
        trusted_verifiers=verifiers,
        objects=LocalContentAddressedStore(
            world.home / load_settings(home=world.home).object_store_relative
        ),
    )
    proofs = []
    for role, executor in (("test", world.plan.executor_id), ("verification", uuid4())):
        with world.store.unit_of_work() as uow:
            run = uow.create_run(
                work_item_id=world.plan.binding.work_item_id,
                config_revision_id=world.plan.binding.config_revision_id,
                plan_digest=digest(role),
                budget={"seconds": 60},
                source_snapshot_id=world.plan.binding.source_snapshot_id,
            )
            uow.commit()
        binding = replace(world.plan.binding, run_id=run.id, plan_digest=run.plan_digest)
        result = run_canonical_job(
            world.home,
            binding=binding,
            operation="test.measure",
            input_digest=digest(role),
            execute=lambda _cancel: {"fixture": "completed-proof-source"},
            validate_inputs=lambda: None,
        )
        # Use the actual completed receipt's artifact, not a guessed digest.
        with sqlite3.connect(database) as connection:
            source_digest = connection.execute(
                "select r.evidence_digest from local_effect_receipt r join local_effect_claim c on c.id=r.claim_id where c.job_id=?",
                (result["canonical_job_id"],),
            ).fetchone()[0]
        proof = GitPublicationEvidence(
            role, world.plan.subject_digest, UUID(run.id), executor, True, dt.datetime.now(dt.UTC)
        )
        envelope = evidence_attestation_body(
            proof,
            source_job_id=result["canonical_job_id"],
            source_evidence_digest=source_digest,
            producer_id=producers[role],
        )
        ledger.record_evidence(
            proof,
            source_job_id=result["canonical_job_id"],
            source_evidence_digest=source_digest,
            producer_id=producers[role],
            attestation=signers[role].seal_digest(envelope),
        )
        proofs.append(proof)
    plan = replace(
        world.plan,
        test_evidence_digest=proofs[0].evidence_digest,
        verifier_evidence_digest=proofs[1].evidence_digest,
    )
    world.ledger.issue(plan)
    authorization = world.ledger.authorization
    envelope = approval_attestation_body(plan, authorization, producers["approval"])
    ledger.record_authorization(
        plan,
        authorization,
        producer_id=producers["approval"],
        attestation=signers["approval"].seal_digest(envelope),
    )
    return SimpleNamespace(
        world=world,
        ledger=ledger,
        plan=plan,
        authorization=authorization,
        signers=signers,
        producers=producers,
        database=database,
    )


def test_signed_sqlite_ledger_authorization_consumes_only_live_claim(signed_world):
    fixture = signed_world
    with pytest.raises(AuthorizationRequired, match="live"):
        fixture.ledger.consume(
            fixture.authorization.id,
            effect_digest=fixture.plan.effect_digest,
            consumed_by="git-publication/v1",
            now=dt.datetime.now(dt.UTC),
        )
    service = GitPublicationService(fixture.world.home, fixture.ledger, fixture.world.transport)
    first = service.apply(fixture.plan, authorization_id=fixture.authorization.id)
    assert fixture.ledger.get(fixture.authorization.id).state == AuthorizationState.CONSUMED
    assert (
        service.apply(fixture.plan, authorization_id=fixture.authorization.id)["canonical_replayed"]
        is True
    )
    with sqlite3.connect(fixture.database) as connection:
        assert (
            connection.execute("select count(*) from git_publication_consumption").fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("delete from git_publication_authorization")
    assert first["remote_oid"] == fixture.plan.target.head


def test_signed_revocation_prevents_new_publication(signed_world):
    fixture = signed_world
    now = dt.datetime.now(dt.UTC)
    producer = fixture.producers["approval"]
    body = revocation_attestation_body(fixture.authorization.id, now, producer)
    fixture.ledger.revoke(
        fixture.authorization.id,
        revoked_at=now,
        producer_id=producer,
        attestation=fixture.signers["approval"].seal_digest(body),
    )
    service = GitPublicationService(fixture.world.home, fixture.ledger, fixture.world.transport)
    with pytest.raises(AuthorizationRequired):
        service.apply(fixture.plan, authorization_id=fixture.authorization.id)
    assert fixture.world.transport.calls == 0


def test_plain_digest_is_not_an_approval_attestation(signed_world):
    fixture = signed_world
    fixture.world.ledger.issue(fixture.plan)
    with pytest.raises(AuthorizationRequired, match="attestation"):
        fixture.ledger.record_authorization(
            fixture.plan,
            fixture.world.ledger.authorization,
            producer_id=fixture.producers["approval"],
            attestation=digest("invented-approval"),
        )


def test_signed_evidence_requires_its_original_cas_bytes(signed_world):
    fixture = signed_world
    with sqlite3.connect(fixture.database) as connection:
        source_digest = connection.execute(
            "select source_evidence_digest from git_publication_evidence where evidence_digest=?",
            (fixture.plan.test_evidence_digest,),
        ).fetchone()[0]
    fixture.ledger._objects.delete(source_digest)
    from zekam.domain.errors import NotFound

    with pytest.raises(NotFound):
        fixture.ledger.publication_evidence(fixture.plan.test_evidence_digest)


@pytest.mark.parametrize(
    "field", ["realm_id", "actor_id", "work_item_id", "plan_digest", "effect_digest", "state"]
)
def test_foreign_or_revoked_ledger_authorization_cannot_push(world, field):
    value = (
        AuthorizationState.REVOKED
        if field == "state"
        else digest("foreign")
        if field.endswith("digest")
        else uuid4()
    )
    world.ledger.authorization = replace(world.ledger.authorization, **{field: value})
    with pytest.raises(AuthorizationRequired):
        world.service.apply(world.plan, authorization_id=world.ledger.authorization.id)
    assert world.transport.calls == 0 and world.ledger.consumes == 0


def test_same_executor_with_another_heading_is_not_independent_verification(world):
    review = replace(world.review, executor_id=world.plan.executor_id)
    world.ledger.evidence[review.evidence_digest] = review
    plan = replace(world.plan, verifier_evidence_digest=review.evidence_digest)
    world.ledger.issue(plan)
    with pytest.raises(PolicyViolation, match="bagimsiz"):
        world.service.apply(plan, authorization_id=world.ledger.authorization.id)
    assert world.transport.calls == 0


def test_remote_timeout_has_unknown_receipt_and_is_never_silently_retried(world):
    authorization_id = world.ledger.authorization.id
    world.transport.fail = True
    with pytest.raises(TimeoutError):
        world.service.apply(world.plan, authorization_id=authorization_id)
    input_digest = digest(
        {"plan_digest": world.plan.plan_digest, "authorization_id": str(authorization_id)}
    )
    job = world.runtime.job_snapshot(
        canonical_job_key(world.plan.binding.binding_digest, "git.push", input_digest)
    )
    assert job["effects"][0]["receipt_status"] == "unknown"
    with pytest.raises(AuthorizationRequired):
        world.service.apply(world.plan, authorization_id=authorization_id)
    assert world.transport.calls == 1


def test_publication_rejects_source_drift_before_consumption(world):
    world.transport.current = replace(world.plan.target, head="b" * 40)
    with pytest.raises(PolicyViolation, match="drift"):
        world.service.apply(world.plan, authorization_id=world.ledger.authorization.id)
    assert world.ledger.consumes == 0 and world.transport.calls == 0


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "refs/tags/v1",
        "refs/heads/../x",
        "refs/heads/a.lock",
        "refs/heads/a//b",
        "refs/heads/main:other",
    ],
)
def test_only_exact_branch_refspec_is_supported(ref):
    with pytest.raises(ValidationFailed):
        GitPublicationTarget(digest("root"), "origin", digest("remote"), ref, "a" * 40)
