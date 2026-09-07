"""Read-only autonomous-evolution planning and truthful readiness status."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.composition import ApplicationContext
from zekam.application.opencode_spool import inspect_spool
from zekam.application.package_acceptance import build_package_manifest
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import EVOLUTION_HANDLERS
from zekam.infrastructure.sqlite.evolution_authority import evolution_authority_schema_digest
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.windows_task_scheduler import WindowsTaskPlan, inspect_windows_task

EVOLUTION_TASK_ID: Final = "ZEKAM-AUTONOMOUS-EVOLUTION-001"
PREVIOUS_TASK_ID: Final = "ZEKAM-LOCAL-INTELLIGENCE-PLANE-001"
PREVIOUS_AUTHORITY_DIGEST: Final = (
    "sha256:ebd9ca00a5cc500a650984e3cdc7be22186b85b5a283d86a6f9d863237530629"
)
PREVIOUS_GIT_BLOB: Final = "ce2980e819df68ccf2ac375c1f550b6d675ebeaa"
TRANSITION_KEY: Final = "scope-transition:ebd9ca00:4788116a"
_ORIGINAL_TASK_AUTHORITY_DIGEST: Final = (
    "sha256:4788116aa01885aa8b884579f4f8ee1ce87b76ee54f55c8fe884aec7e33580b2"
)
_ORIGINAL_BASELINE_HEAD: Final = "8761790e6034c5d2958476b9eb45af36da1b094a"
_AUTHOR_REWRITE_TASK_AUTHORITY_DIGEST: Final = (
    "sha256:9408eff417b42801580c94988c3ee3b6eef5bc02a2a54e4d979bfe8310854aba"
)
_AUTHOR_REWRITE_BASELINE_HEAD: Final = "b59221a0891dc94d3702d042132254066dc089ed"
_TASK_EVENT_LIMIT: Final = 16
_TASK_EVENT_OUTPUT_LIMIT: Final = 128 * 1024


def validated_package_manifest(context: ApplicationContext) -> Path:
    """Return the release manifest only when it matches the current shipped sources."""

    manifest = context.core_path / "src" / "zekam" / "PACKAGE_RELEASE_MANIFEST.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise ValidationFailed("Evolution package manifest missing")
    try:
        stored = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Evolution package manifest unreadable") from exc
    expected = build_package_manifest(context.core_path / "src" / "zekam").body()
    if stored != expected:
        raise ValidationFailed("Evolution package manifest current source ile drifted")
    return manifest


def _git(core: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=core,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    if result.returncode:
        raise ValidationFailed("Evolution source fingerprint Git okumasi basarisiz")
    return result.stdout.rstrip()


def _transition_journal(home: Path) -> tuple[str, str, dict[str, Any]] | None:
    path = home / "runtime" / "local-effects" / "scope-transitions" / f"{EVOLUTION_TASK_ID}.jsonl"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or "\t" not in lines[0]:
        raise ValidationFailed("Scope transition journal exact tek kayit olmali")
    key, raw = lines[0].split("\t", maxsplit=1)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("Scope transition journal JSON gecersiz") from exc
    if not isinstance(document, dict) or not key.startswith("job:") or ":effect:sha256:" not in key:
        raise ValidationFailed("Scope transition journal identity drift")
    return key, raw, document


def _transition_receipt_identity(active: ActiveTaskContract) -> tuple[str, str]:
    """Bridge the exact author-only baseline rewrite to its immutable receipt."""

    current = (active.source_digest, active.baseline_head)
    rewritten = (
        _AUTHOR_REWRITE_TASK_AUTHORITY_DIGEST,
        _AUTHOR_REWRITE_BASELINE_HEAD,
    )
    if current == rewritten:
        return (_ORIGINAL_TASK_AUTHORITY_DIGEST, _ORIGINAL_BASELINE_HEAD)
    return current


def _transition_binding_ok(
    *,
    snapshot: dict[str, Any] | None,
    journal: tuple[str, str, dict[str, Any]] | None,
    active: ActiveTaskContract,
) -> bool:
    if snapshot is None or journal is None:
        return False
    key, raw, document = journal
    effects = snapshot.get("effects")
    payload = snapshot.get("payload")
    if not isinstance(effects, list) or len(effects) != 1 or not isinstance(payload, dict):
        return False
    effect = effects[0]
    effect_payload = payload.get("effect")
    receipt_authority_digest, receipt_source_head = _transition_receipt_identity(active)
    expected_document = {
        "schema": "zekam-scope-transition/v1",
        "previous_task_id": PREVIOUS_TASK_ID,
        "previous_authority_digest": PREVIOUS_AUTHORITY_DIGEST,
        "previous_git_blob": PREVIOUS_GIT_BLOB,
        "new_task_id": EVOLUTION_TASK_ID,
        "new_authority_digest": receipt_authority_digest,
        "source_head": receipt_source_head,
        "open_work_items": 0,
        "running_leases": 0,
        "recovery_cases": 0,
        "archive_ref": f"docs/archive/tasks/{PREVIOUS_TASK_ID}.md",
        "projection_ref": "AKTIF_GOREV.yaml",
        "grants_authority": False,
    }
    if document != expected_document or not isinstance(effect_payload, dict):
        return False
    expected_key = f"job:{snapshot.get('job_id')}:effect:{digest(effect_payload)}"
    expected_evidence = digest({"idempotency_key": expected_key, "line": raw})
    return bool(
        snapshot.get("idempotency_key") == TRANSITION_KEY
        and snapshot.get("state") == "completed"
        and payload.get("operation") == "local.append-journal/v1"
        and effect_payload
        == {
            "relative_path": f"scope-transitions/{EVOLUTION_TASK_ID}.jsonl",
            "line": raw,
        }
        and key == expected_key
        and effect.get("operation") == "local.append-journal/v1"
        and effect.get("effect_digest") == digest(effect_payload)
        and effect.get("receipt_status") == "completed"
        and effect.get("evidence_digest") == expected_evidence
        and snapshot.get("terminal_evidence_digest") == expected_evidence
    )


def _authority_schema_status(
    database: Path, *, required_bindings: dict[str, str], now: dt.datetime
) -> dict[str, Any]:
    """Inspect the dormant OE-01 schema without creating or mutating it."""

    expected = {
        "evolution_grant_approval",
        "evolution_standing_grant",
        "evolution_grant_revocation",
        "evolution_review_decision",
        "evolution_child_reservation",
        "evolution_child_effect_claim",
        "evolution_runtime_attestation",
        "evolution_terminal_readback",
        "evolution_child_terminal",
    }
    result: dict[str, Any] = {
        "contract_implemented": True,
        "registered_schema_version": 5,
        "migration_contract_implemented": True,
        "schema_digest": evolution_authority_schema_digest(),
        "schema_integrated": False,
        "registered_grants": 0,
        "active_grants": 0,
        "current_grant_digests": (),
        "permitted_models": (),
        "permitted_resources": (),
        "budget_limits": {
            "provider_calls": 0,
            "tokens": 0,
            "duration_seconds": 0,
            "cost_micros": 0,
            "disk_bytes": 0,
            "concurrency": 0,
        },
        "admission_ready": False,
        "drifted_grants": 0,
        "required_bindings_digest": digest(required_bindings),
        "grants_authority": False,
    }
    if not database.is_file():
        return result
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            present = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type='table' and name like 'evolution_%'"
                )
            }
            if present != expected:
                return result
            count = connection.execute("select count(*) from evolution_standing_grant").fetchone()
            result["schema_integrated"] = True
            result["registered_grants"] = 0 if count is None else int(count[0])
            rows = connection.execute(
                "select g.grant_digest,g.body_json,"
                "case when r.grant_digest is null then 0 else 1 end revoked,"
                "a.grant_digest,a.owner_id,a.realm_id,a.device_id,a.expires_at "
                "from evolution_standing_grant g join ("
                "select grant_id,max(revision) revision from evolution_standing_grant "
                "group by grant_id) latest on latest.grant_id=g.grant_id "
                "and latest.revision=g.revision left join evolution_grant_revocation r "
                "on r.grant_digest=g.grant_digest left join evolution_grant_approval a "
                "on a.receipt_digest=g.approval_receipt_digest order by g.grant_digest"
            ).fetchall()
            active: list[tuple[str, dict[str, Any]]] = []
            drifted = 0
            for (
                grant_digest,
                raw,
                revoked,
                approval_grant_digest,
                approval_owner_id,
                approval_realm_id,
                approval_device_id,
                approval_expires_at,
            ) in rows:
                try:
                    body = json.loads(str(raw))
                    valid_from = dt.datetime.fromisoformat(str(body["valid_from"]))
                    expires_at = dt.datetime.fromisoformat(str(body["expires_at"]))
                    review_after = dt.datetime.fromisoformat(str(body["review_after"]))
                    approval_expiry = dt.datetime.fromisoformat(str(approval_expires_at))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    drifted += 1
                    continue
                if not isinstance(body, dict) or digest(body) != str(grant_digest):
                    drifted += 1
                    continue
                if body.get("task_scope_digest") != required_bindings.get(
                    "task_scope_digest"
                ):
                    continue
                if bool(revoked):
                    continue
                current = (
                    len(required_bindings) == 7
                    and body.get("state") == "active"
                    and all(body.get(key) == value for key, value in required_bindings.items())
                    and str(approval_grant_digest) == str(grant_digest)
                    and str(approval_owner_id) == str(body.get("owner_id"))
                    and str(approval_realm_id) == str(body.get("realm_id"))
                    and str(approval_device_id) == str(body.get("device_id"))
                    and now < approval_expiry
                    and valid_from <= now < expires_at
                    and now < review_after
                )
                if current:
                    active.append((str(grant_digest), body))
                else:
                    drifted += 1
            models = sorted(
                {
                    str(value)
                    for _, body in active
                    for value in body.get("model_refs", [])
                }
            )
            resources = sorted(
                {
                    str(value)
                    for _, body in active
                    for field in ("readable_resources", "writable_resources")
                    for value in body.get(field, [])
                }
            )
            budget_limits = {
                name: sum(int(body.get("budget", {}).get(name, 0)) for _, body in active)
                for name in (
                    "provider_calls",
                    "tokens",
                    "duration_seconds",
                    "cost_micros",
                    "disk_bytes",
                    "concurrency",
                )
            }
            result.update(
                {
                    "active_grants": len(active),
                    "current_grant_digests": tuple(item[0] for item in active),
                    "permitted_models": tuple(models),
                    "permitted_resources": tuple(resources),
                    "budget_limits": budget_limits,
                    "admission_ready": bool(active) and drifted == 0,
                    "drifted_grants": drifted,
                }
            )
            return result
    except sqlite3.Error:
        return result


def _runtime_observability(database: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "heartbeat_at": None,
        "last_effect": None,
    }
    if not database.is_file():
        return result
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            slot = connection.execute(
                "select slot_key,created_at from local_scheduler_slot "
                "order by created_at desc,slot_key desc limit 1"
            ).fetchone()
            effect = connection.execute(
                "select c.operation,r.status,r.evidence_digest,r.created_at "
                "from local_effect_receipt r join local_effect_claim c on c.id=r.claim_id "
                "order by r.created_at desc,r.id desc limit 1"
            ).fetchone()
        if slot is not None:
            result["heartbeat_at"] = str(slot[1])
            result["last_slot"] = str(slot[0])
        if effect is not None:
            result["last_effect"] = {
                "operation": str(effect[0]),
                "status": str(effect[1]),
                "evidence_digest": str(effect[2]),
                "occurred_at": str(effect[3]),
            }
    except sqlite3.Error:
        result["read_error"] = True
    return result


def _windows_task_success_events() -> tuple[dict[str, object], ...]:
    """Read a bounded native Task Scheduler success history without mutation."""

    if os.name != "nt":
        return ()
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$diagnostics=Join-Path $env:SystemRoot "
        "'System32\\WindowsPowerShell\\v1.0\\Modules\\Microsoft.PowerShell.Diagnostics\\Microsoft.PowerShell.Diagnostics.psd1';"
        "Import-Module -Name $diagnostics -Force;"
        "$events=@(Get-WinEvent "
        "-LogName 'Microsoft-Windows-TaskScheduler/Operational' "
        "-FilterXPath \"*[System[EventID=201] and "
        "EventData[Data[@Name='TaskName']='\\Zekam\\AutonomousEvolution']]\" "
        f"-MaxEvents {_TASK_EVENT_LIMIT} | ForEach-Object {{"
        "[pscustomobject]@{"
        "occurred_at=$_.TimeCreated.ToUniversalTime().ToString('o');"
        "event_record_id=[long]$_.RecordId;"
        "task_name=[string]$_.Properties[0].Value;"
        "action_name=[string]$_.Properties[2].Value;"
        "result_code=[long]$_.Properties[3].Value}});"
        "$events|ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "RemoteSigned",
                "-Command",
                script,
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode or len(result.stdout.encode("utf-8")) > _TASK_EVENT_OUTPUT_LIMIT:
        return ()
    try:
        document = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ()
    rows = document if isinstance(document, list) else [document]
    return tuple(row for row in rows if isinstance(row, dict))


def _native_two_cycle_acceptance(
    context: ApplicationContext,
    *,
    operational_database: Path,
    supervisor: dict[str, object],
    events: tuple[dict[str, object], ...] | None = None,
) -> dict[str, Any]:
    """Correlate two native OS successes with exact terminal operational jobs."""

    base: dict[str, Any] = {
        "schema": "zekam-windows-native-two-cycle-acceptance/v1",
        "state": "missing",
        "verified": False,
        "cycles": (),
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    if os.name != "nt" or supervisor.get("state") != "matching":
        return base | {"evidence_digest": digest(base)}
    try:
        expected_executable = build_windows_supervisor_plan(context).executable
    except (OSError, ValidationFailed):
        return base | {"evidence_digest": digest(base)}
    improvement_database = context.home / "state" / "improvement.db"
    if not improvement_database.is_file() or not operational_database.is_file():
        return base | {"evidence_digest": digest(base)}
    try:
        with sqlite3.connect(
            f"{improvement_database.resolve().as_uri()}?mode=ro", uri=True
        ) as improvement:
            bootstrap = improvement.execute(
                "select occurred_at,body_json from evolution_control_event "
                "where reason='bootstrap-complete' order by ordinal desc limit 1"
            ).fetchone()
        if bootstrap is None:
            return base | {"evidence_digest": digest(base)}
        bootstrap_at = dt.datetime.fromisoformat(str(bootstrap[0]).replace("Z", "+00:00"))
        bootstrap_body = json.loads(str(bootstrap[1]))
        settlement = bootstrap_body.get("bootstrap_settlement")
        installed = settlement.get("supervisor") if isinstance(settlement, dict) else None
        if (
            bootstrap_at.tzinfo is None
            or not isinstance(installed, dict)
            or installed.get("state") not in {"installed", "already-installed"}
            or installed.get("plan_digest") != supervisor.get("plan_digest")
            or installed.get("status_digest") != supervisor.get("status_digest")
        ):
            return base | {"evidence_digest": digest(base)}
        with sqlite3.connect(
            f"{operational_database.resolve().as_uri()}?mode=ro", uri=True
        ) as operational:
            rows = operational.execute(
                "select s.slot_key,s.schedule_digest,j.id,j.idempotency_key,j.payload_json,"
                "j.state,j.terminal_evidence_digest,j.created_at,j.updated_at,c.operation,"
                "c.effect_digest,c.idempotency_key,r.status,r.evidence_digest,r.created_at,"
                "(select count(*) from local_effect_claim cx where cx.job_id=j.id),"
                "(select count(*) from local_effect_receipt rx join local_effect_claim cy "
                "on cy.id=rx.claim_id where cy.job_id=j.id) "
                "from local_scheduler_slot s join local_job j on j.id=s.job_id "
                "join local_effect_claim c on c.job_id=j.id "
                "join local_effect_receipt r on r.claim_id=c.id "
                "where s.slot_key like 'maintenance-reconcile:%' and j.created_at>=? "
                "order by j.created_at desc,j.id desc limit 16",
                (bootstrap_at.astimezone(dt.UTC).isoformat(),),
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        unavailable = base | {"state": "unavailable"}
        return unavailable | {"evidence_digest": digest(unavailable)}

    successful_events: list[tuple[dt.datetime, int]] = []
    for event in _windows_task_success_events() if events is None else events:
        try:
            occurred_at = dt.datetime.fromisoformat(
                str(event["occurred_at"]).replace("Z", "+00:00")
            )
            record_id = int(str(event["event_record_id"]))
            result_code = int(str(event["result_code"]))
        except (KeyError, TypeError, ValueError):
            continue
        if (
            occurred_at.tzinfo is not None
            and occurred_at >= bootstrap_at
            and event.get("task_name") == supervisor.get("task_name")
            and event.get("action_name") == expected_executable
            and result_code == 0
            and record_id > 0
        ):
            successful_events.append((occurred_at.astimezone(dt.UTC), record_id))

    cycles: dict[dt.datetime, dict[str, object]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row[4]))
            effect = payload["effect"]
            scheduled_text = str(effect["scheduled_for"])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z", scheduled_text) is None:
                continue
            scheduled_for = dt.datetime.fromisoformat(
                scheduled_text.replace("Z", "+00:00")
            )
            terminal_digest = str(row[6])
            parse_digest(terminal_digest)
            schedule_digest = str(effect["schedule_digest"])
            parse_digest(schedule_digest)
            effect_digest = digest(effect)
            expected_schedule_digest = digest(
                {
                    "schema": "zekam-maintenance-reconcile-schedule/v1",
                    "interval_minutes": 5,
                    "scheduled_for": scheduled_text,
                    "operation": "maintenance.reconcile/v1",
                    "misfire": "run-once",
                    "overlap": "skip",
                }
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationFailed):
            continue
        if (
            frozenset(payload) != {"operation", "effect"}
            or payload.get("operation") != "maintenance.reconcile/v1"
            or not isinstance(effect, dict)
            or frozenset(effect) != {"scheduled_for", "schedule_digest", "source"}
            or effect.get("source") != "os-supervisor"
            or schedule_digest != expected_schedule_digest
            or str(row[0]) != f"maintenance-reconcile:{scheduled_text}"
            or str(row[1]) != schedule_digest
            or str(row[3]) != f"maintenance-reconcile:{schedule_digest}"
            or str(row[5]) != "completed"
            or str(row[9]) != "maintenance.reconcile/v1"
            or str(row[10]) != effect_digest
            or str(row[11]) != f"job:{row[2]}:effect:{effect_digest}"
            or str(row[12]) != "completed"
            or str(row[13]) != terminal_digest
            or int(row[15]) != 1
            or int(row[16]) != 1
        ):
            continue
        matching_events = [
            event
            for event in successful_events
            if scheduled_for <= event[0] <= scheduled_for + dt.timedelta(seconds=60)
        ]
        if len(matching_events) != 1:
            continue
        event_at, record_id = matching_events[0]
        cycles[scheduled_for] = {
            "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
            "job_id": str(row[2]),
            "terminal_evidence_digest": terminal_digest,
            "task_event_record_id": record_id,
            "task_completed_at": event_at.isoformat().replace("+00:00", "Z"),
        }
    ordered = sorted(cycles)
    pair: tuple[dict[str, object], ...] = ()
    for previous, current in pairwise(ordered):
        if current - previous == dt.timedelta(minutes=5):
            pair = (cycles[previous], cycles[current])
    if not pair:
        return base | {"evidence_digest": digest(base)}
    accepted = base | {"state": "verified", "verified": True, "cycles": pair}
    return accepted | {"evidence_digest": digest(accepted)}


def effective_evolution_state(
    plan: dict[str, Any], report: dict[str, Any]
) -> str:
    """Reduce all public surfaces to one fail-closed canonical state."""

    control = report.get("control")
    control_state = control.get("state") if isinstance(control, dict) else None
    runtime = plan.get("runtime")
    supervisor = plan.get("supervisor")
    if (
        report.get("ledger_state") == "recovery-required"
        or plan.get("state") == "recovery-required"
    ):
        return "recovery-required"
    if control_state in {"disabled", "paused"}:
        return str(control_state)
    if plan.get("blockers"):
        return "blocked"
    if isinstance(supervisor, dict) and supervisor.get("state") in {"drifted", "unavailable"}:
        return "degraded"
    if plan.get("setup_gaps"):
        return "setup-required"
    if isinstance(runtime, dict) and int(runtime.get("running_jobs", 0)):
        return "running"
    return "observing"


def _operational_usage(database: Path) -> dict[str, Any]:
    usage = {
        "provider_calls": 0,
        "tokens": 0,
        "duration_seconds": 0,
        "cost_micros": 0,
        "disk_bytes": 0,
    }
    result: dict[str, Any] = {
        "available": False,
        "terminal_runs": 0,
        "unknown_runs": 0,
        **usage,
    }
    if not database.is_file():
        return result
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            present = connection.execute(
                "select 1 from sqlite_master where type='table' "
                "and name='evolution_child_terminal'"
            ).fetchone()
            if present is None:
                return result
            row = connection.execute(
                "select count(*),sum(case when status='unknown' then 1 else 0 end),"
                "coalesce(sum(provider_calls),0),coalesce(sum(tokens),0),"
                "coalesce(sum(duration_seconds),0),coalesce(sum(cost_micros),0),"
                "coalesce(sum(disk_bytes),0) from evolution_child_terminal"
            ).fetchone()
        assert row is not None
        return {
            "available": True,
            "terminal_runs": int(row[0]),
            "unknown_runs": int(row[1] or 0),
            "provider_calls": int(row[2]),
            "tokens": int(row[3]),
            "duration_seconds": int(row[4]),
            "cost_micros": int(row[5]),
            "disk_bytes": int(row[6]),
        }
    except sqlite3.Error:
        return result | {"read_error": True}


def _capture_gap_summary(home: Path) -> dict[str, Any]:
    root = home / "global" / "runtime" / "client-lifecycle"
    if not root.is_dir() or root.is_symlink():
        return {"count": 0, "bounded": True}
    count = 0
    truncated = False
    for client in sorted(root.iterdir(), key=lambda item: item.name):
        if not client.is_dir() or client.is_symlink():
            continue
        gap_root = client / "capture-gaps"
        if not gap_root.is_dir() or gap_root.is_symlink():
            continue
        for candidate in gap_root.glob("*.json"):
            if candidate.is_file() and not candidate.is_symlink():
                count += 1
                if count >= 4096:
                    truncated = True
                    break
        if truncated:
            break
    return {"count": count, "bounded": True, "truncated": truncated}


def _supervisor_status(context: ApplicationContext, config_digest: str) -> dict[str, object]:
    if os.name != "nt":
        return {"state": "unverified", "platform": os.name, "registered": False}
    executable = shutil.which("zekam")
    if executable is None:
        return {"state": "unavailable", "reason": "zekam-executable-missing", "registered": False}
    try:
        plan = build_windows_supervisor_plan(context, config_digest=config_digest)
        return inspect_windows_task(plan)
    except (OSError, PolicyViolation, ValidationFailed) as exc:
        return {"state": "unavailable", "reason": type(exc).__name__, "registered": False}


def build_windows_supervisor_plan(
    context: ApplicationContext, *, config_digest: str | None = None
) -> WindowsTaskPlan:
    """Build the one current-source-validated Windows supervisor plan."""

    executable = shutil.which("zekam")
    provenance = context.settings.config_provenance
    effective_digest = config_digest or (
        None if provenance is None else provenance.effective_digest
    )
    if executable is None or effective_digest is None:
        raise ValidationFailed("Windows supervisor plan config ve kurulu executable ister")
    return WindowsTaskPlan.create(
        executable=Path(executable),
        home=context.home,
        config_digest=effective_digest,
        implementation_manifest=validated_package_manifest(context),
        start_boundary=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def _admission_bindings(
    context: ApplicationContext,
    *,
    active: ActiveTaskContract,
    config_graph_digest: str,
    supervisor: dict[str, object],
) -> dict[str, str]:
    """Bind resume admission to the current task, source, policy and package manifests."""

    try:
        manifest = validated_package_manifest(context)
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValidationFailed, json.JSONDecodeError):
        return {}
    implementation = supervisor.get("implementation_digest")
    required = {
        "task_scope_digest": active.source_digest,
        "policy_digest": config_graph_digest,
        "verifier_digest": document.get("protocol_schema_digest"),
        "validator_digest": document.get("local_schema_bundle_digest"),
        "source_lineage_digest": document.get("package_source_bundle_digest"),
        "protected_manifest_digest": implementation,
        "dependency_manifest_digest": document.get("config_bundle_digest"),
    }
    if any(
        not isinstance(value, str) or not value.startswith("sha256:")
        for value in required.values()
    ):
        return {}
    return {key: str(value) for key, value in required.items()}


def build_evolution_plan(context: ApplicationContext) -> dict[str, Any]:
    """Build a provider-free OE-00 plan from canonical local evidence."""

    active = ActiveTaskContract.load(context.core_path / "AKTIF_GOREV.md")
    active.verify_projection(context.core_path / "AKTIF_GOREV.yaml")
    archive_root = context.core_path / "docs" / "archive" / "tasks"
    previous = ActiveTaskContract.load(archive_root / f"{PREVIOUS_TASK_ID}.md")
    previous.verify_projection(archive_root / f"{PREVIOUS_TASK_ID}.projection.yaml")
    runtime_store: SQLiteLocalRuntimeStore | None
    try:
        runtime_store = SQLiteLocalRuntimeStore(
            context.settings.database.sqlite_path(context.home),
            existing_only=True,
        )
        runtime = asdict(runtime_store.status())
        transition_job = runtime_store.job_snapshot(TRANSITION_KEY)
    except PolicyViolation:
        runtime_store = None
        runtime = {
            "ready_jobs": 0,
            "running_jobs": 0,
            "recovery_jobs": 0,
            "quarantined_jobs": 0,
            "pending_outbox": 0,
            "claimed_outbox": 0,
            "recovery_outbox": 0,
            "open_recovery_cases": 0,
        }
        transition_job = None
    journal = _transition_journal(context.home)
    spool = inspect_spool(context.home).as_dict()
    config = context.settings.config_provenance
    if config is None:
        raise ValidationFailed("Evolution plan config provenance ister")

    head = _git(context.core_path, "rev-parse", "HEAD")
    branch = _git(context.core_path, "branch", "--show-current")
    dirty_paths = tuple(
        line[3:] for line in _git(context.core_path, "status", "--porcelain").splitlines()
    )
    baseline_ancestor = (
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", active.baseline_head, head),
            cwd=context.core_path,
            check=False,
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )
    transition_ok = _transition_binding_ok(
        snapshot=transition_job,
        journal=journal,
        active=active,
    )

    blockers: list[str] = []
    if active.task_id != EVOLUTION_TASK_ID:
        blockers.append("living-authority-not-evolution-task")
    if previous.task_id != PREVIOUS_TASK_ID or previous.source_digest != PREVIOUS_AUTHORITY_DIGEST:
        blockers.append("previous-authority-archive-drift")
    archived_blob = _git(
        context.core_path,
        "hash-object",
        str(archive_root / f"{PREVIOUS_TASK_ID}.md"),
    )
    if archived_blob != PREVIOUS_GIT_BLOB:
        blockers.append("previous-authority-git-blob-drift")
    if not baseline_ancestor:
        blockers.append("baseline-is-not-source-ancestor")
    if runtime["running_jobs"] or runtime["recovery_jobs"] or runtime["open_recovery_cases"]:
        blockers.append("runtime-recovery-or-live-lease")
    operational_database = context.settings.database.sqlite_path(context.home)
    supervisor = _supervisor_status(context, config.effective_digest)
    admission_bindings = _admission_bindings(
        context,
        active=active,
        config_graph_digest=config.graph_digest,
        supervisor=supervisor,
    )
    authority = _authority_schema_status(
        operational_database,
        required_bindings=admission_bindings,
        now=dt.datetime.now(dt.UTC),
    )
    runtime_observability = _runtime_observability(operational_database)
    native_acceptance = _native_two_cycle_acceptance(
        context,
        operational_database=operational_database,
        supervisor=supervisor,
    )
    setup_gaps: list[str] = []
    if native_acceptance["verified"] is not True:
        setup_gaps.append("native-two-cycle-acceptance-missing")
    if supervisor["state"] != "matching":
        setup_gaps.append("os-supervision-not-installed")
    if runtime_store is None:
        setup_gaps.append("operational-store-not-initialized")
    if not authority["schema_integrated"]:
        setup_gaps.append("standing-grant-operational-migration-not-admitted")
    elif not authority["admission_ready"]:
        setup_gaps.append("standing-grant-missing-expired-revoked-or-drifted")
    if not transition_ok:
        setup_gaps.append("scope-transition-receipt-missing-or-drifted")
    oe00_state = (
        "independently-verified"
        if not blockers and runtime_store is not None and transition_ok
        else "pending"
    )

    state = (
        "recovery-required"
        if "runtime-recovery-or-live-lease" in blockers
        else (
            "blocked"
            if blockers
            else "setup-required"
            if setup_gaps
            else "observing"
        )
    )
    body: dict[str, Any] = {
        "schema": "zekam-evolution-plan/v1",
        "state": state,
        "task": {
            "task_id": active.task_id,
            "authority_digest": active.source_digest,
            "baseline_head": active.baseline_head,
        },
        "source_fingerprint": {
            "branch": branch,
            "head": head,
            "baseline_ancestor": baseline_ancestor,
            "dirty_paths": dirty_paths,
        },
        "config_fingerprint": {
            "effective_digest": config.effective_digest,
            "graph_digest": config.graph_digest,
        },
        "scope_transition": {
            "previous_task_id": previous.task_id,
            "previous_authority_digest": previous.source_digest,
            "previous_git_blob": PREVIOUS_GIT_BLOB,
            "journal_verified": transition_ok,
            "terminal_receipt_verified": transition_ok,
        },
        "runtime": runtime | runtime_observability,
        "evolution_authority": authority,
        "admission_bindings": admission_bindings,
        "handlers": tuple(
            {
                "operation": operation,
                "version": contract.version,
                "readable_resources": tuple(sorted(contract.readable_resources)),
                "writable_resources": tuple(sorted(contract.writable_resources)),
            }
            for operation, contract in sorted(EVOLUTION_HANDLERS.items())
        ),
        "permissions": {
            "models": authority["permitted_models"],
            "resources": authority["permitted_resources"],
            "budget_limits": authority["budget_limits"],
            "source": "current-standing-grants",
        },
        "supervisor": supervisor,
        "capture": {
            "opencode_queued": spool["queued"],
            "opencode_quarantine": spool["quarantine"],
        },
        "packages": (
            {
                "id": "OE-00",
                "state": oe00_state,
                "depends_on": (),
            },
            {"id": "OE-01", "state": "implemented-tested", "depends_on": ("OE-00",)},
            {"id": "OE-02", "state": "implemented-tested", "depends_on": ("OE-01",)},
            {"id": "OE-03", "state": "implemented-tested", "depends_on": ("OE-01",)},
            {"id": "OE-04", "state": "implemented-tested", "depends_on": ("OE-02", "OE-03")},
            {"id": "OE-05", "state": "implemented-tested", "depends_on": ("OE-04",)},
            {"id": "OE-06", "state": "independently-verified", "depends_on": ("OE-05",)},
            {
                "id": "OE-07",
                "state": "independently-verified",
                "depends_on": ("OE-03", "OE-06"),
            },
            {
                "id": "OE-08",
                "state": (
                    "independently-verified"
                    if native_acceptance["verified"] is True
                    else "awaiting-native-two-cycle"
                    if supervisor["state"] == "matching"
                    else "blocked-on-native-authorization"
                ),
                "depends_on": ("OE-07",),
            },
        ),
        "native_acceptance": native_acceptance,
        "setup_gaps": tuple(setup_gaps),
        "blockers": tuple(blockers),
        "provider_calls": 0,
        "network_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    return {**body, "plan_digest": digest(body)}


def build_evolution_report(
    context: ApplicationContext, *, plan: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read canonical local ledgers without turning prose into work authority."""

    database = context.home / "state" / "improvement.db"
    counts = {
        "candidates": 0,
        "evaluations": 0,
        "typed_rollouts": 0,
        "prepared_unsettled": 0,
        "legacy_rollouts": 0,
        "legacy_activations": 0,
        "legacy_rollbacks": 0,
        "rejected": 0,
        "recovered": 0,
        "measured_improvements": 0,
        "regressions": 0,
    }
    candidates: list[dict[str, object]] = []
    evaluation_pairs: list[dict[str, object]] = []
    if database.is_file():
        try:
            with sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True
            ) as connection:
                connection.row_factory = sqlite3.Row
                table_names = {
                    str(row[0])
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                mapping = {
                    "candidates": "improvement_candidate",
                    "evaluations": "improvement_evaluation",
                    "typed_rollouts": "typed_rollout_execution",
                    "legacy_rollouts": "rollout_receipt",
                    "legacy_activations": "improvement_activation",
                    "legacy_rollbacks": "rollback_receipt",
                }
                for key, table in mapping.items():
                    if table in table_names:
                        counts[key] = int(
                            connection.execute(f"select count(*) from {table}").fetchone()[0]
                        )
                if "typed_rollout_pending" in table_names:
                    counts["prepared_unsettled"] = int(
                        connection.execute(
                            "select count(*) from typed_rollout_pending p "
                            "left join typed_rollout_execution e "
                            "on e.plan_digest=p.plan_digest "
                            "left join typed_rollout_recovery r "
                            "on r.plan_digest=p.plan_digest "
                            "where e.plan_digest is null and r.plan_digest is null"
                        ).fetchone()[0]
                    )
                if "typed_rollout_recovery" in table_names:
                    counts["recovered"] = int(
                        connection.execute(
                            "select count(*) from typed_rollout_recovery"
                        ).fetchone()[0]
                    )
                if "improvement_evaluation" in table_names:
                    state_rows = dict(
                        connection.execute(
                            "select state,count(*) from improvement_evaluation group by state"
                        ).fetchall()
                    )
                    counts["measured_improvements"] = int(
                        state_rows.get("improved", 0) + state_rows.get("target-reached", 0)
                    )
                    counts["regressions"] = int(state_rows.get("regressed", 0))
                    counts["rejected"] = int(
                        sum(
                            amount
                            for state, amount in state_rows.items()
                            if state not in {"improved", "target-reached"}
                        )
                    )
                if "improvement_candidate" in table_names:
                    for row in connection.execute(
                            "select c.candidate_digest,c.change_class,c.created_at,"
                            "c.failure_card_digest,c.baseline_aggregate_digest,"
                            "e.evaluation_digest,e.state,r.approved from improvement_candidate c "
                            "left join improvement_evaluation e "
                            "on e.candidate_digest=c.candidate_digest "
                            "left join improvement_review r "
                            "on r.candidate_digest=c.candidate_digest "
                            "order by c.created_at desc limit 100"
                    ):
                        change_class = str(row[1])
                        evaluation_state = None if row[6] is None else str(row[6])
                        blockers: list[str] = []
                        if row[5] is None:
                            blockers.append("evaluation-missing")
                        elif evaluation_state not in {"improved", "target-reached"}:
                            blockers.append(f"evaluation-{evaluation_state}")
                        if change_class == "REVIEW_REQUIRED" and row[7] != 1:
                            blockers.append("independent-review-required")
                        elif change_class == "HUMAN_APPROVAL_REQUIRED":
                            blockers.append("human-approval-required")
                        elif change_class == "PROHIBITED_AUTONOMOUS":
                            blockers.append("prohibited-autonomous")
                        candidates.append(
                            {
                                "candidate_digest": str(row[0]),
                                "change_class": change_class,
                                "risk": change_class,
                                "created_at": str(row[2]),
                                "source_evidence_digest": str(row[3]),
                                "baseline_aggregate_digest": str(row[4]),
                                "evaluation_digest": (
                                    None if row[5] is None else str(row[5])
                                ),
                                "evaluation_state": evaluation_state,
                                "blockers": tuple(blockers),
                            }
                        )
                if "improvement_evaluation" in table_names:
                    for row in connection.execute(
                            "select e.evaluation_digest,e.candidate_digest,"
                            "c.baseline_aggregate_digest,e.after_aggregate_digest,"
                            "e.state,e.finished_at,e.body_json from improvement_evaluation e "
                            "join improvement_candidate c "
                            "on c.candidate_digest=e.candidate_digest "
                            "order by e.finished_at desc limit 100"
                    ):
                        try:
                            evaluation_body = json.loads(str(row[6]))
                        except json.JSONDecodeError:
                            continue
                        if (
                            not isinstance(evaluation_body, dict)
                            or digest(evaluation_body) != str(row[0])
                        ):
                            continue
                        evaluation_pairs.append(
                            {
                                "evaluation_digest": str(row[0]),
                                "candidate_digest": str(row[1]),
                                "before_aggregate_digest": str(row[2]),
                                "after_aggregate_digest": str(row[3]),
                                "baseline_values": evaluation_body.get(
                                    "baseline_values", {}
                                ),
                                "current_values": evaluation_body.get(
                                    "current_values", {}
                                ),
                                "progress": evaluation_body.get("progress", {}),
                                "actual_usage": {
                                    "provider_calls": int(
                                        evaluation_body.get("actual_provider_calls", 0)
                                    ),
                                    "tokens": int(
                                        evaluation_body.get("actual_tokens", 0)
                                    ),
                                    "cost_micros": int(
                                        evaluation_body.get("actual_cost_micros", 0)
                                    ),
                                },
                                "state": str(row[4]),
                                "finished_at": str(row[5]),
                            }
                        )
        except sqlite3.Error:
            counts["prepared_unsettled"] = -1
    ledger_state = (
        "recovery-required"
        if counts["prepared_unsettled"] != 0
        else "observing"
    )
    current_plan = plan or build_evolution_plan(context)
    actual_usage = _operational_usage(context.settings.database.sqlite_path(context.home))
    evaluation_usage = {"provider_calls": 0, "tokens": 0, "cost_micros": 0}
    for pair in evaluation_pairs:
        pair_usage = pair.get("actual_usage")
        if not isinstance(pair_usage, dict):
            continue
        for name in evaluation_usage:
            evaluation_usage[name] += int(pair_usage.get(name, 0))
    body: dict[str, Any] = {
        "schema": "zekam-evolution-report/v1",
        "ledger_state": ledger_state,
        "counts": counts,
        "candidates": candidates,
        "evaluation_pairs": evaluation_pairs,
        "control": _read_evolution_control(database),
        "metrics": {
            "before_after_pairs": counts["evaluations"],
            "measured_improvements": counts["measured_improvements"],
            "regressions": counts["regressions"],
            "rejected": counts["rejected"],
            "recovered": counts["recovered"],
            "rolled_back": counts["legacy_rollbacks"],
        },
        "actual_usage": actual_usage | {"evaluations": evaluation_usage},
        "budget": {
            "limits": current_plan["permissions"]["budget_limits"],
            "usage": {
                key: actual_usage[key]
                for key in (
                    "provider_calls",
                    "tokens",
                    "duration_seconds",
                    "cost_micros",
                    "disk_bytes",
                )
            },
            "source": "operational-authority-ledger",
        },
        "capture_gaps": _capture_gap_summary(context.home),
        "native_acceptance": current_plan.get("native_acceptance", {}),
        "legacy_rollout_authoritative": False,
        "production_canary_claimed": False,
        "provider_calls": 0,
        "network_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    body["state"] = effective_evolution_state(current_plan, body)
    return body | {"report_digest": digest(body)}


def _read_evolution_control(database: Path) -> dict[str, object]:
    if not database.is_file():
        return {"state": "disabled", "reason": "improvement-store-missing"}
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as db:
            present = db.execute(
                "select 1 from sqlite_master where type='table' and name='evolution_control_event'"
            ).fetchone()
            if present is None:
                return {"state": "setup-required", "reason": "control-schema-missing"}
            row = db.execute(
                "select event_digest,ordinal,state,reason,occurred_at,body_json "
                "from evolution_control_event order by ordinal desc limit 1"
            ).fetchone()
        if row is None:
            return {"state": "observing", "ordinal": 0, "event_digest": None}
        result: dict[str, object] = {
            "event_digest": str(row[0]),
            "ordinal": int(row[1]),
            "state": str(row[2]),
            "reason": str(row[3]),
            "occurred_at": str(row[4]),
        }
        try:
            body = json.loads(str(row[5]))
        except json.JSONDecodeError:
            return {"state": "degraded", "reason": "control-event-json-drift"}
        if isinstance(body, dict) and isinstance(body.get("admission_evidence"), dict):
            result["admission_evidence"] = body["admission_evidence"]
        return result
    except sqlite3.Error:
        return {"state": "degraded", "reason": "control-read-failed"}


def build_resume_admission(context: ApplicationContext) -> dict[str, object] | None:
    """Re-read every current authority boundary and build one exact resume proof."""

    plan = build_evolution_plan(context)
    report = build_evolution_report(context, plan=plan)
    runtime = plan["runtime"]
    authority = plan["evolution_authority"]
    supervisor = plan["supervisor"]
    if not all(isinstance(item, dict) for item in (runtime, authority, supervisor)):
        return None
    recovery_clear = not any(
        runtime.get(field, 0) != 0
        for field in ("running_jobs", "recovery_jobs", "open_recovery_cases")
    ) and report["ledger_state"] != "recovery-required"
    raw_grants = authority.get("current_grant_digests", ())
    grant_digests = (
        tuple(str(value) for value in raw_grants)
        if isinstance(raw_grants, (list, tuple))
        else ()
    )
    if not (
        recovery_clear
        and authority.get("admission_ready") is True
        and grant_digests
        and supervisor.get("state") == "matching"
        and not plan["blockers"]
    ):
        return None
    task = plan["task"]
    config = plan["config_fingerprint"]
    if not isinstance(task, dict) or not isinstance(config, dict):
        return None
    evidence_body: dict[str, object] = {
        "task_scope_digest": task["authority_digest"],
        "config_digest": config["effective_digest"],
        "implementation_digest": supervisor["implementation_digest"],
        "supervisor_status_digest": supervisor["status_digest"],
        "grant_binding_digest": authority["required_bindings_digest"],
        "grant_digests": grant_digests,
        "recovery_clear": True,
    }
    return evidence_body | {"evidence_digest": digest(evidence_body)}


class CurrentEvolutionResumeVerifier:
    """Production verifier; caller-provided self-consistent digests grant no authority."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    def verify(self, evidence: Mapping[str, object]) -> None:
        expected = build_resume_admission(self._context)
        if expected is None or digest(dict(evidence)) != digest(expected):
            raise PolicyViolation("Evolution resume current authority readback drift")
