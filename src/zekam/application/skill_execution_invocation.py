"""Project actual receipt-bound native tool execution into invocation state."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from zekam.application.canonical_job_runtime import run_canonical_job
from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.skill_native_execution import read_native_execution
from zekam.application.skill_native_invocation import STATES, _read_rows, _require_home
from zekam.application.skill_trial_aggregation import _object
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillScopeKind
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "skill.invocation.from-execution"
SCHEMA = "zekam-execution-skill-invocation-plan/v1"


def execution_recording_context(plan: dict, binding: LocalJobBinding) -> dict:
    """Recording may use another run, never another execution owner/config/source."""
    origin = plan["execution_work_binding"]
    if (
        type(binding) is not LocalJobBinding
        or set(origin) != set(binding.body())
        or origin["schema"] != "zekam-local-job-binding/v1"
        or origin["grants_authority"] is not False
        or digest(origin) != plan["execution_work_binding_digest"]
        or any(
            origin[key] != binding.body()[key]
            for key in (
                "project_id",
                "work_item_id",
                "config_revision_id",
                "config_digest",
                "source_snapshot_id",
            )
        )
        or type(origin["work_revision"]) is not int
        or origin["work_revision"] > binding.work_revision
    ):
        raise PolicyViolation(
            "Invocation recording cannot reassociate native execution Work/config/source"
        )
    return origin


def build_execution_invocation_plan(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    execution_ref: str,
    project_id: str,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
    require_active: bool = True,
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    execution = read_native_execution(
        home,
        execution_ref,
        lifecycle=lifecycle,
        project_id=project_id,
        allowed_scopes=allowed_scopes,
    )
    observation = execution["observation"]
    if observation["outcome"] != "success":
        raise PolicyViolation(
            "Invocation projection requires an observed completed native skill call"
        )
    runtime = SQLiteLocalRuntimeStore(lifecycle.operational_path, existing_only=True)
    snapshot = runtime.job_snapshot(execution["canonical_job_id"])
    origin = runtime.job_binding(execution["canonical_job_id"])
    if (
        snapshot is None
        or origin is None
        or snapshot["state"] != "completed"
        or origin.project_id != project_id
        or len(snapshot["effects"]) != 1
        or snapshot["terminal_evidence_digest"] != execution["receipt_evidence_digest"]
    ):
        raise PolicyViolation("Invocation projection native source receipt drift")
    source_plan = execution["plan"]
    active = source_plan["active_revision"]
    if (
        require_active
        and lifecycle.require_active_package(
            source_plan["package_digest"], allowed_scopes=allowed_scopes
        )
        != active
    ):
        raise PolicyViolation("Invocation projection native active pointer drift")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    artifact = _object(objects, observation["artifact_digest"], maximum=64_000, public_safe=True)
    native = artifact["native_skill_invocation"]
    observed_at = snapshot["effects"][0]["receipt_created_at"]
    timestamp = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise PolicyViolation("Invocation projection terminal receipt timestamp required")
    harness = {
        "schema": "zekam-executed-native-skill-harness/v1",
        "execution_binding_digest": source_plan["execution_binding_digest"],
        "native_call": native,
        "execution_job_id": execution["canonical_job_id"],
        "execution_receipt_digest": execution["receipt_evidence_digest"],
    }
    body = {
        "schema": SCHEMA,
        "operation": OPERATION,
        "project_id": project_id,
        "execution_job_id": execution["canonical_job_id"],
        "execution_receipt_digest": execution["receipt_evidence_digest"],
        "execution_artifact_digest": observation["artifact_digest"],
        "execution_work_binding": origin.body(),
        "execution_work_binding_digest": origin.binding_digest,
        "managed_binding": active | {"package_digest": source_plan["package_digest"]},
        "harness": harness,
        "harness_digest": digest(harness),
        "agent_ref": "native-call:"
        + digest(
            {"session": native["native_session_digest"], "call": native["native_call_digest"]}
        )[7:],
        "native_observed_at": observed_at,
        "observation_time_source": "execution-terminal-receipt",
        "observation_kind": "canonical-native-instruction-tool-completion",
        "invoking_model_ref": source_plan["model_id"],
        "invoking_model_provenance": "canonical-dispatch-model-request",
        "work_association": "canonical-execution",
        "states": [str(state) for state in STATES],
        "host_attested": False,
        "skill_effect_verified": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}


def run_execution_invocation(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    plan: dict[str, Any],
    binding: LocalJobBinding,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    if type(binding) is not LocalJobBinding or binding.plan_digest != plan["plan_digest"]:
        raise PolicyViolation("Invocation projection exact recording Work plan required")
    execution_recording_context(plan, binding)

    def validate():
        current = build_execution_invocation_plan(
            home,
            lifecycle=lifecycle,
            execution_ref=plan["execution_job_id"],
            project_id=binding.project_id,
            allowed_scopes=allowed_scopes,
        )
        if canonical_json(current) != canonical_json(plan):
            raise PolicyViolation("Invocation projection native source/plan drift")

    def execute(cancel):
        validate()
        if cancel.is_set():
            raise PolicyViolation("Invocation projection cancelled before ledger write")
        return lifecycle.record_native_invocation(
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
    if canonical_json(result["plan"]) != canonical_json(plan):
        raise PolicyViolation("Invocation projection completed replay plan drift")
    _read_rows(lifecycle, result, binding)
    return result
