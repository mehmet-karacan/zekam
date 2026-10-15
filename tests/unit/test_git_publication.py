"""Typed Git publication on real SQLite/CAS; regression code awaits final test phase."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.application.git_publication import (
    GitPublicationEvidence,
    GitPublicationPlan,
    GitPublicationService,
    GitPublicationTarget,
)
from zekam.application.home import HomeLayout
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization, AuthorizationScope, AuthorizationState
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v8
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def world(tmp_path):
    home = HomeLayout(tmp_path / "home").ensure().root
    database = home / "state" / "operational.db"
    bootstrap_v8(database)
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
