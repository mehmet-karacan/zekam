"""Deterministic shadow evaluator for the twenty Memory Contract invariants."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_contract import (
    MEMORY_INVARIANT_IDS,
    InvariantStatus,
    MemoryContractEvaluation,
    MemoryInvariantResult,
)
from zekam.domain.session_continuity import DigestReference, TruthClass

EVALUATOR_VERSION = "memory-contract-evaluator-v1"
_VERIFIED_TRUTH_CLASSES = frozenset(
    {TruthClass.USER_DECISION, TruthClass.REPO_FACT, TruthClass.EXTERNAL_VERIFIED_FACT}
)


@dataclass(frozen=True, slots=True)
class InvariantEvidence:
    invariant_id: str
    references: tuple[DigestReference, ...]

    def __post_init__(self) -> None:
        if self.invariant_id not in MEMORY_INVARIANT_IDS:
            raise ValidationFailed("Memory Contract evidence registry disinda")
        if not self.references:
            raise ValidationFailed("Memory Contract evidence bos olamaz")
        ordered = tuple(sorted(self.references, key=lambda item: (item.ref, item.digest_value)))
        if self.references != ordered or len(set(self.references)) != len(self.references):
            raise ValidationFailed("Memory Contract evidence tekil ve sirali olmali")


@dataclass(frozen=True, slots=True)
class MemoryContractSnapshot:
    """Typed observations; no prose or inferred authority enters evaluation."""

    durable_information_observed: bool = False
    durable_information_persisted: bool = False
    clean_close_requested: bool = False
    checkpoint_durable: bool = False
    compaction_requested: bool = False
    precompaction_ack_durable: bool = False
    mutation_requested: bool = False
    hydration_fresh_complete: bool = False
    active_work_required: bool = False
    active_work_from_canonical_graph: bool = False
    human_decision_observed: bool = False
    human_decision_durable: bool = False
    accepted_adr_observed: bool = False
    adr_rationale_complete: bool = False
    pending_work_observed: bool = False
    continuation_pointer_present: bool = False
    critical_record_observed: bool = False
    critical_record_provenance_complete: bool = False
    inference_fact_transition_attempted: bool = False
    inference_promoted_to_fact: bool = False
    memory_write_failed: bool = False
    memory_write_failure_visible: bool = False
    hydration_failed: bool = False
    hydration_failure_visible: bool = False
    broken_or_missing_state: bool = False
    recovery_mode_active: bool = False
    memory_mutation_requested: bool = False
    memory_mutation_versioned_reversible: bool = False
    self_modification_requested: bool = False
    self_modification_governed: bool = False
    sensitive_data_observed: bool = False
    sensitive_data_correct_tier: bool = False
    public_output_requested: bool = False
    public_output_private_free: bool = False
    stale_information_observed: bool = False
    stale_information_excluded_or_revalidated: bool = False
    duplicate_or_conflict_observed: bool = False
    duplicate_conflict_policy_applied: bool = False
    remember_claim_made: bool = False
    remember_claim_source_present: bool = False
    evidence: tuple[InvariantEvidence, ...] = ()

    def __post_init__(self) -> None:
        ids = tuple(item.invariant_id for item in self.evidence)
        if len(ids) != len(set(ids)):
            raise ValidationFailed("Memory Contract evidence invariantleri tekil olmali")

    def evidence_for(self, invariant_id: str) -> tuple[DigestReference, ...]:
        return next(
            (item.references for item in self.evidence if item.invariant_id == invariant_id),
            (),
        )


@dataclass(frozen=True, slots=True)
class _InvariantRule:
    invariant_id: str
    enforcement_point: str
    applicable: Callable[[MemoryContractSnapshot], bool]
    satisfied: Callable[[MemoryContractSnapshot], bool]
    failure_code: str
    recovery_directive: str


_RULES = (
    _InvariantRule(
        "durable-information-persisted",
        "pre-close-pre-compaction",
        lambda item: item.durable_information_observed,
        lambda item: item.durable_information_persisted,
        "durable-information-missing",
        "prepare-reviewed-gap-recovery",
    ),
    _InvariantRule(
        "clean-close-checkpoint",
        "close-gate",
        lambda item: item.clean_close_requested,
        lambda item: item.checkpoint_durable,
        "clean-close-checkpoint-missing",
        "repair-checkpoint-before-close",
    ),
    _InvariantRule(
        "pre-compaction-durable-ack",
        "pre-compaction-gate",
        lambda item: item.compaction_requested,
        lambda item: item.precompaction_ack_durable,
        "precompaction-ack-missing",
        "return-to-last-safe-checkpoint",
    ),
    _InvariantRule(
        "hydration-before-mutation",
        "mutation-admission",
        lambda item: item.mutation_requested,
        lambda item: item.hydration_fresh_complete,
        "hydration-required",
        "recompile-hydration-from-authority",
    ),
    _InvariantRule(
        "active-task-from-work-graph",
        "work-resolution",
        lambda item: item.active_work_required,
        lambda item: item.active_work_from_canonical_graph,
        "active-work-not-canonical",
        "reconcile-work-graph-projection",
    ),
    _InvariantRule(
        "human-decision-durable",
        "decision-capture",
        lambda item: item.human_decision_observed,
        lambda item: item.human_decision_durable,
        "human-decision-not-durable",
        "request-human-reconfirmation",
    ),
    _InvariantRule(
        "adr-rationale-preserved",
        "decision-validation",
        lambda item: item.accepted_adr_observed,
        lambda item: item.adr_rationale_complete,
        "adr-rationale-incomplete",
        "prepare-adr-backfill-proposal",
    ),
    _InvariantRule(
        "pending-work-continuation-pointer",
        "checkpoint-validation",
        lambda item: item.pending_work_observed,
        lambda item: item.continuation_pointer_present,
        "continuation-pointer-missing",
        "recover-last-safe-action",
    ),
    _InvariantRule(
        "critical-record-provenance",
        "promotion-validation",
        lambda item: item.critical_record_observed,
        lambda item: item.critical_record_provenance_complete,
        "critical-provenance-missing",
        "recollect-exact-evidence",
    ),
    _InvariantRule(
        "inference-not-fact",
        "truth-class-gate",
        lambda item: item.inference_fact_transition_attempted,
        lambda item: not item.inference_promoted_to_fact,
        "inference-promoted-to-fact",
        "quarantine-and-independent-review",
    ),
    _InvariantRule(
        "memory-write-failure-visible",
        "memory-write-transaction",
        lambda item: item.memory_write_failed,
        lambda item: item.memory_write_failure_visible,
        "memory-write-failure-silent",
        "enter-recovery-required",
    ),
    _InvariantRule(
        "hydration-failure-visible",
        "session-start-bridge",
        lambda item: item.hydration_failed,
        lambda item: item.hydration_failure_visible,
        "hydration-failure-silent",
        "recompile-and-record-failure",
    ),
    _InvariantRule(
        "broken-state-enters-recovery",
        "continuity-gap-gate",
        lambda item: item.broken_or_missing_state,
        lambda item: item.recovery_mode_active,
        "broken-state-not-in-recovery",
        "prepare-reviewed-repair-plan",
    ),
    _InvariantRule(
        "memory-mutation-versioned-reversible",
        "memory-mutation-gate",
        lambda item: item.memory_mutation_requested,
        lambda item: item.memory_mutation_versioned_reversible,
        "memory-mutation-not-reversible",
        "restore-previous-version",
    ),
    _InvariantRule(
        "self-modification-governed",
        "self-modification-gate",
        lambda item: item.self_modification_requested,
        lambda item: item.self_modification_governed,
        "self-modification-ungoverned",
        "require-independent-human-review",
    ),
    _InvariantRule(
        "sensitive-data-correct-tier",
        "classification-gate",
        lambda item: item.sensitive_data_observed,
        lambda item: item.sensitive_data_correct_tier,
        "sensitive-data-tier-mismatch",
        "quarantine-and-prepare-incident",
    ),
    _InvariantRule(
        "public-private-separation",
        "projection-export-gate",
        lambda item: item.public_output_requested,
        lambda item: item.public_output_private_free,
        "private-data-public-output",
        "block-output-and-review-history",
    ),
    _InvariantRule(
        "stale-information-not-current",
        "retrieval-hydration-gate",
        lambda item: item.stale_information_observed,
        lambda item: item.stale_information_excluded_or_revalidated,
        "stale-information-presented-current",
        "revalidate-or-reindex-source",
    ),
    _InvariantRule(
        "duplicate-conflict-policy",
        "compiler-hygiene-gate",
        lambda item: item.duplicate_or_conflict_observed,
        lambda item: item.duplicate_conflict_policy_applied,
        "duplicate-conflict-ungoverned",
        "quarantine-for-independent-review",
    ),
    _InvariantRule(
        "remember-claim-has-source",
        "answer-contract",
        lambda item: item.remember_claim_made,
        lambda item: item.remember_claim_source_present,
        "remember-claim-source-missing",
        "abstain-and-request-source",
    ),
)

if tuple(rule.invariant_id for rule in _RULES) != MEMORY_INVARIANT_IDS:
    raise RuntimeError("Memory Contract evaluator registry domain sirasiyla uyusmuyor")


@dataclass(frozen=True, slots=True)
class MemoryContractEvaluator:
    """Current rollout is intentionally shadow-only and has no mutation surface."""

    mode: str = "shadow"

    def __post_init__(self) -> None:
        if self.mode != "shadow":
            raise PolicyViolation(
                "Memory Contract enforce gecisi ayri plan, verifier ve authorization ister"
            )

    def evaluate(
        self,
        snapshot: MemoryContractSnapshot,
        *,
        evaluation_id: UUID,
        realm_id: UUID,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        source_revision: str,
        policy_version: str,
        evaluated_at: dt.datetime,
    ) -> MemoryContractEvaluation:
        results: list[MemoryInvariantResult] = []
        for rule in _RULES:
            if not rule.applicable(snapshot):
                results.append(
                    MemoryInvariantResult(
                        rule.invariant_id,
                        InvariantStatus.NOT_APPLICABLE,
                        rule.enforcement_point,
                        (),
                    )
                )
                continue
            evidence = snapshot.evidence_for(rule.invariant_id)
            if not evidence:
                raise ValidationFailed(f"Applicable invariant evidence eksik: {rule.invariant_id}")
            if rule.satisfied(snapshot):
                if not any(
                    reference.truth_class in _VERIFIED_TRUTH_CLASSES for reference in evidence
                ):
                    results.append(
                        MemoryInvariantResult(
                            rule.invariant_id,
                            InvariantStatus.FAILED,
                            rule.enforcement_point,
                            evidence,
                            "evidence-truth-insufficient",
                            "collect-verified-evidence",
                        )
                    )
                    continue
                results.append(
                    MemoryInvariantResult(
                        rule.invariant_id,
                        InvariantStatus.PASSED,
                        rule.enforcement_point,
                        evidence,
                    )
                )
            else:
                results.append(
                    MemoryInvariantResult(
                        rule.invariant_id,
                        InvariantStatus.FAILED,
                        rule.enforcement_point,
                        evidence,
                        rule.failure_code,
                        rule.recovery_directive,
                    )
                )
        return MemoryContractEvaluation(
            evaluation_id=evaluation_id,
            realm_id=realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            results=tuple(results),
            source_revision=source_revision,
            policy_version=policy_version,
            evaluator_version=EVALUATOR_VERSION,
            evaluated_at=evaluated_at,
        )
