"""Versioned registry -> project suite -> plan -> aggregate baglanti testi."""

from __future__ import annotations

import pytest

from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.domain.canonical import digest
from zekam.domain.model_benchmark import BenchmarkPlan, build_project_suite

pytestmark = pytest.mark.integration


def test_project_suite_is_capability_digest_bound() -> None:
    registry = load_fixture_registry()
    first_digest = digest({"profile": "python"})
    second_digest = digest({"profile": "go"})
    first = build_project_suite(
        project_id="zekam", capability_profile_digest=first_digest, registry=registry
    )
    second = build_project_suite(
        project_id="zekam", capability_profile_digest=second_digest, registry=registry
    )
    assert first.fixture_digests
    assert first.suite_digest != second.suite_digest
    plan = BenchmarkPlan(
        "model-a",
        first.suite_digest,
        digest({"inventory": 1}),
        digest({"policy": 1}),
        registry.registry_digest,
    )
    assert plan.plan_digest.startswith("sha256:")


def test_remote_registry_excludes_local_only_cases() -> None:
    registry = load_fixture_registry()
    remote = registry.eligible(remote=True)
    assert len(remote) < len(registry.fixtures)
    assert all(item.execution_eligibility.value == "remote-allowed" for item in remote)
