"""Project real evaluator/audit evidence into the existing lifecycle ledger.

Current instruction-only evidence cannot promote a skill. Original operations
and terminal jobs remain visible; no legacy evaluator receipt is fabricated.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zekam.application.canonical_job_runtime import replay_canonical_job, run_canonical_job
from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.skill_audits import read_skill_audit
from zekam.application.skill_measured_aggregate import read_measured_aggregate
from zekam.application.skill_trial_aggregation import _object
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillEvaluationV2, SkillScopeKind
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

if TYPE_CHECKING:
    from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle

OPERATION = "skill.evaluation.native-project"
REVIEW_REASON = "Incomplete native quality, independence, preference and provenance evidence; activation is not approved."


def _require_home(home: Path, lifecycle: SQLiteSkillLifecycle) -> None:
    if lifecycle.operational_path.resolve(strict=True) != (
        home / "state" / "operational.db"
    ).resolve(strict=True) or lifecycle.learning_path.resolve(strict=True) != (
        home / "state" / "learning.db"
    ).resolve(strict=True):
        raise PolicyViolation("Skill native projection home/store identity mismatch")


def build_native_projection_plan(
    home: Path,
    *,
    project_id: str,
    evaluator_ref: str,
    verifier_ref: str,
    reviewer_ref: str,
) -> tuple[dict[str, Any], SkillEvaluationV2, dict[str, Any]]:
    measured = read_measured_aggregate(home, evaluator_ref, project_id=project_id)
    verification = read_skill_audit(home, verifier_ref, project_id=project_id)
    review = read_skill_audit(home, reviewer_ref, project_id=project_id)
    aggregate = measured["aggregate"]
    source_plan = measured["plan"]
    if len({item["canonical_job_id"] for item in (measured, verification, review)}) != 3:
        raise PolicyViolation("Skill native projection requires three distinct source jobs")
    for item, role in ((verification, "verifier"), (review, "reviewer")):
        plan = item["plan"]
        if (
            plan["role"] != role
            or plan["aggregate_digest"] != aggregate["aggregate_digest"]
            or plan["revision_digest"] != source_plan["revision_digest"]
            or plan["package_digest"] != source_plan["package_digest"]
        ):
            raise PolicyViolation("Skill native projection source role/subject drift")
    if (
        review["plan"]["prior_verification_evidence_digest"]
        != verification["receipt_evidence_digest"]
    ):
        raise PolicyViolation("Skill native projection reviewer is not bound to this verifier")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    review_context = _object(
        objects, review["plan"]["context_digest"], maximum=180_000, public_safe=True
    )
    prior = review_context["prior_verification"]
    if (
        prior is None
        or prior["job_id"] != verification["canonical_job_id"]
        or prior["receipt_evidence_digest"] != verification["receipt_evidence_digest"]
    ):
        raise PolicyViolation("Skill native projection prior verifier job drift")
    metrics = {
        f"holdout-{item['case_digest'][7:]}": {
            "baseline": item["baseline"]["exact_match_rate"],
            "candidate": item["candidate"]["exact_match_rate"],
            "direction": "maximize",
        }
        for item in aggregate["case_results"]
        if item["partition"] == "holdout"
    }
    observed = aggregate["comparison_state"]
    if observed == "failed" or not verification["decision_valid"] or not review["decision_valid"]:
        state = "failed"
    elif observed == "regressed":
        state = "regressed"
    elif not verification["audit_integrity_accepted"] or not review["audit_integrity_accepted"]:
        state = "blocked"
    elif observed == "equal":
        state = "equal"
    else:
        # Improved bounded measurements still lack the full promotion evidence.
        state = "insufficient-evidence"
    uncertainty = {
        "method": "descriptive-only-no-confidence-claim",
        "sample_size": aggregate["holdout_pair_count"],
        "all_pair_count": aggregate["sample_size"],
        "unique_holdout_case_count": aggregate["holdout_case_count"],
        "observed_comparison_state": observed,
        "source_adapter": "canonical-native-audit/v1",
        "aggregate_digest": aggregate["aggregate_digest"],
        "reviewer_job_id": review["canonical_job_id"],
        "reviewer_evidence_digest": review["receipt_evidence_digest"],
        "independence_proven": False,
        "preference_evidence_complete": False,
        "provenance_complete": False,
        "quality_evaluation_complete": False,
    }
    body = {
        "schema": "zekam-skill-native-projection-plan/v1",
        "project_id": project_id,
        "revision_digest": source_plan["revision_digest"],
        "package_digest": source_plan["package_digest"],
        "evaluator_job_id": measured["canonical_job_id"],
        "evaluator_evidence_digest": measured["receipt_evidence_digest"],
        "verifier_job_id": verification["canonical_job_id"],
        "verifier_evidence_digest": verification["receipt_evidence_digest"],
        "verifier_ref": verification["plan"]["model_id"],
        "reviewer_job_id": review["canonical_job_id"],
        "reviewer_evidence_digest": review["receipt_evidence_digest"],
        "reviewer_ref": review["plan"]["model_id"],
        "state": state,
        "metrics": metrics,
        "uncertainty": uncertainty,
        "review_approved": False,
        "review_reason": REVIEW_REASON,
        "activatable": False,
        "grants_authority": False,
    }
    plan = body | {"plan_digest": digest(body)}
    evaluation = SkillEvaluationV2(
        revision_digest=plan["revision_digest"],
        plan_digest=plan["plan_digest"],
        state=state,
        trials=aggregate["holdout_pair_count"],
        evaluator_ref="zekam:instruction-only-exact-json/v1",
        verifier_ref=plan["verifier_ref"],
        evaluator_evidence_digest=plan["evaluator_evidence_digest"],
        verifier_evidence_digest=plan["verifier_evidence_digest"],
        evaluator_execution_identity=plan["evaluator_job_id"],
        verifier_execution_identity=plan["verifier_job_id"],
        metrics=metrics,
        uncertainty=uncertainty,
    )
    review_input = {
        "schema": "zekam-personal-skill-review-input/v2",
        "revision_digest": plan["revision_digest"],
        "reviewer_ref": plan["reviewer_ref"],
        "reviewer_evidence_digest": plan["reviewer_evidence_digest"],
        "reviewer_execution_identity": plan["reviewer_job_id"],
        "approved": False,
        "reason": REVIEW_REASON,
        "grants_authority": False,
    }
    return plan, evaluation, review_input


def _read_rows(
    lifecycle: SQLiteSkillLifecycle,
    plan: dict,
    result: dict,
    evaluation: SkillEvaluationV2,
    review: dict,
) -> None:
    if (
        lifecycle.recorded_evidence_digest(evaluation.body(), kind="evaluation")
        != result["evaluation_digest"]
    ):
        raise PolicyViolation("Skill native projection evaluation row drift")
    if (
        lifecycle.recorded_evidence_digest(
            review | {"evaluation_digest": result["evaluation_digest"]}, kind="review"
        )
        != result["review_digest"]
    ):
        raise PolicyViolation("Skill native projection review row drift")
    if (
        canonical_json(result["plan"]) != canonical_json(plan)
        or result["state"] != evaluation.state
        or result["approved"] is not False
        or result["activatable"] is not False
        or result["grants_authority"] is not False
    ):
        raise PolicyViolation("Skill native projection result cannot grant authority")


def run_native_projection(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    plan: dict[str, Any],
    binding: LocalJobBinding,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
    validate_inputs: Callable[[], None],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    if (
        type(binding) is not LocalJobBinding
        or binding.project_id != plan["project_id"]
        or binding.plan_digest != plan["plan_digest"]
    ):
        raise PolicyViolation("Skill native projection Work/plan binding drift")
    current, evaluation, review = build_native_projection_plan(
        home,
        project_id=binding.project_id,
        evaluator_ref=plan["evaluator_job_id"],
        verifier_ref=plan["verifier_job_id"],
        reviewer_ref=plan["reviewer_job_id"],
    )
    if canonical_json(current) != canonical_json(plan):
        raise PolicyViolation("Skill native projection current source/plan drift")

    def execute(cancel):
        if cancel.is_set():
            raise PolicyViolation("Skill native projection cancelled before ledger write")
        return lifecycle.record_native_projection(
            home=home,
            plan=plan,
            binding=binding,
            allowed_scopes=allowed_scopes,
            now=dt.datetime.now(dt.UTC),
        )

    result = run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=plan["plan_digest"],
        execute=execute,
        validate_inputs=validate_inputs,
    )
    _read_rows(lifecycle, current, result, evaluation, review)
    return result


def read_native_projection(
    home: Path,
    reference: str,
    *,
    lifecycle: SQLiteSkillLifecycle,
    project_id: str,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Skill native projection exact terminal job required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None or binding.project_id != project_id:
        raise PolicyViolation("Skill native projection foreign project binding")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"], maximum=64_000)
    result = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=snapshot["payload"]["input_digest"]
    )
    if (
        result is None
        or result["canonical_job_id"] != snapshot["job_id"]
        or result.get("schema") != "zekam-skill-native-projection-result/v1"
    ):
        raise PolicyViolation("Skill native projection canonical completion proof invalid")
    plan = result["plan"]
    if (
        plan["plan_digest"] != binding.plan_digest
        or snapshot["payload"]["input_digest"] != plan["plan_digest"]
    ):
        raise PolicyViolation("Skill native projection claimed plan drift")
    current, evaluation, review = build_native_projection_plan(
        home,
        project_id=project_id,
        evaluator_ref=plan["evaluator_job_id"],
        verifier_ref=plan["verifier_job_id"],
        reviewer_ref=plan["reviewer_job_id"],
    )
    lifecycle.evaluation_revision(plan["revision_digest"], allowed_scopes=allowed_scopes)
    _read_rows(lifecycle, current, result, evaluation, review)
    return result | {"receipt_evidence_digest": snapshot["terminal_evidence_digest"]}
