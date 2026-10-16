"""Record one bounded native artifact outcome from actual verifier receipts."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import closing
from pathlib import Path
from typing import Any

from zekam.application.canonical_job_runtime import replay_canonical_job, run_canonical_job
from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.skill_native_invocation import _require_home
from zekam.application.skill_outcome_verification import (
    _verification_owner,
    read_outcome_verification,
)
from zekam.application.skill_trial_aggregation import _object
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillScopeKind
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "skill.outcome.native-record"


def build_outcome_record_plan(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    verifier_ref: str,
    project_id: str,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    verified = read_outcome_verification(
        home,
        verifier_ref,
        lifecycle=lifecycle,
        project_id=project_id,
        allowed_scopes=allowed_scopes,
    )
    runtime = SQLiteLocalRuntimeStore(lifecycle.operational_path, existing_only=True)
    snapshot = runtime.job_snapshot(verified["canonical_job_id"])
    source_binding = runtime.job_binding(verified["canonical_job_id"])
    if (
        snapshot is None
        or source_binding is None
        or source_binding.project_id != project_id
        or len(snapshot["effects"]) != 1
        or snapshot["terminal_evidence_digest"] != verified["receipt_evidence_digest"]
    ):
        raise PolicyViolation("Native outcome verifier receipt/source binding drift")
    status = verified["status"]
    recordable = status in {"verified-success", "verified-failure"}
    outcome = None
    if recordable:
        timestamp = dt.datetime.fromisoformat(
            snapshot["effects"][0]["receipt_created_at"].replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            raise PolicyViolation("Native outcome verifier terminal timestamp required")
        outcome = {
            "schema": "zekam-skill-outcome/v2",
            "invocation_digest": verified["plan"]["subject"]["invocation_digest"],
            "status": status,
            "grader_kind": "artifact",
            "verifier_ref": verified["observation"]["native_identity"],
            "evidence_digest": verified["receipt_evidence_digest"],
            "observed_at": timestamp.astimezone(dt.UTC).replace(microsecond=0).isoformat(),
            "grants_authority": False,
        }
    body = {
        "schema": "zekam-native-skill-outcome-record-plan/v1",
        "operation": OPERATION,
        "project_id": project_id,
        "verifier_job_id": verified["canonical_job_id"],
        "verifier_receipt_digest": verified["receipt_evidence_digest"],
        "verifier_work_binding": source_binding.body(),
        "verifier_work_binding_digest": source_binding.binding_digest,
        "execution_work_binding": verified["plan"]["execution_work_binding"],
        "execution_work_binding_digest": verified["plan"]["execution_work_binding_digest"],
        "subject": verified["plan"]["subject"],
        "subject_digest": verified["plan"]["subject_digest"],
        "outcome_body": outcome,
        "outcome_digest": digest(outcome) if outcome is not None else None,
        "status": status,
        "recordable": recordable,
        "blockers": [] if recordable else ["verifier-terminal-status-unverified"],
        "verification_scope": "one-native-task-artifact",
        "quality_evaluation_complete": False,
        "activatable": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}


def _read_row(lifecycle, result, plan):
    body = plan["outcome_body"]
    if body is None or digest(body) != plan["outcome_digest"]:
        raise PolicyViolation("Native outcome recordable source required")
    with closing(lifecycle._connect(read_only=True)) as db:
        row = db.execute(
            "select outcome_digest,body_json from skill_outcome_v2 where invocation_digest=?",
            (body["invocation_digest"],),
        ).fetchone()
    if (
        row is None
        or row[0] != plan["outcome_digest"]
        or canonical_json(json.loads(row[1])) != canonical_json(body)
        or result["outcome_digest"] != plan["outcome_digest"]
        or canonical_json(result["plan"]) != canonical_json(plan)
        or result["status"] != plan["status"]
        or result["quality_evaluation_complete"] is not False
        or result["activatable"] is not False
        or result["grants_authority"] is not False
    ):
        raise PolicyViolation("Native outcome immutable row/result/source drift")


def run_outcome_recording(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    plan: dict[str, Any],
    binding: LocalJobBinding,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    plan = json.loads(canonical_json(plan))
    _verification_owner(plan, binding)
    if binding.plan_digest != plan["plan_digest"] or plan["recordable"] is not True:
        raise PolicyViolation(
            "Native outcome exact recordable Work/plan required; unverified evidence remains in verifier journal"
        )

    def validate():
        current = build_outcome_record_plan(
            home,
            lifecycle=lifecycle,
            verifier_ref=plan["verifier_job_id"],
            project_id=binding.project_id,
            allowed_scopes=allowed_scopes,
        )
        if canonical_json(current) != canonical_json(plan):
            raise PolicyViolation("Native outcome current verifier source/plan drift")

    validate()

    def execute(cancel):
        if cancel.is_set():
            raise PolicyViolation("Native outcome recording cancelled before ledger write")
        return lifecycle.record_native_outcome(
            home=home, plan=plan, binding=binding, allowed_scopes=allowed_scopes
        )

    result = run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=plan["plan_digest"],
        execute=execute,
        validate_inputs=validate,
    )
    _read_row(lifecycle, result, plan)
    return result


def read_native_outcome(
    home: Path,
    reference: str,
    *,
    lifecycle: SQLiteSkillLifecycle,
    project_id: str,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    runtime = SQLiteLocalRuntimeStore(lifecycle.operational_path, existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Native outcome completed recording receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None or binding.project_id != project_id:
        raise PolicyViolation("Native outcome foreign project scope")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"], maximum=64_000)
    result = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=snapshot["payload"]["input_digest"]
    )
    if (
        result is None
        or result.get("schema") != "zekam-native-skill-outcome-record-result/v1"
        or result["canonical_job_id"] != snapshot["job_id"]
        or result["plan"]["plan_digest"] != binding.plan_digest
        or snapshot["payload"]["input_digest"] != binding.plan_digest
    ):
        raise PolicyViolation("Native outcome completed recording plan/receipt drift")
    _verification_owner(result["plan"], binding)
    plan = build_outcome_record_plan(
        home,
        lifecycle=lifecycle,
        verifier_ref=result["plan"]["verifier_job_id"],
        project_id=project_id,
        allowed_scopes=allowed_scopes,
    )
    _read_row(lifecycle, result, plan)
    return result | {"receipt_evidence_digest": snapshot["terminal_evidence_digest"]}
