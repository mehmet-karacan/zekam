from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import benchmark_effect_digest, benchmark_verifier_effect_digest
from zekam.domain.optimization import (
    MetricAggregation,
    MetricDirection,
    MetricRole,
    MetricSpec,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
)
from zekam.infrastructure.sqlite import local_learning, local_model_benchmark
from zekam.infrastructure.sqlite.local_improvement import (
    AttemptReservation,
    EvaluationReceipt,
    ImprovementCandidate,
    ImprovementChangeClass,
    OperationalExecutionReceipt,
    SQLiteLocalImprovementStore,
    _document,
    _instant,
    _normalized_identity,
    _parse_time,
    _safe,
    _text,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
PROPOSER = UUID("10000000-0000-0000-0000-000000000001")
BUILDER = UUID("20000000-0000-0000-0000-000000000002")
VERIFIER = UUID("30000000-0000-0000-0000-000000000003")
REVIEWER = "reviewer-independent"
CANDIDATE_ID = UUID("40000000-0000-0000-0000-000000000004")
SUITE_DIGEST = digest("suite")
TASK_DIGEST = digest("task")
FIXTURE_DIGEST = digest("fixture")
PROMPT_DIGEST = digest("prompt")
HIDDEN_KEY_DIGEST = digest("hidden-key")
GRADER_DIGEST = digest("grader")
POLICY_DIGEST = digest("policy")
FIXTURE_REGISTRY_DIGEST = digest("fixture-registry")
EVALUATION_PLAN_DIGEST = digest(
    {
        "suite_digest": SUITE_DIGEST,
        "task_digest": TASK_DIGEST,
        "fixture_digest": FIXTURE_DIGEST,
        "prompt_digest": PROMPT_DIGEST,
        "hidden_key_digest": HIDDEN_KEY_DIGEST,
        "grader_digest": GRADER_DIGEST,
        "policy_digest": POLICY_DIGEST,
        "fixture_registry_digest": FIXTURE_REGISTRY_DIGEST,
        "repetitions": 5,
    }
)


def _benchmark_body(
    *,
    quality: float,
    latency: float,
    marker: str,
    overrides: dict[str, dict[str, float]] | None = None,
    confidence: tuple[float, float] | None = None,
) -> dict[str, object]:
    def metric(value: float) -> dict[str, float]:
        return {"mean": value, "median": value, "p95": value, "variance": 0.0}

    if confidence is None:
        z = 1.959963984540054
        denominator = 1 + z * z / 5
        centre = (1.0 + z * z / 10) / denominator
        margin = z * math.sqrt((z * z / 20) / 5) / denominator
        confidence = (max(0.0, centre - margin), min(1.0, centre + margin))
    body: dict[str, object] = {
        "schema": "zekam-benchmark-aggregate/v1",
        "approved": True,
        "unsafe": False,
        "tested_model_id": "tested-model",
        "verifier_model_id": "independent-model",
        "verifier_execution_identity": "local-verifier",
        "verifier_provenance_digest": digest({"verifier": marker}),
        "quality": metric(quality),
        "reliability": metric(0.9),
        "latency_ms": metric(latency),
        "cost": metric(0.001),
        "token_count": metric(100.0),
        "correctness": metric(quality),
        "evidence_citation": metric(1.0),
        "structured_format": metric(1.0),
        "safety": metric(1.0),
        "token_efficiency": metric(1.0),
        "tool_correctness": metric(1.0),
        "recovery": metric(1.0),
        "human_correction": metric(0.0),
        "evidence_digest": digest({"evidence": marker}),
        "trial_count": 5,
        "pass_rate": 1.0,
        "confidence_interval": {"low": confidence[0], "high": confidence[1]},
        "confidence_95": [confidence[0], confidence[1]],
    }
    for group, value in (overrides or {}).items():
        body[group] = value
    return body


def _sources(tmp_path: Path) -> tuple[Path, Path, str, dict[str, str]]:
    root = tmp_path / "sources"
    root.mkdir(mode=0o700)
    learning = root / "learning.sqlite3"
    benchmark = root / "benchmark.sqlite3"
    signature_body = {
        "schema": "zekam-local-failure-signature/v1",
        "signature_key": "failure-one",
        "category": "runtime",
    }
    signature = digest(signature_body)
    card_body = {
        "schema": "zekam-local-failure-card/v1",
        "signature_digest": signature,
        "symptom": "bounded failure",
        "environment": "disposable",
        "root_cause": "verified cause",
        "unsafe_workaround": "skip verification",
        "safe_remediation": "bounded change",
        "verification": "independent benchmark",
        "source_refs": ["receipt-a", "receipt-b"],
        "author_ref": "card-author",
        "reviewed_by": "card-reviewer",
        "created_at": NOW.isoformat(),
    }
    card = digest(card_body)
    with sqlite3.connect(learning) as db:
        db.executescript(local_learning._SCHEMA)
        db.execute(
            "insert into learning_schema values(1,?,?)",
            (local_learning.SCHEMA_VERSION, local_learning.SCHEMA_DIGEST),
        )
        db.execute(
            "insert into failure_signature values(?,?,?,?,?)",
            (signature, "failure-one", "runtime", NOW.isoformat(), canonical_json(signature_body)),
        )
        for index in (1, 2):
            evidence = digest({"receipt": index})
            body = {
                "schema": "zekam-local-failure-occurrence/v1",
                "signature_digest": signature,
                "occurrence_key": "failure-one",
                "evidence_digest": evidence,
                "run_ref": f"run-{index}",
                "failure_category": "runtime",
                "observed_at": NOW.isoformat(),
            }
            db.execute(
                "insert into failure_occurrence values(?,?,?,?,?,?)",
                (
                    digest(body),
                    signature,
                    evidence,
                    f"run-{index}",
                    NOW.isoformat(),
                    canonical_json(body),
                ),
            )
        db.execute(
            "insert into failure_card values(?,?,?,?,?,?)",
            (
                card,
                signature,
                "card-author",
                "card-reviewer",
                NOW.isoformat(),
                canonical_json(card_body),
            ),
        )
    aggregates: dict[str, str] = {}
    with sqlite3.connect(benchmark) as db:
        db.executescript(local_model_benchmark._SCHEMA)
        for name, quality, latency in (
            ("baseline", 0.6, 100.0),
            ("improved", 0.8, 90.0),
            ("regressed", 0.8, 110.0),
            ("cost-regressed", 0.8, 90.0),
            ("variance-regressed", 0.8, 90.0),
            ("confidence-corrupt", 0.8, 90.0),
            ("missing-receipt", 0.8, 90.0),
            ("wrong-plan", 0.8, 90.0),
        ):
            trial_latencies = (
                [80.0, 85.0, 90.0, 95.0, 100.0] if name == "variance-regressed" else [latency] * 5
            )
            trial_cost = 0.002 if name == "cost-regressed" else 0.001
            overrides = {
                "latency_ms": {
                    "mean": sum(trial_latencies) / 5,
                    "median": sorted(trial_latencies)[2],
                    "p95": max(trial_latencies),
                    "variance": sum(
                        (item - sum(trial_latencies) / 5) ** 2 for item in trial_latencies
                    )
                    / 5,
                },
                "cost": {
                    "mean": trial_cost,
                    "median": trial_cost,
                    "p95": trial_cost,
                    "variance": 0.0,
                },
            }
            benchmark_body: dict[str, object] = _benchmark_body(
                quality=quality,
                latency=latency,
                marker=name,
                overrides=overrides,
                confidence=(0.0, 0.01) if name == "confidence-corrupt" else None,
            )
            value = digest(benchmark_body)
            aggregates[name] = value
            plan_digest = digest({"plan": name})
            binding = {
                "schema": "zekam-benchmark-plan-input-binding/v1",
                "plan_digest": plan_digest,
                "suite_digest": SUITE_DIGEST,
                "task_digest": TASK_DIGEST,
                "fixture_digest": FIXTURE_DIGEST,
                "prompt_digest": PROMPT_DIGEST,
                "hidden_key_digest": HIDDEN_KEY_DIGEST,
                "grader_digest": GRADER_DIGEST,
            }
            policy_digest = digest("other-policy") if name == "wrong-plan" else POLICY_DIGEST
            plan_body = {
                "schema": "zekam-benchmark-plan/v1",
                "model_id": "tested-model",
                "suite_digest": SUITE_DIGEST,
                "inventory_digest": digest({"inventory": name}),
                "policy_digest": policy_digest,
                "fixture_registry_digest": FIXTURE_REGISTRY_DIGEST,
                "repetitions": 5,
                "remote_execution": False,
                "input_binding_digest": digest(binding),
            }
            db.execute(
                "insert into contract values(?,?,?)",
                ("plan-binding", plan_digest, canonical_json(binding)),
            )
            db.execute(
                "insert into benchmark_plan values(?,?,?,?,?,?)",
                (
                    f"plan-{name}",
                    plan_digest,
                    SUITE_DIGEST,
                    "tested-model",
                    5,
                    canonical_json(plan_body),
                ),
            )
            for repetition in range(1, 6):
                response_digest = digest({"response": name, "repetition": repetition})
                evidence_digest = digest({"verdict": name, "repetition": repetition})
                claim_ids: dict[str, str] = {}
                for phase, model_id, result_digest in (
                    ("tested", "tested-model", response_digest),
                    ("verifier", "independent-model", evidence_digest),
                ):
                    claim_id = f"{name}-{phase}-{repetition}"
                    claim_ids[phase] = claim_id
                    effect_digest = (
                        benchmark_effect_digest(plan_digest, FIXTURE_DIGEST, repetition)
                        if phase == "tested"
                        else benchmark_verifier_effect_digest(
                            plan_digest,
                            FIXTURE_DIGEST,
                            repetition,
                            model_id,
                            response_digest,
                        )
                    )
                    claim_body = {
                        "schema": "zekam-benchmark-call-claim/v1",
                        "claim_id": claim_id,
                        "plan_digest": plan_digest,
                        "phase": phase,
                        "fixture_digest": FIXTURE_DIGEST,
                        "repetition": repetition,
                        "model_id": model_id,
                        "effect_digest": effect_digest,
                    }
                    db.execute(
                        "insert into call_claim values(?,?,?,?,?,?,?,?)",
                        (
                            claim_id,
                            plan_digest,
                            phase,
                            FIXTURE_DIGEST,
                            repetition,
                            model_id,
                            effect_digest,
                            canonical_json(claim_body),
                        ),
                    )
                    receipt_body = {
                        "schema": "zekam-benchmark-call-receipt/v1",
                        "claim_id": claim_id,
                        "status": "completed",
                        "result_digest": result_digest,
                        "failure_category": None,
                        "evidence_digest": evidence_digest,
                    }
                    if not (name == "missing-receipt" and phase == "verifier"):
                        db.execute(
                            "insert into call_receipt values(?,?,?,?,?,?,?)",
                            (
                                digest(receipt_body),
                                claim_id,
                                "completed",
                                result_digest,
                                None,
                                evidence_digest,
                                canonical_json(receipt_body),
                            ),
                        )
                trial_body = {
                    "schema": "zekam-benchmark-trial/v1",
                    "fixture_digest": FIXTURE_DIGEST,
                    "repetition": repetition,
                    "status": "passed",
                    "parse_ok": True,
                    "format_ok": True,
                    "evidence_ok": True,
                    "verifier_approved": True,
                    "quality": quality,
                    "reliability": 0.9,
                    "latency_ms": int(trial_latencies[repetition - 1]),
                    "input_tokens": 50,
                    "output_tokens": 50,
                    "retry_count": 0,
                    "human_corrections": 0,
                    "estimated_cost": trial_cost,
                    "actual_cost": trial_cost,
                    "response_digest": response_digest,
                    "evidence_digest": evidence_digest,
                    "failure_category": None,
                    "tool_correctness": 1.0,
                    "recovery": 1.0,
                }
                db.execute(
                    "insert into benchmark_trial values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        digest(trial_body),
                        f"plan-{name}",
                        claim_ids["tested"],
                        claim_ids["verifier"],
                        FIXTURE_DIGEST,
                        repetition,
                        "passed",
                        evidence_digest,
                        response_digest,
                        canonical_json(trial_body),
                    ),
                )
            db.execute(
                "insert into benchmark_aggregate values(?,?,?)",
                (value, f"plan-{name}", canonical_json(benchmark_body)),
            )
    learning.chmod(0o600)
    benchmark.chmod(0o600)
    return learning, benchmark, card, aggregates


def _specs() -> tuple[MetricSpec, ...]:
    return (
        MetricSpec(
            "latency_ms.mean",
            "latency",
            "ms",
            MetricDirection.MINIMIZE,
            MetricRole.HARD_GUARD,
            "wp11",
            target_value=80.0,
            regression_tolerance=0.0,
            aggregation=MetricAggregation.MEAN,
        ),
        MetricSpec(
            "quality.mean",
            "quality",
            "ratio",
            MetricDirection.MAXIMIZE,
            MetricRole.PRIMARY,
            "wp11",
            target_value=0.95,
            minimum_meaningful_delta=0.05,
            regression_tolerance=0.0,
            aggregation=MetricAggregation.MEAN,
        ),
    )


def _candidate(
    card: str,
    baseline: str,
    *,
    candidate_id: UUID = CANDIDATE_ID,
    change_class: ImprovementChangeClass = ImprovementChangeClass.AUTO_SAFE,
    max_iterations: int = 2,
    wall_clock_seconds: int = 60,
    patch_marker: str = "patch-one",
    objective: str = "rebuild stale projection",
    observed_problem: str = "verified repeated local failure",
    hypothesis: str = "atomic rebuild prevents stale reads",
    allowed_resources: tuple[str, ...] = ("local-cache",),
) -> ImprovementCandidate:
    return ImprovementCandidate(
        candidate_id,
        objective,
        observed_problem,
        card,
        baseline,
        hypothesis,
        digest(patch_marker),
        change_class,
        allowed_resources,
        _specs(),
        ("no-latency-regression",),
        EVALUATION_PLAN_DIGEST,
        max_iterations,
        20,
        1000,
        10_000,
        wall_clock_seconds,
        "restore prior immutable generation",
        str(PROPOSER),
        "revision-one",
        NOW,
    )


def _manifest(candidate: ImprovementCandidate) -> ValidatorAssetManifest:
    return ValidatorAssetManifest(
        UUID("50000000-0000-0000-0000-000000000005"),
        candidate.candidate_id,
        candidate.evaluation_plan_digest,
        candidate.source_revision,
        BUILDER,
        VERIFIER,
        (
            ValidatorAsset(
                "asset-one",
                "tests/validator.py",
                digest("validator-bytes"),
                ValidatorAssetRole.TEST,
            ),
        ),
        NOW + dt.timedelta(seconds=1),
    )


def _store(tmp_path: Path) -> tuple[SQLiteLocalImprovementStore, str, dict[str, str]]:
    learning, benchmark, card, aggregates = _sources(tmp_path)
    store = SQLiteLocalImprovementStore(
        (tmp_path / "improvement.sqlite3").resolve(), learning.resolve(), benchmark.resolve()
    )
    store.bootstrap()
    return store, card, aggregates


def _evaluated(
    store: SQLiteLocalImprovementStore,
    candidate: ImprovementCandidate,
    after: str,
    *,
    finished_at: dt.datetime = NOW + dt.timedelta(seconds=3),
    actual_calls: int = 10,
) -> str:
    store.propose(candidate)
    manifest = _manifest(candidate)
    assert store.freeze_validators(candidate, manifest) == store.freeze_validators(
        candidate, manifest
    )
    reservation = AttemptReservation(candidate.candidate_digest, 10, 500, 5000)
    claim = store.claim_attempt(
        reservation,
        now=NOW + dt.timedelta(seconds=2),
    )
    assert claim == store.claim_attempt(reservation, now=NOW + dt.timedelta(seconds=2))
    receipt = store.complete_evaluation(
        candidate,
        claim,
        after,
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=actual_calls,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=finished_at,
    )
    assert receipt == store.complete_evaluation(
        candidate,
        claim,
        after,
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=actual_calls,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=finished_at,
    )
    return receipt.evaluation_digest


def _operation(
    store: SQLiteLocalImprovementStore,
    candidate: ImprovementCandidate,
    evaluation: str,
    operation: str,
    *,
    start: int,
    finish: int,
    status: str = "completed",
    executor_ref: str = str(BUILDER),
    verifier_ref: str = str(VERIFIER),
    external_effect_count: int = 0,
    persist: bool = True,
) -> OperationalExecutionReceipt:
    with sqlite3.connect(store.path) as db:
        if operation == "shadow":
            policy_digest = evaluation
        elif operation == "canary":
            row = db.execute(
                "select rollout_digest from rollout_receipt "
                "where candidate_digest=? and stage='shadow'",
                (candidate.candidate_digest,),
            ).fetchone()
            policy_digest = digest("missing-shadow") if row is None else row[0]
        elif operation == "activation":
            row = db.execute(
                "select review_digest from improvement_review where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            policy_digest = digest("missing-review") if row is None else row[0]
        else:
            row = db.execute(
                "select activation_digest from improvement_activation where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            policy_digest = digest("missing-activation") if row is None else row[0]
    receipt = OperationalExecutionReceipt(
        operation,
        candidate.candidate_digest,
        evaluation,
        policy_digest,
        digest({"operation": operation, "start": start, "status": status}),
        executor_ref,
        verifier_ref,
        status,
        NOW + dt.timedelta(seconds=start),
        NOW + dt.timedelta(seconds=finish),
        external_effect_count,
    )
    if persist:
        store.claim_operation(receipt, now=NOW + dt.timedelta(seconds=start))
        store.record_runner_receipt(receipt, now=NOW + dt.timedelta(seconds=finish))
        store.verify_operation(receipt, approved=True, now=NOW + dt.timedelta(seconds=finish))
    return receipt


def test_auto_safe_full_chain_restart_rollback_and_learning_feedback(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    evaluation = _evaluated(store, candidate, aggregates["improved"])
    shadow = store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=_operation(store, candidate, evaluation, "shadow", start=4, finish=5),
        now=NOW + dt.timedelta(seconds=5),
    )
    canary = store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "canary",
        receipt=_operation(store, candidate, evaluation, "canary", start=6, finish=7),
        now=NOW + dt.timedelta(seconds=7),
    )
    review = store.review(
        candidate.candidate_digest,
        evaluation,
        REVIEWER,
        approved=True,
        now=NOW + dt.timedelta(seconds=8),
    )
    activation = store.activate_auto(
        candidate.candidate_digest,
        evaluation,
        review,
        receipt=_operation(store, candidate, evaluation, "activation", start=9, finish=10),
        now=NOW + dt.timedelta(seconds=10),
    )
    with sqlite3.connect(store.path) as db:
        activation_receipt = db.execute(
            "select receipt_digest from improvement_activation where activation_digest=?",
            (activation,),
        ).fetchone()[0]
    with pytest.raises(PolicyViolation, match="outcome receipt"):
        store.record_learning_feedback(
            candidate.candidate_digest,
            evaluation,
            activation,
            now=NOW + dt.timedelta(seconds=11),
        )
    with pytest.raises(PolicyViolation, match="outcome receipt"):
        store.record_learning_feedback(
            candidate.candidate_digest,
            evaluation,
            activation_receipt,
            now=NOW + dt.timedelta(seconds=10),
        )
    feedback = store.record_learning_feedback(
        candidate.candidate_digest,
        evaluation,
        activation_receipt,
        now=NOW + dt.timedelta(seconds=11),
    )
    rollback_receipt = _operation(store, candidate, evaluation, "rollback", start=12, finish=13)
    rollback = store.rollback(
        activation,
        receipt=rollback_receipt,
        now=NOW + dt.timedelta(seconds=13),
    )
    assert all(
        value.startswith("sha256:")
        for value in (shadow, canary, review, activation, feedback, rollback)
    )
    assert shadow == store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=_operation(store, candidate, evaluation, "shadow", start=4, finish=5),
        now=NOW + dt.timedelta(seconds=5),
    )
    assert review == store.review(
        candidate.candidate_digest,
        evaluation,
        REVIEWER,
        approved=True,
        now=NOW + dt.timedelta(seconds=8),
    )
    assert activation == store.activate_auto(
        candidate.candidate_digest,
        evaluation,
        review,
        receipt=_operation(store, candidate, evaluation, "activation", start=9, finish=10),
        now=NOW + dt.timedelta(seconds=10),
    )
    assert feedback == store.record_learning_feedback(
        candidate.candidate_digest,
        evaluation,
        activation_receipt,
        now=NOW + dt.timedelta(seconds=11),
    )
    assert rollback == store.rollback(
        activation,
        receipt=rollback_receipt,
        now=NOW + dt.timedelta(seconds=13),
    )
    reopened = SQLiteLocalImprovementStore(store.path, store.learning_path, store.benchmark_path)
    assert reopened.audit() == {
        "improvement_candidate": 1,
        "validator_manifest": 1,
        "attempt_claim": 1,
        "improvement_evaluation": 1,
        "operational_execution_claim": 4,
        "operational_runner_receipt": 4,
        "operational_execution_receipt": 4,
        "rollout_receipt": 2,
        "improvement_review": 1,
        "improvement_activation": 1,
        "rollback_receipt": 1,
        "learning_feedback": 1,
    }


@pytest.mark.parametrize(
    "change_class",
    [
        ImprovementChangeClass.REVIEW_REQUIRED,
        ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,
        ImprovementChangeClass.PROHIBITED_AUTONOMOUS,
    ],
)
def test_non_auto_safe_never_auto_activates(
    tmp_path: Path, change_class: ImprovementChangeClass
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"], change_class=change_class)
    evaluation = _evaluated(store, candidate, aggregates["improved"])
    store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=_operation(store, candidate, evaluation, "shadow", start=4, finish=5),
        now=NOW + dt.timedelta(seconds=5),
    )
    store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "canary",
        receipt=_operation(store, candidate, evaluation, "canary", start=6, finish=7),
        now=NOW + dt.timedelta(seconds=7),
    )
    review = store.review(
        candidate.candidate_digest,
        evaluation,
        REVIEWER,
        approved=True,
        now=NOW + dt.timedelta(seconds=8),
    )
    with pytest.raises(PolicyViolation, match="AUTO_SAFE"):
        store.activate_auto(
            candidate.candidate_digest,
            evaluation,
            review,
            receipt=_operation(store, candidate, evaluation, "activation", start=9, finish=10),
            now=NOW + dt.timedelta(seconds=10),
        )


def test_any_metric_regression_is_not_improvement(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    receipt = store.complete_evaluation(
        candidate,
        claim,
        aggregates["regressed"],
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=NOW + dt.timedelta(seconds=3),
    )
    assert receipt.state == "regressed"
    with pytest.raises(PolicyViolation, match="Operational claim"):
        store.record_rollout(
            candidate.candidate_digest,
            receipt.evaluation_digest,
            "shadow",
            receipt=_operation(
                store, candidate, receipt.evaluation_digest, "shadow", start=4, finish=5
            ),
            now=NOW + dt.timedelta(seconds=5),
        )


def test_plateau_is_terminal_and_mismatched_benchmark_contract_rejects(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    plateau = store.complete_evaluation(
        candidate,
        claim,
        aggregates["improved"],
        artifact_before_digest=digest("same"),
        artifact_after_digest=digest("same"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=NOW + dt.timedelta(seconds=3),
    )
    assert plateau.state == "plateau"
    with pytest.raises(PolicyViolation, match="Stopped"):
        store.claim_attempt(
            AttemptReservation(candidate.candidate_digest, 1, 100, 1000),
            now=NOW + dt.timedelta(seconds=4),
        )

    other_root = tmp_path / "other"
    other_root.mkdir(mode=0o700)
    other, other_card, other_aggregates = _store(other_root)
    other_candidate = _candidate(other_card, other_aggregates["baseline"])
    with pytest.raises(PolicyViolation, match="plan drift"):
        _evaluated(other, other_candidate, other_aggregates["wrong-plan"])


def test_budget_deadline_timeout_and_terminal_stop_are_enforced(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"], wall_clock_seconds=5)
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    with pytest.raises(PolicyViolation, match="budget"):
        store.claim_attempt(
            AttemptReservation(candidate.candidate_digest, 21, 500, 5000),
            now=NOW + dt.timedelta(seconds=2),
        )
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    receipt = store.complete_evaluation(
        candidate,
        claim,
        aggregates["improved"],
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=NOW + dt.timedelta(seconds=6),
    )
    assert receipt.state == "timeout"
    with pytest.raises(PolicyViolation, match="Stopped"):
        store.claim_attempt(
            AttemptReservation(candidate.candidate_digest, 1, 100, 1000),
            now=NOW + dt.timedelta(seconds=3),
        )


def test_actual_usage_over_reservation_terminalizes_budget_exceeded(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 9, 499, 4999),
        now=NOW + dt.timedelta(seconds=2),
    )
    receipt = store.complete_evaluation(
        candidate,
        claim,
        aggregates["improved"],
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=NOW + dt.timedelta(seconds=3),
    )
    assert receipt.state == "budget-exceeded"


@pytest.mark.parametrize(
    ("aggregate_name", "actual_cost_micros", "expected_guard"),
    (
        ("cost-regressed", 10_000, "cost.mean"),
        ("variance-regressed", 5_000, "latency_ms.variance"),
    ),
)
def test_candidate_metric_subset_cannot_bypass_mandatory_guards(
    tmp_path: Path,
    aggregate_name: str,
    actual_cost_micros: int,
    expected_guard: str,
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, actual_cost_micros),
        now=NOW + dt.timedelta(seconds=2),
    )
    evaluation = store.complete_evaluation(
        candidate,
        claim,
        aggregates[aggregate_name],
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=actual_cost_micros,
        finished_at=NOW + dt.timedelta(seconds=3),
    )
    assert evaluation.state == "regressed"
    with sqlite3.connect(store.path) as db:
        body = db.execute(
            "select body_json from improvement_evaluation where evaluation_digest=?",
            (evaluation.evaluation_digest,),
        ).fetchone()[0]
    assert expected_guard in json.loads(body)["mandatory_regressions"]


@pytest.mark.parametrize(
    ("aggregate_name", "message"),
    (
        ("confidence-corrupt", "confidence reconciliation"),
        ("missing-receipt", "terminal receipt missing"),
    ),
)
def test_isolated_evaluation_rejects_corrupt_confidence_or_missing_terminal_receipt(
    tmp_path: Path, aggregate_name: str, message: str
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    with pytest.raises(PolicyViolation, match=message):
        store.complete_evaluation(
            candidate,
            claim,
            aggregates[aggregate_name],
            artifact_before_digest=digest("before"),
            artifact_after_digest=digest("after"),
            actual_provider_calls=10,
            actual_tokens=500,
            actual_cost_micros=5000,
            finished_at=NOW + dt.timedelta(seconds=3),
        )


def test_caller_counters_cannot_override_immutable_execution_receipts(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    with pytest.raises(PolicyViolation, match="immutable benchmark receipts"):
        store.complete_evaluation(
            candidate,
            claim,
            aggregates["improved"],
            artifact_before_digest=digest("before"),
            artifact_after_digest=digest("after"),
            actual_provider_calls=9,
            actual_tokens=500,
            actual_cost_micros=5000,
            finished_at=NOW + dt.timedelta(seconds=3),
        )


def test_ambiguous_operational_receipts_are_durable_and_require_recovery(
    tmp_path: Path,
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    evaluation = _evaluated(store, candidate, aggregates["improved"])
    ambiguous = _operation(
        store, candidate, evaluation, "shadow", start=4, finish=5, status="ambiguous"
    )
    ambiguous_digest = store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=ambiguous,
        now=NOW + dt.timedelta(seconds=5),
    )
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        store.record_runner_receipt(
            replace(ambiguous, evidence_digest=digest("different-evidence")),
            now=NOW + dt.timedelta(seconds=5),
        )
    assert ambiguous_digest == store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=ambiguous,
        now=NOW + dt.timedelta(seconds=5),
    )
    with pytest.raises(PolicyViolation, match="predecessor/policy"):
        store.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "canary",
            receipt=_operation(store, candidate, evaluation, "canary", start=6, finish=7),
            now=NOW + dt.timedelta(seconds=7),
        )
    recovered_shadow = _operation(store, candidate, evaluation, "shadow", start=6, finish=7)
    shadow_digest = store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=recovered_shadow,
        now=NOW + dt.timedelta(seconds=7),
    )
    recovered_canary = _operation(store, candidate, evaluation, "canary", start=8, finish=9)

    def finish_canary() -> str:
        return store.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "canary",
            receipt=recovered_canary,
            now=NOW + dt.timedelta(seconds=9),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(finish_canary), pool.submit(finish_canary))
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert shadow_digest.startswith("sha256:")
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "select status from operational_execution_receipt order by verified_at,receipt_digest"
        ).fetchall() == [("ambiguous",), ("completed",), ("completed",)]
    review = store.review(
        candidate.candidate_digest,
        evaluation,
        REVIEWER,
        approved=True,
        now=NOW + dt.timedelta(seconds=10),
    )
    ambiguous_activation = _operation(
        store,
        candidate,
        evaluation,
        "activation",
        start=11,
        finish=12,
        status="ambiguous",
    )
    with pytest.raises(PolicyViolation, match="recovered completed"):
        store.activate_auto(
            candidate.candidate_digest,
            evaluation,
            review,
            receipt=ambiguous_activation,
            now=NOW + dt.timedelta(seconds=12),
        )
    activation = store.activate_auto(
        candidate.candidate_digest,
        evaluation,
        review,
        receipt=_operation(store, candidate, evaluation, "activation", start=13, finish=14),
        now=NOW + dt.timedelta(seconds=14),
    )
    recovery_required = _operation(
        store,
        candidate,
        evaluation,
        "rollback",
        start=15,
        finish=16,
        status="recovery-required",
    )
    with pytest.raises(PolicyViolation, match="recovered completed"):
        store.rollback(
            activation,
            receipt=recovery_required,
            now=NOW + dt.timedelta(seconds=16),
        )
    rollback = store.rollback(
        activation,
        receipt=_operation(store, candidate, evaluation, "rollback", start=17, finish=18),
        now=NOW + dt.timedelta(seconds=18),
    )
    reopened = SQLiteLocalImprovementStore(store.path, store.learning_path, store.benchmark_path)
    counts = reopened.audit()
    assert counts["operational_execution_receipt"] == 7
    assert activation.startswith("sha256:") and rollback.startswith("sha256:")


def test_operational_receipt_identity_type_and_effect_boundaries_fail_closed(
    tmp_path: Path,
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    evaluation = _evaluated(store, candidate, aggregates["improved"])
    with pytest.raises(PolicyViolation, match="independent verifier"):
        _operation(
            store,
            candidate,
            evaluation,
            "shadow",
            start=4,
            finish=5,
            executor_ref=str(BUILDER),
            verifier_ref=str(BUILDER),
        )
    with pytest.raises(PolicyViolation, match="production effect"):
        _operation(
            store,
            candidate,
            evaluation,
            "shadow",
            start=4,
            finish=5,
            external_effect_count=1,
        )
    with pytest.raises(PolicyViolation, match="identity/order"):
        _operation(
            store,
            candidate,
            evaluation,
            "shadow",
            start=4,
            finish=5,
            verifier_ref="different-verifier",
        )
    with pytest.raises(ValidationFailed, match="verified operational receipt"):
        store.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "shadow",
            success=True,
            now=NOW + dt.timedelta(seconds=5),
        )
    forged = _operation(
        store,
        candidate,
        evaluation,
        "shadow",
        start=4,
        finish=5,
        persist=False,
    )
    with pytest.raises(PolicyViolation, match="durable runner/verifier"):
        store.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "shadow",
            receipt=forged,
            now=NOW + dt.timedelta(seconds=5),
        )
    valid = _operation(store, candidate, evaluation, "shadow", start=4, finish=5)
    for changes in (
        {"status": None},
        {"external_effect_count": True},
        {"finished_at": dt.datetime(2026, 9, 4, 12, 0, 5)},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            replace(valid, **changes)  # type: ignore[arg-type]


def test_pre_effect_claim_runner_and_independent_verifier_are_separate_restart_safe_gates(
    tmp_path: Path,
) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    evaluation = _evaluated(store, candidate, aggregates["improved"])
    receipt = _operation(
        store,
        candidate,
        evaluation,
        "shadow",
        start=4,
        finish=5,
        persist=False,
    )
    assert store.claim_operation(receipt, now=NOW + dt.timedelta(seconds=4)) == receipt.claim_digest
    competing = replace(
        receipt,
        started_at=NOW + dt.timedelta(seconds=5),
        finished_at=NOW + dt.timedelta(seconds=6),
    )
    with pytest.raises(ConcurrencyConflict, match="pending claim"):
        store.claim_operation(competing, now=NOW + dt.timedelta(seconds=5))
    with pytest.raises(PolicyViolation, match="durable runner/verifier"):
        store.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "shadow",
            receipt=receipt,
            now=NOW + dt.timedelta(seconds=5),
        )
    reopened = SQLiteLocalImprovementStore(store.path, store.learning_path, store.benchmark_path)
    assert (
        reopened.record_runner_receipt(receipt, now=NOW + dt.timedelta(seconds=5))
        == receipt.runner_receipt_digest
    )
    with pytest.raises(PolicyViolation, match="durable runner/verifier"):
        reopened.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "shadow",
            receipt=receipt,
            now=NOW + dt.timedelta(seconds=5),
        )
    rejected = reopened.verify_operation(
        receipt,
        approved=False,
        now=NOW + dt.timedelta(seconds=5),
    )
    with pytest.raises(PolicyViolation, match="receipt binding"):
        reopened.record_rollout(
            candidate.candidate_digest,
            evaluation,
            "shadow",
            receipt=receipt,
            now=NOW + dt.timedelta(seconds=5),
        )
    with sqlite3.connect(store.path) as db:
        assert db.execute("select count(*) from rollout_receipt").fetchone()[0] == 0
        assert (
            db.execute(
                "select status from operational_execution_receipt where receipt_digest=?",
                (rejected,),
            ).fetchone()[0]
            == "failed"
        )


def test_novelty_drift_stale_validator_and_self_review_fail_closed(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    assert store.propose(candidate) == (candidate.candidate_digest, True)
    assert store.propose(candidate) == (candidate.candidate_digest, False)
    drift = _candidate(
        card,
        aggregates["baseline"],
        candidate_id=candidate.candidate_id,
        patch_marker="other-patch",
    )
    with pytest.raises(ConcurrencyConflict, match="drift"):
        store.propose(drift)
    rephrased = _candidate(
        card,
        aggregates["baseline"],
        candidate_id=UUID("40000000-0000-0000-0000-000000000099"),
        objective="REBUILD, stale projection!",
        observed_problem="Verified repeated LOCAL failure.",
        hypothesis="Stale reads are prevented by an atomic rebuild.",
    )
    with pytest.raises(ConcurrencyConflict, match="drift"):
        store.propose(rephrased)
    semantic_paraphrase = _candidate(
        card,
        aggregates["baseline"],
        candidate_id=UUID("40000000-0000-0000-0000-000000000098"),
        objective="Restore query consistency",
        observed_problem="Transactional reads returned obsolete results",
        hypothesis="Transactional reconstruction eliminates outdated query results",
        patch_marker="different-patch",
    )
    with pytest.raises(ConcurrencyConflict, match="drift"):
        store.propose(semantic_paraphrase)
    source_revision_bypass = replace(
        semantic_paraphrase,
        candidate_id=UUID("40000000-0000-0000-0000-000000000097"),
        source_revision="source-two",
    )
    with pytest.raises(ConcurrencyConflict, match="drift"):
        store.propose(source_revision_bypass)
    with pytest.raises(PolicyViolation, match="change class"):
        replace(candidate, allowed_resources=("root-instruction",))
    assert (
        replace(
            candidate,
            change_class=ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,
            allowed_resources=("root-instruction",),
        ).change_class
        is ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED
    )
    with pytest.raises(PolicyViolation, match="plan binding"):
        store.propose(replace(candidate, evaluation_plan_digest=digest("wrong-plan")))
    stale = ValidatorAssetManifest(
        _manifest(candidate).manifest_id,
        candidate.candidate_id,
        candidate.evaluation_plan_digest,
        "wrong-revision",
        BUILDER,
        VERIFIER,
        _manifest(candidate).assets,
        NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(PolicyViolation, match="drift"):
        store.freeze_validators(candidate, stale)
    store.freeze_validators(candidate, _manifest(candidate))
    claim = store.claim_attempt(
        AttemptReservation(candidate.candidate_digest, 10, 500, 5000),
        now=NOW + dt.timedelta(seconds=2),
    )
    evaluation = store.complete_evaluation(
        candidate,
        claim,
        aggregates["improved"],
        artifact_before_digest=digest("before"),
        artifact_after_digest=digest("after"),
        actual_provider_calls=10,
        actual_tokens=500,
        actual_cost_micros=5000,
        finished_at=NOW + dt.timedelta(seconds=3),
    ).evaluation_digest
    store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "shadow",
        receipt=_operation(store, candidate, evaluation, "shadow", start=4, finish=5),
        now=NOW + dt.timedelta(seconds=5),
    )
    store.record_rollout(
        candidate.candidate_digest,
        evaluation,
        "canary",
        receipt=_operation(store, candidate, evaluation, "canary", start=6, finish=7),
        now=NOW + dt.timedelta(seconds=7),
    )
    with pytest.raises(PolicyViolation, match="independent"):
        store.review(
            candidate.candidate_digest,
            evaluation,
            str(BUILDER),
            approved=True,
            now=NOW + dt.timedelta(seconds=8),
        )


def test_concurrent_attempt_claim_has_one_winner_and_append_only_audit(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"], max_iterations=1)
    store.propose(candidate)
    store.freeze_validators(candidate, _manifest(candidate))
    reservation = AttemptReservation(candidate.candidate_digest, 1, 100, 1000)

    def claim() -> str:
        return store.claim_attempt(reservation, now=NOW + dt.timedelta(seconds=2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = []
        for future in (pool.submit(claim), pool.submit(claim)):
            try:
                outcomes.append(future.result())
            except (PolicyViolation, sqlite3.IntegrityError):
                outcomes.append("rejected")
    assert len(set(outcomes)) == 1
    assert outcomes[0].startswith("sha256:")
    with (
        sqlite3.connect(store.path) as db,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        db.execute("update improvement_candidate set proposer_ref='changed'")


def test_corrupt_reopen_and_wrong_types_fail_closed(tmp_path: Path) -> None:
    store, card, aggregates = _store(tmp_path)
    candidate = _candidate(card, aggregates["baseline"])
    store.propose(candidate)
    with sqlite3.connect(store.path) as db:
        db.execute("drop trigger improvement_candidate_no_update")
        db.execute("update improvement_candidate set body_json='{}'")
    with pytest.raises(PolicyViolation, match=r"schema|digest"):
        store.audit()
    with pytest.raises(ValidationFailed):
        AttemptReservation(candidate.candidate_digest, True, 1, 1)
    with pytest.raises(ValidationFailed):
        _candidate(card, aggregates["baseline"], max_iterations=True)


@pytest.mark.parametrize(
    ("validator", "value"),
    (
        (_text, None),
        (_text, ""),
        (_text, "password=not-safe"),
        (_safe, None),
        (_safe, "not safe"),
        (_safe, "token=not-safe"),
    ),
)
def test_bounded_text_helpers_reject_untyped_or_sensitive_values(
    validator: object, value: object
) -> None:
    with pytest.raises(ValidationFailed):
        validator(value, "field")  # type: ignore[operator]


def test_semantic_identity_normalizes_order_stopwords_and_inflection() -> None:
    assert _normalized_identity("Atomic rebuild prevents stale reads") == _normalized_identity(
        "Stale reads are prevented by an atomic rebuild"
    )
    assert _normalized_identity("Projection rebuilding") != _normalized_identity("Network timeout")


@pytest.mark.parametrize(
    "value",
    (None, "invalid", "2026-09-04T12:00:00"),
)
def test_stored_improvement_timestamp_rejects_type_format_and_naive(value: object) -> None:
    with pytest.raises(PolicyViolation):
        _parse_time(value)


@pytest.mark.parametrize("value", (None, dt.datetime(2026, 9, 4, 12)))
def test_new_improvement_timestamp_requires_aware_datetime(value: object) -> None:
    with pytest.raises(ValidationFailed):
        _instant(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    (None, "", '{"a":1,"a":2}', "[]", '{"b":1,"a":2}', "NaN"),
)
def test_stored_improvement_document_requires_bounded_canonical_unique_object(
    raw: object,
) -> None:
    with pytest.raises(PolicyViolation):
        _document(raw)


def test_evaluation_receipt_rejects_unknown_state() -> None:
    with pytest.raises(ValidationFailed):
        EvaluationReceipt(digest("evaluation"), "unknown", digest("progress"))


def test_store_paths_parent_and_database_identity_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        SQLiteLocalImprovementStore(
            Path("relative.db"),
            (tmp_path / "learning.db").resolve(),
            (tmp_path / "bench.db").resolve(),
        )

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    learning, benchmark, _, _ = _sources(private)
    database = (private / "improvement.sqlite3").resolve()
    store = SQLiteLocalImprovementStore(database, learning.resolve(), benchmark.resolve())
    store.bootstrap()
    database.chmod(0o644)
    with pytest.raises(PolicyViolation, match="identity"):
        store.audit()
