"""Direct CAS links for actual test-agent dispatch, without a global CAS scan."""

from __future__ import annotations

import re
from uuid import UUID, uuid5

from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.envelope import parse_envelope
from zekam.domain.errors import PolicyViolation

SCHEMA = "zekam-unit-test-agent-execution/v1"
MAX_BYTES = 1_000_000
_NATIVE_ID = re.compile(r"opencode:([A-Za-z0-9_-]{1,200}):([A-Za-z0-9_-]{1,200})\Z")


def _read(objects, key):
    import json

    parse_digest(key)
    info = objects.stat(key)
    if not 0 < info.size_bytes <= MAX_BYTES:
        raise PolicyViolation("Agent execution evidence read budget exceeded")
    raw = objects.get(key)
    if len(raw) != info.size_bytes or digest_of_bytes(raw) != key:
        raise PolicyViolation("Agent execution evidence CAS digest/size drift")

    def pairs(values):
        body = {}
        for name, value in values:
            if name in body:
                raise PolicyViolation("Agent execution evidence duplicate JSON key")
            body[name] = value
        return body

    def constant(_value):
        raise PolicyViolation("Agent execution evidence nonfinite JSON")

    body = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(body, dict) or find_secret_value(body) is not None:
        raise PolicyViolation("Agent execution evidence unsafe object")
    return body


def capture_agent_execution(
    objects, *, task, assignment, invocation, dispatch, envelope, adapter, artifact_registrar=None
):
    """Call only after actual assignment-first dispatch and structured-result parsing."""
    artifact = (
        dispatch.payload.get("artifact_digest")
        if set(dispatch.payload) == {"artifact_digest"}
        else None
    )
    if artifact is None:
        raw = canonical_bytes(dispatch.payload)
        artifact = objects.put(raw, media_type="application/json").digest
    model = getattr(adapter, "model_id", None)
    profile = getattr(adapter, "provider_profile", None)
    upstream = None
    profile_digest = None
    from zekam.infrastructure.clients.opencode_batch import OpenCodeBatchAdapter

    if profile is not None and model is not None:
        _provider, upstream = profile.resolve(model)
        profile_digest = profile.profile_digest
    body = {
        "schema": SCHEMA,
        "request_digest": task.request_digest,
        "specialty": task.specialty.value,
        "ordinal": task.ordinal,
        "sequence": task.sequence,
        "assignment": assignment.identity_body(),
        "assignment_digest": assignment.assignment_digest,
        "invocation": {
            "id": str(invocation.id),
            "realm_id": str(invocation.realm_id),
            "assignment_id": str(invocation.assignment_id),
            "client_id": invocation.client_id,
            "execution_identity": invocation.execution_identity,
        },
        "invocation_digest": invocation.invocation_digest,
        "dispatch": dispatch.as_dict(),
        "dispatch_result_digest": dispatch.result_digest,
        "artifact_digest": artifact,
        "envelope": envelope.as_dict(),
        "execution_binding_digest": getattr(adapter, "execution_binding_digest", None),
        "configured_model": model,
        "configured_upstream": upstream,
        "provider_profile_digest": profile_digest,
        "native_model_identity_observed": False,
        "grants_authority": False,
        "native_protocol_observed": type(adapter) is OpenCodeBatchAdapter,
    }
    payload = canonical_bytes(body)
    if len(payload) > MAX_BYTES or find_secret_value(body) is not None:
        raise PolicyViolation("Agent execution evidence unsafe or oversized")
    assert_public_safe_projection(payload, relative_path="unit-test-agent-execution.json")
    info = objects.put(payload, media_type="application/json")
    if artifact_registrar is not None:
        for key in (
            assignment.instruction_digest,
            assignment.context_manifest_digest,
            artifact,
            info.digest,
        ):
            item = objects.stat(key)
            artifact_registrar(key, item.size_bytes, item.media_type or "application/json")
    return info.digest


def read_agent_execution(objects, key):
    body = _read(objects, key)
    expected = {
        "schema",
        "request_digest",
        "specialty",
        "ordinal",
        "sequence",
        "assignment",
        "assignment_digest",
        "invocation",
        "invocation_digest",
        "dispatch",
        "dispatch_result_digest",
        "artifact_digest",
        "envelope",
        "execution_binding_digest",
        "configured_model",
        "configured_upstream",
        "provider_profile_digest",
        "native_model_identity_observed",
        "grants_authority",
        "native_protocol_observed",
    }
    if (
        set(body) != expected
        or body["schema"] != SCHEMA
        or body["grants_authority"] is not False
        or body["native_model_identity_observed"] is not False
    ):
        raise PolicyViolation("Agent execution evidence exact schema required")
    assert_public_safe_projection(
        canonical_bytes(body), relative_path="unit-test-agent-execution.json"
    )
    assignment, invocation, dispatch = body["assignment"], body["invocation"], body["dispatch"]
    if not all(isinstance(value, dict) for value in (assignment, invocation, dispatch)):
        raise PolicyViolation("Agent execution evidence identity objects required")
    envelope = parse_envelope(body["envelope"])
    for value in (
        body["request_digest"],
        body["assignment_digest"],
        body["invocation_digest"],
        body["dispatch_result_digest"],
    ):
        parse_digest(value)
    for name in ("ordinal", "sequence"):
        if type(body[name]) is not int or body[name] < 0:
            raise PolicyViolation("Agent execution evidence ordinal invalid")
    for value in (
        assignment["id"],
        assignment["realm_id"],
        assignment["project_id"],
        assignment["work_item_id"],
        invocation["id"],
    ):
        if type(value) is not str or str(UUID(value)) != value:
            raise PolicyViolation("Agent execution evidence canonical UUID required")
    if (
        digest(assignment) != body["assignment_digest"]
        or digest(invocation) != body["invocation_digest"]
        or digest(dispatch) != body["dispatch_result_digest"]
        or invocation["assignment_id"] != assignment["id"]
        or invocation["realm_id"] != assignment["realm_id"]
        or dispatch["assignment_id"] != assignment["id"]
        or dispatch["invocation_id"] != invocation["id"]
        or dispatch["client_id"] != invocation["client_id"]
        or dispatch["role"] != assignment["role"]
        or dispatch["outcome"] != "success"
        or type(dispatch["exit_code"]) is not int
        or dispatch["exit_code"] != 0
        or dispatch["failure_category"] is not None
        or dispatch["grants_authority"] is not False
        or envelope.role.value != assignment["role"]
        or envelope.status.value != "completed"
        or assignment["agent_ref"] != "unit-test-" + body["specialty"]
    ):
        raise PolicyViolation("Agent execution evidence dispatch/assignment/envelope drift")
    artifact = _read(objects, body["artifact_digest"])
    if (
        canonical_bytes(dispatch["payload"])
        not in (
            canonical_bytes({"artifact_digest": body["artifact_digest"]}),
            canonical_bytes(artifact),
        )
        or artifact.get("result", {}).get("agent_id") != envelope.agent_id
    ):
        raise PolicyViolation("Agent execution evidence artifact binding drift")
    context = _read(objects, assignment["context_manifest_digest"])
    if (
        context.get("contract") != "zekam-unit-test-agent-context/v1"
        or context.get("specialty") != body["specialty"]
        or context.get("ordinal") != body["ordinal"]
        or context.get("execution_binding_digest") != body["execution_binding_digest"]
    ):
        raise PolicyViolation("Agent execution evidence actual context drift")
    from zekam.application.unit_test_agents import (
        AgentTask,
        AgentSpecialty,
        _deterministic,
        _INSTRUCTIONS,
        _FORMAT_NOTE,
    )

    task = AgentTask(
        AgentSpecialty(body["specialty"]),
        body["request_digest"],
        body["ordinal"],
        body["sequence"],
        {},
    )
    expected_assignment = _deterministic("assignment", task)
    expected_invocation = _deterministic("invocation", task)
    execution = body["execution_binding_digest"]
    identity = f"{invocation['client_id']}:{body['specialty']}:{body['ordinal']}"
    if execution is not None:
        parse_digest(execution)
        expected_assignment = uuid5(
            expected_assignment,
            digest(
                {
                    "execution_binding": execution,
                    "realm_id": assignment["realm_id"],
                    "work_item_id": assignment["work_item_id"],
                    "parent_assignment_id": assignment["parent_assignment_id"],
                }
            ),
        )
        expected_invocation = uuid5(expected_invocation, f"{expected_assignment}:{execution}")
        identity += ":" + execution
    if (
        str(expected_assignment) != assignment["id"]
        or str(expected_invocation) != invocation["id"]
        or invocation["execution_identity"] != identity
    ):
        raise PolicyViolation("Agent execution evidence deterministic request identity drift")
    instruction = objects.stat(assignment["instruction_digest"])
    if not 0 < instruction.size_bytes <= 16_384:
        raise PolicyViolation("Agent execution evidence instruction budget exceeded")
    raw = objects.get(assignment["instruction_digest"])
    if (
        digest_of_bytes(raw) != assignment["instruction_digest"]
        or len(raw) != instruction.size_bytes
    ):
        raise PolicyViolation("Agent execution evidence instruction digest drift")
    if raw != (_INSTRUCTIONS[task.specialty] + _FORMAT_NOTE).encode("utf-8"):
        raise PolicyViolation("Agent execution evidence instruction contract drift")
    if type(body["native_protocol_observed"]) is not bool:
        raise PolicyViolation("Agent execution evidence native protocol flag invalid")
    if body["native_protocol_observed"] and (execution is None or body["configured_model"] is None):
        raise PolicyViolation("Agent execution evidence native execution binding missing")
    native = (
        body["native_protocol_observed"]
        and invocation["client_id"] == "opencode"
        and _NATIVE_ID.fullmatch(envelope.agent_id) is not None
    )
    for field in ("configured_model", "configured_upstream"):
        value = body[field]
        if value is not None and (type(value) is not str or not 0 < len(value) <= 256):
            raise PolicyViolation("Agent execution evidence configured model invalid")
    if body["provider_profile_digest"] is not None:
        parse_digest(body["provider_profile_digest"])
    return {
        "execution_digest": key,
        "request_digest": body["request_digest"],
        "specialty": body["specialty"],
        "assignment_id": assignment["id"],
        "invocation_id": invocation["id"],
        "project_id": assignment["project_id"],
        "work_item_id": assignment["work_item_id"],
        "realm_id": assignment["realm_id"],
        "native_identity": envelope.agent_id if native else None,
        "native_execution_observed": native,
        "configured_model": body["configured_model"],
        "configured_upstream": body["configured_upstream"],
        "provider_profile_digest": body["provider_profile_digest"],
        "execution_binding_digest": body["execution_binding_digest"],
        "envelope_digest": envelope.result_digest,
        "artifact_digest": body["artifact_digest"],
        "context": context["context"],
        "result_body": artifact["body"],
        "native_model_identity_observed": False,
        "grants_authority": False,
    }


def compare_execution_provenance(
    objects,
    *,
    builder_digests: tuple[str, ...],
    verifier_digest: str,
    candidate_digest: str,
    measurement_digest: str,
) -> dict:
    """Classify observed executions and configured models, without quality approval."""
    parse_digest(candidate_digest)
    parse_digest(measurement_digest)
    if not 0 < len(builder_digests) <= 512 or len(set(builder_digests)) != len(builder_digests):
        raise PolicyViolation("Agent execution builder evidence budget/identity invalid")
    verifier = read_agent_execution(objects, verifier_digest)
    if verifier["specialty"] != "final-verifier" or (
        verifier["context"].get("candidate_digest") != candidate_digest
        or verifier["context"].get("measurement_digest") != measurement_digest
    ):
        raise PolicyViolation("Agent execution final verifier acceptance subject drift")
    builders = [read_agent_execution(objects, key) for key in builder_digests]
    for builder in builders:
        if builder["specialty"] != "builder" or any(
            builder[field] != verifier[field]
            for field in ("project_id", "work_item_id", "realm_id", "request_digest")
        ):
            raise PolicyViolation("Agent execution builder/verifier scope drift")
    execution_state = "unproven"
    if all(value["native_execution_observed"] for value in [verifier, *builders]):
        verifier_session = _NATIVE_ID.fullmatch(verifier["native_identity"])[1]
        independent = all(
            builder["assignment_id"] != verifier["assignment_id"]
            and builder["invocation_id"] != verifier["invocation_id"]
            and _NATIVE_ID.fullmatch(builder["native_identity"])[1] != verifier_session
            for builder in builders
        )
        execution_state = "distinct-native-executions" if independent else "same-native-execution"
    model_state = "unproven"
    if (
        verifier["provider_profile_digest"] is not None
        and verifier["configured_upstream"] is not None
        and all(
            builder["provider_profile_digest"] == verifier["provider_profile_digest"]
            and builder["configured_upstream"] is not None
            for builder in builders
        )
    ):
        distinct = all(
            builder["configured_upstream"].casefold() != verifier["configured_upstream"].casefold()
            for builder in builders
        )
        model_state = "configured-upstream-distinct" if distinct else "same-configured-upstream"
    body = {
        "schema": "zekam-unit-test-execution-provenance/v1",
        "builder_execution_digests": list(builder_digests),
        "verifier_execution_digest": verifier_digest,
        "candidate_digest": candidate_digest,
        "measurement_digest": measurement_digest,
        "execution_independence": execution_state,
        "model_independence": model_state,
        "native_model_identity_observed": False,
        "independent_quality_verified": False,
        "canonical_terminal_verified": False,
        "grants_authority": False,
    }
    return body | {"report_digest": digest(body)}
