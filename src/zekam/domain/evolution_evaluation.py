"""Immutable, task-typed evaluation contracts for autonomous evolution.

These contracts deliberately do not execute a model or grant rollout authority.  They
bind a candidate to separated data partitions and produce paired evidence that the
existing improvement ledger can reference by digest.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

EVALUATION_GOLDEN_ARTIFACT_DIGEST = digest("evaluation-golden-artifact/v1")
EVALUATION_FIXTURE_FINGERPRINT = digest("evaluation-fixture-contract/v1")
EVALUATION_HARNESS_FINGERPRINT = digest("evaluation-harness-contract/v1")


class ReceiptVerifier(Protocol):
    def matches_receipt_digest(
        self, unsigned_body: Mapping[str, object], receipt_digest: str
    ) -> bool: ...


class WorkerReceiptVerifier(ReceiptVerifier, Protocol):
    def matches_worker_identity(self, identity: EvaluationIdentity) -> bool: ...


class ProcessIdentityVerifier(Protocol):
    def matches_live_process(self, process_id: int, process_start_token: str) -> bool: ...


def _parse_attestation(value: str) -> None:
    if type(value) is str and value.startswith("ed25519:") and len(value) == 94:
        return
    parse_digest(value)


class EvaluationTaskProfile(StrEnum):
    MAINTENANCE_V1 = "maintenance/v1"
    KNOWLEDGE_V1 = "knowledge/v1"


class EvaluationPartition(StrEnum):
    CALIBRATION = "calibration"
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class EvaluationCaseOrigin(StrEnum):
    REAL = "real"
    SYNTHETIC = "synthetic"


class EvaluationVerdict(StrEnum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    PLATEAU = "plateau"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"


@dataclass(frozen=True, slots=True)
class TypedMetricProfile:
    profile: EvaluationTaskProfile
    metric_ids: tuple[str, ...]
    hard_guard_ids: tuple[str, ...]
    primary_ids: tuple[str, ...]
    minimum_meaningful_delta: float

    def __post_init__(self) -> None:
        if type(self.profile) is not EvaluationTaskProfile:
            raise ValidationFailed("Exact evaluation task profile required")
        for values in (self.metric_ids, self.hard_guard_ids, self.primary_ids):
            if not values or values != tuple(sorted(set(values))):
                raise ValidationFailed("Typed metric identifiers must be canonical")
        known = set(self.metric_ids)
        if not set(self.hard_guard_ids) <= known or not set(self.primary_ids) <= known:
            raise ValidationFailed("Typed metric roles must reference known metrics")
        if (
            type(self.minimum_meaningful_delta) is not float
            or not 0.0 < self.minimum_meaningful_delta <= 1.0
        ):
            raise ValidationFailed("Typed metric meaningful delta invalid")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-typed-metric-profile/v1",
            "profile": str(self.profile),
            "metric_ids": list(self.metric_ids),
            "hard_guard_ids": list(self.hard_guard_ids),
            "primary_ids": list(self.primary_ids),
            "minimum_meaningful_delta": self.minimum_meaningful_delta,
            "score_scale": "bounded-ratio-0..1-higher-is-better",
        }

    @property
    def profile_digest(self) -> str:
        return digest(self.body())


_PROFILES = {
    EvaluationTaskProfile.MAINTENANCE_V1: TypedMetricProfile(
        EvaluationTaskProfile.MAINTENANCE_V1,
        (
            "continuity",
            "correctness",
            "efficiency",
            "reliability",
            "safety",
            "scope_fidelity",
        ),
        ("correctness", "safety", "scope_fidelity"),
        ("continuity", "correctness", "reliability"),
        0.02,
    ),
    EvaluationTaskProfile.KNOWLEDGE_V1: TypedMetricProfile(
        EvaluationTaskProfile.KNOWLEDGE_V1,
        (
            "abstention_correctness",
            "answer_correctness",
            "citation_validity",
            "efficiency",
            "freshness",
            "scope_fidelity",
        ),
        ("answer_correctness", "citation_validity", "scope_fidelity"),
        ("abstention_correctness", "answer_correctness", "freshness"),
        0.02,
    ),
}


def typed_metric_profile(profile: EvaluationTaskProfile) -> TypedMetricProfile:
    if type(profile) is not EvaluationTaskProfile:
        raise ValidationFailed("Exact evaluation task profile required")
    return _PROFILES[profile]


def deterministic_harness_outcome_digest(
    *,
    input_digest: str,
    artifact_digest: str,
    fixture_fingerprint: str,
    harness_fingerprint: str,
) -> str:
    for value in (
        input_digest,
        artifact_digest,
        fixture_fingerprint,
        harness_fingerprint,
    ):
        parse_digest(value)
    return digest(
        {
            "schema": "zekam-deterministic-evaluation-harness/v1",
            "input_digest": input_digest,
            "artifact_digest": artifact_digest,
            "fixture_fingerprint": fixture_fingerprint,
            "harness_fingerprint": harness_fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    partition: EvaluationPartition
    origin: EvaluationCaseOrigin
    independent_case_ref: str
    transcript_group_digest: str
    input_digest: str
    expected_outcome_digest: str
    provenance_receipt_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.case_id) is not str
            or not self.case_id.strip()
            or type(self.independent_case_ref) is not str
            or not self.independent_case_ref.strip()
        ):
            raise ValidationFailed("Evaluation case exact identity required")
        if (
            type(self.partition) is not EvaluationPartition
            or type(self.origin) is not EvaluationCaseOrigin
        ):
            raise ValidationFailed("Evaluation case exact partition/origin required")
        for value in (
            self.transcript_group_digest,
            self.input_digest,
            self.expected_outcome_digest,
        ):
            parse_digest(value)
        _parse_attestation(self.provenance_receipt_digest)

    def provenance_receipt_body(self) -> dict[str, str]:
        return {
            "schema": "zekam-evaluation-case-provenance/v1",
            "case_id": self.case_id,
            "partition": str(self.partition),
            "origin": str(self.origin),
            "independent_case_ref": self.independent_case_ref,
            "transcript_group_digest": self.transcript_group_digest,
            "input_digest": self.input_digest,
            "expected_outcome_digest": self.expected_outcome_digest,
        }

    def body(self) -> dict[str, str]:
        return self.provenance_receipt_body() | {
            "provenance_receipt_digest": self.provenance_receipt_digest
        }


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    dataset_id: str
    cases: tuple[EvaluationCase, ...]
    source_revision: str

    def __post_init__(self) -> None:
        if type(self.dataset_id) is not str or not self.dataset_id.strip():
            raise ValidationFailed("Evaluation dataset identity required")
        if type(self.source_revision) is not str or not self.source_revision.strip():
            raise ValidationFailed("Evaluation dataset source revision required")
        if (
            type(self.cases) is not tuple
            or not self.cases
            or any(type(item) is not EvaluationCase for item in self.cases)
        ):
            raise ValidationFailed("Evaluation dataset cases required")
        if self.cases != tuple(sorted(self.cases, key=lambda item: item.case_id)):
            raise ValidationFailed("Evaluation dataset cases must be canonical")
        for item in self.cases:
            item.__post_init__()
        for values in (
            tuple(item.case_id for item in self.cases),
            tuple(item.independent_case_ref for item in self.cases),
            tuple(item.transcript_group_digest for item in self.cases),
            tuple(item.input_digest for item in self.cases),
        ):
            if len(set(values)) != len(values):
                raise PolicyViolation("Evaluation cases/transcript groups cannot cross partitions")
        partitions = {item.partition for item in self.cases}
        if partitions != set(EvaluationPartition):
            raise ValidationFailed(
                "Calibration, development and holdout partitions are all required"
            )

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-evaluation-dataset/v1",
            "dataset_id": self.dataset_id,
            "source_revision": self.source_revision,
            "cases": [item.body() for item in self.cases],
            "real_case_count": self.real_case_count,
            "synthetic_case_count": self.synthetic_case_count,
            "independent_case_target": 20,
        }

    @property
    def dataset_digest(self) -> str:
        return digest(self.body())

    @property
    def real_case_count(self) -> int:
        return sum(item.origin is EvaluationCaseOrigin.REAL for item in self.cases)

    @property
    def synthetic_case_count(self) -> int:
        return sum(item.origin is EvaluationCaseOrigin.SYNTHETIC for item in self.cases)

    def cases_for(self, partition: EvaluationPartition) -> tuple[EvaluationCase, ...]:
        if type(partition) is not EvaluationPartition:
            raise ValidationFailed("Exact evaluation partition required")
        return tuple(item for item in self.cases if item.partition is partition)

    def evidence_sufficient_for_activation(self) -> bool:
        holdout_real = sum(
            item.origin is EvaluationCaseOrigin.REAL
            for item in self.cases_for(EvaluationPartition.HOLDOUT)
        )
        return self.real_case_count >= 20 and holdout_real >= 5


@dataclass(frozen=True, slots=True)
class EvaluationPlanBinding:
    improvement_candidate_digest: str
    ledger_baseline_contract_digest: str
    source_revision: str
    dataset_digest: str
    profile_digest: str
    baseline_artifact_digest: str
    candidate_artifact_digest: str
    source_fingerprint: str
    config_fingerprint: str
    fixture_fingerprint: str
    harness_fingerprint: str
    evaluator_fingerprint: str
    verifier_contract_digest: str
    policy_digest: str
    receipt_contract_digest: str
    model_fingerprint: str | None = None
    provider_fingerprint: str | None = None
    model_calls_authorized: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.improvement_candidate_digest,
            self.ledger_baseline_contract_digest,
            self.dataset_digest,
            self.profile_digest,
            self.baseline_artifact_digest,
            self.candidate_artifact_digest,
            self.source_fingerprint,
            self.config_fingerprint,
            self.fixture_fingerprint,
            self.harness_fingerprint,
            self.evaluator_fingerprint,
            self.verifier_contract_digest,
            self.policy_digest,
            self.receipt_contract_digest,
        ):
            parse_digest(value)
        if type(self.source_revision) is not str or not self.source_revision.strip():
            raise ValidationFailed("Evaluation plan source revision required")
        if (
            self.fixture_fingerprint != EVALUATION_FIXTURE_FINGERPRINT
            or self.harness_fingerprint != EVALUATION_HARNESS_FINGERPRINT
        ):
            raise PolicyViolation("Evaluation plan requires frozen evaluator-owned harness")
        if type(self.model_calls_authorized) is not bool:
            raise ValidationFailed("Evaluation model authorization flag must be exact bool")
        if self.model_calls_authorized:
            raise PolicyViolation(
                "Typed local evaluation cannot be presented as live model evidence"
            )
        if self.model_fingerprint is not None or self.provider_fingerprint is not None:
            raise PolicyViolation(
                "Model-free evaluation cannot fabricate model/provider fingerprints"
            )

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-evaluation-plan-binding/v1",
            "improvement_candidate_digest": self.improvement_candidate_digest,
            "ledger_baseline_contract_digest": self.ledger_baseline_contract_digest,
            "source_revision": self.source_revision,
            "dataset_digest": self.dataset_digest,
            "profile_digest": self.profile_digest,
            "baseline_artifact_digest": self.baseline_artifact_digest,
            "candidate_artifact_digest": self.candidate_artifact_digest,
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "provider_fingerprint": self.provider_fingerprint,
            "fixture_fingerprint": self.fixture_fingerprint,
            "harness_fingerprint": self.harness_fingerprint,
            "evaluator_fingerprint": self.evaluator_fingerprint,
            "verifier_contract_digest": self.verifier_contract_digest,
            "policy_digest": self.policy_digest,
            "receipt_contract_digest": self.receipt_contract_digest,
            "model_calls_authorized": self.model_calls_authorized,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def assert_unchanged(self, current: EvaluationPlanBinding) -> None:
        if type(current) is not EvaluationPlanBinding or current.plan_digest != self.plan_digest:
            raise PolicyViolation("Builder cannot alter evaluator/holdout/policy/receipt contracts")


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    assignment_id: UUID
    process_id: int
    process_start_token: str
    implementation_digest: str
    assignment_challenge_digest: str
    boundary_receipt_digest: str

    def __post_init__(self) -> None:
        if type(self.assignment_id) is not UUID:
            raise ValidationFailed("Evaluation identity exact assignment UUID required")
        if (
            type(self.process_id) is not int
            or isinstance(self.process_id, bool)
            or not 1 <= self.process_id <= 2_147_483_647
            or type(self.process_start_token) is not str
            or not self.process_start_token.startswith("psutil-create-time-micros:")
        ):
            raise ValidationFailed("Evaluation identity exact OS process incarnation required")
        parse_digest(self.implementation_digest)
        parse_digest(self.assignment_challenge_digest)
        _parse_attestation(self.boundary_receipt_digest)

    def boundary_receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-evaluation-process-boundary/v1",
            "assignment_id": str(self.assignment_id),
            "process_id": self.process_id,
            "process_start_token": self.process_start_token,
            "implementation_digest": self.implementation_digest,
            "assignment_challenge_digest": self.assignment_challenge_digest,
        }

    def body(self) -> dict[str, Any]:
        return self.boundary_receipt_body() | {
            "boundary_receipt_digest": self.boundary_receipt_digest
        }


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    case_id: str
    partition: EvaluationPartition
    condition_digest: str
    artifact_digest: str
    environment_digest: str
    outcome_digest: str
    producer_identity: EvaluationIdentity
    execution_receipt_digest: str
    metric_values: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if type(self.case_id) is not str or not self.case_id.strip():
            raise ValidationFailed("Evaluation result case identity required")
        if type(self.partition) is not EvaluationPartition:
            raise ValidationFailed("Evaluation result exact partition required")
        parse_digest(self.condition_digest)
        parse_digest(self.artifact_digest)
        parse_digest(self.environment_digest)
        parse_digest(self.outcome_digest)
        if type(self.producer_identity) is not EvaluationIdentity:
            raise ValidationFailed("Evaluation result producer identity required")
        self.producer_identity.__post_init__()
        _parse_attestation(self.execution_receipt_digest)
        if type(self.metric_values) is not tuple:
            raise ValidationFailed("Evaluation result metric tuple required")
        keys = tuple(key for key, _value in self.metric_values)
        if keys != tuple(sorted(set(keys))):
            raise ValidationFailed("Evaluation result metrics must be canonical")
        if any(
            type(value) is not float or not 0.0 <= value <= 1.0
            for _key, value in self.metric_values
        ):
            raise ValidationFailed("Evaluation result metrics require measured 0..1 ratios")

    def execution_receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-evaluation-case-execution/v1",
            "case_id": self.case_id,
            "partition": str(self.partition),
            "condition_digest": self.condition_digest,
            "artifact_digest": self.artifact_digest,
            "environment_digest": self.environment_digest,
            "outcome_digest": self.outcome_digest,
            "producer_identity": self.producer_identity.body(),
            "metric_values": dict(self.metric_values),
        }

    def body(self) -> dict[str, Any]:
        return self.execution_receipt_body() | {
            "execution_receipt_digest": self.execution_receipt_digest
        }


@dataclass(frozen=True, slots=True)
class PairedEvaluationReport:
    plan_digest: str
    dataset_digest: str
    profile_digest: str
    dataset: EvaluationDataset
    partition: EvaluationPartition
    builder_identity: EvaluationIdentity
    evaluator_identity: EvaluationIdentity
    baseline_results: tuple[EvaluationCaseResult, ...]
    candidate_results: tuple[EvaluationCaseResult, ...]
    real_case_count: int
    synthetic_case_count: int
    evaluated_real_case_count: int
    evaluated_synthetic_case_count: int
    evidence_sufficient: bool
    verdict: EvaluationVerdict
    metric_deltas: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        for value in (self.plan_digest, self.dataset_digest, self.profile_digest):
            parse_digest(value)
        if (
            type(self.dataset) is not EvaluationDataset
            or self.dataset.dataset_digest != self.dataset_digest
        ):
            raise PolicyViolation("Paired report dataset manifest binding drift")
        self.dataset.__post_init__()
        if self.partition is EvaluationPartition.CALIBRATION:
            raise PolicyViolation("Calibration cannot be a paired candidate report")
        if (
            type(self.builder_identity) is not EvaluationIdentity
            or type(self.evaluator_identity) is not EvaluationIdentity
        ):
            raise ValidationFailed("Paired report exact builder/evaluator identities required")
        self.builder_identity.__post_init__()
        self.evaluator_identity.__post_init__()
        if (
            self.builder_identity.assignment_id == self.evaluator_identity.assignment_id
            or (
                self.builder_identity.process_id,
                self.builder_identity.process_start_token,
            )
            == (
                self.evaluator_identity.process_id,
                self.evaluator_identity.process_start_token,
            )
            or self.builder_identity.implementation_digest
            == self.evaluator_identity.implementation_digest
        ):
            raise PolicyViolation("Paired report builder and evaluator are not independent")
        if (
            type(self.baseline_results) is not tuple
            or type(self.candidate_results) is not tuple
            or not self.baseline_results
            or len(self.baseline_results) != len(self.candidate_results)
            or any(
                type(item) is not EvaluationCaseResult
                for item in (*self.baseline_results, *self.candidate_results)
            )
        ):
            raise ValidationFailed("Paired report exact result pairs required")
        for item in (*self.baseline_results, *self.candidate_results):
            item.__post_init__()
        baseline_ids = tuple(item.case_id for item in self.baseline_results)
        expected_ids = tuple(item.case_id for item in self.dataset.cases_for(self.partition))
        if (
            baseline_ids != tuple(sorted(set(baseline_ids)))
            or baseline_ids != expected_ids
            or baseline_ids != tuple(item.case_id for item in self.candidate_results)
            or any(
                baseline.partition is not self.partition
                or candidate.partition is not self.partition
                or baseline.condition_digest != candidate.condition_digest
                or baseline.environment_digest != candidate.environment_digest
                or baseline.producer_identity != self.evaluator_identity
                or candidate.producer_identity != self.evaluator_identity
                for baseline, candidate in zip(
                    self.baseline_results, self.candidate_results, strict=True
                )
            )
        ):
            raise PolicyViolation("Paired report baseline/candidate condition drift")
        counts = (
            self.real_case_count,
            self.synthetic_case_count,
            self.evaluated_real_case_count,
            self.evaluated_synthetic_case_count,
        )
        if any(type(value) is not int or isinstance(value, bool) or value < 0 for value in counts):
            raise ValidationFailed("Paired report exact case counts required")
        if (
            self.evaluated_real_case_count + self.evaluated_synthetic_case_count
            != len(self.baseline_results)
            or self.evaluated_real_case_count > self.real_case_count
            or self.evaluated_synthetic_case_count > self.synthetic_case_count
            or self.real_case_count != self.dataset.real_case_count
            or self.synthetic_case_count != self.dataset.synthetic_case_count
        ):
            raise PolicyViolation("Paired report real/synthetic case count drift")
        profile = next(
            (item for item in _PROFILES.values() if item.profile_digest == self.profile_digest),
            None,
        )
        if profile is None:
            raise PolicyViolation("Paired report unknown typed profile digest")
        metric_ids = tuple(key for key, _value in self.baseline_results[0].metric_values)
        if metric_ids != profile.metric_ids or any(
            tuple(key for key, _value in item.metric_values) != metric_ids
            for item in (*self.baseline_results[1:], *self.candidate_results)
        ):
            raise PolicyViolation("Paired report typed metric set drift")
        recalculated = tuple(
            (
                metric,
                statistics.fmean(
                    dict(item.metric_values)[metric] for item in self.candidate_results
                )
                - statistics.fmean(
                    dict(item.metric_values)[metric] for item in self.baseline_results
                ),
            )
            for metric in metric_ids
        )
        if type(self.metric_deltas) is not tuple or self.metric_deltas != recalculated:
            raise PolicyViolation("Paired report metric delta drift")
        sufficient = (
            self.partition is EvaluationPartition.HOLDOUT
            and self.real_case_count >= 20
            and self.evaluated_real_case_count >= 5
        )
        deltas = dict(recalculated)
        regressed = any(deltas[metric] < 0.0 for metric in profile.hard_guard_ids)
        meaningful = any(
            deltas[metric] >= profile.minimum_meaningful_delta for metric in profile.primary_ids
        )
        expected_verdict = (
            EvaluationVerdict.REGRESSED
            if regressed
            else EvaluationVerdict.INSUFFICIENT_EVIDENCE
            if not sufficient
            else EvaluationVerdict.IMPROVED
            if meaningful
            else EvaluationVerdict.PLATEAU
        )
        if self.evidence_sufficient is not sufficient or self.verdict is not expected_verdict:
            raise PolicyViolation("Paired report evidence/verdict derivation drift")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-paired-evaluation-report/v1",
            "plan_digest": self.plan_digest,
            "dataset_digest": self.dataset_digest,
            "profile_digest": self.profile_digest,
            "dataset_manifest": self.dataset.body(),
            "partition": str(self.partition),
            "builder_identity": self.builder_identity.body(),
            "evaluator_identity": self.evaluator_identity.body(),
            "baseline_results": [item.body() for item in self.baseline_results],
            "candidate_results": [item.body() for item in self.candidate_results],
            "real_case_count": self.real_case_count,
            "synthetic_case_count": self.synthetic_case_count,
            "evaluated_real_case_count": self.evaluated_real_case_count,
            "evaluated_synthetic_case_count": self.evaluated_synthetic_case_count,
            "independent_case_target": 20,
            "evidence_sufficient": self.evidence_sufficient,
            "verdict": str(self.verdict),
            "metric_deltas": dict(self.metric_deltas),
            "grants_authority": False,
        }

    @property
    def report_digest(self) -> str:
        return digest(self.body())


def build_paired_evaluation_report(
    *,
    plan: EvaluationPlanBinding,
    dataset: EvaluationDataset,
    profile: TypedMetricProfile,
    partition: EvaluationPartition,
    builder_identity: EvaluationIdentity,
    evaluator_identity: EvaluationIdentity,
    baseline_results: tuple[EvaluationCaseResult, ...],
    candidate_results: tuple[EvaluationCaseResult, ...],
    builder_verifier: WorkerReceiptVerifier,
    execution_verifier: WorkerReceiptVerifier,
    process_verifier: ProcessIdentityVerifier,
) -> PairedEvaluationReport:
    from zekam.infrastructure.evaluation_process_worker import EvaluationWorkerVerifier

    if any(
        type(item) is not expected
        for item, expected in (
            (plan, EvaluationPlanBinding),
            (dataset, EvaluationDataset),
            (profile, TypedMetricProfile),
            (partition, EvaluationPartition),
            (builder_identity, EvaluationIdentity),
            (evaluator_identity, EvaluationIdentity),
        )
    ):
        raise ValidationFailed("Exact paired evaluation inputs required")
    if (
        type(builder_verifier) is not EvaluationWorkerVerifier
        or type(execution_verifier) is not EvaluationWorkerVerifier
        or builder_verifier.role != "builder"
        or execution_verifier.role != "evaluator"
        or not hasattr(builder_verifier, "matches_receipt_digest")
        or not hasattr(execution_verifier, "matches_receipt_digest")
        or not hasattr(builder_verifier, "matches_worker_identity")
        or not hasattr(execution_verifier, "matches_worker_identity")
        or not hasattr(process_verifier, "matches_live_process")
    ):
        raise ValidationFailed("Paired evaluation receipt verifier required")
    if partition is EvaluationPartition.CALIBRATION:
        raise PolicyViolation("Calibration records cannot be candidate evaluation evidence")
    if (
        plan.dataset_digest != dataset.dataset_digest
        or plan.profile_digest != profile.profile_digest
        or builder_identity == evaluator_identity
        or not builder_identity.boundary_receipt_digest.startswith("ed25519:")
        or not evaluator_identity.boundary_receipt_digest.startswith("ed25519:")
        or not builder_verifier.matches_worker_identity(builder_identity)
        or not execution_verifier.matches_worker_identity(evaluator_identity)
        or not builder_verifier.matches_receipt_digest(
            builder_identity.boundary_receipt_body(),
            builder_identity.boundary_receipt_digest,
        )
        or not execution_verifier.matches_receipt_digest(
            evaluator_identity.boundary_receipt_body(),
            evaluator_identity.boundary_receipt_digest,
        )
        or not process_verifier.matches_live_process(
            builder_identity.process_id, builder_identity.process_start_token
        )
        or not process_verifier.matches_live_process(
            evaluator_identity.process_id, evaluator_identity.process_start_token
        )
    ):
        raise PolicyViolation("Paired evaluation plan or independent evaluator binding drift")
    expected_cases = dataset.cases_for(partition)
    expected_ids = tuple(item.case_id for item in expected_cases)
    baseline_ids = tuple(item.case_id for item in baseline_results)
    candidate_ids = tuple(item.case_id for item in candidate_results)
    if not expected_ids or baseline_ids != expected_ids or candidate_ids != expected_ids:
        raise PolicyViolation("Baseline and candidate must use the exact same partition cases")
    metric_ids = profile.metric_ids
    for case, baseline, candidate in zip(
        expected_cases, baseline_results, candidate_results, strict=True
    ):
        expected_condition = digest(
            {
                "plan_digest": plan.plan_digest,
                "case_id": case.case_id,
                "input_digest": case.input_digest,
                "partition": str(partition),
                "environment_digest": baseline.environment_digest,
            }
        )
        if (
            baseline.partition is not partition
            or candidate.partition is not partition
            or baseline.condition_digest != expected_condition
            or candidate.condition_digest != expected_condition
            or tuple(key for key, _value in baseline.metric_values) != metric_ids
            or tuple(key for key, _value in candidate.metric_values) != metric_ids
            or not baseline.execution_receipt_digest.startswith("ed25519:")
            or not candidate.execution_receipt_digest.startswith("ed25519:")
            or not execution_verifier.matches_receipt_digest(
                baseline.execution_receipt_body(), baseline.execution_receipt_digest
            )
            or not execution_verifier.matches_receipt_digest(
                candidate.execution_receipt_body(), candidate.execution_receipt_digest
            )
        ):
            raise PolicyViolation("Paired evaluation conditions or typed metrics drift")
    baseline_by_metric = {
        metric: statistics.fmean(dict(item.metric_values)[metric] for item in baseline_results)
        for metric in metric_ids
    }
    candidate_by_metric = {
        metric: statistics.fmean(dict(item.metric_values)[metric] for item in candidate_results)
        for metric in metric_ids
    }
    deltas = tuple(
        (metric, candidate_by_metric[metric] - baseline_by_metric[metric]) for metric in metric_ids
    )
    regressed = any(dict(deltas)[metric] < 0.0 for metric in profile.hard_guard_ids)
    meaningful = any(
        dict(deltas)[metric] >= profile.minimum_meaningful_delta for metric in profile.primary_ids
    )
    evaluated_real_count = sum(
        item.origin is EvaluationCaseOrigin.REAL for item in expected_cases
    )
    evaluated_synthetic_count = sum(
        item.origin is EvaluationCaseOrigin.SYNTHETIC for item in expected_cases
    )
    sufficient = (
        partition is EvaluationPartition.HOLDOUT
        and dataset.evidence_sufficient_for_activation()
    )
    if regressed:
        verdict = EvaluationVerdict.REGRESSED
    elif not sufficient:
        verdict = EvaluationVerdict.INSUFFICIENT_EVIDENCE
    elif meaningful:
        verdict = EvaluationVerdict.IMPROVED
    else:
        verdict = EvaluationVerdict.PLATEAU
    return PairedEvaluationReport(
        plan.plan_digest,
        dataset.dataset_digest,
        profile.profile_digest,
        dataset,
        partition,
        builder_identity,
        evaluator_identity,
        baseline_results,
        candidate_results,
        dataset.real_case_count,
        dataset.synthetic_case_count,
        evaluated_real_count,
        evaluated_synthetic_count,
        sufficient,
        verdict,
        deltas,
    )


@dataclass(frozen=True, slots=True)
class IndependentEvaluationReceipt:
    report_digest: str
    plan_digest: str
    verifier_identity: EvaluationIdentity
    accepted: bool
    evidence_digest: str

    def __post_init__(self) -> None:
        for value in (self.report_digest, self.plan_digest):
            parse_digest(value)
        _parse_attestation(self.evidence_digest)
        if (
            type(self.verifier_identity) is not EvaluationIdentity
            or type(self.accepted) is not bool
        ):
            raise ValidationFailed("Exact independent evaluation receipt required")
        self.verifier_identity.__post_init__()

    def verification_receipt_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-independent-evaluation-receipt/v1",
            "report_digest": self.report_digest,
            "plan_digest": self.plan_digest,
            "verifier_identity": self.verifier_identity.body(),
            "accepted": self.accepted,
            "grants_authority": False,
        }

    def body(self) -> dict[str, Any]:
        return self.verification_receipt_body() | {"evidence_digest": self.evidence_digest}

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())


def assert_trusted_evaluation_receipts(
    report: PairedEvaluationReport,
    receipt: IndependentEvaluationReceipt,
    *,
    provenance_verifier: ReceiptVerifier,
    builder_verifier: WorkerReceiptVerifier,
    execution_verifier: WorkerReceiptVerifier,
    independent_verifier: WorkerReceiptVerifier,
    process_verifier: ProcessIdentityVerifier,
) -> None:
    from zekam.infrastructure.evaluation_process_worker import EvaluationWorkerVerifier

    if (
        type(report) is not PairedEvaluationReport
        or type(receipt) is not IndependentEvaluationReceipt
    ):
        raise ValidationFailed("Exact activation evidence required")
    identities = (
        report.builder_identity,
        report.evaluator_identity,
        receipt.verifier_identity,
    )
    if (
        type(builder_verifier) is not EvaluationWorkerVerifier
        or type(execution_verifier) is not EvaluationWorkerVerifier
        or type(independent_verifier) is not EvaluationWorkerVerifier
        or builder_verifier.role != "builder"
        or execution_verifier.role != "evaluator"
        or independent_verifier.role != "verifier"
    ):
        raise PolicyViolation("Evaluation requires exact spawned worker role registry")
    if len({item.assignment_id for item in identities}) != 3 or len(
        {(item.process_id, item.process_start_token) for item in identities}
    ) != 3 or len({item.implementation_digest for item in identities}) != 3:
        raise PolicyViolation(
            "Builder, evaluator and verifier require independent assignments/processes"
        )
    if any(
        not identity.boundary_receipt_digest.startswith("ed25519:")
        for identity in identities
    ) or not receipt.evidence_digest.startswith("ed25519:"):
        raise PolicyViolation("Evaluation authority requires worker-confined signatures")
    for identity, verifier in (
        (report.builder_identity, builder_verifier),
        (report.evaluator_identity, execution_verifier),
        (receipt.verifier_identity, independent_verifier),
    ):
        if not verifier.matches_worker_identity(identity):
            raise PolicyViolation("Evaluation receipt signer is not the bound spawned worker")
        if not verifier.matches_receipt_digest(
            identity.boundary_receipt_body(), identity.boundary_receipt_digest
        ):
            raise PolicyViolation("Evaluation identity lacks trusted process-boundary receipt")
        if not process_verifier.matches_live_process(
            identity.process_id, identity.process_start_token
        ):
            raise PolicyViolation("Evaluation identity OS process readback failed")
    if any(
        not item.provenance_receipt_digest.startswith("ed25519:")
        or not provenance_verifier.matches_receipt_digest(
            item.provenance_receipt_body(), item.provenance_receipt_digest
        )
        for item in report.dataset.cases
    ):
        raise PolicyViolation("Evaluation dataset lacks trusted provenance receipts")
    if any(
        not item.execution_receipt_digest.startswith("ed25519:")
        or not execution_verifier.matches_receipt_digest(
            item.execution_receipt_body(), item.execution_receipt_digest
        )
        for item in (*report.baseline_results, *report.candidate_results)
    ):
        raise PolicyViolation("Evaluation report lacks trusted execution receipts")
    if not independent_verifier.matches_receipt_digest(
        receipt.verification_receipt_body(), receipt.evidence_digest
    ):
        raise PolicyViolation("Evaluation independent verifier receipt is untrusted")
    if (
        receipt.report_digest != report.report_digest
        or receipt.plan_digest != report.plan_digest
    ):
        raise PolicyViolation("Evaluation verifier receipt report/plan binding drift")


def assert_activation_evidence(
    report: PairedEvaluationReport,
    receipt: IndependentEvaluationReceipt,
    *,
    provenance_verifier: ReceiptVerifier,
    builder_verifier: WorkerReceiptVerifier,
    execution_verifier: WorkerReceiptVerifier,
    independent_verifier: WorkerReceiptVerifier,
    process_verifier: ProcessIdentityVerifier,
) -> None:
    assert_trusted_evaluation_receipts(
        report,
        receipt,
        provenance_verifier=provenance_verifier,
        builder_verifier=builder_verifier,
        execution_verifier=execution_verifier,
        independent_verifier=independent_verifier,
        process_verifier=process_verifier,
    )
    if (
        not receipt.accepted
        or report.partition is not EvaluationPartition.HOLDOUT
        or not report.evidence_sufficient
        or report.verdict is not EvaluationVerdict.IMPROVED
    ):
        raise PolicyViolation("Candidate lacks accepted sufficient holdout evidence")
