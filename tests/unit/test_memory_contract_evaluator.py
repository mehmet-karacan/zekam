from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import UUID

import pytest

from zekam.application.memory_contract_evaluator import (
    InvariantEvidence,
    MemoryContractEvaluator,
    MemoryContractSnapshot,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_contract import MEMORY_INVARIANT_IDS, InvariantStatus
from zekam.domain.session_continuity import DigestReference, TruthClass

NOW = dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC)
IDS = tuple(UUID(int=index) for index in range(1, 6))

_PASS_FIELDS = {
    "durable-information-persisted": "durable_information_persisted",
    "clean-close-checkpoint": "checkpoint_durable",
    "pre-compaction-durable-ack": "precompaction_ack_durable",
    "hydration-before-mutation": "hydration_fresh_complete",
    "active-task-from-work-graph": "active_work_from_canonical_graph",
    "human-decision-durable": "human_decision_durable",
    "adr-rationale-preserved": "adr_rationale_complete",
    "pending-work-continuation-pointer": "continuation_pointer_present",
    "critical-record-provenance": "critical_record_provenance_complete",
    "inference-not-fact": "inference_promoted_to_fact",
    "memory-write-failure-visible": "memory_write_failure_visible",
    "hydration-failure-visible": "hydration_failure_visible",
    "broken-state-enters-recovery": "recovery_mode_active",
    "memory-mutation-versioned-reversible": "memory_mutation_versioned_reversible",
    "self-modification-governed": "self_modification_governed",
    "sensitive-data-correct-tier": "sensitive_data_correct_tier",
    "public-private-separation": "public_output_private_free",
    "stale-information-not-current": "stale_information_excluded_or_revalidated",
    "duplicate-conflict-policy": "duplicate_conflict_policy_applied",
    "remember-claim-has-source": "remember_claim_source_present",
}


def _evidence() -> tuple[InvariantEvidence, ...]:
    return tuple(
        InvariantEvidence(
            invariant_id,
            (
                DigestReference(
                    ref=f"evidence:{index}",
                    digest_value=digest({"invariant": invariant_id}),
                    truth_class=TruthClass.REPO_FACT,
                ),
            ),
        )
        for index, invariant_id in enumerate(MEMORY_INVARIANT_IDS, start=1)
    )


def _passing_snapshot() -> MemoryContractSnapshot:
    return MemoryContractSnapshot(
        durable_information_observed=True,
        durable_information_persisted=True,
        clean_close_requested=True,
        checkpoint_durable=True,
        compaction_requested=True,
        precompaction_ack_durable=True,
        mutation_requested=True,
        hydration_fresh_complete=True,
        active_work_required=True,
        active_work_from_canonical_graph=True,
        human_decision_observed=True,
        human_decision_durable=True,
        accepted_adr_observed=True,
        adr_rationale_complete=True,
        pending_work_observed=True,
        continuation_pointer_present=True,
        critical_record_observed=True,
        critical_record_provenance_complete=True,
        inference_fact_transition_attempted=True,
        inference_promoted_to_fact=False,
        memory_write_failed=True,
        memory_write_failure_visible=True,
        hydration_failed=True,
        hydration_failure_visible=True,
        broken_or_missing_state=True,
        recovery_mode_active=True,
        memory_mutation_requested=True,
        memory_mutation_versioned_reversible=True,
        self_modification_requested=True,
        self_modification_governed=True,
        sensitive_data_observed=True,
        sensitive_data_correct_tier=True,
        public_output_requested=True,
        public_output_private_free=True,
        stale_information_observed=True,
        stale_information_excluded_or_revalidated=True,
        duplicate_or_conflict_observed=True,
        duplicate_conflict_policy_applied=True,
        remember_claim_made=True,
        remember_claim_source_present=True,
        evidence=_evidence(),
    )


def _evaluate(snapshot: MemoryContractSnapshot):  # type: ignore[no-untyped-def]
    return MemoryContractEvaluator().evaluate(
        snapshot,
        evaluation_id=IDS[0],
        realm_id=IDS[1],
        project_id=IDS[2],
        work_item_id=IDS[3],
        run_id=IDS[4],
        source_revision="git:abc123",
        policy_version="memory-policy-v1",
        evaluated_at=NOW,
    )


def test_all_twenty_invariants_pass_deterministically() -> None:
    first = _evaluate(_passing_snapshot())
    second = _evaluate(_passing_snapshot())

    assert tuple(item.invariant_id for item in first.results) == MEMORY_INVARIANT_IDS
    assert all(item.status is InvariantStatus.PASSED for item in first.results)
    assert first.passed is True
    assert first.evaluation_digest == second.evaluation_digest
    assert first.body()["grants_authority"] is False


@pytest.mark.parametrize("invariant_id", MEMORY_INVARIANT_IDS)
def test_each_invariant_has_an_exact_failure_and_recovery(invariant_id: str) -> None:
    field = _PASS_FIELDS[invariant_id]
    # MC-010 passes when the forbidden transition remains false; flip it true.
    failing_value = invariant_id == "inference-not-fact"
    result = _evaluate(
        replace(_passing_snapshot(), **{field: failing_value})  # type: ignore[arg-type]
    )
    target = next(item for item in result.results if item.invariant_id == invariant_id)

    assert target.status is InvariantStatus.FAILED
    assert target.failure_code
    assert target.recovery_directive
    assert result.passed is False


def test_non_applicable_invariants_are_explicit_and_evidence_free() -> None:
    result = _evaluate(MemoryContractSnapshot())
    assert len(result.results) == 20
    assert all(item.status is InvariantStatus.NOT_APPLICABLE for item in result.results)
    assert all(not item.evidence_refs for item in result.results)


def test_applicable_invariant_without_evidence_fails_closed() -> None:
    with pytest.raises(ValidationFailed, match="evidence eksik"):
        _evaluate(
            MemoryContractSnapshot(
                mutation_requested=True,
                hydration_fresh_complete=True,
            )
        )


def test_enforcement_cannot_be_enabled_by_this_shadow_evaluator() -> None:
    with pytest.raises(PolicyViolation, match="ayri plan"):
        MemoryContractEvaluator(mode="enforced")


def test_passing_invariant_requires_at_least_one_verified_truth_reference() -> None:
    weak = DigestReference("evidence:weak", digest("weak"), TruthClass.MODEL_INFERENCE)
    snapshot = MemoryContractSnapshot(
        remember_claim_made=True,
        remember_claim_source_present=True,
        evidence=(InvariantEvidence("remember-claim-has-source", (weak,)),),
    )
    result = _evaluate(snapshot)
    target = result.results[-1]
    assert target.status is InvariantStatus.FAILED
    assert target.failure_code == "evidence-truth-insufficient"
