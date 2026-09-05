from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.unit.test_loop_progress import _attempt, _checkpoint, _novelty

from zekam.application.loop_progress_compiler import LoopProgressCompiler
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_progress import (
    LoopProgressGateDecision,
    LoopStopReason,
    evaluate_attempt_gates,
    require_progress_packet,
)
from zekam.domain.optimization import ProgressState

pytestmark = pytest.mark.unit


def _packet() -> Any:
    return LoopProgressCompiler().compile(_checkpoint())


def _require(packet: Any, *, attempt_ordinal: int = 2, source_revision: str | None = None) -> None:
    checkpoint = _checkpoint()
    require_progress_packet(
        attempt_ordinal=attempt_ordinal,
        packet=packet,
        objective_digest=checkpoint.objective_digest,
        source_revision=source_revision or checkpoint.source_revision,
        plan_digest=checkpoint.plan_digest,
        policy_revision_digest=checkpoint.policy_revision_digest,
        validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
    )


def test_novelty_and_attempt_reject_digest_drift_invalid_ordinal_and_optional_digest() -> None:
    novelty = _novelty("valid")
    with pytest.raises(ValidationFailed, match="novelty digest drift"):
        replace(novelty, novelty_digest=digest("forged"))
    valid = _attempt(1, "valid", "before", "after", ProgressState.IMPROVED)
    with pytest.raises(ValidationFailed, match="ordinal"):
        replace(valid, attempt_ordinal=0)
    with pytest.raises(ValidationFailed):
        replace(valid, diagnosis_evidence_digest="bad")
    assert replace(valid, diagnosis_evidence_digest=None).diagnosis_evidence_digest is None


def _decision(**changes: Any) -> LoopProgressGateDecision:
    valid = evaluate_attempt_gates(
        _attempt(1, "valid", "before", "after", ProgressState.IMPROVED),
        (),
        stall_limit=2,
    )
    return replace(valid, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"grants_authority": True},
        {"allow_next_attempt": False, "stop_reason": None},
        {"allow_next_attempt": True, "stop_reason": LoopStopReason.NO_PROGRESS},
        {"progress_counted": True, "diagnostic_retry": True},
        {"reason_codes": ("z", "a")},
        {"reason_codes": ("same", "same")},
        {"decision_digest": "bad"},
    ],
)
def test_gate_decision_rejects_authority_state_reason_and_digest_drift(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _decision(**changes)


def test_evaluate_rejects_invalid_limits_history_order_and_objective_scope() -> None:
    current = _attempt(2, "current", "b", "c", ProgressState.IMPROVED)
    first = _attempt(1, "first", "a", "b", ProgressState.IMPROVED)
    with pytest.raises(ValidationFailed, match="stall"):
        evaluate_attempt_gates(current, (first,), stall_limit=0)
    with pytest.raises(ValidationFailed, match="stall"):
        evaluate_attempt_gates(current, (first,), stall_limit=1, diagnostic_patience=-1)
    with pytest.raises(ValidationFailed, match="ordinal"):
        evaluate_attempt_gates(first, (first,), stall_limit=2)
    with pytest.raises(ValidationFailed, match="ordinal"):
        evaluate_attempt_gates(current, (replace(first, attempt_ordinal=2),), stall_limit=2)
    drifted = replace(
        first,
        novelty=_novelty("other-objective"),
    )
    drifted = replace(
        drifted,
        novelty=replace(
            drifted.novelty,
            objective_digest=digest("other-objective"),
            novelty_digest=digest(
                {
                    **drifted.novelty.semantic_body(),
                    "objective_digest": digest("other-objective"),
                }
            ),
        ),
    )
    with pytest.raises(ValidationFailed, match="objective drift"):
        evaluate_attempt_gates(current, (drifted,), stall_limit=2)


def test_evaluate_covers_invalid_improved_and_exhausted_diagnostic_paths() -> None:
    invalid = _attempt(1, "invalid", "a", "b", ProgressState.INVALID)
    assert (
        evaluate_attempt_gates(invalid, (), stall_limit=2).stop_reason
        is LoopStopReason.INVALID_MEASUREMENT
    )
    improved = _attempt(1, "improved", "a", "b", ProgressState.IMPROVED)
    decision = evaluate_attempt_gates(improved, (), stall_limit=2)
    assert decision.allow_next_attempt and decision.progress_counted
    plateau = replace(
        _attempt(1, "plateau", "a", "b", ProgressState.PLATEAU),
        diagnosis_evidence_digest=None,
    )
    assert (
        evaluate_attempt_gates(plateau, (), stall_limit=2).stop_reason is LoopStopReason.NO_PROGRESS
    )
    diagnosed = _attempt(1, "diagnosed", "a", "b", ProgressState.PLATEAU)
    assert (
        evaluate_attempt_gates(diagnosed, (), stall_limit=2, diagnostic_patience=0).stop_reason
        is LoopStopReason.NO_PROGRESS
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": " "},
        {"validator_diagnosis_ref": " "},
        {"next_attempt_ordinal": 1},
        {"remaining_attempts": -1},
        {"remaining_tokens": -1},
        {"remaining_cost_micros": -1},
        {"remaining_time_seconds": -1},
        {"next_allowed_focus": " "},
        {"rejected_hypothesis_digests": tuple(sorted((digest("z"), digest("a")), reverse=True))},
        {"rejected_hypothesis_digests": ("bad",)},
        {"new_evidence_refs": (("z", digest("z")), ("a", digest("a")))},
        {"new_evidence_refs": ((" ", digest("blank")),)},
        {"new_evidence_refs": (("ref", "bad"),)},
        {"forbidden_retries": ("z", "a")},
        {"forbidden_retries": ("same", "same")},
    ],
)
def test_checkpoint_rejects_invalid_scope_budget_and_canonical_collections(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationFailed):
        replace(_checkpoint(), **changes)


def test_packet_rejects_authority_delta_and_token_budget_drift() -> None:
    packet = _packet()
    with pytest.raises(PolicyViolation, match="authority"):
        replace(packet, grants_authority=True)
    with pytest.raises(ValidationFailed, match="metric delta"):
        replace(packet, metric_deltas=())
    with pytest.raises(ValidationFailed, match="token budget"):
        replace(packet, max_packet_tokens=0)
    with pytest.raises(PolicyViolation, match="token budget"):
        replace(packet, max_packet_tokens=1)


def test_require_packet_rejects_invalid_first_attempt_and_accepts_empty_genesis() -> None:
    with pytest.raises(ValidationFailed, match="ordinal"):
        _require(None, attempt_ordinal=0)
    with pytest.raises(PolicyViolation, match="Ilk"):
        _require(_packet(), attempt_ordinal=1)
    _require(None, attempt_ordinal=1)
