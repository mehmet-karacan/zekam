from __future__ import annotations

from uuid import uuid4

import pytest

from zekam.application.capability_profile import CapabilityProfile, Detection
from zekam.application.project_routing_context import build_project_routing_evidence
from zekam.application.source_discovery import (
    DiscoveredFile,
    DiscoveryPolicy,
    DiscoveryReport,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation

PROJECT_SUITE_DIGEST = digest("project-suite")


def _profile() -> CapabilityProfile:
    framework = Detection("react", "package.json", "dependency")
    return CapabilityProfile(
        generator_version="test/v1",
        languages=(("typescript", 2),),
        build_systems=(Detection("npm", "package.json", "manifest"),),
        frameworks=(framework,),
        test_frameworks=(Detection("vitest", "package.json", "dependency"),),
        databases=(),
        quality_tools=(),
        security_tools=(),
        continuous_integration=(),
        containers=(),
        modules=("src",),
        file_count=5,
        total_bytes=500,
    )


def _report(*, lock_digest: str | None = None, truncated: bool = False) -> DiscoveryReport:
    files = (
        DiscoveredFile("AGENTS.md", 10, digest("rules"), True),
        DiscoveredFile("README.md", 10, digest("architecture"), True),
        DiscoveredFile("package.json", 10, digest("dependencies"), True),
        DiscoveredFile("pnpm-lock.yaml", 10, lock_digest or digest("lock-v1"), True),
        DiscoveredFile("src/index.ts", 10, digest("source"), True),
    )
    return DiscoveryReport(
        root_label="project",
        tree_digest=digest([item.as_dict() for item in files]),
        files=files,
        skipped=(),
        secrets=(),
        policy=DiscoveryPolicy(),
        truncated=truncated,
        extensions={"ts": 1},
    )


def test_context_is_deterministic_secret_free_and_source_bound() -> None:
    project_id = uuid4()
    source_revision_id = uuid4()
    first = build_project_routing_evidence(
        project_id=project_id,
        source_revision_id=source_revision_id,
        source_revision="abc123",
        report=_report(),
        profile=_profile(),
        workloads=("React", "TypeScript"),
        project_suite_digest=PROJECT_SUITE_DIGEST,
    )
    second = build_project_routing_evidence(
        project_id=project_id,
        source_revision_id=source_revision_id,
        source_revision="abc123",
        report=_report(),
        profile=_profile(),
        workloads=("typescript", "react"),
        project_suite_digest=PROJECT_SUITE_DIGEST,
    )
    assert first.context_digest == second.context_digest
    assert first.sanitized()["source_content"] is False
    assert not {"raw_content", "secret", "endpoint"}.intersection(first.sanitized())
    assert first.project_suite_digest.startswith("sha256:")


def test_lock_drift_changes_context_and_suite_stays_source_bound() -> None:
    project_id = uuid4()
    source_revision_id = uuid4()
    first = build_project_routing_evidence(
        project_id=project_id,
        source_revision_id=source_revision_id,
        source_revision="abc123",
        report=_report(),
        profile=_profile(),
        workloads=("react",),
        project_suite_digest=PROJECT_SUITE_DIGEST,
    )
    second = build_project_routing_evidence(
        project_id=project_id,
        source_revision_id=source_revision_id,
        source_revision="abc123",
        report=_report(lock_digest=digest("lock-v2")),
        profile=_profile(),
        workloads=("react",),
        project_suite_digest=PROJECT_SUITE_DIGEST,
    )
    assert first.dependency_lock_digest != second.dependency_lock_digest
    assert first.context_digest != second.context_digest


def test_truncated_discovery_is_rejected() -> None:
    with pytest.raises(PolicyViolation, match="Eksik source discovery"):
        build_project_routing_evidence(
            project_id=uuid4(),
            source_revision_id=uuid4(),
            source_revision="abc123",
            report=_report(truncated=True),
            profile=_profile(),
            workloads=("react",),
            project_suite_digest=PROJECT_SUITE_DIGEST,
        )
