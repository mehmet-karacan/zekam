"""Independent real-input startup/environment composition tests, not checkpoint acceptance."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_environment import AKILLI_SOURCE, Fixture
from tests.unit.test_local_continuity_environment import environment as environment

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService, StartupRequest
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.context_continuity import ContextCandidateKind
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

SOURCE_ROOT = AKILLI_SOURCE.parents[3]
SOURCE_REF = AKILLI_SOURCE.relative_to(SOURCE_ROOT).as_posix()
NOW = dt.datetime(2026, 9, 2, 18, tzinfo=dt.UTC)


def _request() -> StartupRequest:
    return StartupRequest((SOURCE_REF,), 16384, "startup-environment-integration", NOW)


def _compose(
    gate: LocalContinuityEnvironment, binding: ContinuityBinding, *, guarded: bool = True
) -> dict[str, Any]:
    base = SQLiteContinuityStore(gate.operational_path)
    project_sources = ProjectContinuitySourceResolver(
        SOURCE_ROOT,
        project_id=binding.project_id,
        realm_id=binding.realm_id,
        source_snapshot_id=binding.source_snapshot_id,
        allowed_paths=(SOURCE_REF,),
    )
    sources = SQLiteStartupSourceResolver(
        base, project_sources, environment=gate if guarded else None
    )
    base.source_resolver = sources
    spool = ClientLifecycleSpool(gate.home, client_id=binding.client_id)
    lifecycle = LocalLifecycleContinuity(
        base,
        spool,
        binding,
        source_probe=lambda: digest_of_bytes(AKILLI_SOURCE.read_bytes()),
        entry_validator=validate_reviewed_control_entry,
    )
    return {
        "gate": gate,
        "binding": binding,
        "base": base,
        "project_sources": project_sources,
        "sources": sources,
        "spool": spool,
        "lifecycle": lifecycle,
        "service": LocalStartupService(lifecycle, sources),
    }


@pytest.fixture
def integrated(environment: Fixture, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    gate, original_binding = environment
    source_before = AKILLI_SOURCE.read_bytes()
    task_before = gate.task_path.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Startup environment integration invoked network/provider access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    payload = {
        "summary": f"Inspect the real Akilli Kasa health endpoint in {SOURCE_REF}.",
        "acceptance_criteria": ["Evidence cites the exact allowed health endpoint source."],
    }
    with sqlite3.connect(gate.operational_path) as db:
        config_id = db.execute("select id from config_revision where active=1").fetchone()[0]
    with SQLiteOperationalStore(gate.operational_path).unit_of_work() as uow:
        work = uow.create_work(
            project_id=original_binding.project_id,
            kind="task",
            title="Real startup environment integration",
            state="ready",
            payload=payload,
            payload_digest=digest(payload),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config_id,
            source_snapshot_id=original_binding.source_snapshot_id,
            plan_digest=digest("startup-environment-integration-plan"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    binding = replace(
        original_binding,
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
        work_item_id=work.id,
        run_id=run.id,
        plan_digest=run.plan_digest,
    )
    SQLiteContinuityStore(gate.operational_path).bind_session(binding)
    value = _compose(gate, binding)
    parsed = parse_codex_hook_input(
        canonical_json(
            {
                "session_id": binding.external_session_id,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    )
    entry = value["spool"].stage(
        parsed.observation_body(),
        delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
        occurred_at=NOW,
    )
    validate_reviewed_control_entry(entry)
    assert value["lifecycle"].drain() == 1
    value["entry"] = entry
    yield value
    assert AKILLI_SOURCE.read_bytes() == source_before
    assert gate.task_path.read_bytes() == task_before


def _receipts(value: dict[str, Any]) -> tuple[int, int]:
    with sqlite3.connect(value["gate"].operational_path) as db:
        return (
            db.execute("select count(*) from context_manifest").fetchone()[0],
            db.execute("select count(*) from hydration_receipt").fetchone()[0],
        )


def _change_actual_config(gate: LocalContinuityEnvironment) -> None:
    config = gate.home / "config.yaml"
    config.write_bytes(config.read_bytes() + b"runtime:\n  log_level: DEBUG\n")


def test_actual_environment_typed_start_four_fragments_receipt_and_timing(
    integrated: dict[str, Any], record_property: Callable[[str, object], None]
) -> None:
    gate, binding = integrated["gate"], integrated["binding"]
    assert gate.operational_path == gate.home / "state/operational.db"
    assert gate.task_path.name == "AKTIF_GOREV.md"
    snapshot = integrated["sources"].snapshot(binding, _request())
    assert {item.kind for item in snapshot.candidates} == {
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.RUN_STATUS,
        ContextCandidateKind.SOURCE_SLICE,
    }
    source = next(
        item for item in snapshot.candidates if item.kind is ContextCandidateKind.SOURCE_SLICE
    )
    assert source.source_ref == SOURCE_REF
    assert dict(snapshot.fragments)[source.candidate_id] == AKILLI_SOURCE.read_text(
        encoding="utf-8"
    )
    started = time.perf_counter()
    result = integrated["service"].hydrate(_request())
    elapsed = time.perf_counter() - started
    record_property("successful_guarded_hydration_wall_seconds", elapsed)
    assert result["selected_count"] == 4
    assert result["token_count"] <= _request().token_budget
    assert result["environment"]["status"] == "validated"
    assert result["environment"]["task_digest"] == digest_of_bytes(gate.task_path.read_bytes())
    assert result["environment"]["policy_digest"] == binding.policy_digest
    assert result["environment"]["binding_digest"] == binding.binding_digest
    assert "home-config-composition" not in result["remaining_gates"]
    assert "prior-checkpoint" in result["remaining_gates"]
    assert result["learned_state"] == "not-implemented"
    assert result["grants_authority"] is result["provider_called"] is False
    assert _receipts(integrated) == (1, 1)
    with sqlite3.connect(gate.operational_path) as db:
        row = db.execute(
            "select h.manifest_digest,m.body_json,m.checkpoint_digest"
            " from hydration_receipt h join context_manifest m"
            " on m.manifest_digest=h.manifest_digest where h.session_id=?",
            (binding.session_id,),
        ).fetchone()
        assert row[0] == result["manifest_digest"]
        body = json.loads(row[1])
        assert body["binding_digest"] == binding.binding_digest
        assert row[2] is None  # This scope does not silently invent a prior checkpoint.
        start = db.execute(
            "select e.event_kind,d.spool_digest from session_event e"
            " join session_event_detail d on d.event_id=e.id where e.session_id=?",
            (binding.session_id,),
        ).fetchone()
        assert tuple(start) == ("SESSION_START", integrated["entry"].entry_digest)


def test_exact_replay_and_on_disk_reopen_do_not_duplicate_receipts(
    integrated: dict[str, Any],
) -> None:
    first = integrated["service"].hydrate(_request())
    assert integrated["service"].hydrate(_request()) == first
    reopened = _compose(integrated["gate"], integrated["binding"])
    assert reopened["service"].hydrate(_request()) == first
    assert _receipts(integrated) == (1, 1)


def test_subprocess_restart_reads_real_existing_home_and_replays(
    integrated: dict[str, Any],
) -> None:
    first = integrated["service"].hydrate(_request())
    gate = integrated["gate"]
    payload = {
        "home": str(gate.home),
        "core": str(gate.core_root),
        "task": str(gate.task_path),
        "db": str(gate.operational_path),
        "binding": asdict(integrated["binding"]),
    }
    script = """
import json, socket, sys
from pathlib import Path
from tests.unit.test_local_startup_environment_integration import _compose, _request
from zekam.application.local_continuity import ContinuityBinding
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
def forbidden(*args, **kwargs):
    raise AssertionError('provider/network forbidden')
socket.socket.connect = forbidden
socket.create_connection = forbidden
p = json.loads(sys.argv[1])
gate = LocalContinuityEnvironment(*(Path(p[k]) for k in ('home','core','task','db')))
value = _compose(gate, ContinuityBinding(**p['binding']))
print(json.dumps(value['service'].hydrate(_request())))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        cwd=gate.core_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(process.stdout) == first
    assert _receipts(integrated) == (1, 1)


def test_actual_config_drift_after_final_assert_current_blocks_hydration(
    integrated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = integrated["sources"].assert_current
    before = logical_database_digest(integrated["gate"].operational_path)
    checked = False

    def race(*args: Any, **kwargs: Any) -> None:
        nonlocal checked
        original(*args, **kwargs)
        checked = True
        _change_actual_config(integrated["gate"])

    monkeypatch.setattr(integrated["sources"], "assert_current", race)
    with pytest.raises(ConfigurationError, match="actual settings digest drift"):
        integrated["service"].hydrate(_request())
    assert checked
    assert _receipts(integrated) == (0, 0)
    assert logical_database_digest(integrated["gate"].operational_path) == before


def test_actual_config_drift_after_manifest_insert_rolls_back_manifest_and_receipt(
    integrated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    base: SQLiteContinuityStore = integrated["base"]
    original = base._verified_manifest
    before = logical_database_digest(integrated["gate"].operational_path)
    saw_uncommitted_manifest = False

    def race(db: sqlite3.Connection, binding: ContinuityBinding, manifest: str) -> dict[str, Any]:
        nonlocal saw_uncommitted_manifest
        assert db.in_transaction
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1
        saw_uncommitted_manifest = True
        _change_actual_config(integrated["gate"])
        return original(db, binding, manifest)

    monkeypatch.setattr(base, "_verified_manifest", race)
    with pytest.raises(ConfigurationError, match="actual settings digest drift"):
        integrated["service"].hydrate(_request())
    assert saw_uncommitted_manifest
    assert _receipts(integrated) == (0, 0)
    assert logical_database_digest(integrated["gate"].operational_path) == before


def test_different_environment_database_path_is_rejected_at_composition(
    integrated: dict[str, Any], tmp_path: Path
) -> None:
    other = replace(integrated["gate"], operational_path=tmp_path / "other.db")
    with pytest.raises(ValidationFailed, match="exact operational path"):
        SQLiteStartupSourceResolver(
            integrated["base"], integrated["project_sources"], environment=other
        )
    assert not (tmp_path / "other.db").exists()
    assert _receipts(integrated) == (0, 0)


@pytest.mark.parametrize("mode", ["missing-config", "missing-home", "legacy-layout", "legacy-pg"])
def test_environment_failure_precedes_source_probe_and_spool_access(
    integrated: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    gate = integrated["gate"]
    source = integrated["sources"]
    if mode == "missing-config":
        (gate.home / "config.yaml").rename(gate.home / "config.saved")
    elif mode == "missing-home":
        source.environment = replace(gate, home=tmp_path / "missing-home")
    elif mode == "legacy-layout":
        layout = json.loads((gate.home / "layout.json").read_text())
        layout["schema"] = "zekam-home-layout/v1"
        (gate.home / "layout.json").write_text(json.dumps(layout))
    else:
        (gate.home / "config.yaml").write_text(
            "schema: zekam-config/v1\ndatabase:\n  backend: postgresql\n"
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid environment reached source callback or spool barrier")

    monkeypatch.setattr(integrated["lifecycle"], "source_probe", forbidden)
    monkeypatch.setattr(integrated["spool"], "frozen_session_entries", forbidden)
    monkeypatch.setattr(integrated["project_sources"], "read_fragment", forbidden)
    before = logical_database_digest(gate.operational_path)
    with pytest.raises(ConfigurationError):
        integrated["service"].hydrate(_request())
    assert _receipts(integrated) == (0, 0)
    assert logical_database_digest(gate.operational_path) == before
    assert not (tmp_path / "missing-home").exists()


@pytest.mark.parametrize(
    "field",
    ["project_id", "realm_id", "work_item_id", "run_id", "source_snapshot_id", "session_id"],
)
def test_foreign_binding_cannot_hydrate_with_valid_environment(
    integrated: dict[str, Any], field: str
) -> None:
    integrated["lifecycle"].binding = replace(integrated["binding"], **{field: str(uuid4())})
    with pytest.raises((PolicyViolation, ValidationFailed)):
        integrated["service"].hydrate(_request())
    assert _receipts(integrated) == (0, 0)


def test_environment_none_retains_explicit_partial_scope(integrated: dict[str, Any]) -> None:
    partial = _compose(integrated["gate"], integrated["binding"], guarded=False)
    result = partial["service"].hydrate(_request())
    assert result["environment"] is None
    assert "home-config-composition" in result["remaining_gates"]
    assert "prior-checkpoint" in result["remaining_gates"]
    assert _receipts(integrated) == (1, 1)


def test_typed_start_is_required_even_with_valid_environment(
    integrated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrated["lifecycle"], "entry_validator", None)
    with pytest.raises(PolicyViolation, match="reviewed lifecycle decoder"):
        integrated["service"].hydrate(_request())
    assert _receipts(integrated) == (0, 0)
