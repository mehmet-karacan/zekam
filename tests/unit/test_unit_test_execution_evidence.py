"""Direct gateway CAS receipts and legacy hydration; all clients are replay fixtures."""

import json

import pytest

from tests.unit.test_unit_test_agents_and_adapters import task
from tests.unit.unit_test_loop_support import Harness
from zekam.application.unit_test_agents import AgentSpecialty
from zekam.application.unit_test_execution_evidence import (
    read_agent_execution,
    compare_execution_provenance,
)
from zekam.application.unit_test_loop_state import LoopState
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation, NotFound
from zekam.domain.unit_test_engineering import UnitTestStopReason


def test_gateway_direct_execution_links_do_not_upgrade_replay_to_native(tmp_path):
    h = Harness(tmp_path)
    response = h.gateway.invoke(task())
    report = read_agent_execution(h.objects, response.execution_evidence_digest)
    assert report["assignment_id"] == str(response.assignment_id)
    assert report["invocation_id"] == str(response.invocation_id)
    assert report["envelope_digest"] == response.envelope_digest
    assert report["native_execution_observed"] is False
    assert report["native_model_identity_observed"] is False
    assert report["configured_upstream"] is None
    assert report["grants_authority"] is False


def test_terminal_carries_accepted_builder_and_final_verifier_execution_refs(tmp_path):
    h = Harness(tmp_path)
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.TARGET_REACHED
    terminal = json.loads(h.objects.get(outcome.terminal.evidence_digest))
    extra = terminal["extra"]
    assert extra["builder_provenance_complete"] is True
    assert extra["builder_execution_digests"]
    verifier = read_agent_execution(h.objects, extra["final_verifier_execution_digest"])
    assert verifier["specialty"] == "final-verifier"
    assert verifier["envelope_digest"] == extra["final_verifier_envelope"]
    state = terminal["state"]
    report = compare_execution_provenance(
        h.objects,
        builder_digests=tuple(extra["builder_execution_digests"]),
        verifier_digest=extra["final_verifier_execution_digest"],
        candidate_digest=state["accepted_candidate_digest"],
        measurement_digest=extra["measurement_digest"],
    )
    assert report["execution_independence"] == "unproven"
    assert report["model_independence"] == "unproven"
    assert report["independent_quality_verified"] is False
    assert report["canonical_terminal_verified"] is False
    legacy = {
        key: value
        for key, value in state.items()
        if key
        not in {
            "last_builder_execution_digest",
            "accepted_builder_execution_digests",
            "builder_provenance_complete",
        }
    }
    hydrated = LoopState.from_payload(legacy, h.request)
    assert hydrated.accepted_builder_execution_digests == []
    assert hydrated.builder_provenance_complete is False


@pytest.mark.parametrize("tamper", ["request", "dispatch", "context"])
def test_execution_reader_rejects_rehashed_cross_binding_records(tmp_path, tamper):
    h = Harness(tmp_path)
    response = h.gateway.invoke(task())
    body = json.loads(h.objects.get(response.execution_evidence_digest))
    if tamper == "request":
        body["request_digest"] = digest("unrelated-request")
    elif tamper == "dispatch":
        body["dispatch"]["role"] = "verifier"
        body["dispatch_result_digest"] = digest(body["dispatch"])
    else:
        body["execution_binding_digest"] = digest("unrelated-execution")
    key = h.objects.put(canonical_bytes(body), media_type="application/json").digest
    with pytest.raises(PolicyViolation):
        read_agent_execution(h.objects, key)


def test_execution_reader_rejects_missing_actual_result_artifact(tmp_path):
    h = Harness(tmp_path)
    response = h.gateway.invoke(task())
    body = json.loads(h.objects.get(response.execution_evidence_digest))
    h.objects._paths(body["artifact_digest"])[0].unlink()
    with pytest.raises((NotFound, OSError)):
        read_agent_execution(h.objects, response.execution_evidence_digest)


def test_execution_comparison_rejects_wrong_acceptance_subject(tmp_path):
    h = Harness(tmp_path)
    builder = h.gateway.invoke(task())
    verifier = h.gateway.invoke(
        task(
            AgentSpecialty.FINAL_VERIFIER,
            candidate_digest=digest("candidate"),
            measurement_digest=digest("measurement"),
        )
    )
    with pytest.raises(PolicyViolation, match="acceptance subject"):
        compare_execution_provenance(
            h.objects,
            builder_digests=(builder.execution_evidence_digest,),
            verifier_digest=verifier.execution_evidence_digest,
            candidate_digest=digest("other-candidate"),
            measurement_digest=digest("measurement"),
        )


@pytest.mark.parametrize("distinct", [False, True])
def test_different_model_aliases_do_not_prove_different_upstream_or_native_execution(
    tmp_path, distinct
):
    from zekam.application.unit_test_provider_profile import BatchProvider, BatchProviderProfile

    h = Harness(tmp_path)
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "local",
                "http://127.0.0.1:1234/v1",
                "loopback",
                (("builder", "model-a"), ("reviewer", "model-b" if distinct else "MODEL-A")),
            ),
        )
    )
    for specialty, model in (
        (AgentSpecialty.BUILDER, "local/builder"),
        (AgentSpecialty.FINAL_VERIFIER, "local/reviewer"),
    ):
        h.adapters[specialty].provider_profile = profile
        h.adapters[specialty].model_id = model
    builder = h.gateway.invoke(task())
    verifier = h.gateway.invoke(
        task(
            AgentSpecialty.FINAL_VERIFIER,
            candidate_digest=digest("candidate"),
            measurement_digest=digest("measurement"),
        )
    )
    report = compare_execution_provenance(
        h.objects,
        builder_digests=(builder.execution_evidence_digest,),
        verifier_digest=verifier.execution_evidence_digest,
        candidate_digest=digest("candidate"),
        measurement_digest=digest("measurement"),
    )
    assert report["model_independence"] == (
        "configured-upstream-distinct" if distinct else "same-configured-upstream"
    )
    assert report["execution_independence"] == "unproven"
    assert report["independent_quality_verified"] is False


def test_gateway_registers_execution_and_retained_dependency_artifacts(tmp_path):
    from dataclasses import replace

    h = Harness(tmp_path)
    registered = {}
    gateway = replace(
        h.gateway,
        artifact_registrar=lambda key, size, media: registered.update({key: (size, media)}),
    )
    response = gateway.invoke(task())
    body = json.loads(h.objects.get(response.execution_evidence_digest))
    for key in (
        response.execution_evidence_digest,
        body["artifact_digest"],
        body["assignment"]["instruction_digest"],
        body["assignment"]["context_manifest_digest"],
    ):
        assert registered[key][0] == h.objects.stat(key).size_bytes
