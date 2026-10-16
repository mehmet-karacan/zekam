"""Record native instruction-tool completion as unverified invocation evidence.

The Work association is explicit recording authority, not proof that the host
executed under a Zekam lease. Native hook metadata is not an independent grader.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from zekam.application.canonical_job_runtime import replay_canonical_job, run_canonical_job
from zekam.application.composition import build_context
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.native_skill_binding import resolve_native_skill_binding
from zekam.application.opencode_lifecycle import recent_events
from zekam.application.skill_evidence import _constant, _pairs
from zekam.application.skill_trial_aggregation import _object
from zekam.application.config import load_settings
from zekam.application.unit_test_models import validate_model_id
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillInvocationState, SkillScopeKind
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

if TYPE_CHECKING:
    from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle

OPERATION = "skill.invocation.native-record"
STATES = (
    SkillInvocationState.DISCOVERED,
    SkillInvocationState.SELECTED,
    SkillInvocationState.LOADED,
    SkillInvocationState.INVOKED,
    SkillInvocationState.COMPLETED,
    SkillInvocationState.UNVERIFIED,
)
_HEX = re.compile(r"[a-f0-9]{64}\Z")


def _require_home(home: Path, lifecycle: SQLiteSkillLifecycle) -> None:
    context = build_context(home=home)
    if (
        lifecycle.operational_path.resolve(strict=True)
        != (context.home / "state" / "operational.db").resolve(strict=True)
        or context.settings.database.sqlite_path(context.home).resolve(strict=True)
        != lifecycle.operational_path.resolve(strict=True)
        or lifecycle.learning_path.resolve(strict=True)
        != (context.home / "state" / "learning.db").resolve(strict=True)
    ):
        raise PolicyViolation("Native invocation canonical home/store identity mismatch")


def _canonical_uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def build_native_invocation_plan(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    event_id: str,
    project_root: Path,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    if not _canonical_uuid(event_id):
        raise PolicyViolation("Native invocation exact event UUID required")
    # Read only: malformed unrelated ledger files are not quarantined here.
    events = [
        e
        for e in recent_events(home, limit=5000, quarantine_invalid=False)
        if e["event_id"] == event_id
    ]
    if len(events) != 1:
        raise PolicyViolation("Native invocation exact retained event required")
    event = events[0]
    label = event.get("task_label")
    if (
        event["event_type"] != "tool.execute.after"
        or event.get("tool") != "skill"
        or event.get("status") != "completed"
        or event.get("error_category") is not None
        or type(label) is not str
        or not label.startswith(("native-skill-load/v1:", "native-skill-load/v2:"))
    ):
        raise PolicyViolation("Native invocation requires completed bound skill load")
    version = 2 if label.startswith("native-skill-load/v2:") else 1
    prefix = f"native-skill-load/v{version}:"
    keys = {"j", "r", "p", "b", "c", "m"} | ({"u"} if version == 2 else set())
    try:
        observed = json.loads(
            label.removeprefix(prefix),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise PolicyViolation("Native invocation strict load label required") from exc
    if (
        not isinstance(observed, dict)
        or set(observed) != keys
        or any(
            type(observed[k]) is not str or _HEX.fullmatch(observed[k]) is None
            for k in keys - {"j"}
        )
        or not _canonical_uuid(observed["j"])
    ):
        raise PolicyViolation("Native invocation exact bounded load identities required")
    model_ref = None
    if version == 2:
        model_ref = validate_model_id(event.get("model_ref"))
        if (
            len(model_ref) > 160
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]*/[A-Za-z0-9][A-Za-z0-9_./:-]*(?:#[A-Za-z0-9][A-Za-z0-9_.+\-]*)?",
                model_ref,
            )
            is None
            or digest_of_bytes(model_ref.encode("utf-8")) != "sha256:" + observed["u"]
        ):
            raise PolicyViolation("Native invocation exact step model binding drift")
    resource = event.get("resource")
    if type(resource) is not str or len(resource.split("/")) != 4:
        raise PolicyViolation("Native invocation managed resource required")
    resolved = resolve_native_skill_binding(
        lifecycle,
        home=home,
        project_root=project_root,
        policy=load_settings(home=home).cli.integrations,
        allowed_scopes=allowed_scopes,
        observation={
            "schema": "zekam-native-skill-load-query/v1",
            "skill_id": resource.split("/")[2],
            "relative_path": resource,
            "instruction_digest": "sha256:" + observed["b"],
        },
    )
    if (
        resolved["export_job_id"] != observed["j"]
        or resolved["revision_digest"] != "sha256:" + observed["r"]
        or resolved["package_digest"] != "sha256:" + observed["p"]
    ):
        raise PolicyViolation("Native invocation current export/revision source drift")
    with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
        project = uow.resolve_project(allowed_scopes[0][1])
    session_digest = digest_of_bytes(event["session_id"].encode("utf-8"))
    harness = {
        "schema": "zekam-native-instruction-tool-harness/v1",
        "client_id": "opencode",
        "native_session_digest": session_digest,
        "native_call_digest": "sha256:" + observed["c"],
        "native_message_digest": "sha256:" + observed["m"],
        "revision_digest": resolved["revision_digest"],
        "package_digest": resolved["package_digest"],
        "instruction_digest": resolved["instruction_digest"],
    }
    body = {
        "schema": "zekam-native-skill-invocation-plan/v1",
        "operation": OPERATION,
        "project_id": project.id,
        "native_event_id": event_id,
        "native_event_digest": event["event_digest"],
        "native_observed_at": event["occurred_at"],
        "managed_binding": resolved,
        "harness": harness,
        "harness_digest": digest(harness),
        "agent_ref": "native-call:"
        + digest({"session": session_digest, "call": "sha256:" + observed["c"]})[7:],
        "states": [str(s) for s in STATES],
        "observation_kind": "native-instruction-tool-completion",
        "host_attested": False,
        "invoking_model_ref": model_ref,
        "invoking_model_provenance": "native-step-message" if model_ref else "unavailable",
        "work_association": "explicit-recording",
        "skill_effect_verified": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}


def _read_rows(
    lifecycle: SQLiteSkillLifecycle, result: dict[str, Any], binding: LocalJobBinding
) -> None:
    plan = result["plan"]
    previous = None
    rows = result.get("invocation_digests")
    if type(rows) is not list or len(rows) != len(STATES):
        raise PolicyViolation("Native invocation complete observation chain required")
    for state, value in zip(STATES, rows, strict=True):
        expected = {
            "schema": "zekam-skill-invocation/v2",
            "revision_digest": plan["managed_binding"]["revision_digest"],
            "package_digest": plan["managed_binding"]["package_digest"],
            "work_ref": binding.work_item_id,
            "run_ref": binding.run_id,
            "agent_ref": plan["agent_ref"],
            "client_id": "opencode",
            "harness_digest": plan["harness_digest"],
            "state": str(state),
            "previous_invocation_digest": previous,
            "observed_at": dt.datetime.fromisoformat(
                plan["native_observed_at"].replace("Z", "+00:00")
            )
            .astimezone(dt.UTC)
            .replace(microsecond=0)
            .isoformat(),
            "grants_authority": False,
        }
        if digest(expected) != value:
            raise PolicyViolation("Native invocation result chain drift")
        with closing(lifecycle._connect(read_only=True)) as db:
            row = db.execute(
                "select body_json from skill_invocation_v2 where invocation_digest=?", (value,)
            ).fetchone()
        if row is None or canonical_json(json.loads(row[0])) != canonical_json(expected):
            raise PolicyViolation("Native invocation recorded row missing or drifted")
        previous = value
    if (
        result.get("state") != "unverified"
        or result.get("skill_effect_verified") is not False
        or result.get("host_attested") is not False
        or result.get("grants_authority") is not False
        or result.get("provider_calls") != 0
    ):
        raise PolicyViolation("Native load observation cannot verify skill effects")


def run_native_invocation(
    home: Path,
    *,
    lifecycle: SQLiteSkillLifecycle,
    plan: dict[str, Any],
    binding: LocalJobBinding,
    project_root: Path,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
) -> dict[str, Any]:
    _require_home(home, lifecycle)
    if (
        type(binding) is not LocalJobBinding
        or binding.project_id != plan["project_id"]
        or binding.plan_digest != plan["plan_digest"]
    ):
        raise PolicyViolation("Native invocation exact Work/plan binding required")

    def validate():
        current = build_native_invocation_plan(
            home,
            lifecycle=lifecycle,
            event_id=plan["native_event_id"],
            project_root=project_root,
            allowed_scopes=allowed_scopes,
        )
        if canonical_json(current) != canonical_json(plan):
            raise PolicyViolation("Native invocation source/plan drift")

    def execute(cancel):
        validate()
        if cancel.is_set():
            raise PolicyViolation("Native invocation recording cancelled")
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
        raise PolicyViolation("Native invocation recording replay plan drift")
    _read_rows(lifecycle, result, binding)
    return result


def read_native_invocation(
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
        raise PolicyViolation("Native invocation completed recording job required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None or binding.project_id != project_id:
        raise PolicyViolation("Native invocation recording project scope mismatch")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"], maximum=64_000)
    result = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=snapshot["payload"]["input_digest"]
    )
    if (
        result is None
        or result.get("schema") != "zekam-native-skill-invocation-result/v1"
        or result["canonical_job_id"] != snapshot["job_id"]
        or snapshot["payload"]["input_digest"] != binding.plan_digest
        or result["plan"]["plan_digest"] != binding.plan_digest
        or digest({k: v for k, v in result["plan"].items() if k != "plan_digest"})
        != binding.plan_digest
    ):
        raise PolicyViolation("Native invocation recorded plan/receipt drift")
    candidate = lifecycle.evaluation_revision(
        result["plan"]["managed_binding"]["revision_digest"], allowed_scopes=allowed_scopes
    )
    if candidate["package_digest"] != result["plan"]["managed_binding"]["package_digest"]:
        raise PolicyViolation("Native invocation recorded candidate scope drift")
    _read_rows(lifecycle, result, binding)
    return result | {"receipt_evidence_digest": snapshot["terminal_evidence_digest"]}
