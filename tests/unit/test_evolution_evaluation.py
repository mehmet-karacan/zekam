from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zekam.application.local_attestation import (
    Ed25519ReceiptSigner,
    Ed25519ReceiptVerifier,
    LocalProcessIdentityVerifier,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.evolution_evaluation import (
    EVALUATION_FIXTURE_FINGERPRINT,
    EVALUATION_GOLDEN_ARTIFACT_DIGEST,
    EVALUATION_HARNESS_FINGERPRINT,
    EvaluationCase,
    EvaluationCaseOrigin,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationIdentity,
    EvaluationPartition,
    EvaluationPlanBinding,
    EvaluationTaskProfile,
    EvaluationVerdict,
    assert_activation_evidence,
    build_paired_evaluation_report,
    typed_metric_profile,
)
from zekam.infrastructure.evaluation_process_worker import (
    EvaluationProcessWorker,
    EvaluationWorkerVerifier,
)

PROCESS_VERIFIER = LocalProcessIdentityVerifier()
_WORKERS: tuple[EvaluationProcessWorker, ...] = ()
PROVENANCE_VERIFIER: EvaluationWorkerVerifier
BUILDER_VERIFIER: EvaluationWorkerVerifier
EXECUTION_VERIFIER: EvaluationWorkerVerifier
INDEPENDENT_VERIFIER: EvaluationWorkerVerifier


@pytest.fixture(scope="module", autouse=True)
def _live_role_processes() -> Iterator[None]:
    global BUILDER_VERIFIER, EXECUTION_VERIFIER, INDEPENDENT_VERIFIER
    global PROVENANCE_VERIFIER, _WORKERS
    _WORKERS = tuple(
        EvaluationProcessWorker(
            UUID(int=index + 1),
            digest(f"implementation:{index}"),
            digest(f"challenge:{index}"),
            ("provenance", "builder", "evaluator", "verifier")[index],
        )
        for index in range(4)
    )
    PROVENANCE_VERIFIER = _WORKERS[0].verifier
    BUILDER_VERIFIER = _WORKERS[1].verifier
    EXECUTION_VERIFIER = _WORKERS[2].verifier
    INDEPENDENT_VERIFIER = _WORKERS[3].verifier
    try:
        yield
    finally:
        for worker in _WORKERS:
            worker.close()


def _identity(number: int) -> EvaluationIdentity:
    return _WORKERS[number].identity


def _dataset(*, count: int = 24, regression: bool = False) -> EvaluationDataset:
    cases: list[EvaluationCase] = []
    for index in range(count):
        if index < max(1, count // 6):
            partition = EvaluationPartition.CALIBRATION
        elif index < max(2, count // 2):
            partition = EvaluationPartition.DEVELOPMENT
        else:
            partition = EvaluationPartition.HOLDOUT
        origin = (
            EvaluationCaseOrigin.SYNTHETIC
            if count >= 20 and 4 <= index < 8
            else EvaluationCaseOrigin.REAL
        )
        input_value = digest(f"input:{index}")
        draft = EvaluationCase(
                f"case-{index:03d}",
                partition,
                origin,
                f"work:{index:03d}",
                digest(f"transcript:{index}"),
                input_value,
                digest(f"caller-expected:{index}"),
                digest(f"provenance-receipt:{index}"),
            )
        cases.append(draft)
    return _WORKERS[0].attest_dataset(
        EvaluationDataset(
            "maintenance-regression/v1" if regression else "maintenance-cases/v1",
            tuple(cases),
            "revision-1",
        )
    )


def _plan(dataset: EvaluationDataset) -> EvaluationPlanBinding:
    profile = typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1)
    regression = "regression" in dataset.dataset_id
    return EvaluationPlanBinding(
        digest("improvement-candidate"),
        digest("ledger-baseline-contract"),
        "revision-1",
        dataset.dataset_digest,
        profile.profile_digest,
        EVALUATION_GOLDEN_ARTIFACT_DIGEST
        if regression
        else digest("baseline-artifact"),
        digest("candidate-artifact")
        if regression
        else EVALUATION_GOLDEN_ARTIFACT_DIGEST,
        digest("source"),
        digest("config"),
        EVALUATION_FIXTURE_FINGERPRINT,
        EVALUATION_HARNESS_FINGERPRINT,
        digest("evaluator"),
        digest("verifier-contract"),
        digest("policy"),
        digest("receipt-contract"),
    )


def _results(
    dataset: EvaluationDataset,
    plan: EvaluationPlanBinding,
    *,
    candidate: bool,
) -> tuple[EvaluationCaseResult, ...]:
    profile = typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1)
    results: list[EvaluationCaseResult] = []
    for case in dataset.cases_for(EvaluationPartition.HOLDOUT):
        results.append(
            _WORKERS[2].evaluate_case(
                case=case,
                plan=plan,
                profile=profile,
                partition=EvaluationPartition.HOLDOUT,
                artifact_digest=(
                    plan.candidate_artifact_digest
                    if candidate
                    else plan.baseline_artifact_digest
                ),
                environment_digest=digest("local-eval-environment"),
            )
        )
    return tuple(results)


def test_paired_holdout_report_tracks_real_and_synthetic_without_fake_model_fields() -> None:
    dataset = _dataset()
    plan = _plan(dataset)
    profile = typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1)
    report = build_paired_evaluation_report(
        plan=plan,
        dataset=dataset,
        profile=profile,
        partition=EvaluationPartition.HOLDOUT,
        builder_identity=_identity(1),
        evaluator_identity=_identity(2),
        baseline_results=_results(dataset, plan, candidate=False),
        candidate_results=_results(dataset, plan, candidate=True),
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )

    assert dataset.real_case_count == 20
    assert dataset.synthetic_case_count == 4
    assert plan.body()["model_fingerprint"] is None
    assert plan.body()["provider_fingerprint"] is None
    assert report.verdict is EvaluationVerdict.IMPROVED
    assert report.evidence_sufficient is True
    receipt = _WORKERS[3].verify_report(
        report=report,
        plan=plan,
        builder_verifier=BUILDER_VERIFIER,
        evaluator_verifier=EXECUTION_VERIFIER,
    )
    assert_activation_evidence(
        report,
        receipt,
        provenance_verifier=PROVENANCE_VERIFIER,
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        independent_verifier=INDEPENDENT_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )
    assert report.body()["grants_authority"] is False


def test_small_or_development_set_stays_insufficient_evidence() -> None:
    dataset = _dataset(count=6)
    plan = _plan(dataset)
    report = build_paired_evaluation_report(
        plan=plan,
        dataset=dataset,
        profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
        partition=EvaluationPartition.HOLDOUT,
        builder_identity=_identity(1),
        evaluator_identity=_identity(2),
        baseline_results=_results(dataset, plan, candidate=False),
        candidate_results=_results(dataset, plan, candidate=True),
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )
    assert report.verdict is EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert report.real_case_count == 6
    assert report.synthetic_case_count == 0
    assert report.evaluated_real_case_count == 3

    receipt = _WORKERS[3].verify_report(
        report=report,
        plan=plan,
        builder_verifier=BUILDER_VERIFIER,
        evaluator_verifier=EXECUTION_VERIFIER,
    )
    with pytest.raises(PolicyViolation, match="sufficient holdout"):
        assert_activation_evidence(
            report,
            receipt,
            provenance_verifier=PROVENANCE_VERIFIER,
            builder_verifier=BUILDER_VERIFIER,
            execution_verifier=EXECUTION_VERIFIER,
            independent_verifier=INDEPENDENT_VERIFIER,
            process_verifier=PROCESS_VERIFIER,
        )


def test_partition_leak_and_unpaired_conditions_are_rejected() -> None:
    dataset = _dataset()
    duplicate = replace(
        dataset.cases[-1], transcript_group_digest=dataset.cases[0].transcript_group_digest
    )
    with pytest.raises(PolicyViolation, match="cross partitions"):
        EvaluationDataset(
            dataset.dataset_id,
            tuple(sorted((*dataset.cases[:-1], duplicate), key=lambda item: item.case_id)),
            dataset.source_revision,
        )

    plan = _plan(dataset)
    candidate = list(_results(dataset, plan, candidate=True))
    candidate[0] = replace(candidate[0], condition_digest=digest("different-condition"))
    with pytest.raises(PolicyViolation, match="conditions"):
        build_paired_evaluation_report(
            plan=plan,
            dataset=dataset,
            profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
            partition=EvaluationPartition.HOLDOUT,
            builder_identity=_identity(1),
            evaluator_identity=_identity(2),
            baseline_results=_results(dataset, plan, candidate=False),
            candidate_results=tuple(candidate),
            builder_verifier=BUILDER_VERIFIER,
            execution_verifier=EXECUTION_VERIFIER,
            process_verifier=PROCESS_VERIFIER,
        )


def test_hard_guard_regression_wins_over_efficiency_and_contract_is_immutable() -> None:
    dataset = _dataset(regression=True)
    plan = _plan(dataset)
    report = build_paired_evaluation_report(
        plan=plan,
        dataset=dataset,
        profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
        partition=EvaluationPartition.HOLDOUT,
        builder_identity=_identity(1),
        evaluator_identity=_identity(2),
        baseline_results=_results(dataset, plan, candidate=False),
        candidate_results=_results(dataset, plan, candidate=True),
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )
    assert report.verdict is EvaluationVerdict.REGRESSED

    changed = replace(plan, policy_digest=digest("builder-changed-policy"))
    with pytest.raises(PolicyViolation, match="Builder cannot alter"):
        plan.assert_unchanged(changed)
    with pytest.raises(PolicyViolation, match="cannot fabricate"):
        replace(plan, model_fingerprint=digest("fake-model"))


def test_calibration_cannot_be_candidate_evidence_and_verifier_is_independent() -> None:
    dataset = _dataset()
    plan = _plan(dataset)
    with pytest.raises(PolicyViolation, match="Calibration"):
        build_paired_evaluation_report(
            plan=plan,
            dataset=dataset,
            profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
            partition=EvaluationPartition.CALIBRATION,
            builder_identity=_identity(1),
            evaluator_identity=_identity(2),
            baseline_results=(),
            candidate_results=(),
            builder_verifier=BUILDER_VERIFIER,
            execution_verifier=EXECUTION_VERIFIER,
            process_verifier=PROCESS_VERIFIER,
        )

    report = build_paired_evaluation_report(
        plan=plan,
        dataset=dataset,
        profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
        partition=EvaluationPartition.HOLDOUT,
        builder_identity=_identity(1),
        evaluator_identity=_identity(2),
        baseline_results=_results(dataset, plan, candidate=False),
        candidate_results=_results(dataset, plan, candidate=True),
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )
    signed_receipt = _WORKERS[3].verify_report(
        report=report,
        plan=plan,
        builder_verifier=BUILDER_VERIFIER,
        evaluator_verifier=EXECUTION_VERIFIER,
    )
    receipt = replace(
        signed_receipt,
        verifier_identity=replace(
            _identity(3),
            process_id=report.evaluator_identity.process_id,
            process_start_token=report.evaluator_identity.process_start_token,
        ),
    )
    with pytest.raises(PolicyViolation, match="independent"):
        assert_activation_evidence(
            report,
            receipt,
            provenance_verifier=PROVENANCE_VERIFIER,
            builder_verifier=BUILDER_VERIFIER,
            execution_verifier=EXECUTION_VERIFIER,
            independent_verifier=INDEPENDENT_VERIFIER,
            process_verifier=PROCESS_VERIFIER,
        )


def test_report_verdict_and_case_counts_cannot_be_forged() -> None:
    dataset = _dataset(count=6)
    plan = _plan(dataset)
    report = build_paired_evaluation_report(
        plan=plan,
        dataset=dataset,
        profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
        partition=EvaluationPartition.HOLDOUT,
        builder_identity=_identity(1),
        evaluator_identity=_identity(2),
        baseline_results=_results(dataset, plan, candidate=False),
        candidate_results=_results(dataset, plan, candidate=True),
        builder_verifier=BUILDER_VERIFIER,
        execution_verifier=EXECUTION_VERIFIER,
        process_verifier=PROCESS_VERIFIER,
    )
    with pytest.raises(PolicyViolation, match="derivation"):
        replace(
            report,
            evidence_sufficient=True,
            verdict=EvaluationVerdict.IMPROVED,
        )
    with pytest.raises(PolicyViolation, match="case count"):
        replace(report, real_case_count=20)


def test_parent_generated_key_cannot_pose_as_spawned_builder_worker() -> None:
    dataset = _dataset()
    plan = _plan(dataset)
    parent_key = Ed25519PrivateKey.generate()
    parent_signer = Ed25519ReceiptSigner(parent_key)
    forged_draft = replace(
        _identity(1), boundary_receipt_digest=digest("parent-forgery-placeholder")
    )
    forged = replace(
        forged_draft,
        boundary_receipt_digest=parent_signer.seal_digest(
            forged_draft.boundary_receipt_body()
        ),
    )
    parent_verifier = Ed25519ReceiptVerifier(parent_key.public_key())
    with pytest.raises(ValidationFailed, match="receipt verifier"):
        build_paired_evaluation_report(
            plan=plan,
            dataset=dataset,
            profile=typed_metric_profile(EvaluationTaskProfile.MAINTENANCE_V1),
            partition=EvaluationPartition.HOLDOUT,
            builder_identity=forged,
            evaluator_identity=_identity(2),
            baseline_results=_results(dataset, plan, candidate=False),
            candidate_results=_results(dataset, plan, candidate=True),
            builder_verifier=parent_verifier,  # type: ignore[arg-type]
            execution_verifier=EXECUTION_VERIFIER,
            process_verifier=PROCESS_VERIFIER,
        )


def test_parent_cannot_request_arbitrary_metric_or_acceptance_signatures() -> None:
    with pytest.raises(PolicyViolation, match="Generic"):
        _WORKERS[2].reject_untyped_sign_request(
            {"producer_identity": _identity(2).body(), "correctness": 1.0}
        )
    with pytest.raises(PolicyViolation, match="Generic"):
        _WORKERS[3].reject_untyped_sign_request(
            {"verifier_identity": _identity(3).body(), "accepted": True}
        )


def test_worker_terminal_receipt_is_signed_before_clean_exit() -> None:
    worker = EvaluationProcessWorker(
        UUID("90000000-0000-0000-0000-000000000009"),
        digest("terminal-worker-implementation"),
        digest("terminal-worker-challenge"),
        "verifier",
    )
    terminal_body, terminal_receipt = worker.close()
    assert terminal_body["status"] == "completed"
    assert worker.verifier.matches_receipt_digest(terminal_body, terminal_receipt)
