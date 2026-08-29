"""Existing queue worker icin one-job-per-attempt measured-loop handler'i."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from zekam.application.loop_orchestrator import DurableLoopOrchestrator
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import (
    LoopAdmission,
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopPolicy,
    LoopValidation,
)
from zekam.domain.loop_progress import (
    AttemptNoveltyFingerprint,
    LoopProgressPacket,
    LoopStopReason,
)
from zekam.domain.optimization import MeasurementEvidence, OptimizationObjective
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.measured_loop_repository import (
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork, JobRepository

if TYPE_CHECKING:
    from zekam.application.worker import Worker


@dataclass(frozen=True, slots=True)
class MeasuredLoopAttemptExecution:
    packet: LoopProgressPacket
    evidence: tuple[MeasurementEvidence, ...]
    outcome: LoopAttemptOutcome
    result_invocation_id: UUID
    verifier_invocation_id: UUID
    actual_input_tokens: int
    actual_output_tokens: int
    actual_cost_micros: int
    effect_receipt_id: UUID | None = None
    stop_reason: LoopStopReason | None = None
    next_request: LoopAttemptRequest | None = None
    auto_enqueue_next: bool = True


class MeasuredLoopAttemptRunner(Protocol):
    def run(self, work: ClaimedWork, admission: LoopAdmission) -> MeasuredLoopAttemptExecution: ...


class MeasuredLoopContractLoader(Protocol):
    def load(self, loop_id: UUID) -> tuple[OptimizationObjective, LoopPolicy]: ...


class MeasuredLoopCheckpointWriter(Protocol):
    def write(
        self,
        work: ClaimedWork,
        admission: LoopAdmission,
        execution: MeasuredLoopAttemptExecution,
        *,
        packet_digest: str,
        progress_decision_digest: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MeasuredLoopWorkerHandler:
    measured_repository: PostgresMeasuredLoopRepository
    policy_repository: PostgresLoopPolicyRepository
    orchestrator: DurableLoopOrchestrator
    contract_loader: MeasuredLoopContractLoader
    runner: MeasuredLoopAttemptRunner
    checkpoint_writer: MeasuredLoopCheckpointWriter | None = None

    def __call__(self, work: ClaimedWork) -> str:
        request = self._request(work)
        attempt_id = UUID(str(work.job.payload.get("attempt_id")))
        admission = self.policy_repository.admit(request, attempt_id=attempt_id)
        if not admission.admitted or admission.attempt_id is None or admission.ordinal is None:
            raise PolicyViolation(f"Measured loop admission reddedildi: {admission.reason}")

        execution = self.runner.run(work, admission)
        if (
            execution.packet.predecessor_attempt_id != admission.attempt_id
            or execution.packet.attempt_ordinal != admission.ordinal + 1
        ):
            raise ValidationFailed("Measured loop runner packet/attempt binding drift")
        objective, policy = self.contract_loader.load(request.loop_id)
        if (
            objective.objective_digest != execution.packet.objective_digest
            or policy.id != request.loop_id
        ):
            raise ValidationFailed("Measured loop runner current contract binding drift")

        with self.measured_repository.connection.transaction():
            stored = self.measured_repository.store_loop_progress(
                loop_id=request.loop_id,
                packet=execution.packet,
                evidence=execution.evidence,
                producer_assignment_id=policy.assignment_id,
                verifier_assignment_id=policy.validator_assignment_id,
                stop_reason=execution.stop_reason,
            )
            if self.checkpoint_writer is not None:
                self.checkpoint_writer.write(
                    work,
                    admission,
                    execution,
                    packet_digest=stored.packet_digest,
                    progress_decision_digest=stored.progress_decision_digest,
                )
            validation = LoopValidation(
                outcome=execution.outcome,
                validator_spec_digest=policy.validator_spec_digest,
                actual_input_tokens=execution.actual_input_tokens,
                actual_output_tokens=execution.actual_output_tokens,
                actual_cost_micros=execution.actual_cost_micros,
                result_invocation_id=execution.result_invocation_id,
                verifier_invocation_id=execution.verifier_invocation_id,
                effect_receipt_id=execution.effect_receipt_id,
                metric_evidence_refs=tuple(item.evidence_ref for item in execution.evidence),
                metric_vector_digest=execution.packet.current_metric_vector.progress_digest,
                progress_state=execution.packet.current_metric_vector.progress_state,
                progress_decision_digest=stored.progress_decision_digest,
                progress_packet_digest=stored.packet_digest,
            )
            terminal_state = self.policy_repository.complete(admission.attempt_id, validation)
            next_job_id: UUID | None = None
            if terminal_state == "active" and execution.auto_enqueue_next:
                if execution.stop_reason is not None or execution.next_request is None:
                    raise PolicyViolation("Active measured loop exact next request ister")
                next_plan = self.orchestrator.plan_attempt(
                    objective=objective,
                    policy=policy,
                    attempt_ordinal=execution.packet.attempt_ordinal,
                    predecessor_attempt_id=admission.attempt_id,
                    progress_packet=execution.packet,
                    admission_request=execution.next_request,
                    run_id=work.job.run_id,
                )
                next_job_id = self.orchestrator.enqueue_attempt(next_plan).job.id
        return digest(
            {
                "schema": "zekam-measured-loop-worker-result/v1",
                "job_id": str(work.job.id),
                "attempt_id": str(admission.attempt_id),
                "packet_digest": stored.packet_digest,
                "progress_decision_digest": stored.progress_decision_digest,
                "terminal_state": terminal_state,
                "next_job_id": None if next_job_id is None else str(next_job_id),
            }
        )

    @staticmethod
    def _request(work: ClaimedWork) -> LoopAttemptRequest:
        payload = work.job.payload
        admission = payload.get("admission")
        if not isinstance(admission, dict):
            raise PolicyViolation("Measured loop job exact admission payload ister")
        try:
            ordinal = int(payload["ordinal"])
            predecessor = payload.get("predecessor_attempt_id")
            evidence_ids = tuple(UUID(str(item)) for item in admission["delta_evidence_ids"])
            return LoopAttemptRequest(
                loop_id=UUID(str(payload["loop_id"])),
                prompt_digest=str(admission["prompt_digest"]),
                context_digest=str(admission["context_digest"]),
                action_digest=str(admission["action_digest"]),
                source_revision=str(admission["source_revision"]),
                plan_digest=str(admission["plan_digest"]),
                policy_revision_digest=str(admission["policy_revision_digest"]),
                validator_spec_digest=str(admission["validator_spec_digest"]),
                reserved_input_tokens=int(admission["reserved_input_tokens"]),
                reserved_output_tokens=int(admission["reserved_output_tokens"]),
                reserved_cost_micros=int(admission["reserved_cost_micros"]),
                predecessor_attempt_id=(None if predecessor is None else UUID(str(predecessor))),
                delta_evidence_ids=evidence_ids,
                attempt_ordinal=ordinal,
                objective_digest=_optional_text(admission.get("objective_digest")),
                validator_asset_manifest_digest=_optional_text(
                    admission.get("validator_asset_manifest_digest")
                ),
                progress_packet_digest=_optional_text(admission.get("progress_packet_digest")),
                metric_vector_digest=_optional_text(admission.get("metric_vector_digest")),
                novelty_digest=_optional_text(admission.get("novelty_digest")),
                novelty=_novelty(admission.get("novelty_body"), admission.get("novelty_digest")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("Measured loop job admission payload gecersiz") from exc


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _novelty(body: object, supplied_digest: object) -> AttemptNoveltyFingerprint | None:
    if body is None and supplied_digest is None:
        return None
    if not isinstance(body, dict) or supplied_digest is None:
        raise ValidationFailed("Measured loop job canonical novelty body ister")
    keys = {
        "objective_digest",
        "artifact_digest",
        "hypothesis_digest",
        "patch_digest",
        "failure_signature",
        "action_semantics_digest",
    }
    if set(body) != keys:
        raise ValidationFailed("Measured loop job novelty body exact component seti ister")
    return AttemptNoveltyFingerprint(
        objective_digest=str(body["objective_digest"]),
        artifact_digest=str(body["artifact_digest"]),
        hypothesis_digest=str(body["hypothesis_digest"]),
        patch_digest=str(body["patch_digest"]),
        failure_signature=str(body["failure_signature"]),
        action_semantics_digest=str(body["action_semantics_digest"]),
        novelty_digest=str(supplied_digest),
    )


def build_measured_loop_worker(
    connection: Any,
    realm_id: UUID,
    *,
    contract_loader: MeasuredLoopContractLoader,
    runner: MeasuredLoopAttemptRunner,
    checkpoint_writer: MeasuredLoopCheckpointWriter | None = None,
    worker_label: str = "measured-loop-worker",
    max_iterations: int | None = 1,
    poll_seconds: float = 2.0,
    lease_seconds: int = 60,
) -> Worker:
    """Compose the existing queue Worker with one explicit measured-loop capability."""

    from zekam.application.worker import WorkerSettings, build_worker
    from zekam.domain.runtime import JobKind

    measured = PostgresMeasuredLoopRepository(connection, realm_id)
    policy = PostgresLoopPolicyRepository(connection, realm_id)
    orchestrator = DurableLoopOrchestrator(measured, JobRepository(connection, realm_id))
    handler = MeasuredLoopWorkerHandler(
        measured,
        policy,
        orchestrator,
        contract_loader,
        runner,
        checkpoint_writer,
    )
    return build_worker(
        connection,
        realm_id,
        settings=WorkerSettings(
            worker_label=worker_label,
            capabilities=("loop.measured-attempt",),
            max_iterations=max_iterations,
            poll_seconds=poll_seconds,
            lease_seconds=lease_seconds,
        ),
        handlers={str(JobKind.MUTATION): handler},
        with_scheduler=False,
    )
