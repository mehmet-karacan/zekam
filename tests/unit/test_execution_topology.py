"""AC-11 worker fan-in: partial/contradiction disposition ve parallelism kurallari.

- Fan-in acik disposition tipi (partial/contradiction/failed/consistent).
- Worker failure coordinator tarafindan success olarak maskelenemez.
- Ayni writable resource'da parallel builder olamaz; disjoint -> parallel.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    FanInDisposition,
    FanInNodeOutcome,
    GraphFanInResult,
    GraphNodeTerminalState,
    GraphTerminalState,
    aggregate_fan_in_result,
    fan_in_disposition,
)

NOW = dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)


def _outcome(
    child: str,
    state: GraphNodeTerminalState = GraphNodeTerminalState.COMPLETED,
    *,
    evidence: str = "e",
    key: str | None = "agreement",
) -> FanInNodeOutcome:
    return FanInNodeOutcome(
        child_id=child,
        state=state,
        result_digest=digest(evidence),
        agreement_key=key,
    )


def test_consistent_fan_in_when_all_completed_and_same_result() -> None:
    result = fan_in_disposition(
        (_outcome("a", evidence="same"), _outcome("b", evidence="same"))
    )
    assert result is FanInDisposition.CONSISTENT


def test_contradiction_when_same_agreement_key_has_different_results() -> None:
    outcome_a = _outcome("a", GraphNodeTerminalState.COMPLETED, evidence="one")
    outcome_b = _outcome("b", GraphNodeTerminalState.COMPLETED, evidence="two")
    assert fan_in_disposition((outcome_a, outcome_b)) is FanInDisposition.CONTRADICTION


def test_disjoint_agreement_keys_are_not_contradiction() -> None:
    outcome_a = _outcome("a", GraphNodeTerminalState.COMPLETED, key="k1")
    outcome_b = _outcome("b", GraphNodeTerminalState.COMPLETED, key="k2")
    assert fan_in_disposition((outcome_a, outcome_b)) is FanInDisposition.CONSISTENT


def test_partial_result_is_explicit_disposition() -> None:
    outcome_a = _outcome("a", GraphNodeTerminalState.COMPLETED)
    outcome_b = _outcome("b", GraphNodeTerminalState.PARTIAL)
    assert fan_in_disposition((outcome_a, outcome_b)) is FanInDisposition.PARTIAL


@pytest.mark.parametrize(
    "state",
    (
        GraphNodeTerminalState.FAILED,
        GraphNodeTerminalState.CANCELLED,
        GraphNodeTerminalState.RECOVERY_REQUIRED,
    ),
)
def test_worker_failure_is_never_masked_as_success(state: GraphNodeTerminalState) -> None:
    outcome_a = _outcome("a", GraphNodeTerminalState.COMPLETED)
    outcome_b = _outcome("b", state)
    assert fan_in_disposition((outcome_a, outcome_b)) is FanInDisposition.FAILED


def test_aggregate_fan_in_result_binds_disposition_and_digest() -> None:
    result = aggregate_fan_in_result(
        "coordinator-1",
        (_outcome("a"), _outcome("b", GraphNodeTerminalState.FAILED)),
    )
    assert isinstance(result, GraphFanInResult)
    assert result.disposition is FanInDisposition.FAILED
    assert result.fan_in_result_digest == result.computed_digest


def test_fan_in_result_authority_free() -> None:
    result = aggregate_fan_in_result(
        "c1",
        (_outcome("a"), _outcome("b")),
    )
    assert result.body()["grants_authority"] is False


def test_fan_in_requires_unique_children_and_nonempty() -> None:
    with pytest.raises(ValidationFailed):
        fan_in_disposition(())
    with pytest.raises(ValidationFailed):
        GraphFanInResult(
            disposition=FanInDisposition.CONSISTENT,
            outcomes=(_outcome("a"), _outcome("a")),
            fan_in_result_digest=digest("x"),
        )


def test_receipt_forbids_masking_worker_failure_as_success() -> None:
    from zekam.domain.execution_topology import (
        GraphExecutionReceipt,
        GraphNodeMode,
        GraphNodeReceipt,
    )

    node = GraphNodeReceipt(
        step_id="s1",
        mode=GraphNodeMode.DIRECT,
        queued_at=NOW,
        started_at=NOW,
        ended_at=NOW + dt.timedelta(seconds=2),
        dependency_wait_millis=0,
        resource_wait_millis=0,
        coordination_input_tokens=0,
        coordination_output_tokens=0,
        coordination_cost_micros=0,
        coordination_message_count=0,
        result_digest=digest("r"),
        terminal_state=GraphNodeTerminalState.FAILED,
    )
    with pytest.raises(PolicyViolation, match="maskelenemez"):
        GraphExecutionReceipt(
            graph_root_id=UUID(int=1),
            plan_digest=digest("plan"),
            node_receipts=(node,),
            critical_path=("s1",),
            max_observed_concurrency=1,
            parallel_overlap_duration_millis=0,
            parallel_efficiency_ppm=0,
            coordination_input_tokens=0,
            coordination_output_tokens=0,
            coordination_cost_micros=0,
            coordination_message_count=0,
            fan_in_result_digest=digest("fanin"),
            fan_in_disposition=FanInDisposition.FAILED,
            terminal_state=GraphTerminalState.COMPLETED,  # masked -> reject
            topology_feedback=(),
            receipt_digest="",
        )
