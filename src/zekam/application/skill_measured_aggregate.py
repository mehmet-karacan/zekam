"""Persist the real local evaluator result behind its own canonical receipt."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zekam.application.canonical_job_runtime import replay_canonical_job, run_canonical_job
from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.skill_trial_aggregation import _object, aggregate_skill_trials
from zekam.application.skill_trials import _validate_case
from zekam.domain.canonical import canonical_bytes, canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "skill.evaluation.aggregate"
MAX_RESULT_BYTES = 4 * 1024 * 1024


def build_measured_aggregate_plan(
    home: Path,
    *,
    references: tuple[str, ...],
    cases: tuple[dict[str, Any], ...],
    project_id: str,
    revision_digest: str,
    package_digest: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    aggregate = aggregate_skill_trials(
        home,
        references=references,
        cases=cases,
        project_id=project_id,
        revision_digest=revision_digest,
        package_digest=package_digest,
    )
    body = {
        "schema": "zekam-skill-measured-aggregate-plan/v1",
        "project_id": project_id,
        "revision_digest": revision_digest,
        "package_digest": package_digest,
        "aggregate_digest": aggregate["aggregate_digest"],
        "trial_sources": [
            {"job_id": item["job_id"], "receipt_evidence_digest": item["receipt_evidence_digest"]}
            for item in aggregate["trials"]
        ],
        "case_manifest": sorted(digest(case) for case in cases),
        "evaluator_profile": "instruction-only-exact-json/v1",
        "max_source_jobs": 100,
        "max_case_bytes": 24_576,
        "max_result_bytes": MAX_RESULT_BYTES,
        "provider_calls": 0,
        "activatable": False,
        "grants_authority": False,
    }
    plan = body | {"plan_digest": digest(body)}
    result = {
        "schema": "zekam-skill-measured-aggregate-result/v1",
        "plan": plan,
        "aggregate": aggregate,
        "cases": sorted(cases, key=lambda item: item["case_id"]),
        "case_provenance": "caller-supplied-not-independently-attested",
        "provider_calls": 0,
        "independence_proven": False,
        "quality_evaluation_complete": False,
        "activatable": False,
        "grants_authority": False,
    }
    if len(canonical_bytes(result)) > MAX_RESULT_BYTES:
        raise PolicyViolation("Skill measured aggregate result exceeds bounded storage budget")
    return plan, result


def run_measured_aggregate(
    home: Path,
    *,
    binding: LocalJobBinding,
    plan: dict[str, Any],
    cases: tuple[dict[str, Any], ...],
    validate_inputs: Callable[[], None],
) -> dict[str, Any]:
    plan = json.loads(canonical_json(plan))
    cases = tuple(json.loads(canonical_json(case)) for case in cases)
    if binding.project_id != plan["project_id"] or binding.plan_digest != plan["plan_digest"]:
        raise PolicyViolation("Skill measured aggregate exact Work/plan binding required")

    def prepare():
        current_plan, current_result = build_measured_aggregate_plan(
            home,
            references=tuple(item["job_id"] for item in plan["trial_sources"]),
            cases=cases,
            project_id=binding.project_id,
            revision_digest=plan["revision_digest"],
            package_digest=plan["package_digest"],
        )
        if canonical_json(current_plan) != canonical_json(plan):
            raise PolicyViolation("Skill measured aggregate current source/plan drift")
        return current_result

    prepare()

    def execute(cancel):
        if cancel.is_set():
            raise PolicyViolation("Skill measured aggregate cancelled before evaluation")
        return prepare()

    return run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=digest({"plan": plan["plan_digest"]}),
        execute=execute,
        validate_inputs=validate_inputs,
    )


def read_measured_aggregate(home: Path, reference: str, *, project_id: str) -> dict[str, Any]:
    """Verify completion and regrade all source evidence using retained exact cases."""
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Skill measured aggregate exact completed evaluator job required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None or binding.project_id != project_id:
        raise PolicyViolation("Skill measured aggregate foreign project binding")
    _object(objects, snapshot["terminal_evidence_digest"], maximum=MAX_RESULT_BYTES)
    result = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=snapshot["payload"]["input_digest"]
    )
    if (
        result is None
        or result["canonical_job_id"] != snapshot["job_id"]
        or result.get("schema") != "zekam-skill-measured-aggregate-result/v1"
    ):
        raise PolicyViolation("Skill measured aggregate terminal completion proof invalid")
    plan = result["plan"]
    if (
        plan["project_id"] != project_id
        or plan["plan_digest"] != binding.plan_digest
        or snapshot["payload"]["input_digest"] != digest({"plan": plan["plan_digest"]})
    ):
        raise PolicyViolation("Skill measured aggregate claimed plan binding drift")
    cases = result["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise PolicyViolation("Skill measured aggregate retained case list invalid")
    for case in cases:
        _validate_case(case)
    rebuilt_plan, rebuilt_result = build_measured_aggregate_plan(
        home,
        references=tuple(item["job_id"] for item in plan["trial_sources"]),
        cases=tuple(cases),
        project_id=project_id,
        revision_digest=plan["revision_digest"],
        package_digest=plan["package_digest"],
    )
    stored = {
        key: value
        for key, value in result.items()
        if key not in {"canonical_job_id", "canonical_replayed"}
    }
    if canonical_json(rebuilt_plan) != canonical_json(plan) or canonical_json(
        rebuilt_result
    ) != canonical_json(stored):
        raise PolicyViolation(
            "Skill measured aggregate evaluator result differs from actual evidence"
        )
    return result | {"receipt_evidence_digest": snapshot["terminal_evidence_digest"]}
