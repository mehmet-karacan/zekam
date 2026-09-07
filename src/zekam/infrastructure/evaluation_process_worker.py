"""Spawned asymmetric receipt worker for independent evaluation roles."""

from __future__ import annotations

import multiprocessing as mp
from collections.abc import Mapping
from dataclasses import replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from zekam.application.local_attestation import Ed25519ReceiptSigner, Ed25519ReceiptVerifier
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.evolution_evaluation import (
    EVALUATION_FIXTURE_FINGERPRINT,
    EVALUATION_GOLDEN_ARTIFACT_DIGEST,
    EVALUATION_HARNESS_FINGERPRINT,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationIdentity,
    EvaluationPartition,
    EvaluationPlanBinding,
    IndependentEvaluationReceipt,
    PairedEvaluationReport,
    TypedMetricProfile,
    deterministic_harness_outcome_digest,
)
from zekam.infrastructure.process_identity import process_incarnation_token

_WORKER_VERIFIER_TOKEN = object()


class EvaluationWorkerVerifier:
    """Public-key verifier bound to the exact spawned process handle and startup identity."""

    def __init__(
        self,
        token: object,
        process: BaseProcess,
        identity: EvaluationIdentity,
        public_key: bytes,
        role: str,
    ) -> None:
        if token is not _WORKER_VERIFIER_TOKEN:
            raise PolicyViolation("Evaluation worker verifier requires spawned composition")
        self._process = process
        self._identity = identity
        self._delegate = Ed25519ReceiptVerifier(
            Ed25519PublicKey.from_public_bytes(public_key)
        )
        self.role = role
        self.public_key = public_key

    @property
    def identity(self) -> EvaluationIdentity:
        return self._identity

    def matches_receipt_digest(
        self, unsigned_body: Mapping[str, object], receipt_digest: str
    ) -> bool:
        return self._delegate.matches_receipt_digest(unsigned_body, receipt_digest)

    def matches_worker_identity(self, identity: EvaluationIdentity) -> bool:
        return (
            identity == self._identity
            and self._process.pid == identity.process_id
            and self._process.is_alive()
            and process_incarnation_token(identity.process_id)
            == identity.process_start_token
        )


def _worker_main(
    channel: Connection,
    assignment_id: UUID,
    implementation_digest: str,
    challenge_digest: str,
    role: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = Ed25519ReceiptSigner(private_key)
    process_id = mp.current_process().pid
    if process_id is None:
        raise RuntimeError("Evaluation worker process identity unavailable")
    process_token = process_incarnation_token(process_id)
    if process_token is None:
        raise RuntimeError("Evaluation worker process incarnation unavailable")
    draft = EvaluationIdentity(
        assignment_id,
        process_id,
        process_token,
        implementation_digest,
        challenge_digest,
        digest("worker-boundary-placeholder"),
    )
    identity = replace(
        draft,
        boundary_receipt_digest=signer.seal_digest(draft.boundary_receipt_body()),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    channel.send((identity, public_key))
    receipt_count = 0
    finalized = False
    try:
        while True:
            message = channel.recv()
            if message == "finalize":
                terminal_body = {
                    "schema": "zekam-evaluation-worker-terminal/v1",
                    "assignment_id": str(assignment_id),
                    "assignment_challenge_digest": challenge_digest,
                    "process_id": process_id,
                    "process_start_token": process_token,
                    "implementation_digest": implementation_digest,
                    "receipt_count": receipt_count,
                    "status": "completed",
                }
                channel.send((terminal_body, signer.seal_digest(terminal_body)))
                finalized = True
                continue
            if message == "exit" and finalized:
                return
            command = message[0] if type(message) is tuple and message else None
            if finalized:
                channel.send(None)
                continue
            if command == "attest-dataset" and role == "provenance":
                _command, dataset = message
                if type(dataset) is not EvaluationDataset:
                    channel.send(None)
                    continue
                cases_list: list[EvaluationCase] = []
                for case in dataset.cases:
                    authority_draft = replace(
                        case,
                        expected_outcome_digest=deterministic_harness_outcome_digest(
                            input_digest=case.input_digest,
                            artifact_digest=EVALUATION_GOLDEN_ARTIFACT_DIGEST,
                            fixture_fingerprint=EVALUATION_FIXTURE_FINGERPRINT,
                            harness_fingerprint=EVALUATION_HARNESS_FINGERPRINT,
                        ),
                        provenance_receipt_digest=digest(
                            "provenance-authority-placeholder"
                        ),
                    )
                    cases_list.append(
                        replace(
                            authority_draft,
                            provenance_receipt_digest=signer.seal_digest(
                                authority_draft.provenance_receipt_body()
                            ),
                        )
                    )
                cases = tuple(cases_list)
                receipt_count += len(cases)
                channel.send(replace(dataset, cases=cases))
                continue
            if command == "evaluate" and role == "evaluator":
                _command, case, plan, profile, partition, artifact, environment = message
                if (
                    type(case) is not EvaluationCase
                    or type(plan) is not EvaluationPlanBinding
                    or type(profile) is not TypedMetricProfile
                    or type(partition) is not EvaluationPartition
                    or partition is EvaluationPartition.CALIBRATION
                    or plan.profile_digest != profile.profile_digest
                    or artifact
                    not in {plan.baseline_artifact_digest, plan.candidate_artifact_digest}
                ):
                    channel.send(None)
                    continue
                condition = digest(
                    {
                        "plan_digest": plan.plan_digest,
                        "case_id": case.case_id,
                        "input_digest": case.input_digest,
                        "partition": str(partition),
                        "environment_digest": environment,
                    }
                )
                outcome = deterministic_harness_outcome_digest(
                    input_digest=case.input_digest,
                    artifact_digest=artifact,
                    fixture_fingerprint=plan.fixture_fingerprint,
                    harness_fingerprint=plan.harness_fingerprint,
                )
                measured = 1.0 if outcome == case.expected_outcome_digest else 0.0
                result_draft = EvaluationCaseResult(
                    case.case_id,
                    partition,
                    condition,
                    artifact,
                    environment,
                    outcome,
                    identity,
                    digest("worker-execution-placeholder"),
                    tuple((metric, measured) for metric in profile.metric_ids),
                )
                result = replace(
                    result_draft,
                    execution_receipt_digest=signer.seal_digest(
                        result_draft.execution_receipt_body()
                    ),
                )
                receipt_count += 1
                channel.send(result)
                continue
            if command == "verify" and role == "verifier":
                _command, report, plan, builder_key, evaluator_key = message
                try:
                    report.__post_init__()
                    plan.__post_init__()
                    builder_verifier = Ed25519ReceiptVerifier(
                        Ed25519PublicKey.from_public_bytes(builder_key)
                    )
                    evaluator_verifier = Ed25519ReceiptVerifier(
                        Ed25519PublicKey.from_public_bytes(evaluator_key)
                    )
                    valid = (
                        report.plan_digest == plan.plan_digest
                        and builder_verifier.matches_receipt_digest(
                            report.builder_identity.boundary_receipt_body(),
                            report.builder_identity.boundary_receipt_digest,
                        )
                        and evaluator_verifier.matches_receipt_digest(
                            report.evaluator_identity.boundary_receipt_body(),
                            report.evaluator_identity.boundary_receipt_digest,
                        )
                        and all(
                            evaluator_verifier.matches_receipt_digest(
                                item.execution_receipt_body(),
                                item.execution_receipt_digest,
                            )
                            for item in (
                                *report.baseline_results,
                                *report.candidate_results,
                            )
                        )
                    )
                except (PolicyViolation, ValidationFailed, ValueError):
                    valid = False
                accepted = valid
                verification_draft = IndependentEvaluationReceipt(
                    report.report_digest,
                    plan.plan_digest,
                    identity,
                    accepted,
                    digest("worker-verification-placeholder"),
                )
                verification = replace(
                    verification_draft,
                    evidence_digest=signer.seal_digest(
                        verification_draft.verification_receipt_body()
                    ),
                )
                receipt_count += 1
                channel.send(verification)
                continue
            channel.send(None)
    finally:
        channel.close()


class EvaluationProcessWorker:
    """Coordinator view: public verifier + bounded IPC, never the private signing key."""

    def __init__(
        self,
        assignment_id: UUID,
        implementation_digest: str,
        challenge_digest: str,
        role: str,
    ) -> None:
        if type(assignment_id) is not UUID:
            raise ValidationFailed("Evaluation worker exact assignment required")
        if role not in {"provenance", "builder", "evaluator", "verifier"}:
            raise ValidationFailed("Evaluation worker exact role required")
        for value in (implementation_digest, challenge_digest):
            from zekam.domain.canonical import parse_digest

            parse_digest(value)
        parent, child = mp.get_context("spawn").Pipe(duplex=True)
        process = mp.get_context("spawn").Process(
            target=_worker_main,
            args=(child, assignment_id, implementation_digest, challenge_digest, role),
        )
        process.start()
        child.close()
        identity, public_key = parent.recv()
        self._channel = parent
        self._process = process
        self._closed = False
        self._terminal: tuple[dict[str, Any], str] | None = None
        self.identity: EvaluationIdentity = identity
        self.verifier = EvaluationWorkerVerifier(
            _WORKER_VERIFIER_TOKEN, process, identity, public_key, role
        )
        self.challenge_digest = challenge_digest
        self.role = role
        boundary_body = identity.boundary_receipt_body()
        if not self.verifier.matches_receipt_digest(
            boundary_body, identity.boundary_receipt_digest
        ):
            self.close()
            raise PolicyViolation("Evaluation worker startup receipt untrusted")

    def evaluate_case(
        self,
        *,
        case: EvaluationCase,
        plan: EvaluationPlanBinding,
        profile: TypedMetricProfile,
        partition: EvaluationPartition,
        artifact_digest: str,
        environment_digest: str,
    ) -> EvaluationCaseResult:
        if self.role != "evaluator" or self._process.exitcode is not None:
            raise PolicyViolation("Evaluation case requires live evaluator worker")
        self._channel.send(
            (
                "evaluate",
                case,
                plan,
                profile,
                partition,
                artifact_digest,
                environment_digest,
            )
        )
        result = self._channel.recv()
        if type(result) is not EvaluationCaseResult:
            raise PolicyViolation("Evaluator worker rejected typed case")
        return result

    def attest_dataset(self, dataset: EvaluationDataset) -> EvaluationDataset:
        if self.role != "provenance" or self._process.exitcode is not None:
            raise PolicyViolation("Evaluation dataset requires live provenance worker")
        self._channel.send(("attest-dataset", dataset))
        result = self._channel.recv()
        if type(result) is not EvaluationDataset:
            raise PolicyViolation("Provenance worker rejected evaluation dataset")
        return result

    def verify_report(
        self,
        *,
        report: PairedEvaluationReport,
        plan: EvaluationPlanBinding,
        builder_verifier: EvaluationWorkerVerifier,
        evaluator_verifier: EvaluationWorkerVerifier,
    ) -> IndependentEvaluationReceipt:
        if self.role != "verifier" or self._process.exitcode is not None:
            raise PolicyViolation("Evaluation report requires live verifier worker")
        if (
            type(builder_verifier) is not EvaluationWorkerVerifier
            or type(evaluator_verifier) is not EvaluationWorkerVerifier
            or builder_verifier.role != "builder"
            or evaluator_verifier.role != "evaluator"
        ):
            raise PolicyViolation("Verifier worker requires exact trusted role registry")
        self._channel.send(
            (
                "verify",
                report,
                plan,
                builder_verifier.public_key,
                evaluator_verifier.public_key,
            )
        )
        result = self._channel.recv()
        if type(result) is not IndependentEvaluationReceipt:
            raise PolicyViolation("Verifier worker rejected paired report")
        return result

    def reject_untyped_sign_request(self, unsigned_body: Mapping[str, object]) -> None:
        """Explicit fail-closed surface used to prove the old signing oracle is gone."""
        if self._process.exitcode is not None:
            raise PolicyViolation("Evaluation worker is not live")
        raise PolicyViolation("Generic evaluation receipt signing is forbidden")

    def finalize(self) -> tuple[dict[str, Any], str]:
        if self._closed:
            raise PolicyViolation("Evaluation worker already closed")
        if self._terminal is not None:
            return self._terminal
        self._channel.send("finalize")
        terminal_body, terminal_receipt = self._channel.recv()
        if not self.verifier.matches_receipt_digest(terminal_body, terminal_receipt):
            raise PolicyViolation("Evaluation worker terminal receipt untrusted")
        self._terminal = (terminal_body, terminal_receipt)
        return self._terminal

    def close(self) -> tuple[dict[str, Any], str]:
        if self._closed:
            raise PolicyViolation("Evaluation worker already closed")
        terminal_body, terminal_receipt = self.finalize()
        self._channel.send("exit")
        self._process.join(timeout=10)
        if self._process.exitcode != 0:
            raise PolicyViolation("Evaluation worker terminal process failed")
        self._channel.close()
        self._closed = True
        return terminal_body, terminal_receipt
