from __future__ import annotations

import datetime as dt
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.package_smoke import _sdist_manifest, isolated_environment

from zekam.application.package_acceptance import (
    _file_bundle,
    build_package_manifest,
    load_package_manifest,
    verify_package_manifest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.package_acceptance import (
    AcceptanceStatus,
    PackageAcceptanceResult,
    PackageAcceptanceRun,
    PackageManifestV2,
    PackageVerifierProvenance,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def _result(check_id: str = "cli.version") -> PackageAcceptanceResult:
    return PackageAcceptanceResult(
        check_id=check_id,
        status=AcceptanceStatus.PASSED,
        command_digest=digest("command"),
        stdout_digest=digest("stdout"),
        stderr_digest=digest("stderr"),
        duration_ms=3,
    )


def _run(**overrides: object) -> PackageAcceptanceRun:
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
    values: dict[str, object] = {
        "id": uuid4(),
        "manifest_digest": digest("manifest"),
        "artifact_digest": digest("wheel"),
        "artifact_kind": "wheel",
        "source_revision": "abc123",
        "suite_digest": digest("suite"),
        "platform": "windows-amd64",
        "python_version": "3.12.9",
        "builder_identity": "builder-a",
        "verifier_identity": "verifier-b",
        "verifier_provenance": provenance,
        "started_at": NOW,
        "completed_at": NOW + dt.timedelta(seconds=1),
        "results": (_result(),),
    }
    values.update(overrides)
    return PackageAcceptanceRun(**values)  # type: ignore[arg-type]


def test_tracked_manifest_exactly_matches_source_package_resources() -> None:
    package_root = ROOT / "src" / "zekam"
    shipped = load_package_manifest(package_root)
    current = build_package_manifest(package_root)

    assert shipped.body() == current.body()
    assert verify_package_manifest(package_root).manifest_digest == current.manifest_digest


def test_file_bundle_digest_is_newline_platform_independent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    target = bundle / "migration.sql"
    target.write_bytes(b"select 1;\r\nselect 2;\r\n")
    windows_digest = _file_bundle(bundle, "*.sql")
    target.write_bytes(b"select 1;\nselect 2;\n")

    assert _file_bundle(bundle, "*.sql") == windows_digest


def test_manifest_parser_is_exact_and_tamper_fails_verification(tmp_path: Path) -> None:
    package_root = ROOT / "src" / "zekam"
    document = load_package_manifest(package_root).body()
    document["unexpected"] = True
    with pytest.raises(ValidationFailed, match="exact v2"):
        PackageManifestV2.parse(document)

    fake_root = tmp_path / "zekam"
    fake_root.mkdir()
    (fake_root / "PACKAGE_RELEASE_MANIFEST.json").write_text(
        json.dumps(load_package_manifest(package_root).body() | {"version": "9.9.9"}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed):
        verify_package_manifest(fake_root)


def test_acceptance_run_requires_independent_verifier_and_sorted_checks() -> None:
    with pytest.raises(PolicyViolation, match="bagimsiz"):
        _run(builder_identity="same", verifier_identity="same")
    with pytest.raises(ValidationFailed, match="unique ve sirali"):
        _run(results=(_result("z.check"), _result("a.check")))


def test_verifier_provenance_rejects_shared_execution_and_is_canonical() -> None:
    with pytest.raises(PolicyViolation, match="execution identity"):
        PackageVerifierProvenance(
            builder_assignment_id=uuid4(),
            builder_invocation_id=uuid4(),
            builder_execution_identity="shared",
            builder_envelope_digest=digest("builder-envelope"),
            verifier_assignment_id=uuid4(),
            verifier_invocation_id=uuid4(),
            verifier_execution_identity="shared",
            verifier_envelope_digest=digest("verifier-envelope"),
            verifier_source_digest=digest("verifier-source"),
        )
    run = _run()
    assert run.body()["verifier_provenance_digest"] == digest(run.body()["verifier_provenance"])


def test_failed_check_makes_terminal_run_failed_without_authority() -> None:
    failed = PackageAcceptanceResult(
        check_id="cli.version",
        status=AcceptanceStatus.FAILED,
        command_digest=digest("command"),
        stdout_digest=digest("stdout"),
        stderr_digest=digest("stderr"),
        duration_ms=9,
        detail="exit-7",
    )
    run = _run(results=(failed,))

    assert run.status is AcceptanceStatus.FAILED
    assert run.body()["grants_authority"] is False
    assert run.run_digest.startswith("sha256:")


def test_isolated_environment_removes_user_python_shell_and_proxy_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "BASH_ENV",
        "ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "VIRTUAL_ENV",
        "ZEKAM_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "PIP_CONFIG_FILE",
    ):
        monkeypatch.setenv(key, "poison")
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)

    environment = isolated_environment(tmp_path, scripts)

    assert all(key not in environment for key in ("BASH_ENV", "PYTHONPATH", "ZEKAM_HOME"))
    assert all("PROXY" not in key.upper() for key in environment)
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["HOME"].startswith(str(tmp_path))


def test_sdist_traversal_and_links_are_rejected_before_build(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(ValueError, match="traversal"):
        _sdist_manifest(archive)
