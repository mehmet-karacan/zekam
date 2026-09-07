from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.evolution_rollout import RolloutPlan, RolloutStage, RolloutStatus
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.rollout_process_worker import (
    RolloutProcessWorker,
    bootstrap_rollout_fixture,
    register_rollout_artifact,
    rollout_recovery_status,
    rollout_resource_manifest,
    rollout_selector,
)

LKG = digest("last-known-good")
CANDIDATE = digest("candidate")


def _root(tmp_path: Path) -> Path:
    root = (tmp_path / "rollout").resolve()
    root.mkdir(mode=0o700)
    restrict_private_tree(root)
    bootstrap_rollout_fixture(root, "active.pointer", LKG)
    register_rollout_artifact(root, CANDIDATE, "salted-v1")
    return root


def _plan(
    root: Path, stage: RolloutStage, *, authorization: str, before: str = LKG
) -> RolloutPlan:
    return RolloutPlan(
        digest("candidate-record"),
        digest("evaluation-record"),
        stage,
        "fixture-pointer",
        "active.pointer",
        before,
        CANDIDATE,
        LKG,
        digest("fixture"),
        digest("harness"),
        rollout_resource_manifest(root, "active.pointer"),
        tuple(sorted(digest(f"input:{index}") for index in range(5))),
        5,
        digest("grant"),
        digest(authorization),
    )


def test_real_shadow_canary_activation_and_rollback_readback(tmp_path: Path) -> None:
    root = _root(tmp_path)
    executor = RolloutProcessWorker(
        root, UUID(int=1), digest("executor"), digest("executor-challenge"), "executor"
    )
    verifier = RolloutProcessWorker(
        root, UUID(int=2), digest("verifier"), digest("verifier-challenge"), "verifier"
    )
    try:
        for index, stage in enumerate((RolloutStage.SHADOW, RolloutStage.CANARY)):
            plan = _plan(root, stage, authorization=f"authorization:{index}")
            observation = executor.execute(plan)
            verification = verifier.verify(plan, observation, executor.verifier)
            assert observation.status is RolloutStatus.COMPLETED
            assert observation.observation_count == 5
            assert observation.before_digest == observation.after_digest == LKG
            assert verification.accepted is True
            assert rollout_selector(root, "active.pointer") == LKG

        activation = _plan(root, RolloutStage.ACTIVATION, authorization="activation")
        activated = executor.execute(activation)
        assert activated.production_effect_count == 1
        assert verifier.verify(activation, activated, executor.verifier).accepted is True
        assert rollout_selector(root, "active.pointer") == CANDIDATE

        rollback = _plan(
            root,
            RolloutStage.ROLLBACK,
            authorization="rollback",
            before=CANDIDATE,
        )
        rolled_back = executor.execute(rollback)
        assert rolled_back.production_effect_count == 1
        assert verifier.verify(rollback, rolled_back, executor.verifier).accepted is True
        assert rollout_selector(root, "active.pointer") == LKG
    finally:
        executor.close()
        verifier.close()


def test_forged_receipt_and_user_pointer_drift_fail_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    executor = RolloutProcessWorker(
        root, UUID(int=3), digest("executor"), digest("executor-challenge"), "executor"
    )
    verifier = RolloutProcessWorker(
        root, UUID(int=4), digest("verifier"), digest("verifier-challenge"), "verifier"
    )
    try:
        shadow = _plan(root, RolloutStage.SHADOW, authorization="shadow-forgery")
        observation = executor.execute(shadow)
        forged = replace(observation, evidence_digest=digest("caller-forged"))
        assert verifier.verify(shadow, forged, executor.verifier).accepted is False
        with pytest.raises(PolicyViolation, match="independent worker roles"):
            verifier.verify(shadow, observation, verifier.verifier)

        import sqlite3

        with sqlite3.connect(root / "rollout-state.sqlite3") as db:
            user_value = digest("user-new-value")
            selector = {
                "schema": "zekam-rollout-active-selector/v1",
                "pointer_name": "active.pointer",
                "artifact_digest": user_value,
                "revision": 2,
            }
            db.execute(
                "update active_selector set artifact_digest=?,revision=2,body_json=?",
                (user_value, canonical_json(selector)),
            )
            db.commit()
        rollback = _plan(
            root,
            RolloutStage.ROLLBACK,
            authorization="rollback-drift",
            before=CANDIDATE,
        )
        result = executor.execute(rollback)
        assert result.status is RolloutStatus.RECOVERY_REQUIRED
        assert result.production_effect_count == 0
        assert rollout_selector(root, "active.pointer") == digest("user-new-value")
        assert verifier.verify(rollback, result, executor.verifier).accepted is False
    finally:
        executor.close()
        verifier.close()


def test_unsettled_activation_recovery_is_bounded_and_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    executor = RolloutProcessWorker(
        root, UUID(int=5), digest("executor"), digest("recovery-challenge"), "executor"
    )
    try:
        activation = _plan(root, RolloutStage.ACTIVATION, authorization="recovery")
        observation = executor.execute(activation)
        assert observation.production_effect_count == 1
        assert rollout_selector(root, "active.pointer") == CANDIDATE
        first = executor.recover_unsettled_activation(activation)
        assert executor.recover_unsettled_activation(activation) == first
        assert rollout_selector(root, "active.pointer") == LKG
        assert rollout_recovery_status(root, ())["state"] == "clean"
    finally:
        executor.close()


def test_unsettled_rollback_recovery_never_reenables_candidate(tmp_path: Path) -> None:
    root = _root(tmp_path)
    executor = RolloutProcessWorker(
        root, UUID(int=6), digest("executor"), digest("rollback-recovery"), "executor"
    )
    try:
        activation = _plan(root, RolloutStage.ACTIVATION, authorization="activate-first")
        executor.execute(activation)
        rollback = _plan(
            root,
            RolloutStage.ROLLBACK,
            authorization="rollback-interrupted",
            before=CANDIDATE,
        )
        executor.execute(rollback)
        assert rollout_selector(root, "active.pointer") == LKG
        executor.recover_unsettled_activation(rollback)
        assert rollout_selector(root, "active.pointer") == LKG
    finally:
        executor.close()
