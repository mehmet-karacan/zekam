"""Capability profilinin deterministikligi ve kanit zorunlulugu."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.capability_profile import (
    PROFILER_VERSION,
    build_profile,
    profile_from_mapping,
)
from zekam.application.source_discovery import discover

pytestmark = pytest.mark.unit


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")


@pytest.fixture
def python_project(tmp_path: Path) -> Path:
    root = tmp_path / "py-proje"
    _write(
        root,
        "pyproject.toml",
        '[project]\nname = "ornek"\ndependencies = ["fastapi>=0.1", "psycopg[binary]>=3"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
    )
    _write(root, "src/ornek/__init__.py", "")
    _write(root, "src/ornek/api.py", "from fastapi import FastAPI\n")
    _write(root, "tests/test_api.py", "def test_api(): pass\n")
    _write(root, "Dockerfile", "FROM python:3.12\n")
    _write(root, ".github/workflows/ci.yml", "name: ci\n")
    _write(root, "mypy.ini", "[mypy]\n")
    return root


def _profile(root: Path):  # type: ignore[no-untyped-def]
    return build_profile(root, discover(root))


def test_generator_version_is_recorded(python_project: Path) -> None:
    assert _profile(python_project).generator_version == PROFILER_VERSION


def test_same_source_produces_same_digest(python_project: Path) -> None:
    assert _profile(python_project).digest == _profile(python_project).digest


def test_changed_dependency_changes_digest(python_project: Path) -> None:
    before = _profile(python_project).digest
    _write(
        python_project,
        "pyproject.toml",
        '[project]\nname = "ornek"\ndependencies = ["django>=5"]\n',
    )
    assert _profile(python_project).digest != before


def test_build_system_is_detected_with_evidence(python_project: Path) -> None:
    build_systems = _profile(python_project).build_systems
    assert [item.identifier for item in build_systems] == ["python-pyproject"]
    assert build_systems[0].evidence_path == "pyproject.toml"
    assert build_systems[0].evidence_kind == "manifest"


def test_framework_and_database_are_detected(python_project: Path) -> None:
    profile = _profile(python_project)
    assert "fastapi" in [item.identifier for item in profile.frameworks]
    assert "postgresql" in [item.identifier for item in profile.databases]


def test_test_framework_is_detected(python_project: Path) -> None:
    assert "pytest" in [item.identifier for item in _profile(python_project).test_frameworks]


def test_quality_and_ci_and_container_are_detected(python_project: Path) -> None:
    profile = _profile(python_project)
    assert "mypy" in [item.identifier for item in profile.quality_tools]
    assert "github-actions" in [item.identifier for item in profile.continuous_integration]
    assert "container" in [item.identifier for item in profile.containers]


def test_primary_language_is_the_most_frequent(python_project: Path) -> None:
    assert _profile(python_project).primary_language == "python"


def test_modules_are_top_level_directories(python_project: Path) -> None:
    modules = _profile(python_project).modules
    assert "src" in modules
    assert "tests" in modules
    assert not any(module.startswith(".") for module in modules)


def test_node_project_is_profiled(tmp_path: Path) -> None:
    root = tmp_path / "node-proje"
    _write(
        root,
        "package.json",
        json.dumps(
            {
                "name": "ornek",
                "dependencies": {"react": "^18", "pg": "^8"},
                "devDependencies": {"jest": "^29"},
            }
        ),
    )
    _write(root, "src/index.js", "console.log(1)\n")
    profile = build_profile(root, discover(root))
    assert "node-npm" in [item.identifier for item in profile.build_systems]
    assert "react" in [item.identifier for item in profile.frameworks]
    assert "jest" in [item.identifier for item in profile.test_frameworks]
    assert "postgresql" in [item.identifier for item in profile.databases]


def test_maven_project_is_profiled(tmp_path: Path) -> None:
    root = tmp_path / "java-proje"
    _write(
        root,
        "pom.xml",
        "<project><dependencies>"
        "<dependency><artifactId>spring-boot-starter-web</artifactId></dependency>"
        "<dependency><artifactId>junit-jupiter</artifactId></dependency>"
        "<dependency><artifactId>ojdbc11</artifactId></dependency>"
        "</dependencies></project>",
    )
    _write(root, "src/main/java/Ana.java", "class Ana {}\n")
    profile = build_profile(root, discover(root))
    assert "java-maven" in [item.identifier for item in profile.build_systems]
    assert "spring-boot" in [item.identifier for item in profile.frameworks]
    assert "junit" in [item.identifier for item in profile.test_frameworks]
    assert "oracle" in [item.identifier for item in profile.databases]


def test_plsql_sources_imply_oracle(tmp_path: Path) -> None:
    root = tmp_path / "plsql-proje"
    _write(root, "paket.pks", "create or replace package p as end;\n")
    _write(root, "paket.pkb", "create or replace package body p as end;\n")
    profile = build_profile(root, discover(root))
    assert "oracle" in [item.identifier for item in profile.databases]


def test_empty_project_produces_empty_but_valid_profile(tmp_path: Path) -> None:
    root = tmp_path / "bos"
    root.mkdir()
    profile = build_profile(root, discover(root))
    assert profile.file_count == 0
    assert profile.languages == ()
    assert profile.primary_language is None
    assert profile.digest.startswith("sha256:")


def test_profile_roundtrips_through_mapping(python_project: Path) -> None:
    profile = _profile(python_project)
    restored = profile_from_mapping(profile.body())
    assert restored.digest == profile.digest
    assert restored == profile


def test_unknown_dependency_produces_no_guess(tmp_path: Path) -> None:
    root = tmp_path / "bilinmeyen"
    _write(root, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["cok-ozel-kutuphane"]\n')
    profile = build_profile(root, discover(root))
    assert profile.frameworks == ()
    assert profile.databases == ()
