"""P1-008 bounded loop domain ve executor testleri."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.loop_service import BoundedLoopExecutor
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import (
    LoopAdmission,
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
    LoopValidation,
)
from zekam.domain.loop_progress import AttemptNoveltyFingerprint
from zekam.domain.optimization import ProgressState

NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
TERMINALS = tuple(sorted(LoopTerminalState, key=str))


def _policy(**changes: object) -> LoopPolicy:
    values: dict[str, object] = {
        "id": uuid4(),
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "step_id": "build",
        "assignment_id": uuid4(),
        "context_manifest_id": uuid4(),
        "validator_assignment_id": uuid4(),
        "max_attempts": 3,
        "max_tokens": 1000,
        "max_cost_micros": 10000,
        "deadline": NOW + dt.timedelta(minutes=10),
        "validator_spec_digest": digest("validator"),
        "required_delta": tuple(sorted(LoopDeltaKind, key=str)),
        "forbidden_effects": tuple(
            sorted(
                (
                    LoopEffectClass.DEPLOY,
                    LoopEffectClass.MIGRATION_APPLY,
                    LoopEffectClass.EXTERNAL_MESSAGE,
                ),
                key=str,
            )
        ),
        "terminal_states": TERMINALS,
        "source_revision": "revision-1",
        "context_manifest_digest": digest("context"),
        "plan_digest": digest("plan"),
        "policy_revision_digest": digest("policy"),
        "canonical_effect_kind": "none",
        "created_at": NOW,
    }
    values.update(changes)
    return LoopPolicy(**values)  # type: ignore[arg-type]


def _request(loop_id=None, **changes: object) -> LoopAttemptRequest:  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "loop_id": loop_id or uuid4(),
        "prompt_digest": digest("prompt"),
        "context_digest": digest("context"),
        "action_digest": digest("action"),
        "source_revision": "revision-1",
        "plan_digest": digest("plan"),
        "policy_revision_digest": digest("policy"),
        "validator_spec_digest": digest("validator"),
        "reserved_input_tokens": 100,
        "reserved_output_tokens": 50,
        "reserved_cost_micros": 1000,
    }
    values.update(changes)
    return LoopAttemptRequest(**values)  # type: ignore[arg-type]


def test_loop_policy_digest_butce_validator_delta_ve_terminal_setini_baglar() -> None:
    policy = _policy()
    assert policy.policy_digest == replace(policy).policy_digest
    assert policy.body()["grants_authority"] is False
    with pytest.raises(ValidationFailed, match="terminal state"):
        _policy(terminal_states=(LoopTerminalState.PASSED,))
    with pytest.raises(ValidationFailed, match="evidence delta"):
        _policy(required_delta=())


def test_semantic_request_uuid_ve_predecessor_degisiminden_etkilenmez() -> None:
    first = _request()
    second = replace(first, predecessor_attempt_id=uuid4())
    assert first.semantic_request_digest == second.semantic_request_digest
    assert first.binding_digest != second.binding_digest
    assert first.binding_digest == digest(
        {
            "source_revision": first.source_revision,
            "plan_digest": first.plan_digest,
            "policy_revision_digest": first.policy_revision_digest,
            "validator_spec_digest": first.validator_spec_digest,
            "predecessor_attempt_id": None,
        }
    )


def test_measured_loop_policy_v2_additive_ve_exact_bindinglidir() -> None:
    policy = _policy(
        objective_id=uuid4(),
        stable_objective_digest=digest("objective"),
        measurement_plan_digest=digest("measurement-plan"),
        validator_manifest_id=uuid4(),
        validator_asset_manifest_digest=digest("validator-assets"),
        metric_specs_digest=digest("metric-specs"),
        stall_limit=2,
        diagnostic_patience=1,
        progress_token_budget=256,
        minimum_value_per_cost=0.0,
    )
    assert policy.body()["measured_v2"]["stable_objective_digest"] == digest("objective")
    with pytest.raises(ValidationFailed, match="exact ve tam"):
        _policy(objective_id=uuid4())


def test_attempt2_progress_packet_ve_rephrase_proof_novelty_ister() -> None:
    with pytest.raises(ValidationFailed, match=r"attempt 2\+"):
        _request(attempt_ordinal=2, predecessor_attempt_id=uuid4())
    novelty = AttemptNoveltyFingerprint.build(
        objective_digest=digest("objective"),
        artifact_digest=digest("artifact"),
        hypothesis_digest=digest("hypothesis"),
        patch_digest=digest("patch"),
        failure_signature=digest("failure"),
        action_semantics_digest=digest("action-semantics"),
    )
    request = _request(
        attempt_ordinal=2,
        predecessor_attempt_id=uuid4(),
        objective_digest=digest("objective"),
        validator_asset_manifest_digest=digest("validator-assets"),
        progress_packet_digest=digest("packet"),
        metric_vector_digest=digest("metric-vector"),
        novelty_digest=novelty.novelty_digest,
        novelty=novelty,
    )
    rephrased = replace(request, prompt_digest=digest("rephrased prompt"))
    assert request.semantic_request_digest == novelty.novelty_digest
    assert rephrased.semantic_request_digest == novelty.novelty_digest
    assert request.binding_digest != _request().binding_digest
    with pytest.raises(ValidationFailed, match="body/digest/objective drift"):
        replace(request, novelty_digest=digest("forged-novelty"))


class Ledger:
    def __init__(self, admission: LoopAdmission) -> None:
        self.admission = admission
        self.completed: list[tuple[object, LoopValidation]] = []
        self.interrupted: list[str] = []

    def admit(self, _request: LoopAttemptRequest) -> LoopAdmission:
        return self.admission

    def complete(self, attempt_id, validation: LoopValidation) -> str:  # type: ignore[no-untyped-def]
        self.completed.append((attempt_id, validation))
        return "passed"

    def interrupt(self, _attempt_id, failure_digest: str) -> LoopTerminalState:  # type: ignore[no-untyped-def]
        self.interrupted.append(failure_digest)
        return LoopTerminalState.MANUAL_REVIEW

    def bind_dispatch(self, _attempt_id, _surface, _dispatch_id) -> None:  # type: ignore[no-untyped-def]
        pass


def test_executor_admission_olmadan_effect_calistirmaz() -> None:
    request = _request()
    admission = LoopAdmission(
        False,
        request.loop_id,
        None,
        None,
        LoopTerminalState.BUDGET_EXHAUSTED,
        "budget",
        digest("decision"),
    )
    called = False

    def effect() -> str:
        nonlocal called
        called = True
        return "forbidden"

    with pytest.raises(PolicyViolation, match="budget-exhausted"):
        BoundedLoopExecutor(Ledger(admission)).execute(
            request,
            effect=effect,
            validator=lambda _value, _admission: pytest.fail("validator calismamali"),
        )
    assert called is False


def test_executor_effecti_validator_ve_terminal_receipt_ile_kapatir() -> None:
    request = _request()
    attempt_id = uuid4()
    admission = LoopAdmission(
        True, request.loop_id, attempt_id, 1, None, "admitted", digest("decision")
    )
    ledger = Ledger(admission)
    validation = LoopValidation(
        outcome=LoopAttemptOutcome.PASSED,
        validator_spec_digest=request.validator_spec_digest,
        actual_input_tokens=80,
        actual_output_tokens=20,
        actual_cost_micros=500,
        result_invocation_id=uuid4(),
        verifier_invocation_id=uuid4(),
    )
    result = BoundedLoopExecutor(ledger).execute(
        request,
        effect=lambda: "result",
        validator=lambda _value, _admission: validation,
    )
    assert result.value == "result"
    assert result.terminal_state is LoopTerminalState.PASSED
    assert ledger.completed == [(attempt_id, validation)]


def test_executor_exceptioni_sessiz_retry_yerine_manual_review_yapar() -> None:
    request = _request()
    admission = LoopAdmission(
        True, request.loop_id, uuid4(), 1, None, "admitted", digest("decision")
    )
    ledger = Ledger(admission)

    def broken() -> str:
        raise RuntimeError("raw detail kalici olmamali")

    with pytest.raises(RuntimeError):
        BoundedLoopExecutor(ledger).execute(
            request,
            effect=broken,
            validator=lambda _value, _admission: pytest.fail("validator calismamali"),
        )
    assert len(ledger.interrupted) == 1


@pytest.mark.parametrize(
    ("producer_self_report", "hard_guard_regressed", "message"),
    (
        (True, False, "self-report"),
        (False, True, "Hard guard"),
    ),
)
def test_executor_v2_beyan_ve_guard_regresyonunu_progress_saymaz(
    producer_self_report: bool, hard_guard_regressed: bool, message: str
) -> None:
    request = _request()
    admission = LoopAdmission(
        True, request.loop_id, uuid4(), 1, None, "admitted", digest("decision")
    )
    ledger = Ledger(admission)
    validation = LoopValidation(
        outcome=LoopAttemptOutcome.RETRYABLE_FAILURE,
        validator_spec_digest=request.validator_spec_digest,
        actual_input_tokens=10,
        actual_output_tokens=10,
        actual_cost_micros=10,
        result_invocation_id=uuid4(),
        verifier_invocation_id=uuid4(),
        metric_evidence_refs=("evidence:external",),
        metric_vector_digest=digest("metric-vector"),
        progress_state=ProgressState.IMPROVED,
        progress_decision_digest=digest("progress-decision"),
        progress_packet_digest=digest("progress-packet"),
        producer_self_report=producer_self_report,
        hard_guard_regressed=hard_guard_regressed,
    )
    with pytest.raises(PolicyViolation, match=message):
        BoundedLoopExecutor(ledger).execute(
            request,
            effect=lambda: "result",
            validator=lambda _value, _admission: validation,
        )
    assert ledger.completed == []
    assert len(ledger.interrupted) == 1
