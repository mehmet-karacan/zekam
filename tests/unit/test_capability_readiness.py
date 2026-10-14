"""WP-11.05: core health and capability readiness stay separate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zekam.application import capability_inventory as inventory

pytestmark = pytest.mark.unit


def test_provider_free_readiness_does_not_promote_declared_caps_to_ready() -> None:
    report = inventory.capability_readiness(root=Path(__file__).resolve().parents[2])

    assert report["provider_calls"] == 0
    assert report["network_calls"] == 0
    assert report["read_only"] is True
    assert report["core_health_independent"] is True
    assert report["all_ready"] is False
    assert all(
        {"status", "evidence_state", "permission_gap", "installation_gap"}
        <= set(item)
        for item in report["capabilities"]
    )


def test_permission_and_installation_gaps_are_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    capability = inventory.Capability(
        capability_id="opencode-continuity",
        title="Fixture",
        status="ready",
        surfaces=("fixture",),
        agent_roles=(),
        verified_by=("fixture.py",),
    )
    monkeypatch.setattr(inventory, "CAPABILITIES", (capability,))
    monkeypatch.setattr(inventory.shutil, "which", lambda _command: None)
    settings = SimpleNamespace(
        permission_profile=SimpleNamespace(allowed_capabilities=("filesystem.read",)),
        clients=(),
    )

    report = inventory.capability_readiness(root=Path.cwd(), settings=settings)
    item = report["capabilities"][0]

    assert "process.run" in item["permission_gap"]
    assert item["installation_gap"] == ["executable:opencode"]
    assert item["permission_gap"] != item["installation_gap"]


def test_inventory_keeps_static_status_and_exposes_dynamic_readiness() -> None:
    document = inventory.capability_inventory(root=Path(__file__).resolve().parents[2])

    for item in document["capabilities"]:
        assert item["status"] in {"ready", "partial", "scaffold"}
        assert item["readiness_status"] in {"ready", "partial", "blocked"}
        assert item["readiness"]["capability_id"] == item["capability_id"]
        assert item["readiness"]["grants_authority"] is False
