from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.capability_profile import CapabilityProfile, Detection
from zekam.application.project_routing_targets import (
    load_project_routing_targets,
    workloads_for_profile,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_routing import AgentRole, RouteCapabilityDimension


def _requirements_yaml() -> str:
    body = "capability_requirements:\n"
    for role in AgentRole:
        body += (
            f"  {role.value}:\n"
            f"    evidence_role: {role.value}\n"
            "    minimum_context_tokens: 1024\n"
            "    minimum_tool_score: 0.5\n"
            "    minimum_structured_output_score: 0.75\n"
            "    minimum_long_session_seconds: 30\n"
            "    minimum_long_session_score: 0.7\n"
        )
    return body


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        generator_version="test/v1",
        languages=(("java", 10),),
        build_systems=(),
        frameworks=(Detection("spring", "pom.xml", "dependency"),),
        test_frameworks=(Detection("junit", "pom.xml", "dependency"),),
        databases=(Detection("oracle", "pom.xml", "dependency"),),
        quality_tools=(),
        security_tools=(),
        continuous_integration=(),
        containers=(),
        modules=(),
        file_count=10,
        total_bytes=100,
    )


def test_default_targets_are_exact_requested_eight() -> None:
    targets = load_project_routing_targets()
    assert targets.projects == (
        "gpu-fusion",
        "plsql-java-transformer",
        "plsql-test-sync",
        "utplsql",
        "schema-compare-platform",
        "schema-transform-platform",
        "sky-microservis",
        "sky-ui",
    )
    assert targets.sanitized()["target_count"] == 8
    assert targets.requirements_for(AgentRole.IMPLEMENTER).required_dimensions == tuple(
        RouteCapabilityDimension
    )
    assert targets.evidence_role_for(AgentRole.VERIFIER) is AgentRole.REVIEWER


def test_workloads_are_derived_from_profile_evidence() -> None:
    assert workloads_for_profile(_profile()) == ("java", "junit", "oracle", "project", "spring")


def test_duplicate_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "schema: zekam-project-routing-targets/v2\n"
        "projects: [gpu-fusion, gpu-fusion]\n" + _requirements_yaml(),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="tekrarli"):
        load_project_routing_targets(path)


def test_missing_reviewed_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "schema: zekam-project-routing-targets/v2\n"
        "projects: [gpu-fusion, plsql-java-transformer, plsql-test-sync, utplsql, "
        "schema-compare-platform, schema-transform-platform, sky-microservis]\n"
        + _requirements_yaml(),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="exact sekiz"):
        load_project_routing_targets(path)
