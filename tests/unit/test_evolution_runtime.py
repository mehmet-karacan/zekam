from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import zekam.application.evolution_runtime as evolution_runtime_module
from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.composition import build_context
from zekam.application.evolution_runtime import (
    EVOLUTION_TASK_ID,
    PREVIOUS_TASK_ID,
    TRANSITION_KEY,
    CurrentEvolutionResumeVerifier,
    _authority_schema_status,
    _native_two_cycle_acceptance,
    _transition_binding_ok,
    _transition_journal,
    _transition_receipt_identity,
    build_evolution_plan,
    build_evolution_report,
    effective_evolution_state,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.evolution_authority import EVOLUTION_AUTHORITY_DDL


def test_evolution_task_identities_are_distinct_and_canonical() -> None:
    assert EVOLUTION_TASK_ID == "ZEKAM-AUTONOMOUS-EVOLUTION-001"
    assert PREVIOUS_TASK_ID == "ZEKAM-LOCAL-INTELLIGENCE-PLANE-001"
    assert EVOLUTION_TASK_ID != PREVIOUS_TASK_ID


def test_transition_journal_requires_one_receipt_gated_record(tmp_path: Path) -> None:
    path = (
        tmp_path / "runtime" / "local-effects" / "scope-transitions" / f"{EVOLUTION_TASK_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    document = {"schema": "zekam-scope-transition/v1", "grants_authority": False}
    path.write_text(
        "job:one:effect:sha256:" + "a" * 64 + "\t" + json.dumps(document) + "\n",
        encoding="utf-8",
    )

    assert _transition_journal(tmp_path) == (
        "job:one:effect:sha256:" + "a" * 64,
        json.dumps(document),
        document,
    )

    path.write_text("not-a-receipt\t{}\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="identity drift"):
        _transition_journal(tmp_path)


def test_transition_identity_bridges_only_the_exact_author_rewrite() -> None:
    rewritten = SimpleNamespace(
        source_digest="sha256:9408eff417b42801580c94988c3ee3b6eef5bc02a2a54e4d979bfe8310854aba",
        baseline_head="b59221a0891dc94d3702d042132254066dc089ed",
    )
    assert _transition_receipt_identity(rewritten) == (
        "sha256:4788116aa01885aa8b884579f4f8ee1ce87b76ee54f55c8fe884aec7e33580b2",
        "8761790e6034c5d2958476b9eb45af36da1b094a",
    )
    other = SimpleNamespace(source_digest=digest("other"), baseline_head="a" * 40)
    assert _transition_receipt_identity(other) == (other.source_digest, other.baseline_head)


def _active_contract() -> ActiveTaskContract:
    return ActiveTaskContract.from_bytes(
        b"---\n"
        b"schema: zekam-active-task/v2\n"
        b"task_id: ZEKAM-AUTONOMOUS-EVOLUTION-001\n"
        b"status: APPROVED_ACTIVE_TASK\n"
        b"title: Evolution\n"
        b"created_at: 2026-09-06T15:18:10+03:00\n"
        b"baseline_repository: mehmet-karacan/zekam\n"
        b"baseline_branch: main\n"
        b"baseline_head: b59221a0891dc94d3702d042132254066dc089ed\n"
        b"legacy_postgresql_data_import: FORBIDDEN\n"
        b"postgresql_runtime_dependency: FORBIDDEN\n"
        b"docker_required_for_zekam_core: false\n"
        b"push_authorized: false\n"
        b"---\n"
    )


def test_transition_binding_requires_exact_job_effect_journal_and_receipt_chain() -> None:
    active = _active_contract()
    document = {
        "schema": "zekam-scope-transition/v1",
        "previous_task_id": PREVIOUS_TASK_ID,
        "previous_authority_digest": (
            "sha256:ebd9ca00a5cc500a650984e3cdc7be22186b85b5a283d86a6f9d863237530629"
        ),
        "previous_git_blob": "ce2980e819df68ccf2ac375c1f550b6d675ebeaa",
        "new_task_id": EVOLUTION_TASK_ID,
        "new_authority_digest": active.source_digest,
        "source_head": active.baseline_head,
        "open_work_items": 0,
        "running_leases": 0,
        "recovery_cases": 0,
        "archive_ref": f"docs/archive/tasks/{PREVIOUS_TASK_ID}.md",
        "projection_ref": "AKTIF_GOREV.yaml",
        "grants_authority": False,
    }
    raw = json.dumps(document, separators=(",", ":"))
    effect_payload = {
        "relative_path": f"scope-transitions/{EVOLUTION_TASK_ID}.jsonl",
        "line": raw,
    }
    effect_digest = digest(effect_payload)
    key = f"job:job-1:effect:{effect_digest}"
    evidence = digest({"idempotency_key": key, "line": raw})
    snapshot = {
        "job_id": "job-1",
        "idempotency_key": TRANSITION_KEY,
        "state": "completed",
        "payload": {"operation": "local.append-journal/v1", "effect": effect_payload},
        "terminal_evidence_digest": evidence,
        "effects": [
            {
                "operation": "local.append-journal/v1",
                "effect_digest": effect_digest,
                "receipt_status": "completed",
                "evidence_digest": evidence,
            }
        ],
    }

    assert _transition_binding_ok(snapshot=snapshot, journal=(key, raw, document), active=active)

    drifted = {**snapshot, "terminal_evidence_digest": digest("other")}
    assert not _transition_binding_ok(
        snapshot=drifted,
        journal=(key, raw, document),
        active=active,
    )
    incomplete = {**document, "new_task_id": "ZEKAM-OTHER-TASK-001"}
    assert not _transition_binding_ok(
        snapshot=snapshot,
        journal=(key, raw, incomplete),
        active=active,
    )


def test_evolution_plan_does_not_create_a_missing_home(tmp_path: Path) -> None:
    missing = tmp_path / "missing-home"

    document = build_evolution_plan(build_context(home=missing))

    assert document["state"] == "setup-required"
    assert "operational-store-not-initialized" in document["setup_gaps"]
    assert document["packages"][0]["state"] == "pending"
    assert document["handlers"]
    assert document["permissions"] == {
        "models": (),
        "resources": (),
        "budget_limits": {
            "provider_calls": 0,
            "tokens": 0,
            "duration_seconds": 0,
            "cost_micros": 0,
            "disk_bytes": 0,
            "concurrency": 0,
        },
        "source": "current-standing-grants",
    }
    assert not missing.exists()


@pytest.mark.parametrize(
    ("plan_state", "gaps", "control", "ledger", "supervisor", "expected"),
    [
        ("setup-required", ("missing",), "observing", "observing", "absent", "setup-required"),
        ("setup-required", ("missing",), "paused", "observing", "absent", "paused"),
        ("setup-required", ("missing",), "disabled", "observing", "absent", "disabled"),
        ("setup-required", (), "observing", "observing", "drifted", "degraded"),
        ("setup-required", (), "paused", "recovery-required", "matching", "recovery-required"),
        ("setup-required", (), "observing", "observing", "matching", "observing"),
    ],
)
def test_effective_evolution_state_has_one_fail_closed_precedence(
    plan_state: str,
    gaps: tuple[str, ...],
    control: str,
    ledger: str,
    supervisor: str,
    expected: str,
) -> None:
    plan = {
        "state": plan_state,
        "setup_gaps": gaps,
        "blockers": (),
        "runtime": {"running_jobs": 0},
        "supervisor": {"state": supervisor},
    }
    report = {"ledger_state": ledger, "control": {"state": control}}

    assert effective_evolution_state(plan, report) == expected


def test_native_two_cycle_acceptance_correlates_os_events_and_terminal_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    improvement = state / "improvement.db"
    operational = state / "operational.db"
    plan_digest = digest("supervisor-plan")
    status_digest = digest("supervisor-status")
    bootstrap_at = "2026-09-07T12:00:00+00:00"
    bootstrap_body = {
        "bootstrap_settlement": {
            "supervisor": {
                "state": "installed",
                "plan_digest": plan_digest,
                "status_digest": status_digest,
            }
        }
    }
    with sqlite3.connect(improvement) as connection:
        connection.execute(
            "create table evolution_control_event("
            "ordinal integer,reason text,occurred_at text,body_json text)"
        )
        connection.execute(
            "insert into evolution_control_event values(1,'bootstrap-complete',?,?)",
            (bootstrap_at, json.dumps(bootstrap_body)),
        )
    with sqlite3.connect(operational) as connection:
        connection.executescript(
            "create table local_scheduler_slot(slot_key text,schedule_digest text,"
            "job_id text,created_at text);"
            "create table local_job(id text,idempotency_key text,payload_json text,"
            "state text,terminal_evidence_digest text,created_at text,updated_at text);"
            "create table local_effect_claim(id text,job_id text,operation text,"
            "effect_digest text,idempotency_key text);"
            "create table local_effect_receipt(id text,claim_id text,status text,"
            "evidence_digest text,created_at text);"
        )
        for minute in (5, 10):
            scheduled = f"2026-09-07T12:{minute:02d}:00Z"
            schedule_digest = digest(
                {
                    "schema": "zekam-maintenance-reconcile-schedule/v1",
                    "interval_minutes": 5,
                    "scheduled_for": scheduled,
                    "operation": "maintenance.reconcile/v1",
                    "misfire": "run-once",
                    "overlap": "skip",
                }
            )
            effect = {
                "scheduled_for": scheduled,
                "schedule_digest": schedule_digest,
                "source": "os-supervisor",
            }
            payload = {
                "operation": "maintenance.reconcile/v1",
                "effect": effect,
            }
            job_id = f"job-{minute}"
            terminal_digest = digest(f"terminal-{minute}")
            effect_digest = digest(effect)
            claim_id = f"claim-{minute}"
            connection.execute(
                "insert into local_scheduler_slot values(?,?,?,?)",
                (
                    f"maintenance-reconcile:{scheduled}",
                    schedule_digest,
                    job_id,
                    f"2026-09-07T12:{minute:02d}:01+00:00",
                ),
            )
            connection.execute(
                "insert into local_job values(?,?,?,?,?,?,?)",
                (
                    job_id,
                    f"maintenance-reconcile:{schedule_digest}",
                    json.dumps(payload),
                    "completed",
                    terminal_digest,
                    f"2026-09-07T12:{minute:02d}:02+00:00",
                    f"2026-09-07T12:{minute:02d}:03+00:00",
                ),
            )
            connection.execute(
                "insert into local_effect_claim values(?,?,?,?,?)",
                (
                    claim_id,
                    job_id,
                    "maintenance.reconcile/v1",
                    effect_digest,
                    f"job:{job_id}:effect:{effect_digest}",
                ),
            )
            connection.execute(
                "insert into local_effect_receipt values(?,?,?,?,?)",
                (
                    f"receipt-{minute}",
                    claim_id,
                    "completed",
                    terminal_digest,
                    f"2026-09-07T12:{minute:02d}:03+00:00",
                ),
            )
    executable = str((tmp_path / "zekam.exe").resolve())
    monkeypatch.setattr(
        "zekam.application.evolution_runtime.build_windows_supervisor_plan",
        lambda _context: SimpleNamespace(executable=executable),
    )
    monkeypatch.setattr("zekam.application.evolution_runtime.os.name", "nt")
    supervisor = {
        "state": "matching",
        "task_name": r"\Zekam\AutonomousEvolution",
        "plan_digest": plan_digest,
        "status_digest": status_digest,
    }
    events = tuple(
        {
            "occurred_at": f"2026-09-07T12:{minute:02d}:04Z",
            "event_record_id": 100 + minute,
            "task_name": r"\Zekam\AutonomousEvolution",
            "action_name": executable,
            "result_code": 0,
        }
        for minute in (5, 10)
    )

    result = _native_two_cycle_acceptance(
        build_context(home=home),
        operational_database=operational,
        supervisor=supervisor,
        events=events,
    )

    assert result["verified"] is True
    assert result["state"] == "verified"
    assert len(result["cycles"]) == 2
    assert result["cycles"][1]["scheduled_for"] == "2026-09-07T12:10:00Z"
    failed = ({**events[0], "result_code": 1}, events[1])
    assert _native_two_cycle_acceptance(
        build_context(home=home),
        operational_database=operational,
        supervisor=supervisor,
        events=failed,
    )["verified"] is False
    with sqlite3.connect(operational) as connection:
        connection.execute(
            "update local_scheduler_slot set schedule_digest=? where job_id='job-10'",
            (digest("wrong-schedule"),),
        )
    assert _native_two_cycle_acceptance(
        build_context(home=home),
        operational_database=operational,
        supervisor=supervisor,
        events=events,
    )["verified"] is False


def test_windows_event_reader_pins_native_diagnostics_module_and_bounds_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "occurred_at": "2026-09-07T12:10:04Z",
                    "event_record_id": 110,
                    "task_name": r"\Zekam\AutonomousEvolution",
                    "action_name": r"C:\Python\zekam.exe",
                    "result_code": 0,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(evolution_runtime_module.os, "name", "nt")
    monkeypatch.setattr(evolution_runtime_module.subprocess, "run", run)

    events = evolution_runtime_module._windows_task_success_events()

    assert len(events) == 1
    command = captured["command"]
    assert isinstance(command, tuple)
    script = command[-1]
    assert "Microsoft.PowerShell.Diagnostics.psd1" in script
    assert "Import-Module -Name $diagnostics -Force" in script
    assert "-MaxEvents 16" in script
    assert "EventID=201" in script
    assert "TaskName" in script
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 15


@pytest.mark.parametrize(
    "failure",
    [
        OSError("missing"),
        evolution_runtime_module.subprocess.TimeoutExpired("powershell.exe", 15),
    ],
)
def test_windows_event_reader_fails_closed_on_process_errors(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    monkeypatch.setattr(evolution_runtime_module.os, "name", "nt")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(evolution_runtime_module.subprocess, "run", fail)

    assert evolution_runtime_module._windows_task_success_events() == ()


def test_authority_status_requires_current_exact_approval_and_all_bindings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operational.db"
    bindings = {
        name: digest(name)
        for name in (
            "task_scope_digest",
            "policy_digest",
            "verifier_digest",
            "validator_digest",
            "source_lineage_digest",
            "protected_manifest_digest",
            "dependency_manifest_digest",
        )
    }
    now = dt.datetime(2026, 9, 7, 12, tzinfo=dt.UTC)
    body = {
        **bindings,
        "owner_id": "owner-1",
        "realm_id": "realm-1",
        "device_id": "device-1",
        "state": "active",
        "valid_from": (now - dt.timedelta(minutes=1)).isoformat(),
        "expires_at": (now + dt.timedelta(hours=1)).isoformat(),
        "review_after": (now + dt.timedelta(minutes=30)).isoformat(),
        "model_refs": ["none"],
        "readable_resources": ["local-capture"],
        "writable_resources": ["local-report"],
        "budget": {
            "provider_calls": 0,
            "tokens": 0,
            "duration_seconds": 60,
            "cost_micros": 0,
            "disk_bytes": 4096,
            "concurrency": 1,
        },
    }
    grant_digest = digest(body)
    approval_digest = digest("approval")
    with sqlite3.connect(database) as connection:
        connection.executescript(EVOLUTION_AUTHORITY_DDL)
        connection.execute(
            "insert into evolution_standing_grant values(?,?,?,?,?,?)",
            (grant_digest, "grant-1", 1, approval_digest, json.dumps(body), now.isoformat()),
        )
        connection.commit()

    unapproved = _authority_schema_status(database, required_bindings=bindings, now=now)
    assert unapproved["active_grants"] == 0
    assert unapproved["admission_ready"] is False

    with sqlite3.connect(database) as connection:
        connection.execute(
            "insert into evolution_grant_approval values(?,?,?,?,?,?,?,?)",
            (
                approval_digest,
                grant_digest,
                "owner-1",
                "realm-1",
                "device-1",
                "approver-1",
                now.isoformat(),
                (now + dt.timedelta(minutes=30)).isoformat(),
            ),
        )
        connection.commit()

    approved = _authority_schema_status(database, required_bindings=bindings, now=now)
    assert approved["active_grants"] == 1
    assert approved["admission_ready"] is True
    assert approved["current_grant_digests"] == (grant_digest,)


def test_authority_status_ignores_historical_scopes_and_revoked_current_grants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operational.db"
    bindings = {
        name: digest(name)
        for name in (
            "task_scope_digest",
            "policy_digest",
            "verifier_digest",
            "validator_digest",
            "source_lineage_digest",
            "protected_manifest_digest",
            "dependency_manifest_digest",
        )
    }
    now = dt.datetime(2026, 9, 7, 12, tzinfo=dt.UTC)

    def body(task_scope_digest: str) -> dict[str, object]:
        return {
            **bindings,
            "task_scope_digest": task_scope_digest,
            "owner_id": "owner-1",
            "realm_id": "realm-1",
            "device_id": "device-1",
            "state": "active",
            "valid_from": (now - dt.timedelta(minutes=1)).isoformat(),
            "expires_at": (now + dt.timedelta(hours=1)).isoformat(),
            "review_after": (now + dt.timedelta(minutes=30)).isoformat(),
            "model_refs": ["none"],
            "readable_resources": ["local-capture"],
            "writable_resources": ["local-report"],
            "budget": {"provider_calls": 0},
        }

    historical = body(digest("historical-scope"))
    revoked = body(str(bindings["task_scope_digest"]))
    with sqlite3.connect(database) as connection:
        connection.executescript(EVOLUTION_AUTHORITY_DDL)
        for grant_id, value in (("historical", historical), ("revoked", revoked)):
            grant_digest = digest(value)
            approval_digest = digest(f"approval-{grant_id}")
            connection.execute(
                "insert into evolution_standing_grant values(?,?,?,?,?,?)",
                (grant_digest, grant_id, 1, approval_digest, json.dumps(value), now.isoformat()),
            )
            connection.execute(
                "insert into evolution_grant_approval values(?,?,?,?,?,?,?,?)",
                (
                    approval_digest,
                    grant_digest,
                    "owner-1",
                    "realm-1",
                    "device-1",
                    "approver-1",
                    now.isoformat(),
                    (now + dt.timedelta(minutes=30)).isoformat(),
                ),
            )
            if grant_id == "revoked":
                connection.execute(
                    "insert into evolution_grant_revocation values(?,?,?)",
                    (grant_digest, "superseded", now.isoformat()),
                )
        connection.commit()

    status = _authority_schema_status(database, required_bindings=bindings, now=now)
    assert status["registered_grants"] == 2
    assert status["active_grants"] == 0
    assert status["drifted_grants"] == 0
    assert status["admission_ready"] is False


def test_resume_verifier_rejects_self_consistent_but_noncurrent_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_body = {"grant_digests": (digest("current-grant"),), "recovery_clear": True}
    current = current_body | {"evidence_digest": digest(current_body)}
    forged_body = {"grant_digests": (digest("forged-grant"),), "recovery_clear": True}
    forged = forged_body | {"evidence_digest": digest(forged_body)}
    monkeypatch.setattr(
        "zekam.application.evolution_runtime.build_resume_admission",
        lambda _context: current,
    )
    verifier = CurrentEvolutionResumeVerifier(object())  # type: ignore[arg-type]

    verifier.verify(current)
    with pytest.raises(PolicyViolation, match="current authority readback drift"):
        verifier.verify(forged)


def test_evolution_report_exposes_bounded_real_metric_values_and_usage(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    database = state / "improvement.db"
    candidate_digest = digest("candidate")
    evaluation_body = {
        "baseline_values": {"correctness": 0.7},
        "current_values": {"correctness": 0.9},
        "progress": {"progress_state": "improved"},
        "actual_provider_calls": 1,
        "actual_tokens": 42,
        "actual_cost_micros": 7,
    }
    evaluation_digest = digest(evaluation_body)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "create table improvement_candidate(candidate_digest text,change_class text,"
            "created_at text,failure_card_digest text,baseline_aggregate_digest text);"
            "create table improvement_evaluation(evaluation_digest text,candidate_digest text,"
            "after_aggregate_digest text,state text,finished_at text,body_json text);"
            "create table improvement_review(candidate_digest text,approved integer);"
        )
        connection.execute(
            "insert into improvement_candidate values(?,?,?,?,?)",
            (
                candidate_digest,
                "AUTO_SAFE",
                "2026-09-07T12:00:00+00:00",
                digest("failure"),
                digest("before"),
            ),
        )
        connection.execute(
            "insert into improvement_evaluation values(?,?,?,?,?,?)",
            (
                evaluation_digest,
                candidate_digest,
                digest("after"),
                "improved",
                "2026-09-07T12:01:00+00:00",
                json.dumps(evaluation_body, separators=(",", ":"), sort_keys=True),
            ),
        )
        connection.commit()
    zero_budget = {
        "provider_calls": 0,
        "tokens": 0,
        "duration_seconds": 0,
        "cost_micros": 0,
        "disk_bytes": 0,
        "concurrency": 0,
    }
    plan = {
        "state": "setup-required",
        "setup_gaps": ("native",),
        "blockers": (),
        "runtime": {"running_jobs": 0},
        "supervisor": {"state": "absent"},
        "permissions": {"budget_limits": zero_budget},
    }

    report = build_evolution_report(build_context(home=home), plan=plan)

    pair = report["evaluation_pairs"][0]
    assert pair["baseline_values"] == {"correctness": 0.7}
    assert pair["current_values"] == {"correctness": 0.9}
    assert pair["progress"] == {"progress_state": "improved"}
    assert pair["actual_usage"] == {
        "provider_calls": 1,
        "tokens": 42,
        "cost_micros": 7,
    }
    assert report["actual_usage"]["evaluations"] == pair["actual_usage"]
    assert report["candidates"][0]["risk"] == "AUTO_SAFE"
    assert report["candidates"][0]["blockers"] == ()
