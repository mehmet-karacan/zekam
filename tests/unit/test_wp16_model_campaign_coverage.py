from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application import model_benchmark_service as benchmark_service
from zekam.application import model_capability_benchmark as capability
from zekam.application import opencode_benchmark_campaign as opencode
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_campaign import (
    AUDIO_EXCLUSION_REASON,
    CampaignContinuation,
    CampaignMember,
    CampaignMemberDisposition,
    CampaignMemberPlan,
    CampaignMemberResult,
    CampaignMemberResultStage,
    CampaignMemberResultStatus,
    CampaignOutcome,
    CampaignOutcomeStatus,
    OpenCodeBenchmarkCampaign,
    QualificationAction,
    QualificationEvent,
    ResultAdoption,
    ResultRecoveryEvidence,
)
from zekam.domain.model_inventory import Modality
from zekam.domain.model_routing import (
    AgentRole,
    CandidateDisposition,
    ExecutionTargetSnapshot,
    LayerCandidateEvidence,
    LayeredModelDecision,
    LayeredRouteRequest,
    ModelFamilyPolicy,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RouteCapabilityBinding,
    RouteCapabilityDimension,
    RouteCapabilityEvidence,
    RouteCapabilityRequirements,
    RouteStatus,
    RoutingLayer,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 30))
D = digest("evidence")


def _member(name: str = "aihub/code") -> CampaignMember:
    return CampaignMember(
        configured_model_id=name,
        canonical_model_id=f"canonical:{name}",
        modality="code",
        disposition=CampaignMemberDisposition.HEALTH_PENDING,
        fixture_digests=(digest("fixture-1"), digest("fixture-2")),
    )


def _campaign(**changes: Any) -> OpenCodeBenchmarkCampaign:
    values: dict[str, Any] = {
        "campaign_key": "opencode-aihub",
        "revision": 3,
        "work_item_id": IDS[0],
        "task_plan_id": IDS[1],
        "source_revision": "git:abc",
        "provider_ref": "aihub",
        "catalog_digest": digest("catalog"),
        "endpoint_identity_digest": digest("endpoint"),
        "inventory_digest": digest("inventory"),
        "policy_digest": digest("policy"),
        "fixture_registry_digest": digest("fixtures"),
        "verifier_identity": "independent-verifier",
        "verifier_provenance_digest": digest("verifier"),
        "source_digest": digest("source"),
        "repetitions": 5,
        "verifier_provider_calls_per_trial": 1,
        "members": (_member(),),
    }
    values.update(changes)
    return OpenCodeBenchmarkCampaign(**values)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CampaignContinuation(IDS[0], "rev", D, D, -1, 0),
        lambda: CampaignContinuation(IDS[0], "rev", D, D, 2, 1),
        lambda: CampaignMember(
            "/absolute", "model", "code", CampaignMemberDisposition.HEALTH_PENDING, (D,)
        ),
        lambda: CampaignMember(
            "audio",
            "model",
            "audio_transcription",
            CampaignMemberDisposition.EXCLUDED_AUDIO,
            (D,),
            AUDIO_EXCLUSION_REASON,
        ),
        lambda: CampaignMember(
            "audio",
            None,
            "audio_transcription",
            CampaignMemberDisposition.EXCLUDED_AUDIO,
            (),
            "wrong",
        ),
        lambda: CampaignMember(
            "model", "canonical", "code", CampaignMemberDisposition.HEALTH_PENDING, (D, D)
        ),
        lambda: _campaign(revision=0),
        lambda: _campaign(benchmark_suite_version=0),
        lambda: _campaign(repetitions=4),
        lambda: _campaign(verifier_provider_calls_per_trial=2),
        lambda: _campaign(members=()),
        lambda: _campaign(members=(_member(), _member())),
    ],
)
def test_campaign_contracts_reject_invalid_duplicate_and_boundary_inputs(factory: Any) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        factory()


def test_campaign_continuation_replay_budget_and_terminal_result_invariants() -> None:
    continuation = CampaignContinuation(IDS[2], "parent-rev", D, digest("continuation"), 3, 5)
    current = _campaign(revision=4, continuation=continuation)
    replay = _campaign(revision=4, continuation=continuation)
    assert current.campaign_digest == replay.campaign_digest
    assert current.current_tested_call_budget == 3
    assert current.current_provider_call_budget == 5
    assert current.as_dict()["continuation"] == continuation.as_dict()
    with pytest.raises(PolicyViolation, match="full campaign"):
        _campaign(
            revision=4,
            continuation=replace(
                continuation, maximum_tested_call_count=99, maximum_provider_call_count=99
            ),
        )

    adoption = ResultAdoption(IDS[3], digest("adoption"))
    recovery = ResultRecoveryEvidence(IDS[4], IDS[5], digest("recovery"))
    adopted = CampaignMemberResult(
        CampaignMemberResultStage.HEALTH,
        CampaignMemberResultStatus.PASSED,
        D,
        0,
        0,
        adoption=adoption,
    )
    recovered = CampaignMemberResult(
        CampaignMemberResultStage.HEALTH,
        CampaignMemberResultStatus.FAILED,
        D,
        0,
        0,
        failure_category="health-contract-failed",
        recovery_evidence=recovery,
    )
    assert adopted.result_digest != recovered.result_digest
    invalid_results = (
        {"actual_tested_call_count": -1},
        {"actual_tested_call_count": 2, "actual_provider_call_count": 1},
        {"adoption": adoption, "recovery_evidence": recovery},
        {"adoption": adoption, "actual_provider_call_count": 1},
        {"aggregate_id": IDS[6]},
    )
    base = {
        "stage": CampaignMemberResultStage.HEALTH,
        "status": CampaignMemberResultStatus.FAILED,
        "evidence_digest": D,
        "actual_tested_call_count": 0,
        "actual_provider_call_count": 0,
        "failure_category": "failed",
    }
    for changes in invalid_results:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            CampaignMemberResult(**cast(Any, base | changes))


def test_campaign_member_plan_outcome_and_qualification_terminal_matrix() -> None:
    plan = CampaignMemberPlan(IDS[0], D, digest("health"), digest("auth"), 1, 2)
    assert plan.member_plan_digest.startswith("sha256:")
    for tested, provider in ((0, 0), (2, 1)):
        with pytest.raises(ValidationFailed):
            CampaignMemberPlan(IDS[0], D, D, D, tested, provider)
    for outcome in (
        CampaignOutcome(CampaignOutcomeStatus.PASSED, 1, 0, 0, 1, 5, 6, D),
        CampaignOutcome(CampaignOutcomeStatus.FAILED, 0, 1, 0, 0, 0, 1, D),
        CampaignOutcome(CampaignOutcomeStatus.RECOVERY_REQUIRED, 0, 0, 1, 0, 0, 0, D),
    ):
        assert outcome.outcome_digest.startswith("sha256:")
    for values in (
        (CampaignOutcomeStatus.PASSED, 0, 1, 0),
        (CampaignOutcomeStatus.FAILED, 0, 0, 0),
        (CampaignOutcomeStatus.RECOVERY_REQUIRED, 0, 0, 0),
    ):
        with pytest.raises(ValidationFailed):
            CampaignOutcome(values[0], values[1], values[2], values[3], 0, 0, 0, D)
    qualified = QualificationEvent(QualificationAction.QUALIFIED, "model", IDS[1], D, IDS[2])
    rejected = QualificationEvent(
        QualificationAction.DISQUALIFIED, "model", IDS[1], D, reason_code="unsafe"
    )
    assert qualified.event_digest != rejected.event_digest
    with pytest.raises(ValidationFailed):
        QualificationEvent(QualificationAction.QUALIFIED, "model", IDS[1], D)
    with pytest.raises(ValidationFailed):
        QualificationEvent(QualificationAction.DISQUALIFIED, "model", IDS[1], D)


def _context(**changes: Any) -> ProjectRoutingContext:
    values: dict[str, Any] = {
        "project_id": IDS[0],
        "source_revision_id": IDS[1],
        "source_revision": "git:abc",
        "tree_digest": digest("tree"),
        "capability_profile_digest": digest("cap"),
        "dependency_digest": digest("dep"),
        "framework_digest": digest("framework"),
        "technology_digest": digest("tech"),
        "architecture_digest": digest("arch"),
        "rules_digest": digest("rules"),
        "suite_digest": digest("suite"),
        "inventory_digest": digest("inventory"),
        "policy_digest": digest("policy"),
        "captured_at": NOW,
        "expires_at": NOW + dt.timedelta(hours=1),
    }
    values.update(changes)
    return ProjectRoutingContext(**values)


def _request(**changes: Any) -> LayeredRouteRequest:
    values: dict[str, Any] = {
        "role": AgentRole.IMPLEMENTER,
        "target_layer": RoutingLayer.PROJECT,
        "workload": "code",
        "technology": "python",
        "project_id": IDS[0],
        "project_context_digest": D,
        "inventory_digest": D,
        "routing_policy_digest": D,
        "policy_digest": D,
        "execution_target_digest": D,
    }
    values.update(changes)
    return LayeredRouteRequest(**values)


def test_routing_context_execution_policy_and_request_boundaries() -> None:
    context = _context()
    assert context.stale_reasons(context, now=NOW) == ()
    assert context.stale_reasons(replace(context, tree_digest=digest("new")), now=NOW) != ()
    with pytest.raises(PolicyViolation):
        context.stale_reasons(replace(context, project_id=IDS[9]), now=NOW)
    with pytest.raises(ValidationFailed):
        _context(expires_at=NOW)

    family = ModelFamilyPolicy((("m1", "family"),), ("low",))
    assert family.family_for("missing") is None
    for families in ((), (("m1", "Family"),)):
        with pytest.raises(ValidationFailed):
            ModelFamilyPolicy(families, ("low",))

    policy = RoleRoutingPolicy(
        AgentRole.IMPLEMENTER,
        RoutingLayer.PROJECT,
        tuple(RoutingLayer),
        2,
        ("fallback",),
        1.0,
        1000,
        (),
        D,
    )
    assert policy.top_k == 2
    with pytest.raises(ValidationFailed):
        replace(policy, top_k=0)
    with pytest.raises(ValidationFailed):
        replace(policy, fallback_model_ids=("m", "m"))

    target = ExecutionTargetSnapshot(
        "codex",
        "slot",
        "native-parallel",
        True,
        True,
        True,
        1,
        D,
        D,
        NOW,
        NOW + dt.timedelta(hours=1),
    )
    assert target.execution_identity == "codex:slot"
    for changes in ({"execution_mode": "wrong"}, {"max_concurrency": 0}, {"expires_at": NOW}):
        with pytest.raises(ValidationFailed):
            replace(target, **changes)
    assert _request().target_layer is RoutingLayer.PROJECT
    for changes in cast(
        tuple[dict[str, Any], ...],
        (
            {"target_layer": RoutingLayer.GENERAL, "workload": "code"},
            {"target_layer": RoutingLayer.WORKLOAD, "project_id": IDS[0]},
            {"risk": "extreme"},
            {"excluded_model_families": ("Family",), "family_policy_digest": D},
            {"excluded_model_families": ("family",)},
        ),
    ):
        with pytest.raises(ValidationFailed):
            _request(**changes)


def test_routing_capability_and_terminal_decision_validation() -> None:
    binding = RouteCapabilityBinding(AgentRole.IMPLEMENTER, "rev", D, D, D, D)
    requirements = RouteCapabilityRequirements(1, 0.2, 0.3, 5, 0.4)
    assert len(requirements.required_dimensions) == 4
    for changes in cast(
        tuple[dict[str, Any], ...],
        (
            {"minimum_context_tokens": True},
            {"minimum_tool_score": "x"},
            {"minimum_context_tokens": -1},
            {"minimum_tool_score": 1.1},
            {"minimum_long_session_seconds": 1, "minimum_long_session_score": 0},
        ),
    ):
        with pytest.raises(ValidationFailed):
            RouteCapabilityRequirements(**changes)
    with pytest.raises(ValidationFailed):
        replace(binding, source_revision="https://bad")

    evidence = RouteCapabilityEvidence(
        "model",
        AgentRole.IMPLEMENTER,
        RouteCapabilityDimension.TOOL,
        0.8,
        1,
        1,
        D,
        D,
        "rev",
        D,
        D,
        D,
        D,
        D,
        (digest("episode"),),
        NOW,
        NOW + dt.timedelta(hours=1),
    )
    assert evidence.evidence_digest.startswith("sha256:")
    for changes in ({"score": 2}, {"episode_evidence_digests": ()}, {"expires_at": NOW}):
        with pytest.raises(ValidationFailed):
            replace(evidence, **changes)

    candidate = LayerCandidateEvidence("model", (), (), (), CandidateDisposition.ELIGIBLE)
    assert candidate.score == 0
    with pytest.raises(ValidationFailed):
        LayerCandidateEvidence(
            "model", ((RoutingLayer.GENERAL, 1.0), (RoutingLayer.GENERAL, 0.5)), (), ()
        )
    request = _request()
    for kwargs in (
        {"status": RouteStatus.PENDING, "primary_model_id": "model"},
        {"status": RouteStatus.SELECTED, "primary_model_id": None},
        {"status": RouteStatus.SELECTED, "primary_model_id": "model", "fallback_model_id": "model"},
        {"status": RouteStatus.PENDING, "authority_granted": True},
        {"status": RouteStatus.PENDING, "catalog_provider_id": "p"},
    ):
        values = {
            "request": request,
            "policy_digest": D,
            "status": RouteStatus.PENDING,
            "primary_model_id": None,
            "fallback_model_id": None,
            "candidates": (),
            "evidence_digest": D,
        }
        values.update(kwargs)
        with pytest.raises((PolicyViolation, ValidationFailed)):
            LayeredModelDecision(**cast(Any, values))


def _trial_mapping() -> dict[str, Any]:
    return {
        "fixture_digest": D,
        "repetition": 1,
        "status": "passed",
        "parse_ok": True,
        "format_ok": True,
        "evidence_ok": True,
        "verifier_approved": True,
        "quality": 0.9,
        "reliability": 0.8,
        "latency_ms": 10,
        "input_tokens": 5,
        "output_tokens": 4,
        "retry_count": 0,
        "human_corrections": 0,
        "estimated_cost": 0.0,
        "actual_cost": 0.0,
        "response_digest": D,
        "evidence_digest": digest("trial"),
        "failure_category": None,
    }


def test_benchmark_trial_exact_types_and_process_failure_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert benchmark_service.trial_from_mapping(_trial_mapping()).repetition == 1
    for changes in (
        {"parse_ok": 1},
        {"repetition": True},
        {"quality": 1},
        {"reliability": float("nan")},
        {"actual_cost": "0"},
    ):
        with pytest.raises(ValidationFailed):
            benchmark_service.trial_from_mapping(_trial_mapping() | changes)
    with pytest.raises(ValidationFailed):
        benchmark_service.trial_from_mapping([])  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        benchmark_service._run_json_process(("relative",), {}, 1)
    with pytest.raises(PolicyViolation):
        benchmark_service._run_json_process(("/bin/echo",), {}, 0)
    monkeypatch.setattr(
        benchmark_service,
        "_bounded_process",
        lambda *_args, **_kwargs: (1, b"{}", b"error"),
    )
    with pytest.raises(PolicyViolation, match="sanitized"):
        benchmark_service._run_json_process(("/bin/echo",), {}, 1)
    monkeypatch.setattr(
        benchmark_service,
        "_bounded_process",
        lambda *_args, **_kwargs: (0, b'{"x":1,"x":2}', b""),
    )
    with pytest.raises(ValidationFailed):
        benchmark_service._run_json_process(("/bin/echo",), {}, 1)


def test_opencode_scope_operation_and_yaml_fail_closed(tmp_path: Path) -> None:
    verifier = opencode.ScopeVerifier("verifier", "slot")
    chat = opencode.ScopeTarget("route", ("canonical",), Modality.CHAT, "chat")
    audio = opencode.ScopeTarget(
        "audio",
        ("canonical-audio",),
        Modality.AUDIO_TRANSCRIPTION,
        "audio",
        opencode.AUDIO_EXCLUSION_REASON,
    )
    scope = opencode.OpenCodeCampaignScope(
        1,
        opencode.PROVIDER_ID,
        opencode.PROVIDER_FAMILY,
        "configured-canonical-all",
        5,
        verifier,
        (chat, audio),
    )
    assert scope.scope_digest.startswith("sha256:")
    with pytest.raises(ValidationFailed):
        opencode.ScopeVerifier("", "slot")
    with pytest.raises(ValidationFailed):
        opencode.ScopeTarget("route", (), Modality.CHAT, "chat")
    with pytest.raises(PolicyViolation):
        opencode.ScopeTarget("route", ("a", "b"), Modality.CHAT, "chat")
    with pytest.raises(PolicyViolation):
        opencode.ScopeTarget("route", ("a",), Modality.CHAT, "chat", "excluded")
    with pytest.raises(ValidationFailed):
        replace(scope, targets=(chat, chat))
    with pytest.raises(PolicyViolation):
        replace(scope, provider_id="other")

    for modality, expected in (
        (Modality.EMBEDDING, "embeddings"),
        (Modality.RERANK, "rerank"),
        (Modality.CHAT, "chat-completions"),
        (Modality.CODE, "chat-completions"),
    ):
        assert opencode._operation(modality)[0] == expected
    with pytest.raises(PolicyViolation):
        opencode._operation(Modality.AUDIO_TRANSCRIPTION)
    assert opencode._operation_endpoint("https://host/v1/embeddings", "/rerank")[1] == "/v1/rerank"
    with pytest.raises(ConfigurationError):
        opencode._operation_endpoint("https://host/v1/chat", "/rerank")
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ConfigurationError):
        opencode.load_campaign_scope(missing)
    malformed = tmp_path / "bad.yaml"
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        opencode.load_campaign_scope(malformed)


def _cap_response(**changes: Any) -> capability.CapabilityResponse:
    checkpoint = capability.CapabilityCheckpointReceipt("start", 1, D, 1, 1)
    values: dict[str, Any] = {
        "payload": {"status": "completed", "markers": [], "artifact_digest": D},
        "duration_ms": 10,
        "input_tokens": 1,
        "output_tokens": 1,
        "provider_latency_ms": 1,
        "checkpoint_receipts": (checkpoint,),
        "tool_receipts": (capability.CapabilityToolReceipt("read", digest("tool")),),
        "self_correction_count": 0,
        "hidden_acceptance_passed": 1,
        "hidden_acceptance_total": 1,
        "regression_count": 0,
        "context_retention_ratio": 1.0,
        "unsafe": False,
        "acceptance_evidence_digest": D,
    }
    values.update(changes)
    return capability.CapabilityResponse(**values)


def test_capability_receipt_response_and_verifier_failures() -> None:
    response = _cap_response()
    assert response.response_digest.startswith("sha256:")
    for factory in (
        lambda: capability.CapabilityCheckpointReceipt("", 0, D, 0, 0),
        lambda: capability.CapabilityCheckpointReceipt("x", 0, D, 2, 1),
        lambda: capability.CapabilityCheckpointReceipt("x", 0, "bad", 0, 0),
        lambda: capability.CapabilityToolReceipt("", D),
        lambda: capability.CapabilityToolReceipt("read", "bad"),
    ):
        with pytest.raises(ValidationFailed):
            factory()
    first = capability.CapabilityCheckpointReceipt("a", 2, digest("a"), 0, 1)
    second = capability.CapabilityCheckpointReceipt("b", 1, digest("b"), 1, 1)
    for changes in (
        {"duration_ms": -1},
        {"context_retention_ratio": 2},
        {"hidden_acceptance_passed": 2},
        {"checkpoint_receipts": ()},
        {"checkpoint_receipts": (first, first)},
        {"checkpoint_receipts": (first, second)},
        {"acceptance_evidence_digest": "bad"},
    ):
        with pytest.raises(ValidationFailed):
            _cap_response(**changes)
    with pytest.raises(PolicyViolation):
        capability.CapabilityVerifier("model", "slot", D).verify(
            tested_model_id="model",
            task=cast(Any, object()),
            response=response,
        )
