from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.capability_profile import CapabilityProfile, Detection
from zekam.application.project_routing_targets import (
    load_project_routing_targets,
    workloads_for_profile,
)
from zekam.domain.errors import ValidationFailed


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


def test_workloads_are_derived_from_profile_evidence() -> None:
    assert workloads_for_profile(_profile()) == ("java", "junit", "oracle", "project", "spring")


def test_duplicate_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "schema: zekam-project-routing-targets/v1\nprojects: [gpu-fusion, gpu-fusion]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="tekrarli"):
        load_project_routing_targets(path)


def test_missing_reviewed_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "schema: zekam-project-routing-targets/v1\n"
        "projects: [gpu-fusion, plsql-java-transformer, plsql-test-sync, utplsql, "
        "schema-compare-platform, schema-transform-platform, sky-microservis]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="exact sekiz"):
        load_project_routing_targets(path)
