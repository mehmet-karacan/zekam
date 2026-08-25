from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.package_evidence_bundle import REQUIRED_GROUPS, build_bundle

from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.package_acceptance import (
    AcceptanceStatus,
    PackageAcceptanceResult,
    PackageAcceptanceRun,
    PackageVerifierProvenance,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def _acceptance(kind: str, artifact_digest: str, revision: str) -> dict[str, object]:
    result = PackageAcceptanceResult(
        check_id="cli.version",
        status=AcceptanceStatus.PASSED,
        command_digest=digest("command"),
        stdout_digest=digest("stdout"),
        stderr_digest=digest("stderr"),
        duration_ms=1,
    )
    provenance = PackageVerifierProvenance(
        builder_assignment_id=uuid4(),
        builder_invocation_id=uuid4(),
        builder_execution_identity="builder-execution",
        builder_envelope_digest=digest("builder-envelope"),
        verifier_assignment_id=uuid4(),
        verifier_invocation_id=uuid4(),
        verifier_execution_identity="verifier-execution",
        verifier_envelope_digest=digest("verifier-envelope"),
        verifier_source_digest=digest("verifier-source"),
    )
    run = PackageAcceptanceRun(
        id=uuid4(),
        manifest_digest=digest("manifest"),
        artifact_digest=artifact_digest,
        artifact_kind=kind,
        source_revision=revision,
        suite_digest=digest("suite"),
        platform="test-platform",
        python_version="3.12.9",
        builder_identity="builder-agent",
        verifier_identity="verifier-agent",
        verifier_provenance=provenance,
        started_at=NOW,
        completed_at=NOW + dt.timedelta(seconds=1),
        results=(result,),
    )
    return run.body() | {"run_digest": run.run_digest}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_required(root: Path, revision: str = "abc123") -> None:
    dist = root / "zekam-distribution"
    dist.mkdir(parents=True)
    wheel = dist / "zekam-0.1.0-py3-none-any.whl"
    sdist = dist / "zekam-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel-bytes")
    sdist.write_bytes(b"sdist-bytes")
    wheel_digest = digest_of_bytes(wheel.read_bytes())
    sdist_digest = digest_of_bytes(sdist.read_bytes())
    (dist / "SHA256SUMS").write_text(
        f"{wheel_digest.removeprefix('sha256:')}  {wheel.name}\n"
        f"{sdist_digest.removeprefix('sha256:')}  {sdist.name}\n",
        encoding="ascii",
    )
    for platform_name in ("ubuntu-latest", "windows-latest", "macos-latest"):
        _write_json(
            root / f"package-acceptance-{platform_name}" / "package-acceptance.json",
            _acceptance("wheel", wheel_digest, revision),
        )
    _write_json(
        root / "sdist-acceptance" / "sdist-acceptance.json",
        _acceptance("sdist", sdist_digest, revision),
    )
    container = {
        "schema": "zekam-container-acceptance/v1",
        "source_revision": revision,
        "image_digest": digest("image"),
        "protocol_schema_digest": digest("protocol"),
        "user": "zekam",
        "installed_wheel_only": True,
        "healthz": "passed",
        "readyz_without_database": "fail-closed-503",
        "grants_authority": False,
    }
    _write_json(
        root / "container-acceptance" / "container-acceptance.json",
        container | {"receipt_digest": digest(container)},
    )
    state = {
        "head": 54,
        "applied_count": 54,
        "migration_digest": digest("migrations"),
        "is_current": True,
    }
    base = {
        "schema": "zekam-package-database-rehearsal/v1",
        "artifact_install": "wheel",
        "grants_authority": False,
        "state": state,
    }
    _write_json(
        root / "package-database-rehearsal" / "migration-rehearsal.json",
        base | {"phase": "upgrade-rollback-reapply", "reapply_count": 1},
    )
    _write_json(
        root / "package-database-rehearsal" / "restore-rehearsal.json",
        base | {"phase": "backup-restore-verify"},
    )
    _write_json(
        root / "zekam-sbom" / "sbom.cdx.json",
        {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []},
    )


def test_bundle_semantically_binds_every_required_receipt(tmp_path: Path) -> None:
    _write_required(tmp_path)
    document = build_bundle(tmp_path, "abc123", tmp_path / "bundle.json")

    body = {key: value for key, value in document.items() if key != "bundle_digest"}
    assert document["bundle_digest"] == digest(body)
    assert len(document["groups"]) == len(REQUIRED_GROUPS)  # type: ignore[arg-type]


def test_bundle_fails_closed_for_named_but_empty_evidence(tmp_path: Path) -> None:
    _write_required(tmp_path)
    target = tmp_path / "package-acceptance-windows-latest/package-acceptance.json"
    target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="semantic/digest drift"):
        build_bundle(tmp_path, "abc123", tmp_path / "bundle.json")


@pytest.mark.parametrize("tamper", ["checksum", "status", "revision", "container"])
def test_bundle_rejects_cross_evidence_tamper(tmp_path: Path, tamper: str) -> None:
    _write_required(tmp_path)
    if tamper == "checksum":
        wheel = tmp_path / "zekam-distribution/zekam-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"tampered")
    elif tamper in {"status", "revision"}:
        target = tmp_path / "package-acceptance-ubuntu-latest/package-acceptance.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document[tamper if tamper == "status" else "source_revision"] = "failed"
        _write_json(target, document)
    else:
        target = tmp_path / "container-acceptance/container-acceptance.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document["readyz_without_database"] = "passed"
        _write_json(target, document)

    with pytest.raises(RuntimeError):
        build_bundle(tmp_path, "abc123", tmp_path / "bundle.json")
