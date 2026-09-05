from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_package_acceptance_workflow_is_cross_platform_and_serverless() -> None:
    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "package-acceptance.yml").read_text(encoding="utf-8")
    )
    jobs = document["jobs"]

    assert set(jobs) == {
        "build-artifacts",
        "wheel-smoke",
        "sdist-smoke",
        "dependency-license-sbom",
        "evidence-bundle",
        "package-gate",
    }
    assert jobs["wheel-smoke"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    assert set(jobs["package-gate"]["needs"]) == {
        "build-artifacts",
        "wheel-smoke",
        "sdist-smoke",
        "dependency-license-sbom",
        "evidence-bundle",
    }
    assert "actions/download-artifact@v4" in json.dumps(jobs["evidence-bundle"])
    assert "package_evidence_bundle.py" in json.dumps(jobs["evidence-bundle"])
    quality = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    )
    assert set(quality["jobs"]) == {"source-quality"}
    default_config = yaml.safe_load(
        (ROOT / "config" / "zekam.default.yaml").read_text(encoding="utf-8")
    )
    assert default_config["database"] == {
        "backend": "sqlite",
        "sqlite_relative_path": "state/operational.db",
    }
    source_steps = json.dumps(quality["jobs"]["source-quality"]["steps"])
    assert "python scripts/ci_pytest.py" in source_steps
    rendered = json.dumps({"package": document, "quality": quality}).casefold()
    assert "docker" not in rendered
    assert "pgvector" not in rendered
    assert "psycopg" not in rendered
