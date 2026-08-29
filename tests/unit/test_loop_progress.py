"""Progress packet, novelty and bounded stop gate tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.loop_progress_compiler import (
    LoopProgressCompiler,
    LoopProgressCompilerPolicy,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_progress import (
    AttemptNoveltyFingerprint,
    LoopAttemptProgress,
    LoopProgressCheckpoint,
    LoopStopReason,
    evaluate_attempt_gates,
    require_progress_packet,
)
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    ProgressState,
    evaluate_progress,
)

NOW = dt.datetime(2026, 8, 29, tzinfo=dt.UTC)


def _vector(previous: float, current: float):  # type: ignore[no-untyped-def]
    spec = MetricSpec(
        "quality",
        "quality",
        "point",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "external-test",
        target_value=1.0,
        minimum_meaningful_delta=0.01,
    )

    def evidence(value: float, label: str) -> MeasurementEvidence:
        return MeasurementEvidence(
            "quality",
            value,
            f"test:{label}",
            digest({"label": label, "value": value}),
            "source-7",
            NOW,
            "measurer",
            "verifier",
        )

    baseline = (evidence(previous, "baseline"),)
    return evaluate_progress((spec,), baseline, baseline, (evidence(current, "current"),))


def _novelty(label: str, *, hypothesis: str | None = None, failure: str | None = None):  # type: ignore[no-untyped-def]
    return AttemptNoveltyFingerprint.build(
        objective_digest=digest("objective"),
        artifact_digest=digest("artifact-baseline"),
        hypothesis_digest=digest(hypothesis or f"hypothesis:{label}"),
        patch_digest=digest(f"patch:{label}"),
        failure_signature=digest(failure or f"failure:{label}"),
        action_semantics_digest=digest(f"action:{label}"),
    )


def _attempt(
    ordinal: int,
    label: str,
    before: str,
    after: str,
    state: ProgressState,
    **novelty_changes: str,
) -> LoopAttemptProgress:
    vector = _vector(0.0, 1.0 if state is ProgressState.TARGET_REACHED else 0.0)
    return LoopAttemptProgress(
        uuid4(),
        ordinal,
        digest(before),
        digest(after),
        _novelty(label, **novelty_changes),
        state,
        vector.progress_digest,
        digest(f"diagnosis:{label}"),
    )


def test_prompt_or_uuid_rephrase_does_not_change_semantic_novelty() -> None:
    first = _novelty("one", hypothesis="same", failure="same")
    second = AttemptNoveltyFingerprint.build(**first.semantic_body())
    assert first.novelty_digest == second.novelty_digest


def test_repeated_patch_hypothesis_and_failure_are_first_class_stops() -> None:
    first = _attempt(1, "one", "a", "b", ProgressState.IMPROVED)
    repeated_patch = replace(
        _attempt(2, "two", "b", "c", ProgressState.IMPROVED),
        novelty=replace(
            _novelty("two"),
            patch_digest=first.novelty.patch_digest,
            novelty_digest=digest(
                {
                    **_novelty("two").semantic_body(),
                    "patch_digest": first.novelty.patch_digest,
                }
            ),
        ),
    )
    assert (
        evaluate_attempt_gates(repeated_patch, (first,), stall_limit=2).stop_reason
        is LoopStopReason.REPEATED_PATCH
    )
    repeated_hypothesis = _attempt(
        2,
        "two",
        "b",
        "c",
        ProgressState.IMPROVED,
        hypothesis="hypothesis:one",
        failure="failure:one",
    )
    assert (
        evaluate_attempt_gates(repeated_hypothesis, (first,), stall_limit=2).stop_reason
        is LoopStopReason.REPEATED_HYPOTHESIS
    )
    repeated_failure = _attempt(2, "two", "b", "c", ProgressState.IMPROVED, failure="failure:one")
    assert (
        evaluate_attempt_gates(repeated_failure, (first,), stall_limit=2).stop_reason
        is LoopStopReason.REPEATED_FAILURE_SIGNATURE
    )


def test_noop_plateau_regression_and_oscillation_stop() -> None:
    no_op = _attempt(1, "noop", "a", "a", ProgressState.IMPROVED)
    assert (
        evaluate_attempt_gates(no_op, (), stall_limit=2).stop_reason is LoopStopReason.NO_PROGRESS
    )
    first = _attempt(1, "one", "a", "b", ProgressState.PLATEAU)
    second = _attempt(2, "two", "b", "c", ProgressState.PLATEAU)
    assert (
        evaluate_attempt_gates(second, (first,), stall_limit=2).stop_reason
        is LoopStopReason.NO_PROGRESS
    )
    regression = _attempt(2, "regression", "b", "c", ProgressState.REGRESSED)
    assert (
        evaluate_attempt_gates(regression, (first,), stall_limit=3).stop_reason
        is LoopStopReason.METRIC_REGRESSION
    )
    oscillation = _attempt(3, "three", "c", "b", ProgressState.IMPROVED)
    assert (
        evaluate_attempt_gates(oscillation, (first, second), stall_limit=4).stop_reason
        is LoopStopReason.OSCILLATION
    )


def test_new_diagnosis_can_retry_but_is_not_measured_progress() -> None:
    current = _attempt(1, "diagnostic", "a", "b", ProgressState.PLATEAU)
    decision = evaluate_attempt_gates(current, (), stall_limit=3, diagnostic_patience=1)
    assert decision.allow_next_attempt
    assert decision.diagnostic_retry
    assert not decision.progress_counted


def test_target_reached_is_terminal_success_not_another_attempt() -> None:
    current = _attempt(1, "success", "a", "b", ProgressState.TARGET_REACHED)
    decision = evaluate_attempt_gates(current, (), stall_limit=2)
    assert not decision.allow_next_attempt
    assert decision.progress_counted
    assert decision.stop_reason is LoopStopReason.TARGET_REACHED
    with pytest.raises(ValidationFailed, match="digest drift"):
        replace(decision, decision_digest=digest("forged"))


def _checkpoint() -> LoopProgressCheckpoint:
    previous = _vector(0.0, 0.1)
    current = _vector(0.1, 0.5)
    return LoopProgressCheckpoint(
        digest("objective"),
        "source-7",
        digest("plan"),
        digest("policy"),
        digest("validator-assets"),
        digest("artifact-before"),
        digest("artifact-after"),
        uuid4(),
        2,
        previous,
        current,
        digest("accepted-hypothesis"),
        tuple(sorted(digest(f"rejected:{index}") for index in range(10))),
        digest("patch"),
        digest("failure"),
        "verification:diagnosis",
        digest("diagnosis"),
        tuple((f"evidence:{index}", digest(index)) for index in range(10)),
        2,
        500,
        1_000,
        60,
        "Only inspect the failing boundary",
        tuple(f"retry:{index}" for index in range(10)),
    )


def test_compiler_emits_deterministic_bounded_packet_and_attempt2_requires_it() -> None:
    policy = LoopProgressCompilerPolicy(
        max_packet_tokens=1600,
        max_rejected_hypotheses=2,
        max_new_evidence_refs=2,
        max_forbidden_retries=2,
    )
    compiler = LoopProgressCompiler(policy)
    checkpoint = _checkpoint()
    packet = compiler.compile(checkpoint)
    assert packet.packet_digest == compiler.compile(checkpoint).packet_digest
    assert packet.estimated_tokens <= 1600
    assert len(packet.rejected_hypothesis_digests) <= 2
    assert len(packet.new_evidence_refs) <= 2
    require_progress_packet(
        attempt_ordinal=2,
        packet=packet,
        objective_digest=checkpoint.objective_digest,
        source_revision=checkpoint.source_revision,
        plan_digest=checkpoint.plan_digest,
        policy_revision_digest=checkpoint.policy_revision_digest,
        validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
    )
    with pytest.raises(PolicyViolation, match="packet olmadan"):
        require_progress_packet(
            attempt_ordinal=2,
            packet=None,
            objective_digest=checkpoint.objective_digest,
            source_revision=checkpoint.source_revision,
            plan_digest=checkpoint.plan_digest,
            policy_revision_digest=checkpoint.policy_revision_digest,
            validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
        )
    with pytest.raises(PolicyViolation, match="stale"):
        require_progress_packet(
            attempt_ordinal=2,
            packet=packet,
            objective_digest=checkpoint.objective_digest,
            source_revision="different-source",
            plan_digest=checkpoint.plan_digest,
            policy_revision_digest=checkpoint.policy_revision_digest,
            validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
        )


def test_compiler_rejects_raw_multiline_focus_and_tiny_budget() -> None:
    with pytest.raises(ValidationFailed, match="raw/multiline"):
        LoopProgressCompiler().compile(replace(_checkpoint(), next_allowed_focus="raw\ntranscript"))
    with pytest.raises(ValidationFailed, match="en az 128"):
        LoopProgressCompilerPolicy(max_packet_tokens=127)
