"""Standing-grant admission and signed local rollout execution composition."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
import threading
from typing import Final

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import (
    EvolutionBudget,
    EvolutionRunPlan,
    StandingGrant,
)
from zekam.domain.evolution_rollout import RolloutPlan, RolloutStage, RolloutVerification
from zekam.domain.security import Authorization
from zekam.infrastructure.rollout_process_worker import (
    RolloutProcessWorker,
    RolloutWorkerVerifier,
)
from zekam.infrastructure.sqlite.evolution_authority import (
    ChildReservation,
    EvolutionApprovalAuthority,
    SQLiteEvolutionApprovalAuthority,
    SQLiteEvolutionAuthorityLedger,
)
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore

_AUTHORITY_TOKEN: Final = object()
_OPERATIONS: Final = {
    RolloutStage.SHADOW: "improvement.shadow",
    RolloutStage.CANARY: "improvement.canary",
    RolloutStage.ACTIVATION: "improvement.activate",
    RolloutStage.ROLLBACK: "improvement.rollback",
}


class _RolloutApprovalAuthority(EvolutionApprovalAuthority):
    def __init__(
        self,
        token: object,
        connection: sqlite3.Connection,
        executor: RolloutWorkerVerifier,
        verifier: RolloutWorkerVerifier,
    ) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise PolicyViolation("Rollout authority requires trusted composition")
        self._base = SQLiteEvolutionApprovalAuthority(connection)
        self._executor = executor
        self._verifier = verifier
        self._binding: tuple[EvolutionRunPlan, RolloutPlan] | None = None
        self._terminal: RolloutVerification | None = None

    def bind(self, run: EvolutionRunPlan, rollout: RolloutPlan) -> None:
        self._binding = (run, rollout)
        self._terminal = None

    def bind_terminal(self, verification: RolloutVerification) -> None:
        self._terminal = verification

    def verify_grant_approval(
        self, grant: StandingGrant, approval_receipt_digest: str, *, now: dt.datetime
    ) -> bool:
        return self._base.verify_grant_approval(
            grant, approval_receipt_digest, now=now
        )

    def verify_review(self, plan: EvolutionRunPlan, *, now: dt.datetime) -> bool:
        return self._base.verify_review(plan, now=now)

    def verify_effect_current(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        now: dt.datetime,
    ) -> bool:
        del now
        if self._binding is None:
            return False
        bound_run, rollout = self._binding
        return bool(
            bound_run == plan
            and rollout.grant_digest == grant.grant_digest
            and rollout.authorization_digest == child.authorization_digest
            and rollout.intent_digest == plan.input_digest
            and self._executor.identity.role == "executor"
            and self._verifier.identity.role == "verifier"
            and self._executor.identity != self._verifier.identity
            and self._executor.matches(
                self._executor.identity.boundary_body(),
                self._executor.identity.boundary_receipt,
            )
            and self._verifier.matches(
                self._verifier.identity.boundary_body(),
                self._verifier.identity.boundary_receipt,
            )
        )

    def verify_terminal_readback(
        self,
        plan: EvolutionRunPlan,
        child: Authorization,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        *,
        now: dt.datetime,
    ) -> bool:
        del usage
        verification = self._terminal
        return bool(
            verification is not None
            and self._binding is not None
            and self._binding[0] == plan
            and self._binding[1].authorization_digest == child.authorization_digest
            and verification.plan_digest == self._binding[1].plan_digest
            and verification.readback_digest == evidence_digest
            and status == ("completed" if verification.accepted else "failed")
            and verification.verified_at <= now
            and self._verifier.matches(
                verification.receipt_body(), verification.verification_receipt
            )
        )


class AuthorizedRolloutRuntime:
    """Only public route from a standing grant to a typed rollout settlement."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        improvement: SQLiteLocalImprovementStore,
        executor: RolloutProcessWorker,
        verifier: RolloutProcessWorker,
    ) -> None:
        if executor.role != "executor" or verifier.role != "verifier":
            raise ValidationFailed("Authorized rollout requires exact worker roles")
        self._authority = _RolloutApprovalAuthority(
            _AUTHORITY_TOKEN, connection, executor.verifier, verifier.verifier
        )
        self.ledger = SQLiteEvolutionAuthorityLedger(connection, self._authority)
        self._improvement = improvement
        self._executor = executor
        self._verifier = verifier
        self._execution_lock = threading.Lock()

    def register_grant(
        self,
        grant: StandingGrant,
        approval_receipt_digest: str,
        *,
        now: dt.datetime,
    ) -> None:
        self.ledger.register_grant(
            grant, approval_receipt_digest=approval_receipt_digest, now=now
        )

    def reserve(
        self,
        grant: StandingGrant,
        run: EvolutionRunPlan,
        *,
        reservation_id: str,
        idempotency_key: str,
        now: dt.datetime,
    ) -> ChildReservation:
        return self.ledger.reserve_child(
            grant,
            run,
            reservation_id=reservation_id,
            idempotency_key=idempotency_key,
            now=now,
        )

    def execute_reserved(
        self,
        grant: StandingGrant,
        run: EvolutionRunPlan,
        reservation: ChildReservation,
        rollout: RolloutPlan,
        *,
        claimed_at: dt.datetime,
    ) -> str:
        with self._execution_lock:
            return self._execute_reserved_unlocked(
                grant, run, reservation, rollout, claimed_at=claimed_at
            )

    def _execute_reserved_unlocked(
        self,
        grant: StandingGrant,
        run: EvolutionRunPlan,
        reservation: ChildReservation,
        rollout: RolloutPlan,
        *,
        claimed_at: dt.datetime,
    ) -> str:
        expected_operation = _OPERATIONS[rollout.stage]
        recovery = self._executor.recovery_status(
            self._improvement.typed_rollout_plan_digests()
        )
        if recovery["state"] != "clean":
            raise PolicyViolation("Unsettled rollout evidence requires reconciliation")
        if (
            run.operation != expected_operation
            or run.parent_grant_digest != rollout.grant_digest
            or run.candidate_digest != rollout.candidate_digest
            or run.fixture_digest != rollout.fixture_digest
            or run.protected_manifest_digest != rollout.resource_manifest_digest
            or run.input_digest != rollout.intent_digest
            or run.executor_ref != str(self._executor.identity.assignment_id)
            or run.verifier_ref != str(self._verifier.identity.assignment_id)
            or reservation.authorization.authorization_digest
            != rollout.authorization_digest
        ):
            raise PolicyViolation("Authorized rollout plan/child binding drift")
        self._authority.bind(run, rollout)
        self.ledger.claim_effect(
            grant,
            run,
            effect_body=run.effect_body(),
            reservation_id=reservation.reservation_id,
            authorization_digest=reservation.authorization.authorization_digest,
            effect_digest=run.effect_digest,
            now=claimed_at,
        )
        self.ledger.assert_effect_current(
            grant,
            run,
            reservation.authorization,
            now=claimed_at,
        )
        observation = self._executor.execute(rollout)
        verification = self._verifier.verify(
            rollout, observation, self._executor.verifier
        )
        self._authority.bind_terminal(verification)
        duration = max(
            1,
            math.ceil((observation.finished_at - observation.started_at).total_seconds()),
        )
        usage = EvolutionBudget(
            0,
            0,
            duration,
            0,
            len(canonical_json(observation.receipt_body()).encode()),
            1,
        )
        terminal_status = "completed" if verification.accepted else "failed"
        self._improvement.record_typed_rollout(
            rollout,
            observation,
            verification,
            executor_verifier=self._executor.verifier,
            independent_verifier=self._verifier.verifier,
            recorded_at=verification.verified_at,
            authority_ledger=self.ledger,
            reservation_id=reservation.reservation_id,
            run_plan=run,
            child_authorization=reservation.authorization,
            prepare_only=True,
        )
        self.ledger.record_terminal(
            reservation.reservation_id,
            run,
            reservation.authorization,
            status=terminal_status,
            evidence_digest=verification.readback_digest,
            usage=usage,
            now=verification.verified_at,
        )
        return self._improvement.finalize_prepared_typed_rollout(
            self.ledger,
            reservation.reservation_id,
            run,
            reservation.authorization,
        )

    def recover_unsettled_activation(
        self,
        run: EvolutionRunPlan,
        reservation: ChildReservation,
        rollout: RolloutPlan,
    ) -> str:
        """Restore only an exact claimed activation whose settlement was interrupted."""

        self.ledger.assert_claimed_recovery(
            reservation.reservation_id, run, reservation.authorization
        )
        if (
            run.input_digest != rollout.intent_digest
            or reservation.authorization.authorization_digest
            != rollout.authorization_digest
        ):
            raise PolicyViolation("Authorized recovery binding drift")
        recovery_digest = self._executor.recover_unsettled_activation(rollout)
        recovered_at = dt.datetime.now(dt.UTC)
        self.ledger.record_recovery_terminal(
            reservation.reservation_id,
            run,
            reservation.authorization,
            recovery_digest=recovery_digest,
            now=recovered_at,
        )
        self._improvement.record_typed_rollout_recovery(
            self.ledger,
            reservation.reservation_id,
            run,
            reservation.authorization,
            rollout.plan_digest,
            recovery_digest,
            recovered_at=recovered_at,
        )
        return recovery_digest

    def recover_from_durable(
        self, reservation_id: str, rollout_plan_digest: str
    ) -> str:
        """Recover after restart without trusting caller-recreated plan objects."""

        run, reservation = self.ledger.load_claimed(reservation_id)
        terminal = self.ledger.claimed_terminal(
            reservation_id, run, reservation.authorization
        )
        if terminal is not None and terminal[0] == "completed":
            return self._improvement.finalize_prepared_typed_rollout(
                self.ledger, reservation_id, run, reservation.authorization
            )
        if terminal is not None and terminal[0] == "unknown":
            raise PolicyViolation("Unknown evolution terminal requires adjudication")
        rollout = self._executor.load_rollout_plan(rollout_plan_digest)
        if terminal is not None:
            return self._improvement.record_typed_rollout_recovery(
                self.ledger,
                reservation_id,
                run,
                reservation.authorization,
                rollout.plan_digest,
                terminal[1],
                recovered_at=terminal[2],
            )
        return self.recover_unsettled_activation(run, reservation, rollout)
