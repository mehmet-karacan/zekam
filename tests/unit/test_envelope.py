"""Agent Result Envelope, fan-in ve bagimsiz verifier kurallari."""

from __future__ import annotations

from uuid import uuid4

import pytest

from zekam.domain.envelope import (
    ENVELOPE_SCHEMA,
    AgentResultEnvelope,
    AgentRole,
    EnvelopeEvidence,
    EnvelopeStatus,
    SubagentAssignment,
    VerificationRequest,
    assert_distinct_verifier,
    assert_independent_verifier,
    assert_single_builder_per_resource,
    assert_subagent_policy,
    count_subagents,
    envelope_identity,
    fan_in,
    parse_envelope,
    verify,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

pytestmark = pytest.mark.unit

EVIDENCE = (EnvelopeEvidence(kind="test", reference="pytest: 5 passed"),)


def _envelope(
    *,
    agent_id: str = "builder-1",
    role: AgentRole = AgentRole.BUILDER,
    status: EnvelopeStatus = EnvelopeStatus.COMPLETED,
    **overrides: object,
) -> AgentResultEnvelope:
    defaults: dict[str, object] = {
        "agent_id": agent_id,
        "role": role,
        "status": status,
        "summary": "is tamamlandi",
        "evidence": EVIDENCE if status is EnvelopeStatus.COMPLETED else (),
    }
    defaults.update(overrides)
    return AgentResultEnvelope.create(**defaults)  # type: ignore[arg-type]


# -- envelope sozlesmesi ------------------------------------------------------------


def test_completed_envelope_requires_evidence() -> None:
    with pytest.raises(ValidationFailed, match="kanit"):
        AgentResultEnvelope.create(
            agent_id="a", role=AgentRole.BUILDER, status=EnvelopeStatus.COMPLETED, summary="bitti"
        )


def test_blocked_envelope_requires_blockers() -> None:
    with pytest.raises(ValidationFailed, match="blocker"):
        AgentResultEnvelope.create(
            agent_id="a", role=AgentRole.BUILDER, status=EnvelopeStatus.BLOCKED, summary="tikandi"
        )


def test_blank_summary_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        _envelope(summary="   ")


def test_result_digest_is_deterministic() -> None:
    first = _envelope()
    assert first.result_digest == first.result_digest


def test_result_digest_changes_with_status() -> None:
    completed = _envelope()
    partial = _envelope(status=EnvelopeStatus.PARTIAL)
    assert completed.result_digest != partial.result_digest


# -- ayristirma ----------------------------------------------------------------------


def test_valid_document_is_parsed() -> None:
    document = _envelope().as_dict()
    parsed = parse_envelope(document)
    assert parsed.agent_id == "builder-1"
    assert parsed.status is EnvelopeStatus.COMPLETED


def test_missing_required_field_is_rejected() -> None:
    document = _envelope().as_dict()
    del document["status"]
    with pytest.raises(ValidationFailed, match="zorunlu alanlari eksik"):
        parse_envelope(document)


def test_unknown_field_is_rejected() -> None:
    document = _envelope().as_dict()
    document["serbest_metin"] = "modelin sozu"
    with pytest.raises(ValidationFailed, match="bilinmeyen alan"):
        parse_envelope(document)


def test_invalid_enum_value_is_rejected() -> None:
    document = _envelope().as_dict()
    document["status"] = "sanirim-oldu"
    with pytest.raises(ValidationFailed, match="gecersiz enum"):
        parse_envelope(document)


def test_wrong_schema_is_rejected() -> None:
    document = _envelope().as_dict()
    document["schema"] = "serbest-metin/v1"
    with pytest.raises(ValidationFailed, match="sema"):
        parse_envelope(document)


def test_free_text_is_not_an_envelope() -> None:
    with pytest.raises(ValidationFailed):
        parse_envelope({"metin": "isi bitirdim"})


def test_schema_constant_is_versioned() -> None:
    assert ENVELOPE_SCHEMA.endswith("/v1")


# -- fan-in ---------------------------------------------------------------------------


def test_fan_in_requires_at_least_one_envelope() -> None:
    with pytest.raises(ValidationFailed):
        fan_in([])


def test_coordinator_alone_is_not_enough_for_agentic_work() -> None:
    coordinator = _envelope(agent_id="koordinator", role=AgentRole.COORDINATOR)
    with pytest.raises(PolicyViolation, match="subagent"):
        fan_in([coordinator], agentic=True)


def test_single_subagent_satisfies_the_minimum() -> None:
    result = fan_in([_envelope()], agentic=True)
    assert result.subagent_count == 1
    assert result.is_success


def test_coordinator_is_not_counted_as_subagent() -> None:
    envelopes = [
        _envelope(agent_id="koordinator", role=AgentRole.COORDINATOR),
        _envelope(agent_id="builder-1"),
    ]
    assert count_subagents(envelopes) == 1


def test_deterministic_work_needs_no_subagent() -> None:
    coordinator = _envelope(agent_id="koordinator", role=AgentRole.COORDINATOR)
    assert fan_in([coordinator], agentic=False).is_success


def test_failure_is_not_swallowed_by_success() -> None:
    envelopes = [
        _envelope(agent_id="builder-1"),
        _envelope(agent_id="builder-2", status=EnvelopeStatus.FAILED),
    ]
    result = fan_in(envelopes)
    assert result.status is EnvelopeStatus.FAILED
    assert not result.is_success
    assert len(result.unresolved) == 1


def test_recovery_required_outranks_failure() -> None:
    envelopes = [
        _envelope(agent_id="builder-1", status=EnvelopeStatus.FAILED),
        _envelope(agent_id="builder-2", status=EnvelopeStatus.RECOVERY_REQUIRED),
    ]
    assert fan_in(envelopes).status is EnvelopeStatus.RECOVERY_REQUIRED


@pytest.mark.parametrize(
    "status",
    [
        EnvelopeStatus.PARTIAL,
        EnvelopeStatus.FAILED,
        EnvelopeStatus.BLOCKED,
        EnvelopeStatus.RECOVERY_REQUIRED,
        EnvelopeStatus.ABSTAINED,
    ],
)
def test_every_non_success_status_stays_visible(status: EnvelopeStatus) -> None:
    blockers = ("dis sistem yanit vermiyor",) if status is EnvelopeStatus.BLOCKED else ()
    envelopes = [
        _envelope(agent_id="builder-1"),
        _envelope(agent_id="builder-2", status=status, blockers=blockers),
    ]
    result = fan_in(envelopes)
    assert result.unresolved
    assert result.unresolved[0].status is status


def test_fan_in_sums_measurements() -> None:
    envelopes = [
        _envelope(agent_id="builder-1", token_count=100, cost_micros=5),
        _envelope(agent_id="builder-2", token_count=250, cost_micros=7),
    ]
    result = fan_in(envelopes)
    assert result.total_tokens == 350
    assert result.total_cost_micros == 12


# -- subagent politikasi -----------------------------------------------------------------


def test_subagent_policy_rejects_missing_subagent() -> None:
    coordinator = _envelope(agent_id="koordinator", role=AgentRole.COORDINATOR)
    with pytest.raises(PolicyViolation):
        assert_subagent_policy([coordinator], agentic=True)


def test_subagent_policy_is_skipped_for_deterministic_work() -> None:
    coordinator = _envelope(agent_id="koordinator", role=AgentRole.COORDINATOR)
    assert_subagent_policy([coordinator], agentic=False)


def test_higher_minimum_is_enforced() -> None:
    with pytest.raises(PolicyViolation, match="en az 2"):
        assert_subagent_policy([_envelope()], agentic=True, minimum_subagents=2)


def test_single_builder_per_writable_resource() -> None:
    assignments = [
        SubagentAssignment("builder-1", AgentRole.BUILDER, ("path:zekam:a.py",)),
        SubagentAssignment("builder-2", AgentRole.BUILDER, ("path:zekam:a.py",)),
    ]
    with pytest.raises(PolicyViolation, match="iki builder"):
        assert_single_builder_per_resource(assignments)


def test_distinct_resources_allow_parallel_builders() -> None:
    assignments = [
        SubagentAssignment("builder-1", AgentRole.BUILDER, ("path:zekam:a.py",)),
        SubagentAssignment("builder-2", AgentRole.BUILDER, ("path:zekam:b.py",)),
    ]
    assert_single_builder_per_resource(assignments)


# -- bagimsiz verifier ---------------------------------------------------------------------


def test_self_verification_is_rejected_for_high_risk() -> None:
    with pytest.raises(PolicyViolation, match="bagimsiz verifier"):
        assert_independent_verifier(builder_agent_id="ayni", verifier_agent_id="ayni", risk="high")


def test_self_verification_is_allowed_for_low_risk() -> None:
    assert_independent_verifier(builder_agent_id="ayni", verifier_agent_id="ayni", risk="low")


def test_verify_rejects_self_verifier() -> None:
    request = VerificationRequest(
        builder_agent_id="ayni",
        verifier_agent_id="ayni",
        risk="critical",
        builder_result=_envelope(agent_id="ayni"),
    )
    with pytest.raises(PolicyViolation):
        verify(request)


def test_verify_passes_with_independent_verifier_and_evidence() -> None:
    outcome = verify(
        VerificationRequest(
            builder_agent_id="builder-1",
            verifier_agent_id="verifier-1",
            risk="high",
            builder_result=_envelope(),
        )
    )
    assert outcome.passed
    assert outcome.verifier_agent_id == "verifier-1"


def test_verify_fails_on_unsuccessful_builder_result() -> None:
    outcome = verify(
        VerificationRequest(
            builder_agent_id="builder-1",
            verifier_agent_id="verifier-1",
            risk="high",
            builder_result=_envelope(status=EnvelopeStatus.PARTIAL),
        )
    )
    assert not outcome.passed
    assert outcome.reason == "builder-status-partial"


def test_high_risk_requires_a_verifier_assignment() -> None:
    assignments = [SubagentAssignment("builder-1", AgentRole.BUILDER, ("path:zekam:a.py",))]
    with pytest.raises(PolicyViolation, match="bagimsiz verifier ister"):
        assert_distinct_verifier(assignments, risk="high")


def test_verifier_cannot_be_a_builder() -> None:
    assignments = [
        SubagentAssignment("ayni", AgentRole.BUILDER, ("path:zekam:a.py",)),
        SubagentAssignment("ayni", AgentRole.VERIFIER),
    ]
    with pytest.raises(PolicyViolation, match="ayni kimlik"):
        assert_distinct_verifier(assignments, risk="critical")


def test_low_risk_needs_no_verifier() -> None:
    assert_distinct_verifier([SubagentAssignment("builder-1", AgentRole.BUILDER)], risk="medium")


def test_envelope_identity_binds_child_to_run() -> None:
    run_id = uuid4()
    identity = envelope_identity(_envelope(), run_id)
    assert identity.startswith("sha256:")
    assert identity != envelope_identity(_envelope(agent_id="baska"), run_id)
