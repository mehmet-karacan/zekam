from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application import capability_profile as profile

pytestmark = pytest.mark.unit


def test_read_and_dependency_dispatch_cover_missing_oversize_and_all_manifest_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert profile._read_text(tmp_path, "missing.txt") is None
    assert profile._dependency_names(tmp_path, "missing.manifest") == ()
    oversized = tmp_path / "oversized.txt"
    oversized.write_text("xx", encoding="utf-8")
    monkeypatch.setattr(profile, "MAX_MANIFEST_BYTES", 1)
    assert profile._read_text(tmp_path, "oversized.txt") is None
    monkeypatch.setattr(profile, "MAX_MANIFEST_BYTES", 1024)

    manifests = {
        "requirements.txt": "pytest>=8\n",
        "pom.xml": "<artifactId>spring-boot-starter-web</artifactId>",
        "build.gradle": "implementation 'org.junit:junit:4.0'",
        "build.gradle.kts": 'implementation("org.junit:junit:4.0")',
        "Cargo.toml": '[dependencies]\naxum = "1"\n',
        "go.mod": "require github.com/gin-gonic/gin v1.0.0\n",
        "unknown.lock": "ignored",
    }
    for name, body in manifests.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
        result = profile._dependency_names(tmp_path, name)
        if name != "unknown.lock":
            assert result
        else:
            assert result == ()


def test_python_project_parser_covers_optional_poetry_and_tool_metadata() -> None:
    parsed = profile._python_pyproject_dependencies(
        """
[project]
dependencies = ["FastAPI>=1"]
[project.optional-dependencies]
test = ["pytest>=8"]
[tool.poetry.dependencies]
python = "^3.12"
django = "^5"
[tool.poetry.dev-dependencies]
tox = "^4"
"""
    )
    assert {"fastapi", "pytest", "django", "tox", "tool:poetry"} <= set(parsed)
    assert profile._python_pyproject_dependencies("not = [valid") == ()
    assert profile._python_pyproject_dependencies("tool = 'scalar'") == ()


def test_requirements_package_and_language_parsers_cover_skip_empty_and_match_paths() -> None:
    assert profile._requirements_dependencies("\n# comment\n-r base.txt\nDjango[extra]>=5\n") == (
        "django",
    )
    assert profile._package_json_dependencies("not-json") == ()
    assert set(
        profile._package_json_dependencies(
            '{"dependencies":{"React":"1"},"devDependencies":{"Jest":"1"},'
            '"peerDependencies":{"Vue":"1"}}'
        )
    ) == {"react", "jest", "vue"}

    jvm = profile._jvm_dependencies(
        "<artifactId>quarkus-core</artifactId> implementation 'org.junit:junit:4.0'"
    )
    assert "quarkus-core" in jvm and "org.junit:junit" in jvm and "junit" in jvm

    cargo = profile._cargo_dependencies(
        """
[dependencies]
axum = "1"
[dev-dependencies]
tokio = "1"
[build-dependencies]
cc = "1"
"""
    )
    assert set(cargo) == {"axum", "tokio", "cc"}
    assert profile._cargo_dependencies("not = [valid") == ()

    go = profile._go_dependencies(
        "// ignored\nrequire github.com/gin-gonic/gin v1.0.0\ngolang.org/x/sync v0.1.0"
    )
    assert "github.com/gin-gonic/gin" in go
    assert "gin-gonic/gin" in go
    assert "golang.org/x/sync" in go
