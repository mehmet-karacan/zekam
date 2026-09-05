from __future__ import annotations

import datetime as dt
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.unit.test_operational_schema_v3 import (
    _pending_close,
    _source,
)

from zekam.application.memory_service import ReviewDecision
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.learning import FailureOccurrence, SkillEvaluation, SkillFixture
from zekam.domain.memory import MemoryCandidate, MemoryClass, MemoryEvidence, MemoryKey, MemoryScope
from zekam.infrastructure.sqlite.local_learning import (
    FailureCardDraft,
    SkillManifestDraft,
    SQLiteLocalLearning,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
OP_NOW = "2026-09-04T12:00:00+00:00"


def _store(tmp_path: Path) -> SQLiteLocalLearning:
    operational = _source((tmp_path / "operational.db").resolve(), 3)
    payload = {
        "session_id": "session",
        "binding_digest": digest("binding"),
        "request_digest": digest("close"),
    }
    with sqlite3.connect(operational) as db:
        db.execute("pragma foreign_keys=on")
        db.execute("insert into local_runtime_config values(1,64)")
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at) "
            "values('close-job','close-job-key',?,'completed',1,?,?,?,?)",
            (canonical_json(payload), OP_NOW, digest("compile"), OP_NOW, OP_NOW),
        )
    _pending_close(operational, control=True)
    with sqlite3.connect(operational) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_outbox values('close-outbox','close-job','close-outbox-key',"
            "'continuity.compile',?,?,?)",
            (canonical_json(payload), digest(payload), OP_NOW),
        )
        db.execute(
            "insert into local_outbox_delivery values('close-outbox','delivered',1,"
            "'delivery-claim','worker',1,'past-owner',?,?)",
            (OP_NOW, OP_NOW),
        )
        db.execute(
            "insert into local_outbox_receipt values('delivery-receipt','close-outbox',"
            "'delivery-claim',1,'delivered',?,?)",
            (digest("delivery"), OP_NOW),
        )
        db.execute(
            "insert into continuity_outbox_binding values('close-outbox','session',"
            "'close-job','close',?,?)",
            (digest("close"), digest("close")),
        )
        db.execute(
            "insert into close_receipt values(?,?,'session',?,?,'close-outbox','[]',?)",
            (digest("receipt"), digest("close"), digest("checkpoint"), digest("context"), OP_NOW),
        )
        db.execute(
            "update session set status='closed',closed_at=?,close_receipt_digest=? "
            "where id='session'",
            (OP_NOW, digest("receipt")),
        )
    operational.chmod(0o600)
    store = SQLiteLocalLearning((tmp_path / "learning.db").resolve(), operational_path=operational)
    store.bootstrap()
    return store


def _candidate(identifier: str, content: str, *, author: str = "author-a") -> MemoryCandidate:
    return MemoryCandidate(
        identifier,
        MemoryKey(MemoryScope.PROJECT, "realm-a", project_ref="project-a"),
        MemoryClass.SEMANTIC,
        content,
        author,
        NOW,
        (MemoryEvidence("receipt", f"receipt/{identifier}", digest("receipt")),),
    )


def _failed_occurrence(
    store: SQLiteLocalLearning,
    key: str,
    evidence: str,
    *,
    now: dt.datetime = NOW,
    operation: str = "failure.observe",
) -> FailureOccurrence:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    evidence_digest = digest(evidence)
    job, _ = runtime.enqueue(
        idempotency_key=f"failure:{evidence}",
        payload={
            "operation": operation,
            "occurrence_key": key,
            "evidence_digest": evidence_digest,
            "failure_category": "storage",
        },
        available_at=now.isoformat(),
    )
    work = runtime.claim_next(
        owner_id="wp09-worker",
        owner_pid=os.getpid(),
        owner_token=f"owner:{evidence}",
        lease_seconds=30,
        supported_operations=(operation,),
        job_id=job.id,
        now=now.isoformat(),
    )
    assert work is not None
    claim, _ = runtime.claim_effect(
        work,
        operation=operation,
        effect_digest=digest(
            {
                "operation": operation,
                "job_id": job.id,
                "occurrence_key": key,
                "evidence_digest": evidence_digest,
                "failure_category": "storage",
            }
        ),
        idempotency_key=f"failure-effect:{evidence}",
        now=now.isoformat(),
    )
    runtime.record_receipt(
        claim, status="failed", evidence_digest=evidence_digest, now=now.isoformat()
    )
    runtime.finish(work, state="failed", evidence_digest=evidence_digest, now=now.isoformat())
    return FailureOccurrence(key, evidence_digest, job.id, now, "storage")


def _activation_job(
    store: SQLiteLocalLearning, manifest: str, evaluation: str, review: str
) -> tuple[str, dt.datetime]:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    claimed_at = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(seconds=10)
    effect = digest(
        {
            "operation": "skill.activate",
            "manifest_digest": manifest,
            "evaluation_digest": evaluation,
            "review_digest": review,
        }
    )
    job, _ = runtime.enqueue(
        idempotency_key=f"activate:{manifest}",
        payload={
            "operation": "skill.activate",
            "authorization_review_digest": review,
        },
        available_at=claimed_at.isoformat(),
    )
    work = runtime.claim_next(
        owner_id="wp09-worker",
        owner_pid=os.getpid(),
        owner_token="activation-owner",
        lease_seconds=30,
        supported_operations=("skill.activate",),
        job_id=job.id,
        now=claimed_at.isoformat(),
    )
    assert work is not None
    claim, _ = runtime.claim_effect(
        work,
        operation="skill.activate",
        effect_digest=effect,
        idempotency_key=f"activate-effect:{manifest}",
        now=(claimed_at + dt.timedelta(seconds=1)).isoformat(),
    )
    evidence = digest({"claim_id": claim.id, "effect_digest": effect})
    runtime.record_receipt(
        claim,
        status="completed",
        evidence_digest=evidence,
        now=(claimed_at + dt.timedelta(seconds=2)).isoformat(),
    )
    runtime.finish(
        work,
        state="completed",
        evidence_digest=evidence,
        now=(claimed_at + dt.timedelta(seconds=3)).isoformat(),
    )
    return job.id, claimed_at + dt.timedelta(seconds=4)


def test_memory_candidate_review_revision_conflict_and_supersession(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _candidate("candidate-1", "Use a bounded local transaction.")
    with pytest.raises(PolicyViolation):
        store.propose_memory(first, source_kind="raw-transcript")
    forged = MemoryCandidate(
        "candidate-forged",
        first.key,
        first.memory_class,
        "Caller-supplied receipt digests are insufficient.",
        "author-a",
        NOW,
        (MemoryEvidence("receipt", "receipt/forged", digest("forged")),),
    )
    with pytest.raises(PolicyViolation, match="terminal WP-08"):
        store.propose_memory(forged, source_kind="receipt")
    first_digest = store.propose_memory(first, source_kind="receipt")
    assert store.propose_memory(first, source_kind="receipt") == first_digest
    with pytest.raises(PolicyViolation, match="payload drift"):
        store.propose_memory(
            _candidate("candidate-duplicate", first.content), source_kind="receipt"
        )
    with pytest.raises(PolicyViolation):
        store.review_memory(first_digest, ReviewDecision(True, "author-a", "self"), now=NOW)
    first_review = store.review_memory(
        first_digest, ReviewDecision(True, "reviewer-b", "verified"), now=NOW
    )
    first_revision = store.activate_memory(first_digest, first_review, now=NOW)

    second = _candidate("candidate-2", "Use a bounded local transaction and reopen it.")
    second_digest = store.propose_memory(second, source_kind="test")
    second_review = store.review_memory(
        second_digest, ReviewDecision(True, "reviewer-b", "verified"), now=NOW
    )
    with pytest.raises(PolicyViolation):
        store.activate_memory(second_digest, second_review, now=NOW)
    second_revision = store.activate_memory(
        second_digest, second_review, now=NOW + dt.timedelta(seconds=1), supersedes=first_revision
    )
    stale = MemoryCandidate(
        "candidate-stale",
        first.key,
        MemoryClass.SEMANTIC,
        "An older observation must not replace the current fact.",
        "author-a",
        NOW - dt.timedelta(seconds=1),
        (MemoryEvidence("receipt", "receipt/stale", digest("receipt")),),
    )
    stale_digest = store.propose_memory(stale, source_kind="receipt")
    stale_review = store.review_memory(
        stale_digest, ReviewDecision(True, "reviewer-b", "verified"), now=NOW
    )
    with pytest.raises(PolicyViolation):
        store.activate_memory(
            stale_digest,
            stale_review,
            now=NOW + dt.timedelta(seconds=2),
            supersedes=second_revision,
        )
    with sqlite3.connect(store.path) as db:
        assert db.execute("select count(*) from memory_revision").fetchone()[0] == 2
        assert db.execute("select count(*) from memory_relation").fetchone()[0] == 1
        assert (
            db.execute(
                "select relation_kind from memory_relation where from_revision_digest=?",
                (second_revision,),
            ).fetchone()[0]
            == "supersedes"
        )
        families = db.execute(
            "select count(distinct memory_id),group_concat(revision, ',') "
            "from memory_revision order by revision"
        ).fetchone()
        assert families == (1, "1,2")
        assert db.execute("select revision_digest,revision from memory_head").fetchone() == (
            second_revision,
            2,
        )


def test_memory_candidate_review_activation_temporal_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate("candidate-time", "Temporal order is durable.")
    candidate_digest = store.propose_memory(candidate, source_kind="receipt")
    with pytest.raises(PolicyViolation, match="cannot predate"):
        store.review_memory(
            candidate_digest,
            ReviewDecision(True, "reviewer-b", "verified"),
            now=NOW - dt.timedelta(seconds=1),
        )
    review = store.review_memory(
        candidate_digest,
        ReviewDecision(True, "reviewer-b", "verified"),
        now=NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(PolicyViolation, match="cannot predate"):
        store.activate_memory(candidate_digest, review, now=NOW)


def test_failure_card_and_lesson_require_two_distinct_observations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    single_failure = MemoryCandidate(
        "single-failure",
        MemoryKey(MemoryScope.PROJECT, "realm-a", project_ref="project-a"),
        MemoryClass.FAILURE,
        "A single failure observation.",
        "author-a",
        NOW,
        (MemoryEvidence("receipt", "receipt/single", digest("receipt")),),
        occurrence_key="single",
        observation_count=1,
    )
    single_digest = store.propose_memory(single_failure, source_kind="receipt")
    single_review = store.review_memory(
        single_digest, ReviewDecision(True, "reviewer-b", "verified"), now=NOW
    )
    with pytest.raises(PolicyViolation):
        store.activate_memory(single_digest, single_review, now=NOW)
    with pytest.raises(PolicyViolation, match="terminal run receipt"):
        store.observe_failure(
            FailureOccurrence("sqlite-locked", digest("untrusted"), "missing-run", NOW, "storage")
        )
    unrelated = _failed_occurrence(
        store, "sqlite-locked", "unrelated", operation="unrelated.failure"
    )
    with pytest.raises(PolicyViolation, match="terminal run receipt"):
        store.observe_failure(unrelated)
    first = _failed_occurrence(store, "sqlite-locked", "evidence-1")
    signature = store.observe_failure(first)
    assert store.observe_failure(first) == signature
    draft = FailureCardDraft(
        "SQLite commit did not complete.",
        "Disposable local runtime.",
        "A reader retained a transaction.",
        "Increasing all timeouts without diagnosis.",
        "Close the reader before the writer commit.",
        "Two independent runs and a restart test passed.",
        ("receipt/run-1", "receipt/run-2"),
        "author-a",
        "reviewer-b",
    )
    with pytest.raises(PolicyViolation):
        store.create_failure_card(signature, draft, now=NOW)
    store.observe_failure(_failed_occurrence(store, "sqlite-locked", "evidence-2"))
    card = store.create_failure_card(signature, draft, now=NOW)
    lesson = store.extract_lesson(
        card,
        "Readers must close their transaction before the bounded writer commit.",
        author_ref="author-c",
        now=NOW,
    )
    assert lesson.startswith("sha256:")


def _skill() -> SkillManifestDraft:
    return SkillManifestDraft(
        "bounded-sqlite-reader",
        1,
        "Close bounded readers before writer commit.",
        ("database is locked",),
        ("SQLite path",),
        ("verified transaction result",),
        (),
        ("Open read-only", "Rollback", "Close", "Commit writer"),
        ("restart", "concurrent reader"),
        ("commit uncertainty",),
        "workspace-write-no-network",
        ("failure-card", "lesson"),
        "deactivate manifest version 1",
        "replace with independently evaluated successor",
        "author-skill",
    )


def _lesson(store: SQLiteLocalLearning, *, now: dt.datetime = NOW) -> str:
    signature = store.observe_failure(_failed_occurrence(store, "locked", "a", now=now))
    store.observe_failure(
        _failed_occurrence(store, "locked", "b", now=now + dt.timedelta(seconds=1))
    )
    card = store.create_failure_card(
        signature,
        FailureCardDraft(
            "locked",
            "local",
            "reader",
            "retry forever",
            "close reader",
            "restart",
            ("run/a", "run/b"),
            "author-card",
            "reviewer-card",
        ),
        now=now + dt.timedelta(seconds=2),
    )
    return store.extract_lesson(
        card,
        "Close the reader.",
        author_ref="lesson-author",
        now=now + dt.timedelta(seconds=3),
    )


def test_skill_requires_tests_independent_review_and_records_effectiveness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(hours=1)
    manifest = store.propose_skill(
        _skill(), _lesson(store, now=base), now=base + dt.timedelta(seconds=4)
    )
    with pytest.raises(PolicyViolation):
        store.activate_skill(
            manifest,
            digest("missing-eval"),
            digest("missing-review"),
            activation_job_id="missing-job",
            now=base + dt.timedelta(seconds=5),
        )
    evaluation = SkillEvaluation(
        "bounded-sqlite-reader",
        (SkillFixture("fixture-a", "1", digest("fixture")),),
        5,
        5,
        "evaluator-a",
        "verifier-b",
        0.5,
    )
    with pytest.raises(PolicyViolation, match="evaluation"):
        store.evaluate_skill(manifest, evaluation, now=base + dt.timedelta(seconds=4))
    evaluation_digest = store.evaluate_skill(
        manifest, evaluation, now=base + dt.timedelta(seconds=5)
    )
    with pytest.raises(PolicyViolation):
        store.review_skill(
            manifest,
            evaluation_digest,
            ReviewDecision(True, "evaluator-a", "self"),
            now=base + dt.timedelta(seconds=6),
        )
    with pytest.raises(PolicyViolation, match="chronology"):
        store.review_skill(
            manifest,
            evaluation_digest,
            ReviewDecision(True, "reviewer-c", "independent"),
            now=base + dt.timedelta(seconds=5),
        )
    review = store.review_skill(
        manifest,
        evaluation_digest,
        ReviewDecision(True, "reviewer-c", "independent"),
        now=base + dt.timedelta(seconds=6),
    )
    with pytest.raises(PolicyViolation, match="authorization"):
        store.activate_skill(
            manifest,
            evaluation_digest,
            review,
            activation_job_id="missing-job",
            now=base + dt.timedelta(seconds=7),
        )
    activation_job, activation_at = _activation_job(store, manifest, evaluation_digest, review)
    with pytest.raises(PolicyViolation, match="chronology"):
        store.activate_skill(
            manifest,
            evaluation_digest,
            review,
            activation_job_id=activation_job,
            now=activation_at - dt.timedelta(seconds=2),
        )
    activation = store.activate_skill(
        manifest,
        evaluation_digest,
        review,
        activation_job_id=activation_job,
        now=activation_at,
    )
    assert (
        store.activate_skill(
            manifest,
            evaluation_digest,
            review,
            activation_job_id=activation_job,
            now=activation_at,
        )
        == activation
    )
    store.record_skill_outcome(
        activation,
        run_ref="run-verified",
        usage_digest=digest("usage-evidence"),
        outcome="verified-success",
        verifier_ref="outcome-verifier",
        now=activation_at + dt.timedelta(seconds=1),
    )
    restarted = SQLiteLocalLearning(store.path, operational_path=store.operational_path)
    assert restarted.effectiveness(activation) == {
        "usage_count": 1,
        "verified_outcome_count": 1,
        "verified_success_count": 1,
    }


def test_append_only_hygiene_bounds_corruption_and_concurrent_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate("candidate-race", "Concurrency keeps one candidate row.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _index: store.propose_memory(candidate, source_kind="receipt"), range(2)
            )
        )
    assert len(set(results)) == 1
    proposal = store.propose_hygiene(results[0], "retention-review", now=NOW)
    assert proposal.startswith("sha256:")
    assert store.audit()["memory_candidate"] == 1
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("delete from memory_candidate")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "insert into hygiene_proposal values(?,?,?,?,?)",
            (digest("bad"), digest("subject"), "stale", NOW.isoformat(), "{}"),
        )
    with pytest.raises(PolicyViolation):
        store.audit()
    with sqlite3.connect(store.path) as db:
        db.execute("pragma writable_schema=on")
        db.execute("update learning_schema set version=2")
    with pytest.raises(PolicyViolation):
        SQLiteLocalLearning(store.path, operational_path=store.operational_path).effectiveness(
            digest("unknown")
        )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        SQLiteLocalLearning(Path("relative.db"), operational_path=store.operational_path)


def test_reopen_rejects_append_only_schema_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("drop trigger memory_candidate_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        SQLiteLocalLearning(store.path, operational_path=store.operational_path).audit()
