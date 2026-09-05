from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from tests.unit.test_operational_schema_upgrade import PROJECT_ID
from tests.unit.test_operational_schema_v3 import _source

from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    DeterministicLocalBenchmarkAdapter,
    LocalProcessBenchmarkAdapter,
    LocalProcessBenchmarkVerifier,
    default_fixture_file,
    load_fixture_registry,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import BenchmarkPlan, BenchmarkSuite, SuiteKind, VerifierIdentity
from zekam.infrastructure.sqlite import local_evidence_routing as routing_module
from zekam.infrastructure.sqlite.local_evidence_routing import (
    LocalRouteBinding,
    LocalRouteRequest,
    RouteEffectClaim,
    SQLiteLocalEvidenceRouter,
    route_execution_effect_digest,
    route_policy_stage_effect_digest,
)
from zekam.infrastructure.sqlite.local_model_benchmark import (
    LocalBenchmarkTask,
    LocalGraderContract,
    SQLiteLocalBenchmarkLab,
)
from zekam.infrastructure.sqlite.local_model_registry import (
    LocalDiscoverySnapshot,
    LocalModelCapabilityProfile,
    LocalModelIdentity,
    SQLiteLocalModelRegistry,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

_routing: Any = routing_module

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _private(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return root


def _aggregate(lab: SQLiteLocalBenchmarkLab, exact_id: str) -> str:
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite(
        f"suite-{exact_id.replace('/', '-')}", 1, SuiteKind.GENERAL, (fixture.fixture_digest,)
    )
    plan = BenchmarkPlan(
        exact_id,
        suite.suite_digest,
        digest("inventory"),
        digest("benchmark-policy"),
        registry.registry_digest,
    )
    grader = LocalGraderContract("exact-json", 1, digest("grader"), ("correctness",))
    task = LocalBenchmarkTask(
        f"task-{exact_id.replace('/', '-')}",
        1,
        fixture.fixture_digest,
        digest(f"prompt:{exact_id}"),
        digest(f"hidden:{exact_id}"),
        grader.grader_digest,
        5,
        10,
    )
    lab.register_contracts(task, grader, plan=plan, suite=suite)
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    tested = LocalProcessBenchmarkAdapter(
        exact_id,
        (sys.executable, str(process)),
        DeterministicLocalBenchmarkAdapter(default_fixture_file().parent),
        lambda *_: None,
        artifact_sink=lab,
    )
    verifier = LocalProcessBenchmarkVerifier(
        VerifierIdentity("independent-verifier", "local:independent", digest("verifier")),
        (sys.executable, str(process)),
        lambda *_: None,
        artifact_sink=lab,
    )
    service = BenchmarkExecutionService(lab, registry)
    plan_id, _ = service.execute(
        suite=suite, plan=plan, adapter=tested, verifier_adapter=verifier, claims=lab
    )
    service.aggregate(
        plan_id, plan=plan, suite=suite, tested_model_id=exact_id, verifier=verifier.verifier
    )
    with sqlite3.connect(lab.path) as db:
        row = db.execute(
            "select aggregate_digest from benchmark_aggregate where plan_id=?", (str(plan_id),)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _world(tmp_path: Path) -> tuple[SQLiteLocalEvidenceRouter, tuple[LocalRouteBinding, ...], str]:
    root = _private(tmp_path)
    operational = _source((root / "operational.db").resolve(), 3)
    with sqlite3.connect(operational) as db:
        db.execute("insert into local_runtime_config values(1,64)")
    operational.chmod(0o600)
    registry = SQLiteLocalModelRegistry((root / "models.db").resolve())
    registry.bootstrap()
    models = (
        LocalModelIdentity("anthropic", "model-b", "r1"),
        LocalModelIdentity("openai", "model-a", "r1"),
    )
    snapshot = LocalDiscoverySnapshot(
        "mac-device",
        "opencode",
        "1",
        digest("client"),
        True,
        models,
        NOW,
        NOW + dt.timedelta(hours=12),
    )
    registry.reconcile(snapshot)
    lab = SQLiteLocalBenchmarkLab((root / "benchmark.db").resolve(), (root / "artifacts").resolve())
    lab.bootstrap()
    bindings: list[LocalRouteBinding] = []
    for index, model in enumerate(models):
        health = registry.record_health(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=True,
            evidence_digest=digest(f"health:{index}"),
            now=NOW,
        )
        aggregate = _aggregate(lab, model.exact_id)
        profile = LocalModelCapabilityProfile(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            f"family-{index}",
            f"process-{index}",
            ("code",),
            ("text",),
            ("internal", "public"),
            ("code", "structured-output", "text"),
        )
        registry.record_capability_profile(profile, now=NOW)
        bindings.append(
            LocalRouteBinding(
                model.exact_id,
                snapshot.snapshot_digest,
                model.revision_fingerprint,
                health,
                aggregate,
                f"family-{index}",
                f"process-{index}",
                ("code",),
                ("text",),
                ("internal", "public"),
                ("code", "structured-output", "text"),
                NOW,
                NOW + dt.timedelta(hours=6),
            )
        )
    router = SQLiteLocalEvidenceRouter(
        (root / "routing.db").resolve(), registry.path, lab.path, operational
    )
    router.bootstrap()
    for binding in bindings:
        assert (
            router.register_candidate(binding, device_id="mac-device", client_id="opencode")
            == binding.candidate_digest
        )
    return router, tuple(bindings), snapshot.snapshot_digest


def _request(
    router: SQLiteLocalEvidenceRouter, policy: str, snapshot: str, **changes: object
) -> LocalRouteRequest:
    bind_current_epoch = (
        "evidence_epoch_digest" not in changes and "project_context_digest" not in changes
    )
    values: dict[str, object] = {
        "workload": "code",
        "modality": "text",
        "data_classification": "internal",
        "device_id": "mac-device",
        "client_id": "opencode",
        "allowed_provider_ids": ("anthropic", "openai"),
        "required_capabilities": ("code", "structured-output"),
        "policy_digest": policy,
        "inventory_snapshot_digest": snapshot,
        "evidence_epoch_digest": digest("epoch-1"),
        "project_id": PROJECT_ID,
        "source_snapshot_id": "snapshot",
        "project_context_digest": router.current_project_context_digest(PROJECT_ID, "snapshot"),
        "max_latency_ms": 100.0,
        "max_cost": 1.0,
    }
    values.update(changes)
    request = LocalRouteRequest(**values)  # type: ignore[arg-type]
    if bind_current_epoch:
        try:
            request = replace(
                request,
                evidence_epoch_digest=router.current_evidence_epoch_digest(request),
            )
        except PolicyViolation as exc:
            if "active policy missing" not in str(exc):
                raise
    return request


def _terminal_job(
    router: SQLiteLocalEvidenceRouter,
    *,
    operation: str,
    effect_digest: str,
    payload: dict[str, object],
    completed: bool,
) -> str:
    runtime = SQLiteLocalRuntimeStore(router.operational_path, existing_only=True)
    moment = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(seconds=5)
    job, created = runtime.enqueue(
        idempotency_key=f"{operation}:{effect_digest}",
        payload=payload,
    )
    if not created:
        return job.id
    work = runtime.claim_next(
        owner_id="wp12-worker",
        owner_pid=os.getpid(),
        owner_token=f"owner:{job.id}",
        lease_seconds=30,
        supported_operations=(operation,),
        job_id=job.id,
        now=moment.isoformat(),
    )
    assert work is not None
    claim, _ = runtime.claim_effect(
        work,
        operation=operation,
        effect_digest=effect_digest,
        idempotency_key=f"effect:{effect_digest}",
        now=(moment + dt.timedelta(seconds=1)).isoformat(),
    )
    evidence = digest({"claim_id": claim.id, "effect_digest": effect_digest})
    receipt_state: Literal["completed", "failed", "unknown"] = (
        "completed" if completed else "failed"
    )
    runtime.record_receipt(
        claim,
        status=receipt_state,
        evidence_digest=evidence,
        now=(moment + dt.timedelta(seconds=2)).isoformat(),
    )
    runtime.finish(
        work,
        state=cast(Literal["completed", "failed", "recovery-required"], receipt_state),
        evidence_digest=evidence,
        now=(moment + dt.timedelta(seconds=3)).isoformat(),
    )
    return job.id


def _activate(router: SQLiteLocalEvidenceRouter, bindings: tuple[LocalRouteBinding, ...]) -> str:
    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    for stage, evidence, actor in (
        ("offline-replay", digest("offline"), "offline-runner"),
        ("shadow", digest("shadow"), "shadow-runner"),
        ("canary", digest("canary"), "canary-runner"),
        ("independent-review", digest("review"), "independent-reviewer"),
        ("approval", digest("approval"), "local-policy-authority"),
    ):
        evidence_job = None
        if stage == "independent-review":
            evidence_job = _terminal_job(
                router,
                operation="model.route.policy-stage",
                effect_digest=route_policy_stage_effect_digest(
                    1, candidates, stage, evidence, actor
                ),
                payload={
                    "operation": "model.route.policy-stage",
                    "revision": 1,
                    "candidate_digests": list(candidates),
                    "stage": stage,
                    "evidence_digest": evidence,
                    "actor_id": actor,
                },
                completed=True,
            )
        router.record_policy_stage(
            1, candidates, stage, evidence, actor, evidence_job_id=evidence_job
        )
    candidate_artifact_digest, activation_effect = router.policy_activation_spec(
        1, candidates, "independent-reviewer"
    )
    activation_job = _terminal_job(
        router,
        operation="model.route.activate",
        effect_digest=activation_effect,
        payload={
            "operation": "model.route.activate",
            "policy_candidate_digest": candidate_artifact_digest,
            "authorization_review_digest": digest("review"),
            "authorization_approval_digest": digest("approval"),
            "authorization_actor_id": "local-policy-authority",
        },
        completed=True,
    )
    return router.activate_policy(
        1,
        candidates,
        offline_replay_digest=digest("offline"),
        shadow_digest=digest("shadow"),
        canary_digest=digest("canary"),
        review_digest=digest("review"),
        approval_digest=digest("approval"),
        reviewer_model_id="independent-reviewer",
        activation_job_id=activation_job,
    )


def test_evidence_route_replay_restart_feedback_and_exactly_once_failover(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    with pytest.raises(PolicyViolation, match="policy"):
        router.decide(_request(router, digest("not-active"), snapshot), now=NOW)
    policy = _activate(router, bindings)
    assert _activate(router, bindings) == policy
    request = _request(router, policy, snapshot)
    first = router.decide(request, now=NOW)
    assert first == router.decide(request, now=NOW)
    assert first["status"] == "selected"
    assert first["primary_id"] != first["fallback_id"]
    primary = str(first["primary_id"])
    fallback = str(first["fallback_id"])
    by_id = {item.exact_id: item for item in bindings}
    assert by_id[primary].family_id != by_id[fallback].family_id
    assert by_id[primary].execution_identity != by_id[fallback].execution_identity
    assert primary.partition("/")[0] != fallback.partition("/")[0]
    decision_digest = digest(first)
    claimed = router.claim_effect(decision_digest, "work:one")
    assert claimed.disposition == "fresh"
    effect = claimed.effect_digest
    assert router.claim_effect(decision_digest, "work:one") == RouteEffectClaim(effect, "replay")
    with pytest.raises(PolicyViolation, match="failed primary receipt"):
        router.failover_target(effect, primary)
    primary_job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=route_execution_effect_digest(effect, primary),
        payload={
            "operation": "model.route.execute",
            "route_effect_digest": effect,
            "route_decision_digest": decision_digest,
            "exact_id": primary,
        },
        completed=False,
    )
    router.record_outcome(effect, primary, execution_job_id=primary_job)
    assert router.claim_effect(decision_digest, "work:one") == RouteEffectClaim(effect, "terminal")
    assert router.failover_target(effect, primary) == fallback
    fallback_job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=route_execution_effect_digest(effect, fallback),
        payload={
            "operation": "model.route.execute",
            "route_effect_digest": effect,
            "route_decision_digest": decision_digest,
            "exact_id": fallback,
        },
        completed=True,
    )
    router.record_outcome(effect, fallback, execution_job_id=fallback_job)
    assert router.counts()["route_effect"] == 1
    restarted = SQLiteLocalEvidenceRouter(
        router.path, router.registry_path, router.benchmark_path, router.operational_path
    )
    second = restarted.decide(_request(router, policy, snapshot), now=NOW)
    assert any(item["outcome_evidence"] for item in second["candidates"])
    assert restarted.counts()["route_outcome"] == 2


@pytest.mark.parametrize("invalid", [None, "", 1])
def test_evidence_epoch_rejects_null_empty_and_wrong_type(invalid: object) -> None:
    with pytest.raises(ValidationFailed):
        LocalRouteRequest(
            "code",
            "text",
            "internal",
            "mac-device",
            "opencode",
            ("openai",),
            ("code",),
            digest("policy"),
            digest("snapshot"),
            cast(Any, invalid),
            PROJECT_ID,
            "snapshot",
            digest("project-context"),
            100.0,
            1.0,
        )


def test_evidence_epoch_is_source_derived_and_drift_fails_closed(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    request = _request(router, policy, snapshot)
    first = router.decide(request, now=NOW)
    assert first == router.decide(request, now=NOW)

    for forged in (digest("caller-a"), digest("caller-b")):
        with pytest.raises(PolicyViolation, match="evidence epoch"):
            router.decide(replace(request, evidence_epoch_digest=forged), now=NOW)
    assert router.counts()["route_decision"] == 1

    registry = SQLiteLocalModelRegistry(router.registry_path)
    binding = bindings[0]
    registry.record_health(
        binding.snapshot_digest,
        binding.exact_id,
        binding.revision_fingerprint,
        passed=False,
        evidence_digest=digest("durable-health-drift"),
        now=NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(PolicyViolation, match="evidence epoch"):
        router.decide(request, now=NOW + dt.timedelta(seconds=2))
    refreshed = _request(router, policy, snapshot)
    assert refreshed.evidence_epoch_digest != request.evidence_epoch_digest
    restarted = SQLiteLocalEvidenceRouter(
        router.path, router.registry_path, router.benchmark_path, router.operational_path
    )
    assert restarted.current_evidence_epoch_digest(refreshed) == refreshed.evidence_epoch_digest


def test_exact_model_identity_blocks_primary_and_reviewer_without_suffix_aliasing(
    tmp_path: Path,
) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    exact_id = bindings[0].exact_id
    backend_id = exact_id.partition("/")[2]
    exact = router.decide(
        _request(router, policy, snapshot, independent_from_model_id=exact_id), now=NOW
    )
    exact_row = next(item for item in exact["candidates"] if item["exact_id"] == exact_id)
    assert "independence-violation" in exact_row["reasons"]

    suffix = router.decide(
        _request(router, policy, snapshot, independent_from_model_id=backend_id), now=NOW
    )
    suffix_row = next(item for item in suffix["candidates"] if item["exact_id"] == exact_id)
    assert "independence-violation" not in suffix_row["reasons"]


def test_duplicate_exact_model_identity_cannot_enter_policy(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    duplicate = replace(bindings[0], expires_at=bindings[0].expires_at + dt.timedelta(minutes=1))
    router.register_candidate(duplicate, device_id="mac-device", client_id="opencode")
    candidates = tuple(
        sorted(
            (bindings[0].candidate_digest, duplicate.candidate_digest, bindings[1].candidate_digest)
        )
    )
    with pytest.raises(PolicyViolation, match="duplicate exact model identity"):
        router.policy_activation_spec(1, candidates, "independent-reviewer")


def test_hard_gates_staleness_policy_independence_and_corruption(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    with pytest.raises(PolicyViolation, match="lifecycle"):
        router.activate_policy(
            1,
            tuple(sorted(item.candidate_digest for item in bindings)),
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="independent-reviewer",
            activation_job_id="missing-activation-job",
        )
    policy = _activate(router, bindings)
    for changes, error in (
        ({"modality": "embedding"}, ValidationFailed),
        ({"data_classification": "secret"}, ValidationFailed),
        ({"local_only": False}, PolicyViolation),
        ({"max_cost": float("nan")}, ValidationFailed),
        ({"max_latency_ms": True}, ValidationFailed),
    ):
        with pytest.raises(error):
            _request(router, policy, snapshot, **changes)
    with pytest.raises(PolicyViolation, match="project context"):
        router.decide(
            _request(router, policy, snapshot, project_context_digest=digest("drift")), now=NOW
        )
    wrong_provider = router.decide(
        _request(
            router,
            policy,
            snapshot,
            allowed_provider_ids=("local",),
        ),
        now=NOW,
    )
    assert wrong_provider["status"] == "pending"
    assert all("provider-mismatch" in item["reasons"] for item in wrong_provider["candidates"])
    stale = router.decide(_request(router, policy, snapshot), now=NOW + dt.timedelta(hours=7))
    assert stale["status"] == "pending"
    assert all("benchmark-stale" in item["reasons"] for item in stale["candidates"])
    registry = SQLiteLocalModelRegistry(router.registry_path)
    failed_binding = bindings[0]
    registry.record_health(
        failed_binding.snapshot_digest,
        failed_binding.exact_id,
        failed_binding.revision_fingerprint,
        passed=False,
        evidence_digest=digest("later-failure"),
        now=NOW + dt.timedelta(seconds=1),
    )
    after_failure = router.decide(
        _request(router, policy, snapshot),
        now=NOW + dt.timedelta(seconds=2),
    )
    failed_row = next(
        item for item in after_failure["candidates"] if item["exact_id"] == failed_binding.exact_id
    )
    assert "availability-health-or-revision-stale" in failed_row["reasons"]
    changed = LocalDiscoverySnapshot(
        "mac-device",
        "opencode",
        "2",
        digest("client"),
        True,
        (LocalModelIdentity("openai", "model-a", "r2"),),
        NOW + dt.timedelta(minutes=1),
        NOW + dt.timedelta(hours=12),
    )
    registry.reconcile(changed)
    after_revision = router.decide(
        _request(router, policy, snapshot),
        now=NOW + dt.timedelta(minutes=1),
    )
    assert after_revision["status"] == "pending"
    assert all(
        "availability-health-or-revision-stale" in item["reasons"]
        for item in after_revision["candidates"]
    )
    with sqlite3.connect(router.path) as db:
        db.execute("drop trigger route_decision_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        router.counts()


def test_policy_reviewer_must_be_independent_from_tested_models(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    tested_reviewer = bindings[0].exact_id
    for stage, evidence, actor in (
        ("offline-replay", digest("offline"), "offline-runner"),
        ("shadow", digest("shadow"), "shadow-runner"),
        ("canary", digest("canary"), "canary-runner"),
        ("independent-review", digest("review"), tested_reviewer),
        ("approval", digest("approval"), "local-policy-authority"),
    ):
        evidence_job = None
        if stage == "independent-review":
            evidence_job = _terminal_job(
                router,
                operation="model.route.policy-stage",
                effect_digest=route_policy_stage_effect_digest(
                    1, candidates, stage, evidence, actor
                ),
                payload={
                    "operation": "model.route.policy-stage",
                    "revision": 1,
                    "candidate_digests": list(candidates),
                    "stage": stage,
                    "evidence_digest": evidence,
                    "actor_id": actor,
                },
                completed=True,
            )
        router.record_policy_stage(
            1, candidates, stage, evidence, actor, evidence_job_id=evidence_job
        )
    with pytest.raises(PolicyViolation, match="independent reviewer"):
        router.activate_policy(
            1,
            candidates,
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id=tested_reviewer,
            activation_job_id="missing-activation-job",
        )


def test_concurrent_decision_and_effect_replay_do_not_duplicate(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    request = _request(router, policy, snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = tuple(pool.map(lambda _: router.decide(request, now=NOW), range(2)))
    assert decisions[0] == decisions[1]
    value = digest(decisions[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        effects = tuple(pool.map(lambda _: router.claim_effect(value, "work:concurrent"), range(2)))
    assert effects[0].effect_digest == effects[1].effect_digest
    assert {item.disposition for item in effects} == {"fresh", "replay"}
    assert router.counts()["route_effect"] == 1


def test_binding_rejects_wrong_scope_duplicates_and_boolean_budget() -> None:
    common = {
        "exact_id": "openai/model",
        "snapshot_digest": digest("snapshot"),
        "revision_fingerprint": digest("revision"),
        "health_digest": digest("health"),
        "aggregate_digest": digest("aggregate"),
        "family_id": "family",
        "execution_identity": "process",
        "modalities": ("text",),
        "data_classifications": ("internal", "public"),
        "observed_at": NOW,
        "expires_at": NOW + dt.timedelta(hours=1),
    }
    with pytest.raises(ValidationFailed):
        LocalRouteBinding(
            workloads=("code", "code"),
            capabilities=("code",),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(PolicyViolation):
        LocalRouteBinding(
            workloads=("embedding",),
            capabilities=("embedding",),
            **common,  # type: ignore[arg-type]
        )


def test_caller_cannot_mint_candidate_activation_or_terminal_outcome(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    forged = replace(bindings[0], family_id="caller-invented-family")
    before = router.counts()
    with pytest.raises(PolicyViolation, match="capability profile"):
        router.register_candidate(forged, device_id="mac-device", client_id="opencode")
    assert router.counts() == before

    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    for stage, evidence, actor in (
        ("offline-replay", digest("offline"), "offline-runner"),
        ("shadow", digest("shadow"), "shadow-runner"),
        ("canary", digest("canary"), "canary-runner"),
        ("independent-review", digest("review"), "independent-reviewer"),
        ("approval", digest("approval"), "local-policy-authority"),
    ):
        evidence_job = None
        if stage == "independent-review":
            evidence_job = _terminal_job(
                router,
                operation="model.route.policy-stage",
                effect_digest=route_policy_stage_effect_digest(
                    1, candidates, stage, evidence, actor
                ),
                payload={
                    "operation": "model.route.policy-stage",
                    "revision": 1,
                    "candidate_digests": list(candidates),
                    "stage": stage,
                    "evidence_digest": evidence,
                    "actor_id": actor,
                },
                completed=True,
            )
        router.record_policy_stage(
            1, candidates, stage, evidence, actor, evidence_job_id=evidence_job
        )
    artifact, _ = router.policy_activation_spec(1, candidates, "independent-reviewer")
    forged_job = _terminal_job(
        router,
        operation="model.route.activate",
        effect_digest=digest("caller-invented-effect"),
        payload={
            "operation": "model.route.activate",
            "policy_candidate_digest": artifact,
            "authorization_review_digest": digest("review"),
            "authorization_approval_digest": digest("approval"),
            "authorization_actor_id": "local-policy-authority",
        },
        completed=True,
    )
    with pytest.raises(PolicyViolation, match="effect binding"):
        router.activate_policy(
            1,
            candidates,
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="independent-reviewer",
            activation_job_id=forged_job,
        )
    assert router.counts()["policy_revision"] == 0

    policy = _activate(router, bindings)
    decision = router.decide(_request(router, policy, snapshot), now=NOW)
    decision_digest = digest(decision)
    claimed = router.claim_effect(decision_digest, "work:forgery")
    primary = str(decision["primary_id"])
    wrong_job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=digest("unrelated-execution"),
        payload={
            "operation": "model.route.execute",
            "route_effect_digest": claimed.effect_digest,
            "route_decision_digest": decision_digest,
            "exact_id": primary,
        },
        completed=True,
    )
    with pytest.raises(PolicyViolation, match="effect binding"):
        router.record_outcome(claimed.effect_digest, primary, execution_job_id=wrong_job)
    assert router.counts()["route_outcome"] == 0

    stale_request = _request(
        router, policy, snapshot, evidence_epoch_digest=digest("project-source-drift")
    )
    with sqlite3.connect(router.operational_path) as db:
        db.execute(
            "insert into source_snapshot values('new-snapshot','source','revision:two',?,?,?,?)",
            (
                digest("new-tree"),
                digest("new-content"),
                digest("new-config"),
                "2026-09-05T12:00:00+00:00",
            ),
        )
    router.operational_path.chmod(0o600)
    with pytest.raises(PolicyViolation, match="project context"):
        router.decide(stale_request, now=NOW)


@pytest.mark.parametrize(
    "operation,error",
    [
        (lambda: routing_module._text(1, "value"), "invalid"),
        (lambda: _routing._instant("bad"), "timezone-aware"),
        (lambda: routing_module._parse_instant(1), "type drift"),
        (lambda: routing_module._parse_instant("2026-09-04T12:00:00"), "lacks timezone"),
        (lambda: routing_module._document(1), "size/type"),
        (lambda: routing_module._document('{"a":1,"a":2}'), "JSON invalid"),
        (lambda: routing_module._document('{"a": 1}'), "not canonical"),
        (lambda: RouteEffectClaim(digest("effect"), cast(Any, "invalid")), "disposition"),
    ],
)
def test_routing_scalar_document_and_claim_boundaries_fail_closed(
    operation: Callable[[], object], error: str
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed), match=error):
        operation()


def test_source_database_path_identity_and_schema_boundaries(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed, match="source path"):
        routing_module._source(Path("relative.db"), digest("schema"))

    wrong_mode = tmp_path / "wrong-mode.db"
    sqlite3.connect(wrong_mode).close()
    wrong_mode.chmod(0o644)
    with pytest.raises(PolicyViolation, match="source identity"):
        routing_module._source(wrong_mode.resolve(), digest("schema"))

    wrong_schema = tmp_path / "wrong-schema.db"
    sqlite3.connect(wrong_schema).close()
    wrong_schema.chmod(0o600)
    with pytest.raises(PolicyViolation, match="source schema"):
        routing_module._source(wrong_schema.resolve(), digest("schema"))


def test_router_path_private_parent_and_database_identity_boundaries(tmp_path: Path) -> None:
    absolute = (tmp_path / "db").resolve()
    with pytest.raises(ValidationFailed, match="absolute"):
        SQLiteLocalEvidenceRouter(Path("routing.db"), absolute, absolute, absolute)

    public = (tmp_path / "public").resolve()
    public.mkdir(mode=0o755)
    router = SQLiteLocalEvidenceRouter(public / "routing.db", absolute, absolute, absolute)
    with pytest.raises(PolicyViolation, match="private directory"):
        router.bootstrap()

    world = tmp_path / "world"
    world.mkdir()
    router, _, _ = _world(world)
    router.path.chmod(0o644)
    with pytest.raises(PolicyViolation, match="database identity"):
        router.counts()


def test_binding_request_and_policy_argument_boundaries(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    binding = bindings[0]
    with pytest.raises(ValidationFailed, match="TTL"):
        replace(binding, expires_at=binding.observed_at)
    with pytest.raises(ValidationFailed, match="allowed providers"):
        _request(router, digest("policy"), snapshot, allowed_provider_ids=())
    with pytest.raises(PolicyViolation, match="local-only"):
        _request(router, digest("policy"), snapshot, client_id="other")
    with pytest.raises(ValidationFailed, match="capability"):
        _request(router, digest("policy"), snapshot, required_capabilities=("unknown",))
    with pytest.raises(ValidationFailed, match="independence"):
        _request(router, digest("policy"), snapshot, independent_from_model_id=1)
    with pytest.raises(ValidationFailed, match="revision"):
        router.policy_activation_spec(0, (), "reviewer")
    with pytest.raises(ValidationFailed, match="policy stage"):
        router.record_policy_stage(0, (), "unknown", digest("evidence"), "actor")
    with pytest.raises(PolicyViolation, match="exact operational evidence"):
        router.record_policy_stage(
            1,
            tuple(sorted(item.candidate_digest for item in bindings)),
            "shadow",
            digest("shadow"),
            "actor",
            evidence_job_id="unexpected-job",
        )


def test_operational_project_policy_and_effect_absence_fail_closed(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    with pytest.raises(PolicyViolation, match="source authority ambiguous"):
        router._project_context("missing-project", "snapshot")
    with pytest.raises(PolicyViolation, match="candidate evidence incomplete"):
        router.policy_activation_spec(1, candidates, "independent-reviewer")
    with pytest.raises(PolicyViolation, match="predecessor"):
        router.record_policy_stage(1, candidates, "shadow", digest("shadow"), "actor")
    with pytest.raises(ValidationFailed, match="Exact local route request"):
        router.decide(object(), now=NOW)  # type: ignore[arg-type]
    policy = _activate(router, bindings)
    pending = router.decide(
        _request(
            router,
            policy,
            snapshot,
            allowed_provider_ids=("local",),
        ),
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="selected decision"):
        router.claim_effect(digest(pending), "work:pending")


def test_candidate_replay_and_missing_runtime_job_are_explicit(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    assert (
        router.register_candidate(bindings[0], device_id="mac-device", client_id="opencode")
        == bindings[0].candidate_digest
    )
    with pytest.raises(PolicyViolation, match="exact operational claim"):
        router._terminal_execution(
            "missing-job",
            operation="model.route.execute",
            effect_digest=digest("missing-effect"),
            expected_payload={"operation": "model.route.execute"},
        )


def test_failover_and_outcome_require_exact_effect_and_failed_primary(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    decision = router.decide(_request(router, policy, snapshot), now=NOW)
    effect = router.claim_effect(digest(decision), "work:boundaries").effect_digest
    primary = str(decision["primary_id"])
    fallback = str(decision["fallback_id"])
    with pytest.raises(PolicyViolation, match="exact primary effect"):
        router.failover_target(digest("missing-effect"), primary)
    with pytest.raises(PolicyViolation, match="failed primary receipt"):
        router.failover_target(effect, primary)
    with pytest.raises(PolicyViolation, match="outside exact effect"):
        router.record_outcome(digest("missing-effect"), primary, execution_job_id="job")
    fallback_job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=route_execution_effect_digest(effect, fallback),
        payload={
            "operation": "model.route.execute",
            "route_effect_digest": effect,
            "route_decision_digest": digest(decision),
            "exact_id": fallback,
        },
        completed=True,
    )
    with pytest.raises(PolicyViolation, match="fallback requires failed primary"):
        router.record_outcome(effect, fallback, execution_job_id=fallback_job)


def test_scope_object_and_operational_identity_guards(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    binding = bindings[0]
    with pytest.raises(ValidationFailed, match="scopes must be tuples"):
        LocalRouteBinding(
            binding.exact_id,
            binding.snapshot_digest,
            binding.revision_fingerprint,
            binding.health_digest,
            binding.aggregate_digest,
            binding.family_id,
            binding.execution_identity,
            ["code"],  # type: ignore[arg-type]
            binding.modalities,
            binding.data_classifications,
            binding.capabilities,
            binding.observed_at,
            binding.expires_at,
        )
    with pytest.raises(ValidationFailed, match="Exact local route binding"):
        router.register_candidate(object(), device_id="mac-device", client_id="opencode")  # type: ignore[arg-type]

    missing = SQLiteLocalEvidenceRouter(
        router.path,
        router.registry_path,
        router.benchmark_path,
        (tmp_path / "missing.db").resolve(),
    )
    with pytest.raises(PolicyViolation, match="operational evidence"):
        missing._operational()
    router.operational_path.chmod(0o644)
    with pytest.raises(PolicyViolation, match="operational identity"):
        router._operational()


def test_decision_remaining_hard_gate_reasons_are_explicit(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    model_id = bindings[0].exact_id
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"inventory_snapshot_digest": digest("other-snapshot")}, "inventory-revision-drift"),
        ({"workload": "analysis"}, "workload-modality-mismatch"),
        ({"data_classification": "restricted"}, "data-classification-mismatch"),
        ({"required_capabilities": ("tools",)}, "capability-missing"),
        ({"independent_from_model_id": model_id}, "independence-violation"),
        ({"max_latency_ms": 0.0}, "latency-budget"),
    )
    for changes, reason in cases:
        decision = router.decide(
            _request(
                router,
                policy,
                snapshot,
                **changes,
            ),
            now=NOW,
        )
        assert any(reason in item["reasons"] for item in decision["candidates"])


def test_benchmark_missing_health_digest_and_trial_census_drift_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_schema_digest = _routing.REGISTRY_SCHEMA_DIGEST
    router, bindings, _ = _world(tmp_path)
    binding = bindings[0]
    missing = replace(binding, aggregate_digest=digest("missing-aggregate"))
    with pytest.raises(PolicyViolation, match="aggregate missing"):
        router._evidence(missing, device_id="mac-device", client_id="opencode", now=NOW)

    with sqlite3.connect(router.registry_path) as db:
        db.execute("drop trigger health_observation_no_update")
        db.execute(
            "update health_observation set body_json=? where health_digest=?",
            (canonical_json({"drift": True}), binding.health_digest),
        )
        monkeypatch.setattr(
            routing_module, "REGISTRY_SCHEMA_DIGEST", routing_module._schema_digest(db)
        )
    with pytest.raises(PolicyViolation, match="health body digest"):
        router._evidence(binding, device_id="mac-device", client_id="opencode", now=NOW)
    monkeypatch.setattr(routing_module, "REGISTRY_SCHEMA_DIGEST", registry_schema_digest)

    other = tmp_path / "benchmark"
    other.mkdir()
    router, bindings, _ = _world(other)
    binding = bindings[0]
    with sqlite3.connect(router.benchmark_path) as db:
        db.execute("drop trigger benchmark_trial_no_delete")
        plan_id = db.execute(
            "select plan_id from benchmark_aggregate where aggregate_digest=?",
            (binding.aggregate_digest,),
        ).fetchone()[0]
        db.execute(
            "delete from benchmark_trial where rowid=(select rowid from benchmark_trial "
            "where plan_id=? limit 1)",
            (plan_id,),
        )
        monkeypatch.setattr(
            routing_module, "BENCHMARK_SCHEMA_DIGEST", routing_module._schema_digest(db)
        )
    with pytest.raises(PolicyViolation, match="trial census"):
        router._evidence(binding, device_id="mac-device", client_id="opencode", now=NOW)


def test_policy_contiguity_candidates_reviewer_and_failed_review_guards(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    with pytest.raises(ValidationFailed, match="revision/candidates"):
        router.activate_policy(
            0,
            (),
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="reviewer",
            activation_job_id="job",
        )
    with pytest.raises(PolicyViolation, match="registered candidates"):
        router.activate_policy(
            1,
            (digest("missing-candidate"),),
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="reviewer",
            activation_job_id="job",
        )
    policy = _activate(router, bindings)
    assert policy
    with pytest.raises(PolicyViolation, match="contiguous"):
        router.activate_policy(
            3,
            candidates,
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="reviewer",
            activation_job_id="job",
        )
    with pytest.raises(PolicyViolation, match="actors"):
        router.activate_policy(
            1,
            candidates,
            offline_replay_digest=digest("offline"),
            shadow_digest=digest("shadow"),
            canary_digest=digest("canary"),
            review_digest=digest("review"),
            approval_digest=digest("approval"),
            reviewer_model_id="different-reviewer",
            activation_job_id="job",
        )
    with pytest.raises(PolicyViolation, match="candidate missing"):
        router.record_policy_stage(
            2,
            (digest("missing-candidate"),),
            "offline-replay",
            digest("evidence"),
            "actor",
        )


def test_terminal_payload_and_policy_stage_failed_receipt_reject(tmp_path: Path) -> None:
    router, bindings, _ = _world(tmp_path)
    job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=digest("terminal-effect"),
        payload={"operation": "model.route.execute", "value": "actual"},
        completed=True,
    )
    with pytest.raises(PolicyViolation, match="payload drift"):
        router._terminal_execution(
            job,
            operation="model.route.execute",
            effect_digest=digest("terminal-effect"),
            expected_payload={"operation": "model.route.execute", "value": "expected"},
        )

    candidates = tuple(sorted(item.candidate_digest for item in bindings))
    for stage, evidence, actor in (
        ("offline-replay", digest("offline-2"), "offline-runner"),
        ("shadow", digest("shadow-2"), "shadow-runner"),
        ("canary", digest("canary-2"), "canary-runner"),
    ):
        router.record_policy_stage(2, candidates, stage, evidence, actor)
    evidence = digest("failed-review")
    actor = "independent-reviewer"
    review_job = _terminal_job(
        router,
        operation="model.route.policy-stage",
        effect_digest=route_policy_stage_effect_digest(
            2, candidates, "independent-review", evidence, actor
        ),
        payload={
            "operation": "model.route.policy-stage",
            "revision": 2,
            "candidate_digests": list(candidates),
            "stage": "independent-review",
            "evidence_digest": evidence,
            "actor_id": actor,
        },
        completed=False,
    )
    with pytest.raises(PolicyViolation, match="must complete"):
        router.record_policy_stage(
            2,
            candidates,
            "independent-review",
            evidence,
            actor,
            evidence_job_id=review_job,
        )


def test_outcome_replay_is_idempotent(tmp_path: Path) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    decision = router.decide(_request(router, policy, snapshot), now=NOW)
    decision_digest = digest(decision)
    claim = router.claim_effect(decision_digest, "work:outcome-replay")
    primary = str(decision["primary_id"])
    job = _terminal_job(
        router,
        operation="model.route.execute",
        effect_digest=route_execution_effect_digest(claim.effect_digest, primary),
        payload={
            "operation": "model.route.execute",
            "route_effect_digest": claim.effect_digest,
            "route_decision_digest": decision_digest,
            "exact_id": primary,
        },
        completed=True,
    )
    first = router.record_outcome(claim.effect_digest, primary, execution_job_id=job)
    assert router.record_outcome(claim.effect_digest, primary, execution_job_id=job) == first
