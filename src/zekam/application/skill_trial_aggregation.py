"""Read terminal trial evidence and regrade paired results without provider calls."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from zekam.application.canonical_job_runtime import replay_canonical_job
from zekam.application.config import load_settings
from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.application.object_store import ObjectStore
from zekam.application.skill_evidence import _constant, _pairs
from zekam.application.skill_trials import _INSTRUCTION, _validate_case
from zekam.application.unit_test_models import validate_model_id
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.clients import DispatchOutcome
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_records import (
    KIND_ASSIGNMENT,
    KIND_INVOCATION,
    KIND_RESULT,
    CasRecordIndex,
)

_COMMON = (
    "revision_digest",
    "package_digest",
    "project_id",
    "source_root_digest",
    "model_id",
    "execution_binding_digest",
    "instruction_contract_digest",
    "candidate_instruction_digest",
    "profile",
    "tools",
    "baseline",
    "timeout_seconds_per_call",
)
_PLAN_FIELDS = set(_COMMON) | {
    "schema",
    "case_digest",
    "case_id",
    "partition",
    "case_provenance",
    "repetition",
    "max_agent_calls",
    "remote_disclosure_required",
    "order",
    "oracle_disclosed_to_builder",
    "activatable",
    "grants_authority",
    "plan_digest",
}
_OBSERVATION_FIELDS = {
    "condition",
    "assignment_id",
    "invocation_id",
    "dispatch_result_digest",
    "dispatch_result",
    "outcome",
    "elapsed_ns",
    "artifact_digest",
    "output_digest",
    "native_identity",
    "valid_output",
    "exact_match",
}
_NATIVE_ID = re.compile(r"opencode:([A-Za-z0-9_-]{1,200}):[A-Za-z0-9_-]{1,200}\Z")


def _object(
    objects: ObjectStore, key: str, *, maximum: int, public_safe: bool = False
) -> dict[str, Any]:
    parse_digest(key)
    info = objects.stat(key)
    if info.size_bytes > maximum:
        raise PolicyViolation("Skill aggregate evidence object exceeds limit")
    raw = objects.get(key)
    if len(raw) != info.size_bytes or digest_of_bytes(raw) != key:
        raise PolicyViolation("Skill aggregate evidence object digest/size drift")
    if public_safe:
        assert_public_safe_projection(raw, relative_path="skill-trial-evidence.json")
    body = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(body, dict):
        raise PolicyViolation("Skill aggregate evidence object required")
    return body


def _record(index: CasRecordIndex, kind: str, key: str) -> dict[str, Any]:
    matches = index.find(kind, key)
    if len(matches) != 1:
        raise PolicyViolation("Skill aggregate assignment evidence missing or ambiguous")
    return _object(index.objects, matches[0], maximum=16_384)


def _observation(
    objects: ObjectStore,
    index: CasRecordIndex,
    body: Any,
    *,
    condition: str,
    plan: dict[str, Any],
    case: dict[str, Any],
    binding: Any,
    input_digest: str,
    instruction: str = _INSTRUCTION,
    execution_namespace: str = "skill-trial",
    expected_context: dict[str, Any] | None = None,
    native_expected: dict[str, str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(body, dict)
        or set(body) != _OBSERVATION_FIELDS
        or body["condition"] != condition
    ):
        raise PolicyViolation("Skill aggregate exact observation required")
    if type(body["elapsed_ns"]) is not int or not 0 <= body["elapsed_ns"] <= 3_600_000_000_000:
        raise PolicyViolation("Skill aggregate measured duration invalid")
    for key in ("assignment_id", "invocation_id"):
        if type(body[key]) is not str or str(UUID(body[key])) != body[key]:
            raise PolicyViolation("Skill aggregate canonical assignment identity required")
    assignment = _record(index, KIND_ASSIGNMENT, body["assignment_id"])
    identity = {k: v for k, v in assignment.items() if k not in {"record", "assignment_digest"}}
    if (
        digest(identity) != assignment.get("assignment_digest")
        or assignment.get("id") != body["assignment_id"]
        or assignment.get("project_id") != binding.project_id
        or assignment.get("work_item_id") != binding.work_item_id
        or assignment.get("role") != "builder"
        or assignment.get("parent_assignment_id") is None
        or assignment.get("instruction_digest") != plan["instruction_contract_digest"]
    ):
        raise PolicyViolation("Skill aggregate canonical assignment scope/harness drift")
    if (
        digest(
            {
                "plan": plan["plan_digest"],
                "realm_id": assignment["realm_id"],
                "coordinator_id": assignment["parent_assignment_id"],
            }
        )
        != input_digest
    ):
        raise PolicyViolation("Skill aggregate job/assignment identity binding drift")
    context = _object(
        objects, assignment["context_manifest_digest"], maximum=64_000, public_safe=True
    )
    if expected_context is not None:
        context_valid = canonical_json(context) == canonical_json(expected_context)
    else:
        context_valid = (
            set(context) == {"schema", "input", "skill_instructions"}
            and context["schema"] == "zekam-skill-trial-context/v1"
            and canonical_json(context["input"]) == canonical_json(case["input"])
            and type(context["skill_instructions"]) is str
            and (condition != "baseline" or context["skill_instructions"] == "")
            and (
                condition != "candidate"
                or digest_of_bytes(context["skill_instructions"].encode("utf-8"))
                == plan["candidate_instruction_digest"]
            )
        )
    if not context_valid:
        raise PolicyViolation("Skill aggregate actual prompt/oracle isolation drift")
    instruction_info = objects.stat(assignment["instruction_digest"])
    if instruction_info.size_bytes != len(instruction.encode("utf-8")) or objects.get(
        assignment["instruction_digest"]
    ) != instruction.encode("utf-8"):
        raise PolicyViolation("Skill aggregate frozen instruction missing")
    invocation = _record(index, KIND_INVOCATION, body["invocation_id"])
    invocation_identity = {
        k: v for k, v in invocation.items() if k not in {"record", "invocation_digest"}
    }
    if (
        digest(invocation_identity) != invocation.get("invocation_digest")
        or invocation.get("id") != body["invocation_id"]
        or invocation.get("assignment_id") != body["assignment_id"]
        or invocation.get("realm_id") != assignment.get("realm_id")
        or invocation.get("client_id") != "opencode"
        or invocation.get("execution_identity")
        != f"{execution_namespace}:{plan['execution_binding_digest']}:{condition}"
    ):
        raise PolicyViolation("Skill aggregate invocation binding drift")
    assignment_id = uuid5(
        UUID(binding.run_id),
        f"{execution_namespace}:{plan['plan_digest']}:{condition}:{assignment['realm_id']}:{assignment['parent_assignment_id']}",
    )
    if (
        str(assignment_id) != body["assignment_id"]
        or str(uuid5(assignment_id, plan["execution_binding_digest"])) != body["invocation_id"]
    ):
        raise PolicyViolation("Skill aggregate assignment is not bound to exact trial plan")
    result = _record(index, KIND_RESULT, f"{body['assignment_id']}:{body['invocation_id']}")
    if (
        result.get("envelope_digest") != body["dispatch_result_digest"]
        or result.get("assignment_id") != body["assignment_id"]
        or result.get("invocation_id") != body["invocation_id"]
    ):
        raise PolicyViolation("Skill aggregate dispatch result binding drift")
    dispatch = body["dispatch_result"]
    if (
        not isinstance(dispatch, dict)
        or set(dispatch)
        != {
            "assignment_id",
            "invocation_id",
            "client_id",
            "role",
            "outcome",
            "exit_code",
            "payload",
            "failure_category",
            "grants_authority",
        }
        or digest(dispatch) != body["dispatch_result_digest"]
        or dispatch["assignment_id"] != body["assignment_id"]
        or dispatch["invocation_id"] != body["invocation_id"]
        or dispatch["client_id"] != "opencode"
        or dispatch["role"] != "builder"
        or dispatch["outcome"] != body["outcome"]
        or dispatch["grants_authority"] is not False
    ):
        raise PolicyViolation("Skill aggregate actual dispatch envelope drift")
    outcome = DispatchOutcome(body["outcome"])
    valid, output_digest, native_identity = False, None, None
    if outcome is DispatchOutcome.SUCCESS:
        if (
            canonical_json(dispatch["payload"])
            != canonical_json({"artifact_digest": body["artifact_digest"]})
            or type(dispatch["exit_code"]) is not int
            or dispatch["exit_code"] != 0
            or dispatch["failure_category"] is not None
        ):
            raise PolicyViolation("Skill aggregate successful process/artifact binding invalid")
        artifact = _object(objects, body["artifact_digest"], maximum=64_000, public_safe=True)
        meta, output = artifact.get("result"), artifact.get("body")
        if set(artifact) != (
            {"result", "body"}
            | ({"native_skill_invocation"} if native_expected is not None else set())
        ) or not isinstance(meta, dict):
            raise PolicyViolation("Skill aggregate native artifact shape invalid")
        native_identity = meta.get("agent_id")
        if not isinstance(native_identity, str) or _NATIVE_ID.fullmatch(native_identity) is None:
            raise PolicyViolation("Skill aggregate observed native identity missing")
        if native_expected is not None:
            native = artifact["native_skill_invocation"]
            expected_keys = set(native_expected) | {
                "schema",
                "native_session_digest",
                "native_message_digest",
                "native_call_digest",
                "native_render_digest",
                "status",
                "skill_effect_verified",
                "grants_authority",
            }
            if (
                not isinstance(native, dict)
                or set(native) != expected_keys
                or any(native[k] != v for k, v in native_expected.items())
                or native["schema"] != "zekam-observed-native-skill-call/v1"
                or native["status"] != "completed"
                or native["skill_effect_verified"] is not False
                or native["grants_authority"] is not False
            ):
                raise PolicyViolation("Native skill observed tool subject drift")
            for key in (
                "native_session_digest",
                "native_message_digest",
                "native_call_digest",
                "native_render_digest",
            ):
                parse_digest(native[key])
            _, session_id, final_message = native_identity.split(":")
            if native["native_session_digest"] != digest_of_bytes(
                session_id.encode("utf-8")
            ) or native["native_message_digest"] == digest_of_bytes(final_message.encode("utf-8")):
                raise PolicyViolation("Native skill tool/final message binding drift")
        valid = set(meta) == {"agent_id"} and isinstance(output, dict) and set(output) == {"output"}
        if valid:
            output_digest = digest(output["output"])
    elif (
        body["artifact_digest"] is not None
        or dispatch["payload"] != {}
        or type(dispatch["failure_category"]) is not str
        or not dispatch["failure_category"]
    ):
        raise PolicyViolation("Skill aggregate failed trial carries success artifact")
    expected = body | {
        "native_identity": native_identity,
        "valid_output": valid,
        "output_digest": output_digest,
        "exact_match": valid and output_digest == digest(case["expected_output"]),
    }
    if canonical_json(expected) != canonical_json(body):
        raise PolicyViolation("Skill aggregate claimed grade differs from actual output")
    return body


def aggregate_skill_trials(
    home: Path,
    *,
    references: tuple[str, ...],
    cases: tuple[dict[str, Any], ...],
    project_id: str,
    revision_digest: str,
    package_digest: str,
) -> dict[str, Any]:
    """No input report JSON is trusted: reopen exact terminal jobs and artifacts."""
    if not 1 <= len(references) <= 100 or len(set(references)) != len(references):
        raise PolicyViolation("Skill aggregate requires 1..100 distinct trial references")
    if not 1 <= len(cases) <= 100:
        raise PolicyViolation("Skill aggregate requires 1..100 bounded cases")
    by_digest: dict[str, dict[str, Any]] = {}
    by_name: dict[str, str] = {}
    for case in cases:
        _validate_case(case)
        key = digest(case)
        if key in by_digest or (case["case_id"] in by_name and by_name[case["case_id"]] != key):
            raise PolicyViolation("Skill aggregate duplicate or conflicting case identity")
        by_digest[key], by_name[case["case_id"]] = case, key
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    index = CasRecordIndex(objects)
    jobs, samples, sessions, used_cases = set(), set(), set(), set()
    common = None
    trials: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for reference in references:
        snapshot = runtime.job_snapshot(reference)
        if (
            snapshot is None
            or snapshot["state"] != "completed"
            or snapshot["payload"].get("operation") != "skill.trial"
        ):
            raise PolicyViolation("Skill aggregate exact completed trial job required")
        job_id = snapshot["job_id"]
        if job_id in jobs:
            raise PolicyViolation("Skill aggregate aliased duplicate job")
        jobs.add(job_id)
        binding = runtime.job_binding(job_id)
        if binding is None or binding.project_id != project_id:
            raise PolicyViolation("Skill aggregate foreign project trial")
        _object(objects, snapshot["terminal_evidence_digest"], maximum=128_000)
        report = replay_canonical_job(
            home,
            binding=binding,
            operation="skill.trial",
            input_digest=snapshot["payload"]["input_digest"],
        )
        if (
            report is None
            or report["canonical_job_id"] != job_id
            or report.get("schema") != "zekam-skill-paired-trial-result/v2"
        ):
            raise PolicyViolation("Skill aggregate canonical trial completion proof invalid")
        plan = report.get("plan")
        if (
            not isinstance(plan, dict)
            or set(plan) != _PLAN_FIELDS
            or digest({k: v for k, v in plan.items() if k != "plan_digest"})
            != plan.get("plan_digest")
        ):
            raise PolicyViolation("Skill aggregate exact plan digest required")
        if (
            plan["schema"] != "zekam-skill-paired-trial-plan/v2"
            or plan["revision_digest"] != revision_digest
            or plan["package_digest"] != package_digest
            or plan["project_id"] != project_id
            or plan["plan_digest"] != binding.plan_digest
            or plan["tools"] != "deny-all"
            or plan["profile"] != "instruction-only-exact-json/v1"
            or plan["baseline"] != "without-skill-instructions"
            or plan["max_agent_calls"] != 2
            or plan["oracle_disclosed_to_builder"] is not False
            or plan["grants_authority"] is not False
            or plan["activatable"] is not False
            or type(plan["repetition"]) is not int
            or not 0 <= plan["repetition"] <= 99
            or plan["instruction_contract_digest"] != digest_of_bytes(_INSTRUCTION.encode("utf-8"))
            or plan["case_digest"] not in by_digest
            or type(plan["timeout_seconds_per_call"]) is not int
            or not 10 <= plan["timeout_seconds_per_call"] <= 600
            or type(plan["remote_disclosure_required"]) is not bool
            or plan["case_provenance"] != "caller-supplied-not-independently-attested"
        ):
            raise PolicyViolation("Skill aggregate scope/profile/case plan drift")
        validate_model_id(plan["model_id"])
        for field in (
            "source_root_digest",
            "execution_binding_digest",
            "candidate_instruction_digest",
        ):
            parse_digest(plan[field])
        case = by_digest[plan["case_digest"]]
        if case["case_id"] != plan["case_id"] or case["partition"] != plan["partition"]:
            raise PolicyViolation("Skill aggregate case identity drift")
        expected_order = (
            ["baseline", "candidate"]
            if (int(digest(case)[-1], 16) + plan["repetition"]) % 2 == 0
            else ["candidate", "baseline"]
        )
        if plan["order"] != expected_order:
            raise PolicyViolation("Skill aggregate paired execution order drift")
        identity = (plan["case_digest"], plan["repetition"])
        if identity in samples:
            raise PolicyViolation("Skill aggregate repeated sample cannot inflate trials")
        samples.add(identity)
        used_cases.add(plan["case_digest"])
        current_common = {key: plan[key] for key in _COMMON}
        if common is not None and canonical_json(current_common) != canonical_json(common):
            raise PolicyViolation("Skill aggregate mixed model/harness/budget conditions")
        common = current_common
        for key in ("revision_digest", "package_digest", "case_digest", "partition", "plan_digest"):
            if report.get(key) != plan[key]:
                raise PolicyViolation("Skill aggregate report/plan drift")
        if (
            report.get("agent_calls") != 2
            or report.get("independent_verification") is not False
            or report.get("activatable") is not False
            or report.get("grants_authority") is not False
        ):
            raise PolicyViolation("Skill aggregate trial cannot grant review authority")
        observations = report.get("observations")
        if not isinstance(observations, dict) or set(observations) != {"baseline", "candidate"}:
            raise PolicyViolation("Skill aggregate paired observations required")
        for condition in ("baseline", "candidate"):
            item = _observation(
                objects,
                index,
                observations[condition],
                condition=condition,
                plan=plan,
                case=case,
                binding=binding,
                input_digest=snapshot["payload"]["input_digest"],
            )
            if item["native_identity"] is not None:
                session = _NATIVE_ID.fullmatch(item["native_identity"]).group(1)
                if session in sessions:
                    raise PolicyViolation("Skill aggregate native session reused across samples")
                sessions.add(session)
        sample = {
            "job_id": job_id,
            "receipt_evidence_digest": snapshot["terminal_evidence_digest"],
            "plan_digest": plan["plan_digest"],
            "case_digest": plan["case_digest"],
            "repetition": plan["repetition"],
            "observations": observations,
        }
        trials.append(sample)
        groups[plan["case_digest"]].append(sample)
    if used_cases != set(by_digest):
        raise PolicyViolation("Skill aggregate unused case could mask selection drift")
    case_results = []
    for key in sorted(groups):
        pairs = groups[key]
        result = {
            "case_digest": key,
            "case_id": by_digest[key]["case_id"],
            "partition": by_digest[key]["partition"],
            "pairs": len(pairs),
        }
        for condition in ("baseline", "candidate"):
            result[condition] = {
                "exact_match_rate": sum(
                    pair["observations"][condition]["exact_match"] for pair in pairs
                )
                / len(pairs),
                "mean_elapsed_ns": sum(
                    pair["observations"][condition]["elapsed_ns"] for pair in pairs
                )
                / len(pairs),
            }
        case_results.append(result)
    holdout = [item for item in case_results if item["partition"] == "holdout"]
    failed = any(
        item["outcome"] != "success" or not item["valid_output"]
        for trial in trials
        for item in trial["observations"].values()
    )
    deltas = [
        item["candidate"]["exact_match_rate"] - item["baseline"]["exact_match_rate"]
        for item in holdout
    ]
    holdout_pairs = sum(item["pairs"] for item in holdout)
    if failed:
        comparison = "failed"
    elif holdout_pairs < 5:
        comparison = "insufficient-evidence"
    elif any(delta < 0 for delta in deltas):
        comparison = "regressed"
    elif any(delta > 0 for delta in deltas):
        comparison = "improved"
    else:
        comparison = "equal"
    partition_metrics = {}
    for partition in ("development", "holdout"):
        selected = [item for item in case_results if item["partition"] == partition]
        partition_metrics[partition] = {
            "case_count": len(selected),
            "pair_count": sum(item["pairs"] for item in selected),
            **{
                condition: {
                    "exact_match_rate": sum(
                        item[condition]["exact_match_rate"] for item in selected
                    )
                    / len(selected)
                    if selected
                    else None,
                    "mean_elapsed_ns": sum(item[condition]["mean_elapsed_ns"] for item in selected)
                    / len(selected)
                    if selected
                    else None,
                }
                for condition in ("baseline", "candidate")
            },
        }
    body = {
        "schema": "zekam-skill-trial-aggregate/v1",
        "conditions": common,
        "trials": sorted(trials, key=lambda item: (item["case_digest"], item["repetition"])),
        "case_results": case_results,
        "sample_size": len(trials),
        "holdout_case_count": len(holdout),
        "holdout_pair_count": holdout_pairs,
        "partition_metrics": partition_metrics,
        "comparison_state": comparison,
        "metric_weighting": "equal-case-within-partition",
        "uncertainty": {
            "method": "descriptive-only-no-confidence-claim",
            "unique_case_count": len(case_results),
            "unique_holdout_case_count": len(holdout),
            "sample_size": len(trials),
            "case_provenance": "caller-supplied-not-independently-attested",
        },
        "independent_verification": False,
        "activatable": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"aggregate_digest": digest(body)}
