from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from tests.unit.test_operational_schema_v3 import _source

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.personal_skill import (
    PersonalSkillRevision,
    SkillEvaluationV2,
    SkillInvocationState,
    SkillOriginEvidence,
    SkillOriginKind,
    SkillScopeKind,
)
from zekam.infrastructure.local_file_security import restrict_private_file, restrict_private_tree
from zekam.infrastructure.sqlite.local_learning import (
    _SCHEMA_V1,
    SCHEMA_DIGEST,
    SCHEMA_V1_DIGEST,
    SQLiteLocalLearning,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.skill_lifecycle import (
    SQLiteSkillLifecycle,
    activation_effect_digest,
    evaluation_evidence_effect_digest,
    outcome_verification_effect_digest,
    review_evidence_effect_digest,
)
from zekam.interfaces.cli.skill import _apply_proposal_with_runtime

NOW = dt.datetime(2026, 9, 13, 14, tzinfo=dt.UTC)
NOW_TEXT = NOW.isoformat()


def _operational(path: Path) -> Path:
    operational = _source(path.resolve(), 3)
    store = SQLiteOperationalStore(operational)
    with store.unit_of_work() as uow:
        for slug in ("project-a", "project-b", "zekam"):
            uow.create_project(slug=slug, display_name=slug)
        uow.commit()
    return operational


def _learning(tmp_path: Path, operational: Path) -> Path:
    path = (tmp_path / "learning.db").resolve()
    SQLiteLocalLearning(path, operational_path=operational).bootstrap()
    return path


def _terminal_receipt(
    path: Path,
    *,
    run_ref: str,
    evidence_digest: str,
    operation: str = "skill.origin.observe",
    effect_digest: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    payload = canonical_json({"operation": operation, "run_ref": run_ref})
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at) "
            "values(?,?,?,'completed',1,?,?,?,?)",
            (
                run_ref,
                idempotency_key or f"key-{run_ref}",
                payload,
                NOW_TEXT,
                evidence_digest,
                NOW_TEXT,
                NOW_TEXT,
            ),
        )
        db.execute(
            "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
            (
                f"claim-{run_ref}",
                run_ref,
                f"lease-{run_ref}",
                1,
                operation,
                effect_digest or digest({"run_ref": run_ref}),
                f"effect-{run_ref}",
                NOW_TEXT,
            ),
        )
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                f"receipt-{run_ref}",
                f"claim-{run_ref}",
                "completed",
                evidence_digest,
                NOW_TEXT,
            ),
        )


def _authorize_evaluation(path: Path, evaluation: SkillEvaluationV2) -> None:
    for role in ("evaluator", "verifier"):
        _terminal_receipt(
            path,
            run_ref=str(getattr(evaluation, f"{role}_execution_identity")),
            evidence_digest=str(getattr(evaluation, f"{role}_evidence_digest")),
            operation=f"skill.evaluation.{role}-v2",
            effect_digest=evaluation_evidence_effect_digest(evaluation, role=role),
        )


def _authorize_review(
    path: Path,
    revision_digest: str,
    evaluation_digest: str,
    *,
    reviewer_ref: str,
    reviewer_evidence_digest: str,
    reviewer_execution_identity: str,
    approved: bool,
    reason: str,
) -> None:
    _terminal_receipt(
        path,
        run_ref=reviewer_execution_identity,
        evidence_digest=reviewer_evidence_digest,
        operation="skill.review.verify-v2",
        effect_digest=review_evidence_effect_digest(
            revision_digest,
            evaluation_digest,
            reviewer_ref=reviewer_ref,
            reviewer_execution_identity=reviewer_execution_identity,
            reviewer_evidence_digest=reviewer_evidence_digest,
            approved=approved,
            reason=reason,
        ),
    )
def _revision(package_digest: str, *, scope_ref: str = "project-a") -> PersonalSkillRevision:
    return PersonalSkillRevision(
        skill_id="zekam-arastirma-uygulama",
        name="zekam-arastirma-uygulama",
        description="Kanıtlı araştırmayı güvenli uygulamaya dönüştürür.",
        version=1,
        scope_kind=SkillScopeKind.PROJECT,
        scope_ref=scope_ref,
        package_digest=package_digest,
        author_ref="builder-a",
        trigger_terms=("araştır", "research"),
        non_trigger_terms=("şiir",),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_id", 7),
        ("name", None),
        ("description", ["not", "text"]),
        ("scope_ref", {"project": "a"}),
        ("author_ref", ("builder",)),
    ],
)
def test_revision_rejects_non_text_identity_fields(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "skill_id": "zekam-arastirma-uygulama",
        "name": "zekam-arastirma-uygulama",
        "description": "description",
        "version": 1,
        "scope_kind": SkillScopeKind.PROJECT,
        "scope_ref": "project-a",
        "package_digest": digest("package"),
        "author_ref": "builder",
        "trigger_terms": ("research",),
    }
    arguments[field] = value

    with pytest.raises(ValidationFailed, match="must be text"):
        PersonalSkillRevision(**arguments)  # type: ignore[arg-type]


def _origins(operational: Path) -> tuple[SkillOriginEvidence, SkillOriginEvidence]:
    success = digest("success-receipt")
    correction = digest("correction-receipt")
    _terminal_receipt(operational, run_ref="success-run", evidence_digest=success)
    _terminal_receipt(operational, run_ref="correction-run", evidence_digest=correction)
    return (
        SkillOriginEvidence(
            SkillOriginKind.VERIFIED_SUCCESS,
            success,
            "work-success",
            "success-run",
        ),
        SkillOriginEvidence(
            SkillOriginKind.USER_CORRECTION,
            correction,
            "work-correction",
            "correction-run",
            artifact_revision_digest=digest("accepted-revision"),
            user_ref="user-explicit-correction",
        ),
    )


def test_learning_v1_to_v2_migration_is_digest_bound_and_backed_up(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = (tmp_path / "learning-v1.db").resolve()
    learning.parent.mkdir(exist_ok=True)
    restrict_private_tree(learning.parent)
    with sqlite3.connect(learning) as db:
        db.executescript(_SCHEMA_V1)
        db.execute("insert into learning_schema values(1,1,?)", (SCHEMA_V1_DIGEST,))
    restrict_private_file(learning)
    store = SQLiteLocalLearning(learning, operational_path=operational)
    backup = (tmp_path / "backups" / "learning-v1.db").resolve()
    plan = store.migration_plan_v2(backup)
    same_name_elsewhere = (tmp_path / "other-backups" / backup.name).resolve()
    other_plan = store.migration_plan_v2(same_name_elsewhere)

    assert plan["migration_required"] is True
    assert plan["plan_digest"] != other_plan["plan_digest"]
    with pytest.raises(PolicyViolation, match="plan digest"):
        store.migrate_v1_to_v2(
            same_name_elsewhere, authorized_plan_digest=str(plan["plan_digest"])
        )
    assert not same_name_elsewhere.exists()
    with pytest.raises(PolicyViolation, match="plan digest"):
        store.migrate_v1_to_v2(backup, authorized_plan_digest=digest("wrong"))
    backup.parent.mkdir()
    with sqlite3.connect(learning) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    restrict_private_file(backup)
    receipt = store.migrate_v1_to_v2(
        backup, authorized_plan_digest=str(plan["plan_digest"])
    )
    replay = store.migrate_v1_to_v2(
        backup, authorized_plan_digest=str(plan["plan_digest"])
    )

    assert receipt["source_schema_digest"] == SCHEMA_V1_DIGEST
    assert receipt["source_content_digest"] == receipt["backup_content_digest"]
    assert replay == receipt
    assert receipt["target_schema_digest"] == SCHEMA_DIGEST
    assert backup.is_file()
    with sqlite3.connect(learning) as db:
        assert db.execute("select version,schema_digest from learning_schema").fetchone() == (
            2,
            SCHEMA_DIGEST,
        )
        assert db.execute("pragma integrity_check").fetchone() == ("ok",)


def test_learning_v2_rejects_schema_valid_content_different_backup(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = (tmp_path / "learning-v1.db").resolve()
    learning.parent.mkdir(exist_ok=True)
    restrict_private_tree(learning.parent)
    with sqlite3.connect(learning) as db:
        db.executescript(_SCHEMA_V1)
        db.execute("insert into learning_schema values(1,1,?)", (SCHEMA_V1_DIGEST,))
    restrict_private_file(learning)
    backup = (tmp_path / "backups" / "learning-v1.db").resolve()
    backup.parent.mkdir()
    with sqlite3.connect(learning) as source, sqlite3.connect(backup) as target:
        source.backup(target)
        target.execute(
            "insert into hygiene_proposal values(?,?,?,?,?)",
            (
                digest("different-backup"),
                digest("subject"),
                "stale",
                NOW_TEXT,
                canonical_json(
                    {
                        "schema": "fixture",
                        "subject": digest("subject"),
                        "finding": "stale",
                    }
                ),
            ),
        )
    restrict_private_file(backup)

    store = SQLiteLocalLearning(learning, operational_path=operational)
    with pytest.raises(PolicyViolation, match="does not match source"):
        store.migration_plan_v2(backup)


def test_success_correction_evaluation_activation_discovery_and_outcome(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    package_digest = digest("pilot-package")
    revision_digest = lifecycle.propose_revision(
        _revision(package_digest), _origins(operational), now=NOW
    )
    allowed = ((SkillScopeKind.PROJECT, "project-a"),)
    with pytest.raises(PolicyViolation, match="revision/package binding"):
        lifecycle.record_invocation_state(
            revision_digest,
            package_digest=package_digest,
            work_ref="inactive-work",
            run_ref="inactive-run",
            agent_ref="agent-a",
            client_id="codex",
            harness_digest=digest("inactive-harness"),
            state=SkillInvocationState.DISCOVERED,
            previous_invocation_digest=None,
            allowed_scopes=allowed,
            now=NOW,
        )
    with pytest.raises(PolicyViolation, match="authorized/registered"):
        lifecycle.discover(
            "research",
            allowed_scopes=((SkillScopeKind.PROJECT, "unregistered-secret-project"),),
        )
    with pytest.raises(ValidationFailed, match="contradict"):
        SkillEvaluationV2(
            revision_digest,
            digest("contradictory-plan"),
            "improved",
            5,
            "evaluator-x",
            "verifier-x",
            digest("evaluator-x-evidence"),
            digest("verifier-x-evidence"),
            "evaluator-x-process",
            "verifier-x-process",
            {"quality": {"baseline": 1.0, "candidate": 0.0}},
            {"method": "exact", "sample_size": 5},
        )

    equal_evaluation = SkillEvaluationV2(
        revision_digest,
        digest("equal-plan"),
        "equal",
        5,
        "evaluator-a",
        "verifier-a",
        digest("evaluator-a-evidence"),
        digest("verifier-a-evidence"),
        "evaluator-a-process",
        "verifier-a-process",
        {"quality": {"baseline": 1.0, "candidate": 1.0}},
        {"method": "exact", "sample_size": 5},
    )
    _authorize_evaluation(operational, equal_evaluation)
    equal = lifecycle.record_evaluation(equal_evaluation, now=NOW)
    assert (
        lifecycle.record_evaluation(equal_evaluation, now=NOW + dt.timedelta(seconds=1))
        == equal
    )
    _authorize_review(
        operational,
        revision_digest,
        equal,
        reviewer_ref="reviewer-a",
        reviewer_evidence_digest=digest("reviewer-a-evidence"),
        reviewer_execution_identity="reviewer-a-process",
        approved=True,
        reason="Equal results are retained, not activated.",
    )
    equal_review = lifecycle.record_review(
        revision_digest,
        equal,
        reviewer_ref="reviewer-a",
        reviewer_evidence_digest=digest("reviewer-a-evidence"),
        reviewer_execution_identity="reviewer-a-process",
        approved=True,
        reason="Equal results are retained, not activated.",
        now=NOW,
    )
    assert (
        lifecycle.record_review(
            revision_digest,
            equal,
            reviewer_ref="reviewer-a",
            reviewer_evidence_digest=digest("reviewer-a-evidence"),
            reviewer_execution_identity="reviewer-a-process",
            approved=True,
            reason="Equal results are retained, not activated.",
            now=NOW + dt.timedelta(seconds=1),
        )
        == equal_review
    )
    with pytest.raises(PolicyViolation, match="passing v2 evaluation"):
        lifecycle.append_activation(
            revision_digest,
            action="active",
            expected_previous_event_digest=None,
            authorization_job_id="missing-activation-job",
            now=NOW,
        )

    improved_evaluation = SkillEvaluationV2(
        revision_digest,
        digest("improved-plan"),
        "improved",
        6,
        "evaluator-b",
        "verifier-b",
        digest("evaluator-b-evidence"),
        digest("verifier-b-evidence"),
        "evaluator-b-process",
        "verifier-b-process",
        {"quality": {"baseline": 0.5, "candidate": 0.9}},
        {"method": "wilson", "sample_size": 6},
    )
    _authorize_evaluation(operational, improved_evaluation)
    improved = lifecycle.record_evaluation(improved_evaluation, now=NOW)
    _authorize_review(
        operational,
        revision_digest,
        improved,
        reviewer_ref="reviewer-b",
        reviewer_evidence_digest=digest("reviewer-b-evidence"),
        reviewer_execution_identity="reviewer-b-process",
        approved=True,
        reason="Independent deterministic fixture review passed.",
    )
    review = lifecycle.record_review(
        revision_digest,
        improved,
        reviewer_ref="reviewer-b",
        reviewer_evidence_digest=digest("reviewer-b-evidence"),
        reviewer_execution_identity="reviewer-b-process",
        approved=True,
        reason="Independent deterministic fixture review passed.",
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="operational authorization"):
        lifecycle.append_activation(
            revision_digest,
            action="active",
            expected_previous_event_digest=None,
            authorization_job_id="missing-passing-activation-job",
            now=NOW,
        )
    activation_effect = activation_effect_digest(
        revision_digest,
        improved,
        review,
        action="active",
        expected_previous_event_digest=None,
    )
    runtime = SQLiteLocalRuntimeStore(operational)
    activation_job, created = runtime.enqueue(
        idempotency_key="skill-activation-honest-runtime",
        payload={"operation": "skill.activate-v2", "effect": activation_effect},
        available_at=NOW_TEXT,
    )
    assert created is True
    activation_work = runtime.claim_next(
        owner_id="activation-test-worker",
        owner_pid=1234,
        owner_token="activation-test-token",
        lease_seconds=30,
        supported_operations=("skill.activate-v2",),
        job_id=activation_job.id,
        now=NOW_TEXT,
    )
    assert activation_work is not None
    activation_claim, claim_created = runtime.claim_effect(
        activation_work,
        operation="skill.activate-v2",
        effect_digest=activation_effect,
        idempotency_key=f"job:{activation_job.id}:skill-activation",
        now=NOW_TEXT,
    )
    assert claim_created is True
    activation = lifecycle.append_activation(
        revision_digest,
        action="active",
        expected_previous_event_digest=None,
        authorization_job_id=activation_job.id,
        now=NOW,
    )
    assert lifecycle.discover("research", allowed_scopes=allowed)["items"] == []
    with pytest.raises(PolicyViolation, match="not visible"):
        lifecycle.inspect_revision(revision_digest, allowed_scopes=allowed)
    with pytest.raises(PolicyViolation, match="active authorized package"):
        lifecycle.require_active_package(package_digest, allowed_scopes=allowed)
    activation_evidence = digest(
        {"activation_event_digest": activation, "effect_digest": activation_effect}
    )
    runtime.record_receipt(
        activation_claim,
        status="completed",
        evidence_digest=activation_evidence,
        now=NOW_TEXT,
    )
    runtime.finish(
        activation_work,
        state="completed",
        evidence_digest=activation_evidence,
        now=NOW_TEXT,
    )
    assert (
        lifecycle.append_activation(
            revision_digest,
            action="active",
            expected_previous_event_digest=None,
            authorization_job_id=activation_job.id,
            now=NOW + dt.timedelta(seconds=1),
        )
        == activation
    )
    assert lifecycle.require_active_package(
        package_digest, allowed_scopes=allowed
    )["activation_event_digest"] == activation
    with pytest.raises(ConcurrencyConflict):
        lifecycle.append_activation(
            revision_digest,
            action="revoked",
            expected_previous_event_digest=None,
            authorization_job_id=activation_job.id,
            now=NOW,
        )
    discovery = lifecycle.discover("Araştır ve research uygula", allowed_scopes=allowed)
    discovered_items = cast(list[dict[str, object]], discovery["items"])
    assert [item["revision_digest"] for item in discovered_items] == [revision_digest]
    assert discovered_items[0]["body_loaded"] is False
    assert lifecycle.discover("şiir araştır", allowed_scopes=allowed)["abstained"] is True
    assert lifecycle.discover(
        "research", allowed_scopes=((SkillScopeKind.PROJECT, "project-b"),)
    )["items"] == []
    inspected = lifecycle.inspect_revision(revision_digest, allowed_scopes=allowed)
    inspected_origins = cast(list[dict[str, object]], inspected["origins"])
    assert {item["origin_kind"] for item in inspected_origins} == {
        "verified_success",
        "user_correction",
    }

    previous: str | None = None
    for state in (
        SkillInvocationState.DISCOVERED,
        SkillInvocationState.SELECTED,
        SkillInvocationState.LOADED,
        SkillInvocationState.INVOKED,
        SkillInvocationState.COMPLETED,
    ):
        if state is SkillInvocationState.SELECTED:
            assert previous is not None
            with pytest.raises(ConcurrencyConflict, match="predecessor drift"):
                lifecycle.record_invocation_state(
                    revision_digest,
                    package_digest=package_digest,
                    work_ref="work-use",
                    run_ref="run-use",
                    agent_ref="agent-a",
                    client_id="opencode",
                    harness_digest=digest("different-harness"),
                    state=state,
                    previous_invocation_digest=previous,
                    allowed_scopes=allowed,
                    now=NOW,
                )
        previous = lifecycle.record_invocation_state(
            revision_digest,
            package_digest=package_digest,
            work_ref="work-use",
            run_ref="run-use",
            agent_ref="agent-a",
            client_id="codex",
            harness_digest=digest("harness-a"),
            state=state,
            previous_invocation_digest=previous,
            allowed_scopes=allowed,
            now=NOW,
        )
    assert previous is not None
    outcome_evidence = digest("output-evidence")
    with pytest.raises(PolicyViolation, match="terminal receipt"):
        lifecycle.record_outcome(
            previous,
            status="verified-success",
            grader_kind="artifact",
            verifier_ref="verifier-outcome",
            evidence_digest=outcome_evidence,
            now=NOW,
        )
    _terminal_receipt(
        operational,
        run_ref="run-use",
        evidence_digest=outcome_evidence,
        operation="skill.outcome.verify-v2",
        effect_digest=outcome_verification_effect_digest(
            previous,
            status="verified-success",
            grader_kind="artifact",
            verifier_ref="verifier-outcome",
            evidence_digest=outcome_evidence,
        ),
    )
    outcome = lifecycle.record_outcome(
        previous,
        status="verified-success",
        grader_kind="artifact",
        verifier_ref="verifier-outcome",
        evidence_digest=outcome_evidence,
        now=NOW,
    )
    assert activation.startswith("sha256:")
    assert outcome.startswith("sha256:")

    revoke_effect = activation_effect_digest(
        revision_digest,
        improved,
        review,
        action="revoked",
        expected_previous_event_digest=activation,
    )
    _terminal_receipt(
        operational,
        run_ref="revoke-run",
        evidence_digest=digest("revoke-terminal-evidence"),
        operation="skill.activate-v2",
        effect_digest=revoke_effect,
    )
    lifecycle.append_activation(
        revision_digest,
        action="revoked",
        expected_previous_event_digest=activation,
        authorization_job_id="revoke-run",
        now=NOW,
    )
    assert lifecycle.discover("research", allowed_scopes=allowed)["items"] == []
    with pytest.raises(PolicyViolation, match="revision/package binding"):
        lifecycle.record_invocation_state(
            revision_digest,
            package_digest=package_digest,
            work_ref="work-use",
            run_ref="run-use",
            agent_ref="agent-a",
            client_id="codex",
            harness_digest=digest("harness-a"),
            state=SkillInvocationState.UNVERIFIED,
            previous_invocation_digest=previous,
            allowed_scopes=allowed,
            now=NOW,
        )


def test_evaluation_execution_alias_resolves_to_one_canonical_job_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    revision_digest = lifecycle.propose_revision(
        _revision(digest("alias-package")), _origins(operational), now=NOW
    )
    evaluation = SkillEvaluationV2(
        revision_digest,
        digest("alias-plan"),
        "improved",
        5,
        "alias-evaluator",
        "alias-verifier",
        digest("alias-evaluator-evidence"),
        digest("alias-verifier-evidence"),
        "alias-evaluator-execution",
        "alias-verifier-execution",
        {"quality": {"baseline": 0.0, "candidate": 1.0}},
        {"method": "exact", "sample_size": 5},
    )
    for role, job_id in (("evaluator", "alias-job-a"), ("verifier", "alias-job-b")):
        _terminal_receipt(
            operational,
            run_ref=job_id,
            idempotency_key=str(getattr(evaluation, f"{role}_execution_identity")),
            evidence_digest=str(getattr(evaluation, f"{role}_evidence_digest")),
            operation=f"skill.evaluation.{role}-v2",
            effect_digest=evaluation_evidence_effect_digest(evaluation, role=role),
        )
    assert lifecycle.record_evaluation(evaluation, now=NOW).startswith("sha256:")

    ambiguous = SkillEvaluationV2(
        revision_digest,
        digest("ambiguous-plan"),
        "improved",
        5,
        "ambiguous-evaluator",
        "ambiguous-verifier",
        digest("ambiguous-evaluator-evidence"),
        digest("ambiguous-verifier-evidence"),
        "ambiguous-job-ref",
        "unambiguous-verifier-ref",
        {"quality": {"baseline": 0.0, "candidate": 1.0}},
        {"method": "exact", "sample_size": 5},
    )
    evaluator_effect = evaluation_evidence_effect_digest(ambiguous, role="evaluator")
    for job_id, key in (
        ("ambiguous-job-ref", "unrelated-key"),
        ("different-job-id", "ambiguous-job-ref"),
    ):
        _terminal_receipt(
            operational,
            run_ref=job_id,
            idempotency_key=key,
            evidence_digest=ambiguous.evaluator_evidence_digest,
            operation="skill.evaluation.evaluator-v2",
            effect_digest=evaluator_effect,
        )
    with pytest.raises(PolicyViolation, match="exact completed terminal receipt"):
        lifecycle.record_evaluation(ambiguous, now=NOW)


def test_explicit_user_request_can_create_candidate_without_fake_effect_receipt(
    tmp_path: Path,
) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    request_digest = digest("exact-user-task-document")
    origin = SkillOriginEvidence(
        SkillOriginKind.USER_REQUEST,
        request_digest,
        "user-task",
        "proposal-run",
        user_ref="local-user-explicit-request",
    )

    revision_digest = lifecycle.propose_revision(
        _revision(digest("requested-package")),
        (origin,),
        explicit_request_evidence_digest=request_digest,
        now=NOW,
    )
    replay_digest = lifecycle.propose_revision(
        _revision(digest("requested-package")),
        (origin,),
        explicit_request_evidence_digest=request_digest,
        now=NOW + dt.timedelta(days=1),
    )
    changed_origin = SkillOriginEvidence(
        SkillOriginKind.USER_REQUEST,
        request_digest,
        "different-user-task",
        "proposal-run",
        user_ref="local-user-explicit-request",
    )
    with pytest.raises(PolicyViolation, match="origin replay drift"):
        lifecycle.propose_revision(
            _revision(digest("requested-package")),
            (changed_origin,),
            explicit_request_evidence_digest=request_digest,
            now=NOW + dt.timedelta(days=2),
        )

    assert replay_digest == revision_digest
    with sqlite3.connect(learning) as db:
        assert db.execute(
            "select origin_kind,evidence_digest from skill_origin_v2"
        ).fetchone() == ("user_request", request_digest)


def test_proposal_scope_rejection_happens_before_runtime_enqueue(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    request_digest = digest("unregistered-scope-request")
    revision = _revision(digest("package"), scope_ref="not-registered")
    origin = SkillOriginEvidence(
        SkillOriginKind.USER_REQUEST,
        request_digest,
        "user-task",
        "proposal-run",
        user_ref="local-user-explicit-request",
    )

    with pytest.raises(PolicyViolation, match="authorized/registered"):
        _apply_proposal_with_runtime(
            lifecycle,
            tmp_path.resolve(),
            revision,
            (origin,),
            request_evidence_digest=request_digest,
            plan_digest=digest("proposal-plan"),
        )

    with sqlite3.connect(operational) as db:
        assert db.execute("select count(*) from local_job").fetchone() == (0,)
        assert db.execute("select count(*) from local_outbox").fetchone() == (0,)
        assert db.execute("select count(*) from local_recovery_case").fetchone() == (0,)


def test_blocked_evaluation_persists_without_fabricated_comparison(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    request_digest = digest("blocked-evaluation-request")
    revision = lifecycle.propose_revision(
        _revision(digest("blocked-package")),
        (
            SkillOriginEvidence(
                SkillOriginKind.USER_REQUEST,
                request_digest,
                "user-task",
                "blocked-run",
                user_ref="local-user-explicit-request",
            ),
        ),
        explicit_request_evidence_digest=request_digest,
        now=NOW,
    )

    blocked_evaluation = SkillEvaluationV2(
        revision,
        digest("blocked-plan"),
        "blocked",
        0,
        "evaluator-blocked",
        "verifier-blocked",
        digest("blocked-evaluator-evidence"),
        digest("blocked-verifier-evidence"),
        "blocked-evaluator-process",
        "blocked-verifier-process",
        {},
        {"reason": "model-not-authorized", "sample_size": 0},
    )
    _authorize_evaluation(operational, blocked_evaluation)
    evaluation = lifecycle.record_evaluation(blocked_evaluation, now=NOW)

    assert evaluation.startswith("sha256:")


def test_discovery_paginates_more_than_256_authorized_records(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    package_digest = digest("package")
    with sqlite3.connect(learning) as db:
        for index in range(300):
            revision_digest = digest(f"revision-{index}")
            evaluation_digest = digest(f"evaluation-{index}")
            review_digest = digest(f"review-{index}")
            authorization_job = f"pagination-activation-{index}"
            effect = activation_effect_digest(
                revision_digest,
                evaluation_digest,
                review_digest,
                action="active",
                expected_previous_event_digest=None,
            )
            receipt_evidence = digest(f"pagination-receipt-{index}")
            _terminal_receipt(
                operational,
                run_ref=authorization_job,
                evidence_digest=receipt_evidence,
                operation="skill.activate-v2",
                effect_digest=effect,
            )
            body = {
                "description": f"Research helper {index}",
                "trigger_terms": ["research"],
                "non_trigger_terms": [],
            }
            db.execute(
                "insert into skill_revision_v2 values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_digest,
                    f"skill-{index}",
                    f"skill-{index}",
                    1,
                    "project",
                    "project-a",
                    package_digest,
                    "builder",
                    "candidate",
                    NOW_TEXT,
                    canonical_json(body),
                ),
            )
            activation_body = {
                "schema": "zekam-personal-skill-activation-event/v2",
                "revision_digest": revision_digest,
                "skill_id": f"skill-{index}",
                "scope": {"kind": "project", "ref": "project-a"},
                "action": "active",
                "previous_event_digest": None,
                "evaluation_digest": evaluation_digest,
                "review_digest": review_digest,
                "authorization_job_id": authorization_job,
                "authorization_claim_id": f"claim-{authorization_job}",
                "authorization_receipt_evidence_digest": receipt_evidence,
                "created_at": NOW_TEXT,
                "grants_authority": False,
            }
            db.execute(
                "insert into skill_activation_event_v2 values(?,?,?,?,?,?,?,?,?)",
                (
                    digest(activation_body),
                    revision_digest,
                    f"skill-{index}",
                    "project",
                    "project-a",
                    "active",
                    None,
                    NOW_TEXT,
                    canonical_json(activation_body),
                ),
            )
    lifecycle = SQLiteSkillLifecycle(learning, operational)
    allowed = ((SkillScopeKind.PROJECT, "project-a"),)
    first = lifecycle.discover("research", allowed_scopes=allowed, maximum=50)
    second = lifecycle.discover(
        "research", allowed_scopes=allowed, maximum=50, after=str(first["next_after"])
    )

    first_items = cast(list[dict[str, object]], first["items"])
    second_items = cast(list[dict[str, object]], second["items"])
    assert len(first_items) == len(second_items) == 50
    assert first["has_more"] is second["has_more"] is True
    assert {item["revision_digest"] for item in first_items}.isdisjoint(
        {item["revision_digest"] for item in second_items}
    )


def test_skill_lifecycle_rejects_missing_unique_cas_index(tmp_path: Path) -> None:
    operational = _operational(tmp_path / "operational.db")
    learning = _learning(tmp_path, operational)
    with sqlite3.connect(learning) as db:
        db.execute("drop index skill_activation_successor_v2")

    lifecycle = SQLiteSkillLifecycle(learning, operational)
    with pytest.raises(PolicyViolation, match="learning schema v2 required"):
        lifecycle.status()
