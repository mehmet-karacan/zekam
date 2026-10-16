"""Offline native event usage projections; no provider or tests run by authoring."""

from copy import deepcopy

import pytest

from tests.unit.test_opencode_batch import _events, _stream
from tests.unit.test_opencode_skill_protocol import _events as skill_events
from tests.unit.test_opencode_skill_protocol import _package
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.clients.opencode_batch import parse_batch_events
from zekam.infrastructure.clients.opencode_skill_protocol import parse_native_skill_events
from zekam.infrastructure.clients.opencode_usage import validate_native_usage


def _metrics(part, *, cost=0.0000001):
    part.update(
        cost=cost,
        tokens={"input": 10, "output": 3, "reasoning": 2, "cache": {"read": 4, "write": 1}},
    )


def _artifact():
    events = _events()
    _metrics(events[-1]["part"])
    return parse_batch_events(_stream(events))


def test_native_metrics_are_bound_to_host_identity_and_not_billing():
    artifact = _artifact()
    usage = validate_native_usage(artifact)
    assert usage["native_reported_tokens"] == 20
    assert usage["token_measurement_complete"] is True
    assert usage["host_estimated_cost_usd"] == "0.0000001"
    assert usage["estimated_cost_micros"] == 1  # conservative ceiling, not a hard budget
    assert usage["provider_requests"] is None
    assert usage["billing_verified"] is False and usage["grants_authority"] is False


def test_legacy_and_partial_measurements_never_become_zero_or_complete():
    assert "native_usage" not in parse_batch_events(_stream(_events()))
    events = _events()
    events[-1]["part"]["tokens"] = {"input": 10, "output": 0}
    usage = parse_batch_events(_stream(events))["native_usage"]
    assert usage["native_reported_tokens"] is None
    assert usage["token_components"]["output"] == 0
    assert usage["token_components"]["reasoning"] is None
    assert usage["estimated_cost_micros"] is None
    assert usage["token_measurement_complete"] is False


@pytest.mark.parametrize("both", [False, True])
def test_skill_two_step_coverage_and_no_transcript_retention(tmp_path, both):
    events = skill_events(tmp_path)
    if both:
        events[4]["part"]["id"] = "prt_tool_finish"
        _metrics(events[4]["part"], cost=0.2)
    events[-1]["part"]["id"] = "prt_answer_finish"
    _metrics(events[-1]["part"], cost=0.1)
    artifact = parse_native_skill_events(
        _stream(events), package=_package(), skill_directory=tmp_path
    )
    usage = validate_native_usage(artifact)
    assert len(usage["steps"]) == 2
    assert usage["native_reported_tokens"] == (40 if both else None)
    assert usage["host_estimated_cost_usd"] == ("0.3" if both else None)
    assert usage["estimated_cost_micros"] == (300_000 if both else None)
    assert "sentinel" not in str(usage) and str(tmp_path) not in str(usage)


@pytest.mark.parametrize("value", [True, -1, 0.5, 2**53, 10**400, "10", [], {}])
def test_invalid_token_numbers_rejected(value):
    events = _events()
    _metrics(events[-1]["part"])
    events[-1]["part"]["tokens"]["input"] = value
    with pytest.raises(ValidationFailed):
        parse_batch_events(_stream(events))


@pytest.mark.parametrize("value", [True, -1, 1_000_001, "0.1", [], {}])
def test_invalid_cost_rejected(value):
    events = _events()
    _metrics(events[-1]["part"], cost=value)
    with pytest.raises(ValidationFailed):
        parse_batch_events(_stream(events))


@pytest.mark.parametrize(
    "mutation",
    [
        "total",
        "authority",
        "billing",
        "provider",
        "session",
        "message",
        "step",
        "component",
        "extra",
    ],
)
def test_cas_usage_projection_tamper_rejected(mutation):
    artifact = deepcopy(_artifact())
    usage = artifact["native_usage"]
    if mutation == "total":
        usage["native_reported_tokens"] = 21
    elif mutation == "authority":
        usage["grants_authority"] = True
    elif mutation == "billing":
        usage["billing_verified"] = True
    elif mutation == "provider":
        usage["provider_requests"] = 1
    elif mutation in {"session", "message"}:
        artifact["result"]["agent_id"] = (
            "opencode:ses_foreign:msg_fixture"
            if mutation == "session"
            else "opencode:ses_fixture:msg_foreign"
        )
    elif mutation == "step":
        usage["steps"][0]["native_step_digest"] = None
    elif mutation == "component":
        usage["steps"][0]["tokens"]["output"] = True
    else:
        usage["promoted"] = True
    with pytest.raises(ValidationFailed):
        validate_native_usage(artifact)


def test_model_cannot_supply_native_usage_even_with_valid_host_event():
    events = _events({"result": {}, "body": {}, "native_usage": _artifact()["native_usage"]})
    _metrics(events[-1]["part"])
    with pytest.raises(ValidationFailed):
        parse_batch_events(_stream(events))


def test_cost_only_zero_is_observed_but_tokens_still_unknown():
    events = _events()
    events[-1]["part"]["cost"] = -0.0
    usage = parse_batch_events(_stream(events))["native_usage"]
    assert usage["estimated_cost_micros"] == 0
    assert usage["host_estimated_cost_usd"] == "0"
    assert usage["native_reported_tokens"] is None


@pytest.mark.parametrize("tokens", [{"future": 1}, {"cache": True}, {"cache": {"future": 1}}])
def test_unknown_or_malformed_native_token_schema_is_not_silently_ignored(tokens):
    events = _events()
    events[-1]["part"]["tokens"] = tokens
    with pytest.raises(ValidationFailed):
        parse_batch_events(_stream(events))


def test_two_step_duplicate_identity_or_skill_message_reassociation_rejected(tmp_path):
    events = skill_events(tmp_path)
    for index, part in enumerate(
        event["part"] for event in events if event["type"] == "step_finish"
    ):
        _metrics(part)
        part["id"] = f"prt_finish_{index}"
    artifact = parse_native_skill_events(
        _stream(events), package=_package(), skill_directory=tmp_path
    )
    duplicate = deepcopy(artifact)
    duplicate["native_usage"]["steps"][1]["native_step_digest"] = duplicate["native_usage"][
        "steps"
    ][0]["native_step_digest"]
    with pytest.raises(ValidationFailed, match="repeated step"):
        validate_native_usage(duplicate)
    artifact["native_skill_invocation"]["native_message_digest"] = artifact["native_usage"][
        "steps"
    ][1]["native_message_digest"]
    with pytest.raises(ValidationFailed, match="skill tool message"):
        validate_native_usage(artifact)
