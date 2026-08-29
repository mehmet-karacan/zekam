"""Production composition for one-attempt measured-loop queue work.

The runtime uses the existing PostgreSQL authority, typed capability process
boundary and agent invocation ledger.  It never selects or calls a remote
provider: only an explicitly configured local driver is accepted.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from zekam.application.execution import ExecutionHost
from zekam.application.loop_orchestrator import measured_loop_effect_scope_digest
from zekam.application.loop_progress_compiler import LoopProgressCompiler
from zekam.application.measured_loop_worker import (
    MeasuredLoopAttemptExecution,
    build_measured_loop_worker,
)
from zekam.domain.agents import AgentInvocation
from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.context_continuity import Checkpoint, JournalEntry
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
)
from zekam.domain.loop_progress import (
    AttemptNoveltyFingerprint,
    LoopProgressCheckpoint,
    LoopStopReason,
)
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricAggregation,
    MetricDirection,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    ProgressState,
    evaluate_progress,
)
from zekam.infrastructure.postgres.agent_assignment_repository import (
    AgentAssignmentRepository,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.project_repository import SourceBindingRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.process.capability_worker import (
    CapabilityProcessWorker,
    CapabilityWorkerRequest,
    CapabilityWorkerSpec,
    CapabilityWorkerStatus,
)


@dataclass(frozen=True, slots=True)
class PostgresMeasuredLoopContractLoader:
    connection: Any
    realm_id: UUID

    def load(self, loop_id: UUID) -> tuple[OptimizationObjective, LoopPolicy]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select objective.objective_body,objective.objective_digest,"
                " policy.policy_body,policy.policy_digest"
                " from runtime.loop_policy_v2 policy"
                " join runtime.optimization_objective objective"
                " on objective.realm_id=policy.realm_id and objective.id=policy.objective_id"
                " where policy.realm_id=%s and policy.loop_id=%s",
                (self.realm_id, loop_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Measured loop v2 contract bulunamadi")
        objective_body = dict(row[0])
        policy_body = dict(row[2])
        objective = _objective(objective_body)
        policy = _policy(policy_body)
        if (
            digest(objective_body) != str(row[1])
            or digest(policy_body) != str(row[3])
            or objective.objective_digest != str(row[1])
            or policy.policy_digest != str(row[3])
            or policy.id != loop_id
            or objective.realm_id != self.realm_id
        ):
            raise PolicyViolation("Measured loop persisted contract digest drift")
        return objective, policy


_FORBIDDEN_EXECUTABLES = frozenset(
    {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "cscript.exe",
        "java",
        "java.exe",
        "node",
        "node.exe",
        "perl",
        "perl.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "ruby",
        "ruby.exe",
        "sh",
        "sh.exe",
        "wscript.exe",
    }
)
_FORBIDDEN_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".js", ".ps1", ".py", ".rb", ".sh"})
_SENSITIVE_KEY_RE = re.compile(
    r"^(raw[-_]?)?(prompt|response|transcript|secret|pii|credential|"
    r"private[-_]?reasoning|patch[-_]?body|test[-_]?log)s?$|"
    r"^(api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|password|passwd)$",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PinnedLocalDriverSpec:
    """One reviewed native executable; shells/interpreters are never accepted."""

    argv: tuple[str, ...]
    executable_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.executable_digest)
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValidationFailed("Measured loop pinned driver exact argv ister")
        executable = Path(self.argv[0])
        if not executable.is_absolute():
            raise PolicyViolation("Measured loop driver executable absolute path olmali")
        try:
            resolved = executable.resolve(strict=True)
            executable_bytes = resolved.read_bytes()
        except OSError as exc:
            raise PolicyViolation("Measured loop pinned executable okunamadi") from exc
        if (
            resolved.name.casefold() in _FORBIDDEN_EXECUTABLES
            or resolved.suffix.casefold() in _FORBIDDEN_SCRIPT_SUFFIXES
        ):
            raise PolicyViolation("Measured loop shell/interpreter driver reddedildi")
        if digest_of_bytes(executable_bytes) != self.executable_digest:
            raise PolicyViolation("Measured loop pinned executable SHA-256 drift")


@dataclass(frozen=True, slots=True)
class LocalMeasuredLoopDriver:
    """Two-process local builder/verifier runtime with exact pinned executables."""

    connection: Any
    realm_id: UUID
    builder: PinnedLocalDriverSpec
    verifier: PinnedLocalDriverSpec
    timeout_seconds: float = 300.0
    max_ipc_bytes: int = 1_048_576

    def run(self, work: Any, admission: Any) -> MeasuredLoopAttemptExecution:
        objective, policy = PostgresMeasuredLoopContractLoader(self.connection, self.realm_id).load(
            admission.loop_id
        )
        assignments = AgentAssignmentRepository(self.connection, self.realm_id)
        builder = assignments.get(policy.assignment_id)
        verifier = assignments.get(policy.validator_assignment_id)
        if builder.agent_ref == verifier.agent_ref:
            raise PolicyViolation("Measured loop builder/verifier agent identity ayrimi ister")
        source_root = _source_root(self.connection, self.realm_id, policy.project_id)
        topology_id, topology_digest = _assert_bounded_loop_topology(
            self.connection, self.realm_id, work=work, policy=policy
        )
        authorization = work.job.payload.get("effect_authorization")
        if not isinstance(authorization, dict):
            raise PolicyViolation("Measured loop local effect exact authorization ister")
        try:
            authorization_id = UUID(str(authorization["authorization_id"]))
            attached_effect_digest = str(authorization["effect_digest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("Measured loop effect authorization gecersiz") from exc

        moment = _database_now(self.connection)
        builder_invocation = _invocation(builder.id, "builder", work, moment)
        verifier_invocation = _invocation(verifier.id, "verifier", work, moment)
        policy_repo = PostgresLoopPolicyRepository(self.connection, self.realm_id)
        policy_repo.bind_dispatch(admission.attempt_id, "agent", builder_invocation.id)
        policy_repo.bind_dispatch(admission.attempt_id, "agent", verifier_invocation.id)
        assignments.record_invocation(builder_invocation)
        assignments.record_invocation(verifier_invocation)

        host = ExecutionHost(self.connection, self.realm_id, worker_label="measured-loop-worker")
        driver_digest = measured_loop_driver_digest(self.builder, self.verifier)
        execution_scope_digest = measured_loop_effect_scope_digest(
            job_id=work.job.id,
            loop_id=policy.id,
            attempt_id=admission.attempt_id,
            driver_digest=driver_digest,
            source_revision=policy.source_revision,
            topology_decision_id=topology_id,
            topology_decision_digest=topology_digest,
            resources=tuple(item.resource.text for item in work.job.resources),
        )
        if (
            attached_effect_digest != execution_scope_digest
            or work.job.payload.get("effect_scope_digest") != execution_scope_digest
        ):
            raise PolicyViolation("Measured loop attached effect scope digest drift")
        claim = _consume_and_claim_effect(
            self.connection,
            self.realm_id,
            host=host,
            work=work,
            policy=policy,
            authorization_id=authorization_id,
            effect_digest=execution_scope_digest,
            driver_digest=driver_digest,
            now=moment,
        )
        common_payload = {
            "loop_id": str(policy.id),
            "attempt_id": str(admission.attempt_id),
            "execution_scope_digest": execution_scope_digest,
            "attempt_ordinal": admission.ordinal,
            "objective_digest": objective.objective_digest,
            "source_revision": policy.source_revision,
            "plan_digest": policy.plan_digest,
            "validator_spec_digest": policy.validator_spec_digest,
            "topology_decision_id": str(topology_id),
            "topology_decision_digest": topology_digest,
            "grants_authority": False,
            "network_allowed": False,
        }
        builder_result = CapabilityProcessWorker().run(
            CapabilityWorkerSpec(
                self.builder.argv,
                source_root,
                self.timeout_seconds,
                self.max_ipc_bytes,
            ),
            CapabilityWorkerRequest(
                request_id=f"{work.job.id}:builder",
                payload={
                    "schema": "zekam-measured-loop-builder-request/v1",
                    **common_payload,
                    "execution_identity": builder_invocation.execution_identity,
                },
            ),
        )
        builder_document = _bound_process_result(
            builder_result,
            schema="zekam-measured-loop-builder-result/v1",
            policy=policy,
            admission=admission,
            execution_scope_digest=execution_scope_digest,
        )
        assignments.store_result(
            assignment_id=builder.id,
            invocation_id=builder_invocation.id,
            envelope_digest=digest(builder_document),
        )
        _refresh_exact_lease(host, work, self.lease_seconds)
        verifier_result = CapabilityProcessWorker().run(
            CapabilityWorkerSpec(
                self.verifier.argv,
                source_root,
                self.timeout_seconds,
                self.max_ipc_bytes,
            ),
            CapabilityWorkerRequest(
                request_id=f"{work.job.id}:verifier",
                payload={
                    "schema": "zekam-measured-loop-verifier-request/v1",
                    **common_payload,
                    "execution_identity": verifier_invocation.execution_identity,
                    "builder_result_digest": digest(builder_document),
                    "builder_result": builder_document,
                },
            ),
        )
        document = _bound_process_result(
            verifier_result,
            schema="zekam-measured-loop-verifier-result/v1",
            policy=policy,
            admission=admission,
            execution_scope_digest=execution_scope_digest,
        )
        assignments.store_result(
            assignment_id=verifier.id,
            invocation_id=verifier_invocation.id,
            envelope_digest=digest(document),
        )
        execution = _execution(
            document,
            objective=objective,
            policy=policy,
            admission=admission,
            builder_invocation=builder_invocation,
            verifier_invocation=verifier_invocation,
            policy_repository=policy_repo,
        )
        _refresh_exact_lease(host, work, self.lease_seconds)
        result_digest = digest(document)
        receipt = host.record_success(
            claim,
            result_digest=result_digest,
            adapter_evidence_digest=driver_digest,
            now=_database_now(self.connection),
        )
        return replace(execution, effect_receipt_id=receipt.id, auto_enqueue_next=False)

    @property
    def lease_seconds(self) -> int:
        return int(self.timeout_seconds * 2) + 30


def measured_loop_driver_digest(
    builder: PinnedLocalDriverSpec, verifier: PinnedLocalDriverSpec
) -> str:
    return digest(
        {
            "builder_argv": list(builder.argv),
            "builder_executable_digest": builder.executable_digest,
            "verifier_argv": list(verifier.argv),
            "verifier_executable_digest": verifier.executable_digest,
            "network": "deny",
        }
    )


@dataclass(frozen=True, slots=True)
class PostgresMeasuredLoopCheckpointWriter:
    connection: Any
    realm_id: UUID

    def write(
        self,
        work: Any,
        admission: Any,
        execution: MeasuredLoopAttemptExecution,
        *,
        packet_digest: str,
        progress_decision_digest: str,
    ) -> None:
        if work.job.project_id is None or work.job.work_item_id is None or work.job.plan_id is None:
            raise PolicyViolation("Measured loop checkpoint exact project/work/plan ister")
        repository = ContextContinuityRepository(
            self.connection,
            self.realm_id,
            work.job.project_id,
            work.job.work_item_id,
        )
        head = repository.journal_head()
        checkpoint_result_digest = digest(
            {
                "packet_digest": packet_digest,
                "progress_decision_digest": progress_decision_digest,
            }
        )
        journal = JournalEntry(
            sequence=1 if head is None else head[0] + 1,
            work_item_id=str(work.job.work_item_id),
            event_kind="measured-loop.attempt-completed",
            payload_digest=checkpoint_result_digest,
            previous_digest=None if head is None else head[1],
            truncated=False,
            created_at=dt.datetime.now(dt.UTC),
        )
        repository.append_journal(
            journal,
            expected_head=None if head is None else head[1],
        )
        repository.store_checkpoint(
            Checkpoint(
                checkpoint_id=f"measured-loop-{admission.attempt_id}",
                project_id=str(work.job.project_id),
                work_item_id=str(work.job.work_item_id),
                plan_revision_id=str(work.job.plan_id),
                source_revision=execution.packet.source_revision,
                plan_steps=("measured-attempt",),
                completed_steps=("measured-attempt",),
                pending_steps=(),
                step_results=(("measured-attempt", checkpoint_result_digest),),
                context_manifest_digest=str(work.job.payload["admission"]["context_digest"]),
                journal_head_digest=journal.entry_digest,
                next_safe_action=(
                    "enqueue-next-measured-attempt"
                    if execution.stop_reason is None
                    else "inspect-terminal-measured-loop"
                ),
                created_at=dt.datetime.now(dt.UTC),
            ),
            task_plan_id=work.job.plan_id,
            job_id=work.job.id,
        )


def build_production_measured_loop_worker(
    connection: Any,
    realm_id: UUID,
    *,
    builder: PinnedLocalDriverSpec,
    verifier: PinnedLocalDriverSpec,
    worker_label: str = "measured-loop-worker",
    timeout_seconds: float = 300.0,
    max_iterations: int | None = 1,
    poll_seconds: float = 2.0,
) -> Any:
    if builder.argv == verifier.argv:
        raise PolicyViolation("Measured loop builder/verifier ayri argv ister")
    loader = PostgresMeasuredLoopContractLoader(connection, realm_id)
    runner = LocalMeasuredLoopDriver(connection, realm_id, builder, verifier, timeout_seconds)
    checkpoint_writer = PostgresMeasuredLoopCheckpointWriter(connection, realm_id)
    return build_measured_loop_worker(
        connection,
        realm_id,
        contract_loader=loader,
        runner=runner,
        checkpoint_writer=checkpoint_writer,
        worker_label=worker_label,
        max_iterations=max_iterations,
        poll_seconds=poll_seconds,
        lease_seconds=runner.lease_seconds,
    )


def load_local_driver_config(
    path: Path,
) -> tuple[PinnedLocalDriverSpec, PinnedLocalDriverSpec, float]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Measured loop driver config okunamadi") from exc
    if not isinstance(body, dict) or set(body) != {
        "schema",
        "builder_argv",
        "builder_executable_sha256",
        "verifier_argv",
        "verifier_executable_sha256",
        "timeout_seconds",
        "network_allowed",
    }:
        raise ValidationFailed("Measured loop driver config exact sema ister")
    builder_argv = body["builder_argv"]
    verifier_argv = body["verifier_argv"]
    if (
        body["schema"] != "zekam-measured-loop-local-drivers/v2"
        or body["network_allowed"] is not False
        or not isinstance(builder_argv, list)
        or not isinstance(verifier_argv, list)
        or not builder_argv
        or not verifier_argv
        or any(not isinstance(item, str) or not item for item in builder_argv + verifier_argv)
    ):
        raise PolicyViolation("Measured loop driver local/network-deny olmali")
    try:
        timeout = float(body["timeout_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Measured loop driver timeout gecersiz") from exc
    if not 0 < timeout <= 300:
        raise PolicyViolation("Measured loop driver timeout 0..300 saniye olmali")
    builder = PinnedLocalDriverSpec(tuple(builder_argv), str(body["builder_executable_sha256"]))
    verifier = PinnedLocalDriverSpec(tuple(verifier_argv), str(body["verifier_executable_sha256"]))
    if builder.argv == verifier.argv:
        raise PolicyViolation("Measured loop builder/verifier ayri argv ister")
    return builder, verifier, timeout


def _source_root(connection: Any, realm_id: UUID, project_id: UUID) -> Path:
    bindings = SourceBindingRepository(connection, realm_id).for_project(project_id)
    if len(bindings) != 1 or not bindings[0].is_usable:
        raise PolicyViolation("Measured loop exact usable project source binding ister")
    root = SourceBindingRepository(connection, realm_id).local_path(bindings[0].id)
    if root is None:
        raise PolicyViolation("Measured loop local source root bulunamadi")
    return root.resolve(strict=True)


def _consume_and_claim_effect(
    connection: Any,
    realm_id: UUID,
    *,
    host: ExecutionHost,
    authorization_id: UUID,
    work: Any,
    policy: LoopPolicy,
    effect_digest: str,
    driver_digest: str,
    now: dt.datetime,
) -> Any:
    authorizations = AuthorizationRepository(connection, realm_id)
    authorization = authorizations.get(authorization_id)
    resources = tuple(item.resource.text for item in work.job.resources)
    if (
        authorization.realm_id != realm_id
        or authorization.work_item_id != work.job.work_item_id
        or authorization.plan_id != work.job.plan_id
        or authorization.plan_digest != policy.plan_digest
        or authorization.effect_digest != effect_digest
        or authorization.rejection_reason(now) is not None
        or tuple(sorted(authorization.scope.allowed_resources)) != tuple(sorted(resources))
        or tuple(sorted(authorization.scope.allowed_effects)) != ("process-run",)
    ):
        raise PolicyViolation("Measured loop issued exact effect authorization drift")
    consumed_by = (
        f"measured-loop:{work.job.id}:{work.attempt_id}:{work.lease.fencing_token}:{driver_digest}"
    )
    with connection.transaction():
        consumed = authorizations.consume(
            authorization_id,
            effect_digest=effect_digest,
            consumed_by=consumed_by,
            now=now,
        )
        if (
            not consumed.consumed
            or consumed.authorization is None
            or consumed.authorization.consumed_by != consumed_by
        ):
            raise PolicyViolation("Measured loop effect authorization atomik tuketilemedi")
        return host.claim_effect(
            work,
            operation="loop.measured-attempt.local-driver",
            effect_digest=effect_digest,
            authorization_digest=authorization.authorization_digest,
            authorization_id=authorization_id,
            resources=work.job.resources,
            adapter_digest=driver_digest,
            idempotency_key=f"measured-loop-driver:{work.job.id}:{work.attempt_id}",
            now=now,
        )


def _database_now(connection: Any) -> dt.datetime:
    with connection.cursor() as cursor:
        cursor.execute("select clock_timestamp()")
        return cast(dt.datetime, cursor.fetchone()[0])


def _refresh_exact_lease(host: ExecutionHost, work: Any, lease_seconds: int) -> None:
    if not host.jobs.heartbeat(
        work.job.id,
        token=work.owner_token,
        fencing_token=work.lease.fencing_token,
        lease_seconds=lease_seconds,
        now=_database_now(host.connection),
    ):
        raise PolicyViolation("Measured loop lease/fencing receipt oncesi stale")


def _assert_bounded_loop_topology(
    connection: Any,
    realm_id: UUID,
    *,
    work: Any,
    policy: LoopPolicy,
) -> tuple[UUID, str]:
    binding = work.job.payload.get("topology")
    if not isinstance(binding, dict) or set(binding) != {
        "decision_id",
        "decision_digest",
        "pattern",
    }:
        raise PolicyViolation("Production measured loop canonical topology binding ister")
    try:
        decision_id = UUID(str(binding["decision_id"]))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Measured loop topology decision UUID gecersiz") from exc
    decision_digest = str(binding["decision_digest"])
    parse_digest(decision_digest)
    if binding["pattern"] != "bounded-loop":
        raise PolicyViolation("Production measured loop bounded-loop topology ister")
    with connection.cursor() as cursor:
        cursor.execute(
            "select decision_digest,selected_pattern,project_id,work_item_id,plan_id"
            " from runtime.execution_topology_decision where realm_id=%s and id=%s",
            (realm_id, decision_id),
        )
        row = cursor.fetchone()
    if row is None or (
        str(row[0]) != decision_digest
        or str(row[1]) != "bounded-loop"
        or UUID(str(row[2])) != policy.project_id
        or UUID(str(row[3])) != policy.work_item_id
        or UUID(str(row[4])) != policy.plan_id
    ):
        raise PolicyViolation("Measured loop canonical topology state drift")
    return decision_id, decision_digest


def _bound_process_result(
    result: Any,
    *,
    schema: str,
    policy: LoopPolicy,
    admission: Any,
    execution_scope_digest: str,
) -> dict[str, Any]:
    if result.status is not CapabilityWorkerStatus.COMPLETED or result.payload is None:
        raise PolicyViolation("Measured loop local process sonucu belirsiz; recovery-required")
    document = dict(result.payload)
    _assert_safe(document)
    if (
        document.get("schema") != schema
        or document.get("loop_id") != str(policy.id)
        or document.get("attempt_id") != str(admission.attempt_id)
        or document.get("execution_scope_digest") != execution_scope_digest
    ):
        raise PolicyViolation("Measured loop child result exact scope echo drift")
    return document


def _invocation(assignment_id: UUID, role: str, work: Any, moment: dt.datetime) -> AgentInvocation:
    invocation_id = new_uuid7(now=moment)
    execution_identity = f"local-measured-loop:{role}:{work.job.id}:{work.lease.fencing_token}"
    body = {
        "id": str(invocation_id),
        "realm_id": str(work.job.realm_id),
        "assignment_id": str(assignment_id),
        "client_id": "local-measured-loop-driver",
        "execution_identity": execution_identity,
    }
    return AgentInvocation(
        invocation_id,
        work.job.realm_id,
        assignment_id,
        "local-measured-loop-driver",
        execution_identity,
        digest(body),
        moment,
    )


def _evidence(
    rows: object,
    *,
    source_revision: str,
    measurement_identity: str,
    verifier_identity: str,
) -> tuple[MeasurementEvidence, ...]:
    if not isinstance(rows, list):
        raise ValidationFailed("Measured loop driver measurement listesi ister")
    result: list[MeasurementEvidence] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationFailed("Measured loop driver measurement object ister")
        try:
            result.append(
                MeasurementEvidence(
                    str(row["metric_id"]),
                    float(row["value"]),
                    str(row["evidence_ref"]),
                    str(row["evidence_digest"]),
                    source_revision,
                    dt.datetime.fromisoformat(str(row["measured_at"])),
                    measurement_identity,
                    verifier_identity,
                    False,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("Measured loop driver measurement gecersiz") from exc
    return tuple(sorted(result, key=lambda item: item.metric_id))


def _execution(
    body: Mapping[str, Any],
    *,
    objective: OptimizationObjective,
    policy: LoopPolicy,
    admission: Any,
    builder_invocation: AgentInvocation,
    verifier_invocation: AgentInvocation,
    policy_repository: PostgresLoopPolicyRepository,
) -> MeasuredLoopAttemptExecution:
    if body.get("schema") != "zekam-measured-loop-verifier-result/v1":
        raise ValidationFailed("Measured loop verifier result semasi gecersiz")
    baseline = _evidence(
        body.get("baseline"),
        source_revision=policy.source_revision,
        measurement_identity=builder_invocation.execution_identity,
        verifier_identity=verifier_invocation.execution_identity,
    )
    previous = _evidence(
        body.get("previous"),
        source_revision=policy.source_revision,
        measurement_identity=builder_invocation.execution_identity,
        verifier_identity=verifier_invocation.execution_identity,
    )
    current = _evidence(
        body.get("current"),
        source_revision=policy.source_revision,
        measurement_identity=builder_invocation.execution_identity,
        verifier_identity=verifier_invocation.execution_identity,
    )
    previous_vector = evaluate_progress(objective.metric_specs, baseline, baseline, previous)
    current_vector = evaluate_progress(
        objective.metric_specs,
        baseline,
        previous,
        current,
        cost_micros=int(body.get("actual_cost_micros", 0)),
    )
    checkpoint = LoopProgressCheckpoint(
        objective.objective_digest,
        policy.source_revision,
        policy.plan_digest,
        policy.policy_revision_digest,
        objective.validator_asset_manifest_digest,
        str(body["artifact_before_digest"]),
        str(body["artifact_after_digest"]),
        admission.attempt_id,
        admission.ordinal + 1,
        previous_vector,
        current_vector,
        str(body["accepted_hypothesis_digest"]),
        tuple(str(item) for item in body.get("rejected_hypothesis_digests", ())),
        str(body["patch_digest"]),
        str(body["failure_signature"]),
        str(body["validator_diagnosis_ref"]),
        str(body["validator_diagnosis_digest"]),
        tuple((str(item[0]), str(item[1])) for item in body.get("new_evidence_refs", ())),
        int(body["remaining_attempts"]),
        int(body["remaining_tokens"]),
        int(body["remaining_cost_micros"]),
        int(body["remaining_time_seconds"]),
        str(body["next_allowed_focus"]),
        tuple(sorted(str(item) for item in body.get("forbidden_retries", ()))),
    )
    packet = LoopProgressCompiler().compile(checkpoint)
    state = current_vector.progress_state
    if state is ProgressState.TARGET_REACHED:
        outcome, stop = LoopAttemptOutcome.PASSED, LoopStopReason.TARGET_REACHED
    elif state is ProgressState.REGRESSED:
        outcome, stop = LoopAttemptOutcome.MANUAL_REVIEW, LoopStopReason.METRIC_REGRESSION
    elif state is ProgressState.INVALID:
        outcome, stop = LoopAttemptOutcome.MANUAL_REVIEW, LoopStopReason.INVALID_MEASUREMENT
    elif checkpoint.remaining_attempts < 1:
        outcome, stop = LoopAttemptOutcome.MANUAL_REVIEW, LoopStopReason.NO_PROGRESS
    else:
        outcome, stop = LoopAttemptOutcome.RETRYABLE_FAILURE, None
    next_request = None
    if stop is None:
        novelty = AttemptNoveltyFingerprint.build(
            objective_digest=objective.objective_digest,
            artifact_digest=str(body["next_artifact_digest"]),
            hypothesis_digest=str(body["next_hypothesis_digest"]),
            patch_digest=str(body["next_patch_digest"]),
            failure_signature=str(body["next_failure_signature"]),
            action_semantics_digest=str(body["next_action_semantics_digest"]),
        )
        delta_id = policy_repository.register_delta_evidence(
            policy.id, str(LoopDeltaKind.NEW_EVIDENCE), verifier_invocation.id
        )
        next_request = LoopAttemptRequest(
            policy.id,
            digest(("prompt", novelty.novelty_digest)),
            policy.context_manifest_digest,
            novelty.action_semantics_digest,
            policy.source_revision,
            policy.plan_digest,
            policy.policy_revision_digest,
            policy.validator_spec_digest,
            int(body["next_reserved_input_tokens"]),
            int(body["next_reserved_output_tokens"]),
            int(body["next_reserved_cost_micros"]),
            predecessor_attempt_id=admission.attempt_id,
            delta_evidence_ids=(delta_id,),
            attempt_ordinal=admission.ordinal + 1,
            objective_digest=objective.objective_digest,
            validator_asset_manifest_digest=objective.validator_asset_manifest_digest,
            progress_packet_digest=packet.packet_digest,
            metric_vector_digest=packet.current_metric_vector.progress_digest,
            novelty_digest=novelty.novelty_digest,
            novelty=novelty,
        )
    return MeasuredLoopAttemptExecution(
        packet,
        current,
        outcome,
        builder_invocation.id,
        verifier_invocation.id,
        int(body.get("actual_input_tokens", 0)),
        int(body.get("actual_output_tokens", 0)),
        int(body.get("actual_cost_micros", 0)),
        stop_reason=stop,
        next_request=next_request,
    )


def _metric(row: Mapping[str, Any]) -> MetricSpec:
    return MetricSpec(
        str(row["metric_id"]),
        str(row["name"]),
        str(row["unit"]),
        MetricDirection(str(row["direction"])),
        MetricRole(str(row["role"])),
        str(row["source_kind"]),
        row.get("target_value"),
        row.get("min_value"),
        row.get("max_value"),
        float(row.get("minimum_meaningful_delta", 0.0)),
        float(row.get("regression_tolerance", 0.0)),
        MetricAggregation(str(row.get("aggregation", "latest"))),
    )


def _objective(body: Mapping[str, Any]) -> OptimizationObjective:
    return OptimizationObjective(
        UUID(str(body["objective_id"])),
        UUID(str(body["realm_id"])),
        UUID(str(body["project_id"])),
        UUID(str(body["work_item_id"])),
        UUID(str(body["plan_id"])),
        str(body["step_id"]),
        str(body["artifact_ref"]),
        str(body["artifact_baseline_digest"]),
        str(body["measurement_plan_digest"]),
        str(body["validator_asset_manifest_digest"]),
        tuple(_metric(row) for row in body["metric_specs"]),
        int(body["max_attempts"]),
        int(body["max_tokens"]),
        int(body["max_cost_micros"]),
        dt.datetime.fromisoformat(str(body["deadline"])),
        str(body["reversibility_class"]),
        dt.datetime.fromisoformat(str(body["created_at"])),
    )


def _policy(body: Mapping[str, Any]) -> LoopPolicy:
    measured = dict(body["measured_v2"])
    return LoopPolicy(
        UUID(str(body["id"])),
        UUID(str(body["realm_id"])),
        UUID(str(body["project_id"])),
        UUID(str(body["work_item_id"])),
        UUID(str(body["plan_id"])),
        str(body["step_id"]),
        UUID(str(body["assignment_id"])),
        UUID(str(body["context_manifest_id"])),
        UUID(str(body["validator_assignment_id"])),
        int(body["max_attempts"]),
        int(body["max_tokens"]),
        int(body["max_cost_micros"]),
        dt.datetime.fromisoformat(str(body["deadline"])),
        str(body["validator_spec_digest"]),
        tuple(LoopDeltaKind(item) for item in body["required_delta"]),
        tuple(LoopEffectClass(item) for item in body["forbidden_effects"]),
        tuple(LoopTerminalState(item) for item in body["terminal_states"]),
        str(body["source_revision"]),
        str(body["context_manifest_digest"]),
        str(body["plan_digest"]),
        str(body["policy_revision_digest"]),
        str(body["canonical_effect_kind"]),
        dt.datetime.fromisoformat(str(body["created_at"])),
        objective_id=UUID(str(measured["objective_id"])),
        stable_objective_digest=str(measured["stable_objective_digest"]),
        measurement_plan_digest=str(measured["measurement_plan_digest"]),
        validator_manifest_id=UUID(str(measured["validator_manifest_id"])),
        validator_asset_manifest_digest=str(measured["validator_asset_manifest_digest"]),
        metric_specs_digest=str(measured["metric_specs_digest"]),
        stall_limit=int(measured["stall_limit"]),
        diagnostic_patience=int(measured["diagnostic_patience"]),
        progress_token_budget=int(measured["progress_token_budget"]),
        minimum_value_per_cost=float(measured["minimum_value_per_cost"]),
    )


def _assert_safe(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.fullmatch(str(key)):
                raise PolicyViolation("Measured loop driver sensitive alan tasiyamaz")
            _assert_safe(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe(item)
    elif isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        raise PolicyViolation("Measured loop driver sensitive deger tasiyamaz")
