from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_package_acceptance_workflow_has_cross_platform_artifact_and_container_gates() -> None:
    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "package-acceptance.yml").read_text(encoding="utf-8")
    )
    jobs = document["jobs"]

    assert set(jobs) >= {
        "build-artifacts",
        "wheel-smoke",
        "sdist-smoke",
        "dependency-license-sbom",
        "container-smoke",
        "database-rehearsal",
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
        "container-smoke",
        "database-rehearsal",
        "evidence-bundle",
    }
    assert "actions/download-artifact@v4" in json.dumps(jobs["evidence-bundle"])
    assert "package_evidence_bundle.py" in json.dumps(jobs["evidence-bundle"])
    container = json.dumps(jobs["container-smoke"])
    assert "schema_bundle_digest" in container
    assert "io.zekam.protocol.schema-digest" in container
    assert "readyz_without_database" in container
    assert "fail-closed-503" in container

    rehearsal = jobs["database-rehearsal"]
    rendered = str(rehearsal)
    assert "upgrade-rollback" in rendered
    assert "pg_dump" in rendered
    assert "pg_restore" in rendered
    assert "pgvector/pgvector:pg18" in rendered
    assert "verify-restored" in rendered

    quality = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    )
    postgres_env = quality["jobs"]["postgres-acceptance"]["env"]
    assert postgres_env["PGPASSWORD"]
    assert postgres_env["ZEKAM_DATABASE_PASSWORD"] == postgres_env["PGPASSWORD"]
    assert "ZEKAM_TEST_DATABASE_PASSWORD" not in postgres_env
    default_config = yaml.safe_load(
        (ROOT / "config" / "zekam.default.yaml").read_text(encoding="utf-8")
    )
    canonical_port = str(default_config["database"]["port"])
    service_ports = quality["jobs"]["postgres-acceptance"]["services"]["postgres"]["ports"]
    assert postgres_env["ZEKAM_TEST_DATABASE_PORT"] == canonical_port
    assert service_ports == [f"{canonical_port}:5432"]
    postgres_steps = json.dumps(quality["jobs"]["postgres-acceptance"]["steps"])
    assert "zekam db upgrade --uygula" in postgres_steps
    assert postgres_steps.index("zekam db upgrade --uygula") < postgres_steps.index(
        "python -m pytest -m postgres"
    )


def test_runtime_container_installs_wheel_and_does_not_copy_source() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM python:3.12-slim AS runtime", maxsplit=1)[1]

    assert "--no-index --find-links=/wheelhouse" in runtime
    assert "USER zekam" in runtime
    assert "COPY src" not in runtime
    assert "/healthz" in runtime
