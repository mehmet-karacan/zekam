from __future__ import annotations

import datetime as dt
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.unit.test_operational_schema_v3 import (
    _pending_close,
    _source,
)

from zekam.application.learning_daily_compiler import (
    DAILY_TIMEZONE,
    LEARNING_DAILY_OPERATION,
    LearningDailyEffectExecutor,
    compile_learning_daily,
    latest_due_learning_day,
    learning_daily_scheduled_for,
)
from zekam.application.local_runtime_service import (
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.application.memory_service import ReviewDecision
from zekam.application.skill_runtime import (
    SKILL_EXECUTE_OPERATION,
    SKILL_VERIFY_HANDLER,
    SKILL_VERIFY_OPERATION,
    SkillRuntimeSigner,
    SkillRuntimeVerifier,
    TrustedJournalSkillExecutor,
    TrustedJournalSkillVerifier,
    execution_receipt_body,
    verification_receipt_body,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.learning import FailureOccurrence, SkillEvaluation, SkillFixture
from zekam.domain.memory import MemoryCandidate, MemoryClass, MemoryEvidence, MemoryKey, MemoryScope
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_runtime_effects import (
    LocalJournalEffectExecutor,
    LocalJournalOutboxPublisher,
)
from zekam.infrastructure.sqlite.local_learning import (
    FailureCardDraft,
    SkillManifestDraft,
    SQLiteLocalLearning,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
OP_NOW = "2026-09-04T12:00:00+00:00"
_TEST_SKILL_SIGNER = SkillRuntimeSigner(b"T" * 32)
_TEST_SKILL_VERIFIER = SkillRuntimeVerifier(b"T" * 32)
_FORGED_SKILL_SIGNER = SkillRuntimeSigner(b"F" * 32)


_HARD_KILL_EXIT = 91


class _KillAfterDailyFileCreate(KnowledgeFileStore):
    def create_note(self, manifest, payload):  # type: ignore[no-untyped-def]
        super().create_note(manifest, payload)
        os._exit(_HARD_KILL_EXIT)


class _KillAfterDailyArchive(KnowledgeFileStore):
    def archive_note(self, manifest):  # type: ignore[no-untyped-def]
        super().archive_note(manifest)
        os._exit(_HARD_KILL_EXIT)


def _daily_hard_kill_child(
    learning_path: str,
    operational_path: str,
    home_path: str,
    cut_point: str,
    day_text: str,
    compiled_at_text: str,
) -> None:
    store = SQLiteLocalLearning(
        Path(learning_path), operational_path=Path(operational_path)
    )
    operational = SQLiteOperationalStore(Path(operational_path))
    files_type = (
        _KillAfterDailyFileCreate
        if cut_point == "after-create"
        else _KillAfterDailyArchive
    )
    compile_learning_daily(
        store,
        operational,
        files_type(Path(home_path)),
        day=dt.date.fromisoformat(day_text),
        compiled_at=dt.datetime.fromisoformat(compiled_at_text),
    )
    os._exit(92)


def _run_daily_hard_kill(
    store: SQLiteLocalLearning,
    home: Path,
    *,
    cut_point: str,
    day: dt.date,
    compiled_at: dt.datetime,
) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_daily_hard_kill_child,
        args=(
            str(store.path),
            str(store.operational_path),
            str(home),
            cut_point,
            day.isoformat(),
            compiled_at.isoformat(),
        ),
    )
    process.start()
    process.join(30)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("Daily hard-kill child did not terminate within bound")
    assert process.exitcode == _HARD_KILL_EXIT


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
    store = SQLiteLocalLearning(
        (tmp_path / "learning.db").resolve(),
        operational_path=operational,
        effects_root=(tmp_path / "trusted-skill-effects").resolve(),
        skill_runtime_verifier=_TEST_SKILL_VERIFIER,
    )
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
    claimed_at = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(seconds=1)
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
    activated_at = claimed_at + dt.timedelta(seconds=4)
    time.sleep(max(0.0, (activated_at - dt.datetime.now(dt.UTC)).total_seconds() + 0.05))
    return job.id, activated_at


def _skill_run(
    store: SQLiteLocalLearning,
    activation: str,
    *,
    now: dt.datetime,
    terminal_state: str = "completed",
    bound_activation: str | None = None,
    line: str = "canonical learned operation",
) -> tuple[str, str]:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    selected = activation if bound_activation is None else bound_activation
    effect = {
        "activation_digest": selected,
        "trigger": "write bounded journal",
        "input_digest": digest("skill-run-input"),
        "line": line,
    }
    job, _ = runtime.enqueue(
        idempotency_key=f"skill-run:{activation}:{terminal_state}:{selected}",
        payload={
            "operation": SKILL_EXECUTE_OPERATION,
            "skill_activation_digest": selected,
            "effect": effect,
        },
    )
    effects_root = store.path.parent / "trusted-skill-effects"
    service = LocalRuntimeService(
        runtime,
        effect_dispatcher=LocalEffectDispatcher(
            (
                (
                    SKILL_EXECUTE_OPERATION,
                    TrustedJournalSkillExecutor(
                        store, effects_root, _TEST_SKILL_SIGNER
                    ),
                ),
            )
        ),
        outbox_publisher=LocalJournalOutboxPublisher(effects_root),
    )
    work = service.run_worker_once(
        owner_id="skill-runner",
        owner_pid=os.getpid(),
        owner_token=f"skill-run:{selected[-12:]}",
        lease_seconds=30,
        job_id=job.id,
    )
    assert work is not None
    snapshot = runtime.job_snapshot(job.id)
    assert snapshot is not None
    evidence = snapshot["terminal_evidence_digest"]
    assert isinstance(evidence, str)
    if terminal_state == "completed":
        assert snapshot["state"] == "completed"
        time.sleep(1.05)
    return job.id, evidence


def _verify_skill_run(
    store: SQLiteLocalLearning, usage_digest: str, *, expected_state: str = "completed"
) -> tuple[str, str]:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    effect = {"usage_digest": usage_digest}
    job, _ = runtime.enqueue(
        idempotency_key=f"skill-verify:{usage_digest}",
        payload={"operation": SKILL_VERIFY_OPERATION, "effect": effect},
    )
    effects_root = store.path.parent / "trusted-skill-effects"
    service = LocalRuntimeService(
        runtime,
        effect_dispatcher=LocalEffectDispatcher(
            (
                (
                    SKILL_VERIFY_OPERATION,
                    TrustedJournalSkillVerifier(
                        store, effects_root, _TEST_SKILL_SIGNER
                    ),
                ),
            )
        ),
        outbox_publisher=LocalJournalOutboxPublisher(effects_root),
    )
    work = service.run_worker_once(
        owner_id="skill-verifier",
        owner_pid=os.getpid(),
        owner_token=f"skill-verify:{usage_digest[-12:]}",
        job_id=job.id,
    )
    assert work is not None
    snapshot = runtime.job_snapshot(job.id)
    assert snapshot is not None and snapshot["state"] == expected_state
    evidence = snapshot["terminal_evidence_digest"]
    assert isinstance(evidence, str)
    return job.id, evidence


def _forged_generic_skill_run(
    store: SQLiteLocalLearning, activation: str
) -> tuple[str, str]:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    effect = {
        "activation_digest": activation,
        "trigger": "write bounded journal",
        "input_digest": digest("forged-generic-input"),
        "line": "forged generic handler did not write this record",
    }
    job, _ = runtime.enqueue(
        idempotency_key=f"forged-generic:{activation}",
        payload={
            "operation": SKILL_EXECUTE_OPERATION,
            "skill_activation_digest": activation,
            "effect": effect,
        },
    )
    effect_key = f"job:{job.id}:effect:{digest(effect)}"
    journal_ref = f"skills/executions/{activation[7:]}.jsonl"
    forged_receipt = digest(
        execution_receipt_body(
            activation_digest=activation,
            trigger=str(effect["trigger"]),
            input_digest=str(effect["input_digest"]),
            journal_ref=journal_ref,
            line=str(effect["line"]),
            idempotency_key=effect_key,
            journal_evidence_digest=digest(
                {"idempotency_key": effect_key, "line": effect["line"]}
            ),
            authority=_FORGED_SKILL_SIGNER,
        )
    )
    LocalJournalEffectExecutor(store.path.parent / "trusted-skill-effects")(
        LocalEffectRequest(
            "local.append-journal/v1",
            effect_key,
            {"relative_path": journal_ref, "line": str(effect["line"])},
        )
    )
    service = LocalRuntimeService(
        runtime,
        effect_dispatcher=LocalEffectDispatcher(
            (
                (
                    SKILL_EXECUTE_OPERATION,
                    lambda _request: LocalEffectResult("completed", forged_receipt),
                ),
            )
        ),
        outbox_publisher=LocalJournalOutboxPublisher(
            store.path.parent / "trusted-skill-effects"
        ),
    )
    assert service.run_worker_once(
        owner_id="forged-generic",
        owner_pid=os.getpid(),
        owner_token="forged-generic-owner",
        job_id=job.id,
    ) is not None
    return job.id, forged_receipt


def _forged_generic_verifier_run(
    store: SQLiteLocalLearning, usage_digest: str
) -> tuple[str, str]:
    runtime = SQLiteLocalRuntimeStore(store.operational_path, existing_only=True)
    target = store.skill_usage_verification_target(usage_digest)
    effect = {"usage_digest": usage_digest}
    job, _ = runtime.enqueue(
        idempotency_key=f"forged-generic-verifier:{usage_digest}",
        payload={"operation": SKILL_VERIFY_OPERATION, "effect": effect},
    )
    effect_key = f"job:{job.id}:effect:{digest(effect)}"
    audit_ref = f"skills/verifications/{usage_digest[7:]}.jsonl"
    audit_line = canonical_json(
        {
            "schema": "zekam-trusted-skill-verification-audit/v1",
            "usage_digest": usage_digest,
            "activation_digest": target["activation_digest"],
            "execution_job_id": target["execution_job_id"],
            "execution_evidence_digest": target["execution_evidence_digest"],
            "journal_ref": target["journal_ref"],
            "journal_record_present": True,
        }
    )
    forged_receipt = digest(
        verification_receipt_body(
            usage_digest=usage_digest,
            activation_digest=target["activation_digest"],
            execution_job_id=target["execution_job_id"],
            execution_evidence_digest=target["execution_evidence_digest"],
            journal_ref=target["journal_ref"],
            journal_record_present=True,
            verification_audit_ref=audit_ref,
            verification_idempotency_key=effect_key,
            verification_audit_line=audit_line,
            verification_audit_evidence_digest=digest(
                {"idempotency_key": effect_key, "line": audit_line}
            ),
            authority=_FORGED_SKILL_SIGNER,
        )
    )
    LocalJournalEffectExecutor(store.path.parent / "trusted-skill-effects")(
        LocalEffectRequest(
            "local.append-journal/v1",
            effect_key,
            {"relative_path": audit_ref, "line": audit_line},
        )
    )
    service = LocalRuntimeService(
        runtime,
        effect_dispatcher=LocalEffectDispatcher(
            (
                (
                    SKILL_VERIFY_OPERATION,
                    lambda _request: LocalEffectResult("completed", forged_receipt),
                ),
            )
        ),
        outbox_publisher=LocalJournalOutboxPublisher(
            store.path.parent / "trusted-skill-effects"
        ),
    )
    assert service.run_worker_once(
        owner_id="forged-generic-verifier",
        owner_pid=os.getpid(),
        owner_token="forged-generic-verifier-owner",
        job_id=job.id,
    ) is not None
    return job.id, forged_receipt


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
        "bounded-journal-entry",
        1,
        "Append one canonical local journal record.",
        ("write bounded journal",),
        ("record digest",),
        ("verified journal receipt",),
        ("local-journal",),
        ("Append the canonical single-line record.",),
        ("Read back the exact appended record.",),
        ("journal write uncertainty",),
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
        "bounded-journal-entry",
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
    run_ref, usage_evidence = _skill_run(
        store, activation, now=activation_at
    )
    used = store.record_skill_usage(
        activation,
        run_ref=run_ref,
        usage_evidence_digest=usage_evidence,
        now=dt.datetime.now(dt.UTC),
    )
    verification_job, verification_evidence = _verify_skill_run(store, used)
    store.verify_skill_outcome(
        used,
        outcome="verified-success",
        verifier_ref=SKILL_VERIFY_HANDLER,
        verification_job_ref=verification_job,
        verification_evidence_digest=verification_evidence,
        now=activation_at + dt.timedelta(seconds=1),
    )
    assert store.select_active_skills("write bounded journal") == (
        {
            "activation_digest": activation,
            "manifest_digest": manifest,
            "skill_id": "bounded-journal-entry",
            "version": 1,
            "trigger": "write bounded journal",
            "purpose": "Append one canonical local journal record.",
            "permissions_ceiling": "workspace-write-no-network",
            "source_evidence": ("failure-card", "lesson"),
            "required_tools": ("local-journal",),
            "steps": ("Append the canonical single-line record.",),
            "checks": ("Read back the exact appended record.",),
        },
    )
    assert store.select_active_skills("unrelated") == ()
    restarted = SQLiteLocalLearning(store.path, operational_path=store.operational_path)
    assert restarted.effectiveness(activation) == {
        "usage_count": 1,
        "verified_outcome_count": 1,
        "verified_success_count": 1,
    }


def test_skill_usage_and_outcome_are_separate_and_receipt_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(hours=1)
    manifest = store.propose_skill(
        _skill(), _lesson(store, now=base), now=base + dt.timedelta(seconds=4)
    )
    evaluation = store.evaluate_skill(
        manifest,
        SkillEvaluation(
            "bounded-journal-entry",
            (SkillFixture("fixture-a", "1", digest("fixture")),),
            5,
            5,
            "evaluator-a",
            "verifier-b",
            0.5,
        ),
        now=base + dt.timedelta(seconds=5),
    )
    review = store.review_skill(
        manifest,
        evaluation,
        ReviewDecision(True, "reviewer-c", "independent"),
        now=base + dt.timedelta(seconds=6),
    )
    activation_job, activation_at = _activation_job(store, manifest, evaluation, review)
    activation = store.activate_skill(
        manifest,
        evaluation,
        review,
        activation_job_id=activation_job,
        now=activation_at,
    )
    forged_generic_run, forged_generic_evidence = _forged_generic_skill_run(
        store, activation
    )
    with pytest.raises(PolicyViolation, match="terminal run binding drift"):
        store.record_skill_usage(
            activation,
            run_ref=forged_generic_run,
            usage_evidence_digest=forged_generic_evidence,
            now=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
        )
    run_ref, evidence = _skill_run(
        store, activation, now=activation_at
    )
    used = store.record_skill_usage(
        activation,
        run_ref=run_ref,
        usage_evidence_digest=evidence,
        now=dt.datetime.now(dt.UTC),
    )
    assert store.effectiveness(activation) == {
        "usage_count": 1,
        "verified_outcome_count": 0,
        "verified_success_count": 0,
    }
    forged_verify_job, forged_verify_evidence = _forged_generic_verifier_run(store, used)
    with pytest.raises(PolicyViolation, match="verifier binding drift"):
        store.verify_skill_outcome(
            used,
            outcome="verified-success",
            verifier_ref=SKILL_VERIFY_HANDLER,
            verification_job_ref=forged_verify_job,
            verification_evidence_digest=forged_verify_evidence,
            now=dt.datetime.now(dt.UTC),
        )
    verification_job, verification_evidence = _verify_skill_run(store, used)
    with pytest.raises(PolicyViolation, match="contradicts"):
        store.verify_skill_outcome(
            used,
            outcome="verified-failure",
            verifier_ref=SKILL_VERIFY_HANDLER,
            verification_job_ref=verification_job,
            verification_evidence_digest=verification_evidence,
            now=activation_at + dt.timedelta(seconds=3),
        )
    with pytest.raises(PolicyViolation, match="trusted verifier"):
        store.verify_skill_outcome(
            used,
            outcome="verified-success",
            verifier_ref="reviewer-c",
            verification_job_ref=verification_job,
            verification_evidence_digest=verification_evidence,
            now=activation_at + dt.timedelta(seconds=3),
        )
    outcome = store.verify_skill_outcome(
        used,
        outcome="verified-success",
        verifier_ref=SKILL_VERIFY_HANDLER,
        verification_job_ref=verification_job,
        verification_evidence_digest=verification_evidence,
        now=activation_at + dt.timedelta(seconds=3),
    )
    assert outcome.startswith("sha256:")

    forged_run, forged_evidence = _skill_run(
        store,
        activation,
        bound_activation=digest("other-activation"),
        terminal_state="recovery-required",
        now=activation_at + dt.timedelta(seconds=4),
    )
    with pytest.raises(PolicyViolation, match=r"terminal run receipt|binding"):
        store.record_skill_usage(
            activation,
            run_ref=forged_run,
            usage_evidence_digest=forged_evidence,
            now=activation_at + dt.timedelta(seconds=5),
        )

    secret_line = "SECRET_" + "KEY=" + ("A" * 40)
    with pytest.raises(PolicyViolation, match="durable queue"):
        _skill_run(
            store,
            activation,
            terminal_state="recovery-required",
            line=secret_line,
            now=activation_at + dt.timedelta(seconds=6),
        )
    with sqlite3.connect(store.operational_path) as connection:
        assert connection.execute(
            "select count(*) from local_job where payload_json like ?",
            (f"%{secret_line}%",),
        ).fetchone() == (0,)
    journals = (store.path.parent / "trusted-skill-effects").rglob("*.jsonl")
    assert all(secret_line not in path.read_text(encoding="utf-8") for path in journals)


def test_skill_verifier_records_failure_after_journal_tamper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(hours=1)
    manifest = store.propose_skill(
        _skill(), _lesson(store, now=base), now=base + dt.timedelta(seconds=4)
    )
    evaluation = store.evaluate_skill(
        manifest,
        SkillEvaluation(
            "bounded-journal-entry",
            (SkillFixture("fixture-a", "1", digest("fixture")),),
            5,
            5,
            "evaluator-a",
            "verifier-b",
            0.5,
        ),
        now=base + dt.timedelta(seconds=5),
    )
    review = store.review_skill(
        manifest,
        evaluation,
        ReviewDecision(True, "reviewer-c", "independent"),
        now=base + dt.timedelta(seconds=6),
    )
    activation_job, activation_at = _activation_job(store, manifest, evaluation, review)
    activation = store.activate_skill(
        manifest,
        evaluation,
        review,
        activation_job_id=activation_job,
        now=activation_at,
    )
    run_ref, evidence = _skill_run(store, activation, now=activation_at)
    usage = store.record_skill_usage(
        activation,
        run_ref=run_ref,
        usage_evidence_digest=evidence,
        now=dt.datetime.now(dt.UTC),
    )
    journal = (
        store.path.parent
        / "trusted-skill-effects"
        / "skills"
        / "executions"
        / f"{activation[7:]}.jsonl"
    )
    journal.unlink()
    verification_job, verification_evidence = _verify_skill_run(
        store, usage, expected_state="failed"
    )
    outcome = store.verify_skill_outcome(
        usage,
        outcome="verified-failure",
        verifier_ref=SKILL_VERIFY_HANDLER,
        verification_job_ref=verification_job,
        verification_evidence_digest=verification_evidence,
        now=dt.datetime.now(dt.UTC),
    )
    assert outcome.startswith("sha256:")


def test_daily_learning_compile_revises_and_preserves_edited_generated_note(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    home = (tmp_path / "knowledge-home").resolve()
    home.mkdir(mode=0o700)
    files = KnowledgeFileStore(home)
    operational = SQLiteOperationalStore(store.operational_path)
    scheduled_for = learning_daily_scheduled_for(NOW.date())
    forged_body = {
        "schema": "zekam-learning-daily-schedule/v1",
        "start_day": NOW.date().isoformat(),
        "day": NOW.date().isoformat(),
        "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
        "operation": LEARNING_DAILY_OPERATION,
        "misfire": "run-once",
        "timezone": DAILY_TIMEZONE,
    }

    with pytest.raises(PolicyViolation, match="schedule digest drift"):
        LearningDailyEffectExecutor(store, operational, files)(
            LocalEffectRequest(
                LEARNING_DAILY_OPERATION,
                "forged-daily",
                {
                    "start_day": NOW.date().isoformat(),
                    "day": NOW.date().isoformat(),
                    "scheduled_for": forged_body["scheduled_for"],
                    "schedule_digest": digest("forged-schedule"),
                    "source": "os-supervisor",
                    "timezone": DAILY_TIMEZONE,
                },
            )
        )

    first = compile_learning_daily(
        store,
        operational,
        files,
        day=NOW.date(),
        compiled_at=NOW,
    )
    replay = compile_learning_daily(
        store,
        operational,
        files,
        day=NOW.date(),
        compiled_at=NOW + dt.timedelta(minutes=1),
    )
    assert replay.as_dict() == first.as_dict()

    store.propose_hygiene(digest("daily-subject"), "stale", now=NOW)
    revised = compile_learning_daily(
        store,
        operational,
        files,
        day=NOW.date(),
        compiled_at=NOW + dt.timedelta(seconds=1),
    )
    assert revised.state == "revised"
    assert revised.predecessor_note_id == first.note_id
    assert revised.snapshot_digest != first.snapshot_digest
    with operational.unit_of_work() as uow:
        prior = uow.get_knowledge_note(first.note_id)
        uow.commit()
    assert prior.state == "archived"

    next_day = NOW.date() + dt.timedelta(days=1)
    conflict_first = compile_learning_daily(
        store,
        operational,
        files,
        day=next_day,
        compiled_at=NOW + dt.timedelta(days=1),
    )
    edited_path = home / conflict_first.portable_ref
    edited_path.write_bytes(edited_path.read_bytes() + b"\nuser edit preserved\n")
    store.propose_hygiene(
        digest("next-day-subject"),
        "conflict",
        now=NOW + dt.timedelta(days=1),
    )
    conflict = compile_learning_daily(
        store,
        operational,
        files,
        day=next_day,
        compiled_at=NOW + dt.timedelta(days=1, seconds=1),
    )
    assert conflict.state == "conflict"
    assert edited_path.read_bytes().endswith(b"user edit preserved\n")
    with sqlite3.connect(store.operational_path) as connection:
        relation = connection.execute(
            "select relation_kind from knowledge_relation where id=?",
            (conflict.relation_id,),
        ).fetchone()
    assert relation is not None
    assert relation[0] == "conflicts-with"


def test_daily_learning_due_window_uses_istanbul_21_and_only_complete_days() -> None:
    assert latest_due_learning_day(
        dt.datetime(2026, 9, 6, 17, 59, tzinfo=dt.UTC)
    ) == dt.date(2026, 9, 4)
    assert latest_due_learning_day(
        dt.datetime(2026, 9, 6, 18, 0, tzinfo=dt.UTC)
    ) == dt.date(2026, 9, 5)
    assert learning_daily_scheduled_for(dt.date(2026, 9, 5)) == dt.datetime(
        2026, 9, 6, 18, 0, tzinfo=dt.UTC
    )


def test_daily_snapshot_includes_local_start_and_excludes_local_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.propose_hygiene(
        digest("before-local-day"),
        "stale",
        now=dt.datetime(2026, 9, 3, 20, 59, 59, tzinfo=dt.UTC),
    )
    included = store.propose_hygiene(
        digest("at-local-day-start"),
        "stale",
        now=dt.datetime(2026, 9, 3, 21, 0, 0, tzinfo=dt.UTC),
    )
    store.propose_hygiene(
        digest("at-next-local-day-start"),
        "stale",
        now=dt.datetime(2026, 9, 4, 21, 0, 0, tzinfo=dt.UTC),
    )

    snapshot = store.daily_snapshot(
        dt.date(2026, 9, 4), timezone_name=DAILY_TIMEZONE
    )

    assert snapshot["range_start_utc"] == "2026-09-03T21:00:00+00:00"
    assert snapshot["range_end_utc"] == "2026-09-04T21:00:00+00:00"
    assert snapshot["counts"]["hygiene_proposal"] == 1
    assert [
        record["record_digest"]
        for record in snapshot["records"]
        if record["kind"] == "hygiene_proposal"
    ] == [included]


def test_daily_learning_reconciles_kill_after_file_before_confirm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    home = (tmp_path / "daily-create-recovery").resolve()
    home.mkdir(mode=0o700)
    operational = SQLiteOperationalStore(store.operational_path)
    _run_daily_hard_kill(
        store,
        home,
        cut_point="after-create",
        day=NOW.date(),
        compiled_at=NOW,
    )
    with sqlite3.connect(store.operational_path) as connection:
        assert connection.execute(
            "select materialized from knowledge_note"
        ).fetchone() == (0,)

    recovered = compile_learning_daily(
        store,
        operational,
        KnowledgeFileStore(home),
        day=NOW.date(),
        compiled_at=NOW,
    )

    assert recovered.state == "current"
    with sqlite3.connect(store.operational_path) as connection:
        assert connection.execute(
            "select materialized from knowledge_note"
        ).fetchone() == (1,)


def test_daily_learning_reconciles_kill_after_archive_before_db_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    home = (tmp_path / "daily-archive-recovery").resolve()
    home.mkdir(mode=0o700)
    operational = SQLiteOperationalStore(store.operational_path)
    first = compile_learning_daily(
        store,
        operational,
        KnowledgeFileStore(home),
        day=NOW.date(),
        compiled_at=NOW,
    )
    store.propose_hygiene(digest("archive-recovery-change"), "stale", now=NOW)
    _run_daily_hard_kill(
        store,
        home,
        cut_point="after-archive",
        day=NOW.date(),
        compiled_at=NOW + dt.timedelta(seconds=1),
    )

    recovered = compile_learning_daily(
        store,
        operational,
        KnowledgeFileStore(home),
        day=NOW.date(),
        compiled_at=NOW + dt.timedelta(seconds=1),
    )

    assert recovered.state == "revised"
    assert recovered.predecessor_note_id == first.note_id
    with sqlite3.connect(store.operational_path) as connection:
        states = connection.execute(
            "select state,count(*) from knowledge_note group by state order by state"
        ).fetchall()
    assert states == [("active", 1), ("archived", 1)]


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
