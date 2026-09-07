from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from zekam.application.composition import build_context
from zekam.application.evolution_bootstrap import (
    LOCAL_GRANT_OPERATIONS,
    EvolutionBootstrapPlan,
    _assert_current_bootstrap_claim,
    _grant_from_body,
    _load_intent,
    _persist_intent,
    apply_evolution_bootstrap,
    build_evolution_bootstrap_plan,
)
from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalSchemaStatus
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_file_security import restrict_private_file
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap, status
from zekam.interfaces.cli import evolve as evolve_cli


def _bindings() -> dict[str, str]:
    return {
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


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvolutionBootstrapPlan:
    project_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    realm_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    supervisor_digest = digest("supervisor")
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.operational_status",
        lambda _path: OperationalSchemaStatus(True, 3, True, True),
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.build_evolution_plan",
        lambda _context: {
            "admission_bindings": _bindings(),
            "supervisor": {"plan_digest": supervisor_digest},
        },
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap._operational_inventory",
        lambda _path: ((project_id, "gpu-fusion"),),
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap._realm_scope",
        lambda _path, _projects: (realm_id, (project_id,)),
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.logical_database_digest",
        lambda _path: digest("operational-v3"),
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap._control_precondition",
        lambda _context: {"state": "observing", "ordinal": 0, "event_digest": None},
    )
    return build_evolution_bootstrap_plan(
        build_context(home=tmp_path),
        now=dt.datetime.now(dt.UTC),
    )


def test_bootstrap_plan_is_narrow_provider_free_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    document = plan.as_dict()

    assert document["apply"] is False
    assert document["authorization_required"] is True
    assert document["grants_authority"] is False
    assert document["provider_calls"] == 0
    assert document["network_scope"] == "none"
    assert document["grant"]["operations"] == list(LOCAL_GRANT_OPERATIONS)
    assert document["grant"]["provider_refs"] == ["local-deterministic"]
    assert document["grant"]["model_refs"] == ["none"]
    assert document["grant"]["budget"]["provider_calls"] == 0
    assert document["grant"]["budget"]["tokens"] == 0
    assert document["operational"]["migration_required"] is True
    assert document["operational"]["realm_bindings_to_add"] == [
        "11111111-1111-4111-8111-111111111111"
    ]
    assert not (tmp_path / "runtime").exists()


def test_bootstrap_intent_roundtrip_survives_schema_transition_and_rejects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    _persist_intent(build_context(home=tmp_path), plan)

    loaded = _load_intent(build_context(home=tmp_path), plan.plan_digest)
    assert loaded is not None
    assert loaded.plan_digest == plan.plan_digest
    assert loaded.grant.grant_digest == plan.grant.grant_digest
    assert _grant_from_body(loaded.as_dict()["grant"]).grant_digest == plan.grant.grant_digest

    path = tmp_path / "runtime" / "evolution-bootstrap" / f"{plan.plan_digest[7:]}.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="digest binding"):
        _load_intent(build_context(home=tmp_path), plan.plan_digest)


def test_bootstrap_intent_requires_exact_authorized_digest(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="digest invalid"):
        _load_intent(build_context(home=tmp_path), "not-a-digest")


def test_enable_apply_reaches_persisted_intent_without_rebuilding_fresh_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = digest("authorized-bootstrap")
    monkeypatch.setattr(evolve_cli, "build_context", lambda home=None: object())

    def reject_fresh_plan(_context: object) -> EvolutionBootstrapPlan:
        raise AssertionError("apply retry fresh plan uretmemeli")

    monkeypatch.setattr(evolve_cli, "build_evolution_bootstrap_plan", reject_fresh_plan)
    monkeypatch.setattr(
        evolve_cli,
        "apply_evolution_bootstrap",
        lambda _context, *, authorized_plan_digest: {
            "plan_digest": authorized_plan_digest,
            "state": "already-applied",
        },
    )
    rendered: list[object] = []
    monkeypatch.setattr(evolve_cli.console, "print_json", rendered.append)

    evolve_cli.enable_command(apply=True, plan_digest=expected, home=str(tmp_path))

    assert rendered and expected in str(rendered[0])


def test_bootstrap_claim_is_append_only_and_exact_plan_bound(tmp_path: Path) -> None:
    store = SQLiteLocalImprovementStore(
        (tmp_path / "state" / "improvement.db").resolve(),
        (tmp_path / "state" / "learning.db").resolve(),
        (tmp_path / "benchmark" / "benchmark.db").resolve(),
    )
    store.bootstrap()
    plan_digest = digest("bootstrap-plan")

    first = store.set_evolution_control_state(
        "paused",
        reason="bootstrap-claim",
        now=dt.datetime(2026, 9, 7, 12, tzinfo=dt.UTC),
        enable_plan_digest=plan_digest,
    )
    assert first["enable_plan_digest"] == plan_digest
    claim_digest = str(first["event_digest"])
    _assert_current_bootstrap_claim(store, plan_digest, claim_digest)
    disabled = store.set_evolution_control_state(
        "disabled",
        reason="owner-disable",
        now=dt.datetime(2026, 9, 7, 12, 1, tzinfo=dt.UTC),
    )
    assert disabled["ordinal"] == 2
    assert disabled["event_digest"] != first["event_digest"]
    with pytest.raises(PolicyViolation, match="claim no longer current"):
        _assert_current_bootstrap_claim(store, plan_digest, claim_digest)


def test_expired_bootstrap_approval_is_rejected_before_durable_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    HomeLayout(home).ensure()
    improvement = SQLiteLocalImprovementStore(
        home / "state" / "improvement.db",
        home / "state" / "learning.db",
        home / "benchmarklar" / "benchmark.db",
    )
    improvement.bootstrap()
    plan = _plan(home, monkeypatch)
    approval = plan.body["approval_receipt"]
    assert isinstance(approval, dict)
    approval["issued_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
    approval["expires_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    plan.body["approval_receipt_digest"] = digest(approval)
    _persist_intent(build_context(home=home), plan)

    with pytest.raises(PolicyViolation, match="approval current degil"):
        apply_evolution_bootstrap(
            build_context(home=home), authorized_plan_digest=plan.plan_digest
        )

    assert improvement.evolution_control_status()["ordinal"] == 0


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows native bootstrap")
def test_full_bootstrap_apply_and_same_intent_replay_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    HomeLayout(home).ensure()
    database = home / "state" / "operational.db"
    bootstrap(database)
    project_id = "11111111-1111-4111-8111-111111111111"
    now = dt.datetime(2026, 9, 7, 12, tzinfo=dt.UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "insert into project(id,slug,display_name,source_ref,created_at,status,revision) "
            "values(?,?,?,null,?,'active',1)",
            (project_id, "gpu-fusion", "GPU Fusion", now),
        )
        connection.commit()
    SQLiteLocalRuntimeStore(database)
    restrict_private_file(database)
    improvement = SQLiteLocalImprovementStore(
        home / "state" / "improvement.db",
        home / "state" / "learning.db",
        home / "benchmarklar" / "benchmark.db",
    )
    improvement.bootstrap()
    plan = _plan(home, monkeypatch)
    _persist_intent(build_context(home=home), plan)
    monkeypatch.setattr("zekam.application.evolution_bootstrap.operational_status", status)

    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.build_windows_supervisor_plan",
        lambda _context: SimpleNamespace(plan_digest=plan.body["supervisor_plan_digest"]),
    )
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.install_windows_task",
        lambda scheduler_plan, *, authorized_plan_digest: {
            "state": "already-installed",
            "plan_digest": authorized_plan_digest,
        },
    )
    evidence_body = {
        "task_scope_digest": digest("task"),
        "config_digest": digest("config"),
        "implementation_digest": digest("implementation"),
        "supervisor_status_digest": digest("supervisor-status"),
        "grant_binding_digest": digest("grant-binding"),
        "grant_digests": [plan.grant.grant_digest],
        "recovery_clear": True,
    }
    evidence = {"evidence_digest": digest(evidence_body), **evidence_body}
    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.build_resume_admission", lambda _context: evidence
    )

    class _Verifier:
        def __init__(self, _context: object) -> None:
            pass

        def verify(self, _evidence: object) -> None:
            pass

    monkeypatch.setattr(
        "zekam.application.evolution_bootstrap.CurrentEvolutionResumeVerifier", _Verifier
    )

    first = apply_evolution_bootstrap(
        build_context(home=home), authorized_plan_digest=plan.plan_digest
    )
    second = apply_evolution_bootstrap(
        build_context(home=home), authorized_plan_digest=plan.plan_digest
    )

    assert first["migration"]["state"] == "migrated"
    assert second["migration"]["state"] == "migrated"
    assert second["receipt_digest"] == first["receipt_digest"]
    assert status(database).schema_version == 5
    with sqlite3.connect(database) as connection:
        grant_count = connection.execute(
            "select count(*) from evolution_standing_grant"
        ).fetchone()
        assert grant_count == (1,)
        assert connection.execute(
            "select realm_id from project_knowledge_realm where project_id=?", (project_id,)
        ).fetchone() == ("22222222-2222-4222-8222-222222222222",)
    control = improvement.evolution_control_status()
    assert control["state"] == "observing"
    assert control["reason"] == "bootstrap-complete"
    assert control["enable_plan_digest"] == plan.plan_digest
    improvement.set_evolution_control_state(
        "disabled",
        reason="owner-disable",
        now=dt.datetime.now(dt.UTC),
    )
    with pytest.raises(PolicyViolation, match="owner control decision"):
        apply_evolution_bootstrap(
            build_context(home=home), authorized_plan_digest=plan.plan_digest
        )
