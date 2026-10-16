"""Offline lifecycle cases: admission failure, claim-before-write and replay."""

from __future__ import annotations

import sqlite3

import pytest

import zekam.application.research_campaign_runtime as campaign_module
from tests.unit.test_research_campaign import FakeGitHubAdapter, _plan, runtime  # noqa: F401
from zekam.application.research_campaign_runtime import run_radar_campaign
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


def _run(home, store, plan, adapter):
    return run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )


def _queue(home):
    return SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)


def test_failed_binding_admission_cannot_create_campaign_store(runtime, monkeypatch):
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()

    def reject(*_args, **_kwargs):
        raise PolicyViolation("binding-rejected")

    monkeypatch.setattr(campaign_module, "validate_dispatch_binding", reject)
    with pytest.raises(PolicyViolation, match="binding-rejected"):
        _run(home, store, plan, adapter)
    assert not (home / "state" / "radar-campaigns.db").exists()
    assert _queue(home).job_snapshot(plan.idempotency_key) is None
    assert adapter.calls == []


def test_schema_failure_is_after_effect_claim_and_requires_recovery(runtime, monkeypatch):
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()

    def failing_store(_path, **_kwargs):
        snapshot = _queue(home).job_snapshot(plan.idempotency_key)
        assert snapshot["state"] == "running" and len(snapshot["effects"]) == 1
        assert snapshot["effects"][0]["operation"] == "research.radar"
        raise ValidationFailed("schema-install-failed")

    monkeypatch.setattr(campaign_module, "RadarCampaignRepository", failing_store)
    with pytest.raises(ValidationFailed, match="schema-install-failed"):
        _run(home, store, plan, adapter)
    snapshot = _queue(home).job_snapshot(plan.idempotency_key)
    assert snapshot["state"] == "recovery-required"
    assert snapshot["effects"][0]["receipt_id"] is None
    assert adapter.calls == []


def test_completed_replay_does_not_recreate_missing_campaign_store(runtime):
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    _run(home, store, plan, adapter)
    adapter.calls.clear()
    path = home / "state" / "radar-campaigns.db"
    path.unlink()
    with pytest.raises(PolicyViolation, match="store eksik"):
        _run(home, store, plan, adapter)
    assert not path.exists() and adapter.calls == []


@pytest.mark.parametrize("corruption", ["claim", "terminal", "campaign-plan"])
def test_completed_replay_rejects_broken_canonical_join(runtime, corruption):
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    _run(home, store, plan, adapter)
    adapter.calls.clear()
    path = home / "state" / "radar-campaigns.db"
    with sqlite3.connect(path) as db:
        if corruption == "claim":
            db.execute("update radar_campaign set effect_claim_id='wrong'")
        elif corruption == "terminal":
            # Simulate a campaign row written before canonical settlement.
            db.execute("update radar_campaign set state='running'")
        else:
            db.execute("update radar_campaign set plan_json='{}'")
    before = path.read_bytes()
    with pytest.raises(PolicyViolation, match="canonical claim/terminal/campaign drift"):
        _run(home, store, plan, adapter)
    assert path.read_bytes() == before and adapter.calls == []


def test_successful_replay_reads_same_actual_receipt_without_dispatch(runtime):
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    first = _run(home, store, plan, adapter)
    adapter.calls.clear()
    second = _run(home, store, plan, adapter)
    assert second["replayed"] is True
    assert second["receipt"] == first["receipt"]
    assert adapter.calls == []
