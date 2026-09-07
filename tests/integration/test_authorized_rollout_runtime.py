from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application.rollout_runtime import AuthorizedRolloutRuntime
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.evolution_authority import (
    EVOLUTION_HANDLER_VERSIONS,
    EvolutionBudget,
    EvolutionRunPlan,
    ExecutionBoundary,
    StandingGrant,
    StandingGrantKind,
    evolution_effect_digest,
)
from zekam.domain.evolution_rollout import RolloutPlan, RolloutStage
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.security import DataClassification
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.rollout_process_worker import (
    RolloutProcessWorker,
    bootstrap_rollout_fixture,
    register_rollout_artifact,
    rollout_resource_manifest,
    rollout_selector,
)
from zekam.infrastructure.sqlite.evolution_authority import EVOLUTION_AUTHORITY_DDL


class _SettlementSink:
    def __init__(self) -> None:
        self.calls = 0
        self.plans: list[str] = []
        self.pending: dict[str, str] = {}
        self.fail_prepare = False
        self.fail_finalize = False

    def record_typed_rollout(self, *args: object, **_kwargs: object) -> str:
        if _kwargs.get("prepare_only") is True:
            if self.fail_prepare:
                raise RuntimeError("simulated-crash-before-prepare")
            self.pending[str(_kwargs["reservation_id"])] = cast(
                RolloutPlan, args[0]
            ).plan_digest
            return digest({"pending": cast(RolloutPlan, args[0]).plan_digest})
        self.calls += 1
        self.plans.append(cast(RolloutPlan, args[0]).plan_digest)
        return digest({"settlement": self.calls})

    def typed_rollout_plan_digests(self) -> tuple[str, ...]:
        return tuple(sorted(self.plans))

    def finalize_prepared_typed_rollout(
        self, _ledger: object, _reservation: str, run: EvolutionRunPlan, _child: object
    ) -> str:
        if self.fail_finalize:
            raise RuntimeError("simulated-crash-after-terminal")
        self.calls += 1
        self.plans.append(self.pending[_reservation])
        return digest({"settlement": self.calls})

    def record_typed_rollout_recovery(
        self, *_args: object, **_kwargs: object
    ) -> str:
        return digest({"recovery": _args[-1]})


def _grant(
    now: dt.datetime,
    executor_ref: str,
    verifier_ref: str,
    resource_manifest: str,
) -> StandingGrant:
    del executor_ref, verifier_ref
    operations = tuple(
        sorted(
            (
                "improvement.activate",
                "improvement.canary",
                "improvement.rollback",
                "improvement.shadow",
            )
        )
    )
    return StandingGrant(
        UUID(int=9101),
        1,
        StandingGrantKind.EVOLUTION,
        UUID(int=9102),
        UUID(int=9103),
        "windows-fixture",
        UUID(int=9104),
        (UUID(int=9105),),
        ("zekam-fixture",),
        operations,
        tuple(sorted(EVOLUTION_HANDLER_VERSIONS[item] for item in operations)),
        (ImprovementChangeClass.AUTO_SAFE,),
        tuple(
            sorted(
                {
                    "improvement-candidate",
                    "improvement-evaluation",
                    "rollout-canary",
                    "rollout-pointer",
                    "rollout-shadow",
                }
            )
        ),
        tuple(sorted({"rollout-canary", "rollout-pointer", "rollout-shadow"})),
        ("none",),
        ("local-deterministic",),
        (DataClassification.LOCAL_ONLY,),
        (),
        ExecutionBoundary.LOCAL,
        digest("task"),
        digest("policy"),
        digest("verifier"),
        digest("validator"),
        digest("source"),
        resource_manifest,
        digest("dependency"),
        EvolutionBudget(0, 0, 120, 0, 131072, 1),
        now - dt.timedelta(minutes=1),
        now + dt.timedelta(minutes=20),
        now + dt.timedelta(minutes=20),
        True,
        True,
    )


def _draft_rollout(
    stage: RolloutStage, grant: StandingGrant, lkg: str, resource_manifest: str
) -> RolloutPlan:
    candidate = digest("candidate-artifact")
    return RolloutPlan(
        digest("candidate-record"),
        digest("evaluation-record"),
        stage,
        "fixture-pointer",
        "active.pointer",
        candidate if stage is RolloutStage.ROLLBACK else lkg,
        candidate,
        lkg,
        digest("fixture"),
        digest("harness"),
        resource_manifest,
        tuple(sorted(digest(f"input:{index}") for index in range(5))),
        5,
        grant.grant_digest,
        digest("authorization-placeholder"),
    )


def _run(
    stage: RolloutStage,
    grant: StandingGrant,
    rollout: RolloutPlan,
    executor_ref: str,
    verifier_ref: str,
    now: dt.datetime,
) -> EvolutionRunPlan:
    operation = {
        RolloutStage.SHADOW: "improvement.shadow",
        RolloutStage.CANARY: "improvement.canary",
        RolloutStage.ACTIVATION: "improvement.activate",
        RolloutStage.ROLLBACK: "improvement.rollback",
    }[stage]
    reads = tuple(sorted({
        RolloutStage.SHADOW: ("improvement-candidate", "improvement-evaluation"),
        RolloutStage.CANARY: ("improvement-candidate", "rollout-shadow"),
        RolloutStage.ACTIVATION: ("rollout-canary",),
        RolloutStage.ROLLBACK: ("rollout-pointer",),
    }[stage]))
    writes = tuple(sorted({
        RolloutStage.SHADOW: ("rollout-shadow",),
        RolloutStage.CANARY: ("rollout-canary",),
        RolloutStage.ACTIVATION: ("rollout-pointer",),
        RolloutStage.ROLLBACK: ("rollout-pointer",),
    }[stage]))
    budget = EvolutionBudget(0, 0, 20, 0, 32768, 1)
    arguments: dict[str, Any] = {
        "parent_grant_digest": grant.grant_digest,
        "project_id": grant.project_ids[0],
        "logical_source": "zekam-fixture",
        "operation": operation,
        "handler_version": EVOLUTION_HANDLER_VERSIONS[operation],
        "change_class": ImprovementChangeClass.AUTO_SAFE,
        "readable_resources": reads,
        "writable_resources": writes,
        "model_ref": "none",
        "provider_ref": "local-deterministic",
        "data_classifications": (DataClassification.LOCAL_ONLY,),
        "network_scope": None,
        "execution_boundary": ExecutionBoundary.LOCAL,
        "task_scope_digest": grant.task_scope_digest,
        "policy_digest": grant.policy_digest,
        "verifier_digest": grant.verifier_digest,
        "validator_digest": grant.validator_digest,
        "source_lineage_digest": grant.source_lineage_digest,
        "protected_manifest_digest": grant.protected_manifest_digest,
        "dependency_manifest_digest": grant.dependency_manifest_digest,
        "input_digest": rollout.intent_digest,
        "candidate_digest": rollout.candidate_digest,
        "fixture_digest": rollout.fixture_digest,
        "budget_digest": digest(budget.body()),
        "budget": budget,
        "executor_ref": executor_ref,
        "verifier_ref": verifier_ref,
        "review_receipt_digest": None,
        "requested_at": now,
        "deadline": now + dt.timedelta(minutes=5),
    }
    arguments["effect_digest"] = evolution_effect_digest(
        operation=operation,
        handler_version=arguments["handler_version"],
        input_digest=arguments["input_digest"],
        candidate_digest=arguments["candidate_digest"],
        fixture_digest=arguments["fixture_digest"],
        budget_digest=arguments["budget_digest"],
        readable_resources=reads,
        writable_resources=writes,
    )
    return EvolutionRunPlan(**arguments)


def test_authority_child_claim_executes_and_revoke_blocks_next_effect(tmp_path: Path) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(seconds=30)
    root = (tmp_path / "rollout").resolve()
    root.mkdir(mode=0o700)
    restrict_private_tree(root)
    lkg = digest("lkg")
    bootstrap_rollout_fixture(root, "active.pointer", lkg)
    register_rollout_artifact(root, digest("candidate-artifact"), "salted-v1")
    executor = RolloutProcessWorker(
        root, UUID(int=9201), digest("executor"), digest("executor-challenge"), "executor"
    )
    verifier = RolloutProcessWorker(
        root, UUID(int=9202), digest("verifier"), digest("verifier-challenge"), "verifier"
    )
    authority_path = tmp_path / "evolution-authority.sqlite3"
    connection = sqlite3.connect(authority_path)
    connection.executescript(EVOLUTION_AUTHORITY_DDL)
    sink = _SettlementSink()
    runtime = AuthorizedRolloutRuntime(
        connection,
        cast(Any, sink),
        executor,
        verifier,
    )
    try:
        grant = _grant(
            now,
            str(executor.identity.assignment_id),
            str(verifier.identity.assignment_id),
            rollout_resource_manifest(root, "active.pointer"),
        )
        approval = digest("owner-approval")
        connection.execute(
            "insert into evolution_grant_approval values(?,?,?,?,?,?,?,?)",
            (
                approval,
                grant.grant_digest,
                str(grant.owner_id),
                str(grant.realm_id),
                grant.device_id,
                str(grant.owner_id),
                (now - dt.timedelta(seconds=1)).isoformat(),
                (now + dt.timedelta(minutes=10)).isoformat(),
            ),
        )
        connection.commit()
        runtime.register_grant(grant, approval, now=now)

        draft = _draft_rollout(
            RolloutStage.SHADOW,
            grant,
            lkg,
            rollout_resource_manifest(root, "active.pointer"),
        )
        run = _run(
            RolloutStage.SHADOW,
            grant,
            draft,
            str(executor.identity.assignment_id),
            str(verifier.identity.assignment_id),
            now,
        )
        reservation = runtime.reserve(
            grant,
            run,
            reservation_id="shadow-1",
            idempotency_key="shadow-1",
            now=now,
        )
        rollout = replace(
            draft,
            authorization_digest=reservation.authorization.authorization_digest,
        )
        assert runtime.execute_reserved(
            grant,
            run,
            reservation,
            rollout,
            claimed_at=now + dt.timedelta(seconds=1),
        ).startswith("sha256:")
        assert sink.calls == 1

        activation_draft = _draft_rollout(
            RolloutStage.ACTIVATION,
            grant,
            lkg,
            rollout_resource_manifest(root, "active.pointer"),
        )
        activation_run = _run(
            RolloutStage.ACTIVATION,
            grant,
            activation_draft,
            str(executor.identity.assignment_id),
            str(verifier.identity.assignment_id),
            now + dt.timedelta(seconds=2),
        )
        activation_reservation = runtime.reserve(
            grant,
            activation_run,
            reservation_id="activation-crash",
            idempotency_key="activation-crash",
            now=now + dt.timedelta(seconds=2),
        )
        activation = replace(
            activation_draft,
            authorization_digest=(
                activation_reservation.authorization.authorization_digest
            ),
        )
        sink.fail_prepare = True
        with pytest.raises(RuntimeError, match="simulated-crash"):
            runtime.execute_reserved(
                grant,
                activation_run,
                activation_reservation,
                activation,
                claimed_at=now + dt.timedelta(seconds=3),
            )
        sink.fail_prepare = False
        activation_plan_digest = activation.plan_digest
        executor.close()
        verifier.close()
        connection.close()
        executor = RolloutProcessWorker(
            root,
            UUID(int=9301),
            digest("recovery-executor"),
            digest("recovery-executor-challenge"),
            "executor",
        )
        verifier = RolloutProcessWorker(
            root,
            UUID(int=9302),
            digest("recovery-verifier"),
            digest("recovery-verifier-challenge"),
            "verifier",
        )
        connection = sqlite3.connect(authority_path)
        runtime = AuthorizedRolloutRuntime(
            connection,
            cast(Any, sink),
            executor,
            verifier,
        )
        assert runtime.recover_from_durable(
            activation_reservation.reservation_id, activation_plan_digest
        ).startswith("sha256:")
        assert executor.recovery_status(tuple(sink.plans))["state"] == "clean"
        assert connection.execute(
            "select status from evolution_child_terminal where reservation_id=?",
            (activation_reservation.reservation_id,),
        ).fetchone() == ("failed",)

        terminal_crash_draft = replace(
            _draft_rollout(
                RolloutStage.ACTIVATION,
                grant,
                lkg,
                rollout_resource_manifest(root, "active.pointer"),
            ),
            fixture_digest=digest("fixture-terminal-crash"),
        )
        terminal_crash_run = _run(
            RolloutStage.ACTIVATION,
            grant,
            terminal_crash_draft,
            str(executor.identity.assignment_id),
            str(verifier.identity.assignment_id),
            now + dt.timedelta(seconds=4),
        )
        terminal_crash_reservation = runtime.reserve(
            grant,
            terminal_crash_run,
            reservation_id="activation-terminal-crash",
            idempotency_key="activation-terminal-crash",
            now=now + dt.timedelta(seconds=4),
        )
        terminal_crash_rollout = replace(
            terminal_crash_draft,
            authorization_digest=(
                terminal_crash_reservation.authorization.authorization_digest
            ),
        )
        sink.fail_finalize = True
        with pytest.raises(RuntimeError, match="after-terminal"):
            runtime.execute_reserved(
                grant,
                terminal_crash_run,
                terminal_crash_reservation,
                terminal_crash_rollout,
                claimed_at=now + dt.timedelta(seconds=5),
            )
        sink.fail_finalize = False
        selector_before_reconcile = terminal_crash_rollout.candidate_artifact_digest
        executor.close()
        verifier.close()
        connection.close()
        executor = RolloutProcessWorker(
            root,
            UUID(int=9401),
            digest("terminal-recovery-executor"),
            digest("terminal-recovery-executor-challenge"),
            "executor",
        )
        verifier = RolloutProcessWorker(
            root,
            UUID(int=9402),
            digest("terminal-recovery-verifier"),
            digest("terminal-recovery-verifier-challenge"),
            "verifier",
        )
        connection = sqlite3.connect(authority_path)
        runtime = AuthorizedRolloutRuntime(
            connection, cast(Any, sink), executor, verifier
        )
        assert runtime.recover_from_durable(
            terminal_crash_reservation.reservation_id,
            terminal_crash_rollout.plan_digest,
        ).startswith("sha256:")
        assert executor.recovery_status(tuple(sink.plans))["state"] == "clean"
        assert rollout_selector(root, "active.pointer") == selector_before_reconcile

        next_draft = _draft_rollout(
            RolloutStage.CANARY,
            grant,
            lkg,
            rollout_resource_manifest(root, "active.pointer"),
        )
        next_run = _run(
            RolloutStage.CANARY,
            grant,
            next_draft,
            str(executor.identity.assignment_id),
            str(verifier.identity.assignment_id),
            now + dt.timedelta(seconds=6),
        )
        next_reservation = runtime.reserve(
            grant,
            next_run,
            reservation_id="canary-1",
            idempotency_key="canary-1",
            now=now + dt.timedelta(seconds=6),
        )
        runtime.ledger.revoke(
            grant.grant_digest,
            reason="owner-request",
            now=now + dt.timedelta(seconds=7),
        )
        with pytest.raises(PolicyViolation, match="revoked"):
            runtime.execute_reserved(
                grant,
                next_run,
                next_reservation,
                replace(
                    next_draft,
                    authorization_digest=next_reservation.authorization.authorization_digest,
                ),
                claimed_at=now + dt.timedelta(seconds=8),
            )
        assert sink.calls == 2
    finally:
        connection.close()
        executor.close()
        verifier.close()
