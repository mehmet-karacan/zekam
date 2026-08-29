"""Measured-loop execution plane icin canonical PostgreSQL adapter'i."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from zekam.application.loop_control import (
    LoopControlPlan,
    LoopControlSnapshot,
    LoopControlState,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import LoopPolicy
from zekam.domain.loop_progress import LoopProgressPacket, LoopStopReason
from zekam.domain.optimization import (
    MeasurementEvidence,
    OptimizationObjective,
    ProgressState,
    ValidatorAssetManifest,
)
from zekam.domain.runtime import Job
from zekam.domain.work import OPEN_STATES, WorkState

_EVIDENCE_NAMESPACE = UUID("db04cb17-569a-53bf-a99f-4aa33b18c76f")
_PACKET_NAMESPACE = UUID("5c2fe4c1-c53d-507c-b520-ab4394703731")


def _stable_id(namespace: UUID, value: str) -> UUID:
    return uuid5(namespace, value)


@dataclass(frozen=True, slots=True)
class MeasuredLoopContractTuning:
    stall_limit: int
    diagnostic_patience: int
    progress_token_budget: int
    minimum_value_per_cost: float

    def __post_init__(self) -> None:
        if not 1 <= self.stall_limit <= 100:
            raise ValidationFailed("Measured loop stall limit 1..100 olmali")
        if not 0 <= self.diagnostic_patience <= 100:
            raise ValidationFailed("Measured loop diagnostic patience 0..100 olmali")
        if not 64 <= self.progress_token_budget <= 32768:
            raise ValidationFailed("Measured loop progress token budget 64..32768 olmali")
        if self.minimum_value_per_cost < 0:
            raise ValidationFailed("Measured loop minimum value-per-cost negatif olamaz")

    def as_dict(self) -> dict[str, int | float]:
        return {
            "stall_limit": self.stall_limit,
            "diagnostic_patience": self.diagnostic_patience,
            "progress_token_budget": self.progress_token_budget,
            "minimum_value_per_cost": self.minimum_value_per_cost,
        }


@dataclass(frozen=True, slots=True)
class StoredLoopProgress:
    evidence_id: UUID
    packet_id: UUID
    evidence_digest: str
    packet_digest: str
    progress_decision_digest: str
    created: bool


@dataclass(frozen=True, slots=True)
class PostgresMeasuredLoopRepository:
    connection: Any
    realm_id: UUID

    def assert_loop_open(self, loop_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select not exists(select 1 from runtime.loop_terminal"
                " where realm_id=%s and loop_id=%s),coalesce((select state"
                " from runtime.loop_control_event where realm_id=%s and loop_id=%s"
                " order by created_at desc,id desc limit 1),'active')",
                (self.realm_id, loop_id, self.realm_id, loop_id),
            )
            row = cursor.fetchone()
            if not bool(row[0]):
                raise PolicyViolation("Terminal loop yeni attempt job uretemez")
            if str(row[1]) != "active":
                raise PolicyViolation("Paused/draining/cancelled loop yeni attempt job uretemez")

    def read_loop_control_snapshot(self, loop_id: UUID) -> LoopControlSnapshot:
        """Read the exact current Work/TaskPlan and latest immutable control event."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select lp.work_item_id,lp.plan_id,lp.plan_digest,wi.state,"
                " coalesce((select event.state from runtime.loop_control_event event"
                " where event.realm_id=lp.realm_id and event.loop_id=lp.id"
                " order by event.created_at desc,event.id desc limit 1),'active'),"
                " (select terminal.state from runtime.loop_terminal terminal"
                " where terminal.realm_id=lp.realm_id and terminal.loop_id=lp.id),"
                " lp.plan_id=(select candidate.id from work.task_plan candidate"
                " where candidate.realm_id=lp.realm_id"
                " and candidate.work_item_id=lp.work_item_id"
                " order by candidate.revision desc,candidate.id desc limit 1)"
                " from runtime.loop_policy lp join work.work_item wi"
                " on wi.realm_id=lp.realm_id and wi.id=lp.work_item_id"
                " where lp.realm_id=%s and lp.id=%s",
                (self.realm_id, loop_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Loop control hedefi bulunamadi")
        return LoopControlSnapshot(
            realm_id=self.realm_id,
            loop_id=loop_id,
            work_item_id=UUID(str(row[0])),
            plan_id=UUID(str(row[1])),
            plan_digest=str(row[2]),
            work_state=str(row[3]),
            current_state=LoopControlState(str(row[4])),
            terminal_state=None if row[5] is None else str(row[5]),
            current_plan=bool(row[6]),
        )

    def record_loop_control_event(
        self,
        plan: LoopControlPlan,
        *,
        event_id: UUID,
        authorization_id: UUID,
        authorization_digest: str,
    ) -> dt.datetime:
        """Apply one reviewed transition through migration 76's guarded routine."""

        if plan.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm loop control reddedildi")
        plan.assert_integrity()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{self.realm_id}:{plan.loop_id}",),
            )
        snapshot = self.read_loop_control_snapshot(plan.loop_id)
        if (
            snapshot.work_item_id != plan.work_item_id
            or snapshot.plan_id != plan.plan_id
            or snapshot.plan_digest != plan.plan_digest
            or snapshot.current_state is not plan.source_state
            or snapshot.terminal_state is not None
            or WorkState(snapshot.work_state) not in OPEN_STATES
            or not snapshot.current_plan
        ):
            raise PolicyViolation("Loop control locked state/plan drift; replan required")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.record_loop_control_event(%s,%s,%s,%s,%s,%s)",
                (
                    event_id,
                    plan.loop_id,
                    plan.target_state.value,
                    authorization_id,
                    authorization_digest,
                    plan.reason_digest,
                ),
            )
            recorded_id = UUID(str(cursor.fetchone()[0]))
            if recorded_id != event_id:
                raise PolicyViolation("Loop control event identity drift")
            cursor.execute(
                "select created_at from runtime.loop_control_event"
                " where realm_id=%s and id=%s and loop_id=%s and state=%s"
                " and plan_digest=%s and authorization_id=%s"
                " and authorization_digest=%s and reason_digest=%s",
                (
                    self.realm_id,
                    event_id,
                    plan.loop_id,
                    plan.target_state.value,
                    plan.plan_digest,
                    authorization_id,
                    authorization_digest,
                    plan.reason_digest,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Loop control terminal event kaydi dogrulanamadi")
        created_at = row[0]
        if not isinstance(created_at, dt.datetime):
            raise ValidationFailed("Loop control event zamani gecersiz")
        return created_at

    def store_measured_loop_contract(
        self,
        *,
        objective: OptimizationObjective,
        policy: LoopPolicy,
        validator_manifest: ValidatorAssetManifest,
        tuning: MeasuredLoopContractTuning,
    ) -> bool:
        """Persist stable objective, immutable validator assets and LoopPolicy v2."""

        self._assert_contract_binding(objective, policy, validator_manifest, tuning)
        manifest_body = validator_manifest.as_dict()
        validator_manifest_digest = validator_manifest.manifest_digest
        requested_policy_body = policy.body()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select canonical_body from runtime.loop_policy where realm_id=%s and id=%s",
                (self.realm_id, policy.id),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], dict):
            raise ValidationFailed("Measured LoopPolicy canonical base kaydi bulunamadi")
        policy_body = dict(row[0])
        policy_body.pop("schema", None)
        policy_body["created_at"] = dt.datetime.fromisoformat(str(policy_body["created_at"]))
        policy_body["deadline"] = dt.datetime.fromisoformat(str(policy_body["deadline"]))
        policy_body["measured_v2"] = requested_policy_body["measured_v2"]
        policy_digest = digest(policy_body)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.store_measured_loop_contract("
                " %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,"
                " %s,%s,%s,%s)",
                (
                    objective.objective_id,
                    policy.id,
                    validator_manifest.manifest_id,
                    canonical_json(objective.as_dict()),
                    objective.objective_digest,
                    policy.source_revision,
                    policy.assignment_id,
                    policy.validator_assignment_id,
                    canonical_json(manifest_body),
                    validator_manifest_digest,
                    canonical_json(policy_body),
                    policy_digest,
                    tuning.stall_limit,
                    tuning.diagnostic_patience,
                    tuning.progress_token_budget,
                    tuning.minimum_value_per_cost,
                ),
            )
            return bool(cursor.fetchone()[0])

    def store_loop_progress(
        self,
        *,
        loop_id: UUID,
        packet: LoopProgressPacket,
        evidence: Sequence[MeasurementEvidence],
        producer_assignment_id: UUID,
        verifier_assignment_id: UUID,
        stop_reason: LoopStopReason | None,
        omission_count: int = 0,
    ) -> StoredLoopProgress:
        """Store one predecessor attempt's external evidence and bounded packet."""

        if producer_assignment_id == verifier_assignment_id:
            raise PolicyViolation("Loop measurement producer ve verifier farkli olmali")
        if packet.attempt_ordinal < 2:
            raise ValidationFailed("Stored progress packet next attempt 2+ icin olmali")
        if omission_count < 0:
            raise ValidationFailed("Loop progress omission count negatif olamaz")
        evidence_rows = tuple(evidence)
        metric_ids = tuple(item.metric_id for item in evidence_rows)
        if not evidence_rows or metric_ids != tuple(sorted(set(metric_ids))):
            raise ValidationFailed("Loop measurement evidence dolu, tekil ve kanonik olmali")
        if any(item.producer_self_report for item in evidence_rows):
            raise PolicyViolation("Producer self-report canonical measurement evidence olamaz")
        evidence_body = {
            "schema": "zekam-loop-measurement-evidence/v1",
            "loop_id": str(loop_id),
            "attempt_id": str(packet.predecessor_attempt_id),
            "attempt_ordinal": packet.attempt_ordinal - 1,
            "producer_assignment_id": str(producer_assignment_id),
            "verifier_assignment_id": str(verifier_assignment_id),
            "measurements": [item.as_dict() for item in evidence_rows],
            "producer_self_report": False,
        }
        evidence_digest = digest(evidence_body)
        evidence_id = _stable_id(_EVIDENCE_NAMESPACE, evidence_digest)
        packet_id = _stable_id(_PACKET_NAMESPACE, packet.packet_digest)
        observed_at = max(item.measured_at for item in evidence_rows)
        improved = packet.current_metric_vector.progress_state in {
            ProgressState.IMPROVED,
            ProgressState.TARGET_REACHED,
        }
        progress_state = packet.current_metric_vector.progress_state
        progress_decision_body = {
            "schema": "zekam-loop-progress-decision/v1",
            "packet_digest": packet.packet_digest,
            "progress_state": str(progress_state),
            "allow_next_attempt": stop_reason is None,
            "progress_counted": improved,
            "stop_reason": None if stop_reason is None else str(stop_reason),
            "grants_authority": False,
        }
        progress_decision_digest = digest(progress_decision_body)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.store_loop_progress("
                " %s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                (
                    evidence_id,
                    packet_id,
                    loop_id,
                    packet.predecessor_attempt_id,
                    producer_assignment_id,
                    verifier_assignment_id,
                    canonical_json(evidence_body),
                    evidence_digest,
                    observed_at,
                    packet.attempt_ordinal - 1,
                    canonical_json(packet.as_dict()),
                    packet.packet_digest,
                    improved,
                    None if stop_reason is None else str(stop_reason),
                    omission_count,
                    str(progress_state),
                    canonical_json(progress_decision_body),
                    progress_decision_digest,
                    packet.current_metric_vector.progress_digest,
                ),
            )
            created = bool(cursor.fetchone()[0])
        return StoredLoopProgress(
            evidence_id,
            packet_id,
            evidence_digest,
            packet.packet_digest,
            progress_decision_digest,
            created,
        )

    def bind_loop_attempt_job(
        self,
        *,
        loop_id: UUID,
        ordinal: int,
        predecessor_attempt_id: UUID | None,
        progress_packet: LoopProgressPacket | None,
        job: Job,
        idempotency_digest: str,
    ) -> bool:
        """Bind a max-attempts=1 durable job to exactly one logical loop attempt."""

        if job.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm measured loop job reddedildi")
        if job.max_attempts != 1:
            raise ValidationFailed("Loop attempt job max_attempts=1 olmali")
        if job.payload.get("loop_id") != str(loop_id) or job.payload.get("ordinal") != ordinal:
            raise ValidationFailed("Loop attempt job payload binding drift")
        packet_digest = None if progress_packet is None else progress_packet.packet_digest
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.bind_loop_attempt_job(%s,%s,%s,%s,%s,%s)",
                (
                    loop_id,
                    ordinal,
                    predecessor_attempt_id,
                    packet_digest,
                    job.id,
                    idempotency_digest,
                ),
            )
            return bool(cursor.fetchone()[0])

    def _assert_contract_binding(
        self,
        objective: OptimizationObjective,
        policy: LoopPolicy,
        validator_manifest: ValidatorAssetManifest,
        tuning: MeasuredLoopContractTuning,
    ) -> None:
        if objective.realm_id != self.realm_id or policy.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm measured loop contract reddedildi")
        objective_scope = (
            objective.project_id,
            objective.work_item_id,
            objective.plan_id,
            objective.step_id,
        )
        policy_scope = (policy.project_id, policy.work_item_id, policy.plan_id, policy.step_id)
        if objective_scope != policy_scope:
            raise ValidationFailed("Measured loop objective/policy scope drift")
        validator_manifest_digest = validator_manifest.manifest_digest
        if objective.validator_asset_manifest_digest != validator_manifest_digest:
            raise ValidationFailed("Measured loop objective/validator manifest drift")
        if (
            validator_manifest.objective_id != objective.objective_id
            or validator_manifest.source_revision != policy.source_revision
            or validator_manifest.builder_assignment_id != policy.assignment_id
            or validator_manifest.verifier_assignment_id != policy.validator_assignment_id
        ):
            raise ValidationFailed("Measured loop validator manifest exact binding drift")
        if (
            objective.max_attempts != policy.max_attempts
            or objective.max_tokens != policy.max_tokens
            or objective.max_cost_micros != policy.max_cost_micros
        ):
            raise ValidationFailed("Measured loop objective/policy budget drift")
        if objective.deadline != policy.deadline:
            raise ValidationFailed("Measured loop objective/policy deadline drift")
        expected_metric_specs_digest = digest([item.as_dict() for item in objective.metric_specs])
        expected_v2 = (
            objective.objective_id,
            objective.objective_digest,
            objective.measurement_plan_digest,
            validator_manifest.manifest_id,
            validator_manifest_digest,
            expected_metric_specs_digest,
            tuning.stall_limit,
            tuning.diagnostic_patience,
            tuning.progress_token_budget,
            tuning.minimum_value_per_cost,
        )
        observed_v2 = (
            policy.objective_id,
            policy.stable_objective_digest,
            policy.measurement_plan_digest,
            policy.validator_manifest_id,
            policy.validator_asset_manifest_digest,
            policy.metric_specs_digest,
            policy.stall_limit,
            policy.diagnostic_patience,
            policy.progress_token_budget,
            policy.minimum_value_per_cost,
        )
        if observed_v2 != expected_v2:
            raise ValidationFailed("Measured LoopPolicy v2 objective/metric/tuning binding drift")
