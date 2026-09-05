from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.package_smoke import _sdist_manifest, _wheel_manifest, isolated_environment

from zekam.application.package_acceptance import (
    ARCHIVE_ONLY_WHEEL_PATHS,
    _file_bundle,
    _package_source_bundle,
    _parse_wheel_exclusion_policy,
    _strict_json_document,
    _wheel_exclusion_paths,
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
    PackageManifestV3,
    PackageVerifierProvenance,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def _exclusion_policy(entries: object | None = None) -> bytes:
    configured = (
        [f"/src/zekam/{path}" for path in sorted(ARCHIVE_ONLY_WHEEL_PATHS)]
        if entries is None
        else entries
    )
    rendered = json.dumps(configured, ensure_ascii=True)
    return f"[tool.hatch.build.targets.wheel]\nexclude = {rendered}\n".encode()


def _prepare_excluded_sources(repository: Path) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    (repository / "pyproject.toml").write_bytes(_exclusion_policy())
    for relative in ARCHIVE_ONLY_WHEEL_PATHS:
        target = repository / "src" / "zekam" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"archive-only\n")


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
    with pytest.raises(ValidationFailed, match="exact v3"):
        PackageManifestV3.parse(document)

    fake_root = tmp_path / "zekam"
    fake_root.mkdir()
    (fake_root / "PACKAGE_RELEASE_MANIFEST.json").write_text(
        json.dumps(load_package_manifest(package_root).body() | {"version": "9.9.9"}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed):
        verify_package_manifest(fake_root)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    (
        ("version", None),
        ("python", 312),
        ("entrypoints", None),
        ("entrypoints", ["zekam", 7]),
        ("local_schema_bundle_digest", None),
        ("package_source_bundle_digest", False),
    ),
)
def test_manifest_parser_rejects_wrong_types_and_nulls(field: str, bad_value: object) -> None:
    document = load_package_manifest(ROOT / "src" / "zekam").body()
    document[field] = bad_value

    with pytest.raises(ValidationFailed):
        PackageManifestV3.parse(document)


def test_manifest_json_rejects_duplicate_keys_and_non_object() -> None:
    with pytest.raises(ValidationFailed, match="yinelenen JSON"):
        _strict_json_document('{"schema":"first","schema":"second"}')
    with pytest.raises(ValidationFailed, match="JSON object"):
        _strict_json_document("[]")


def test_source_manifest_models_exact_forced_include_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    package = repository / "src" / "zekam"
    staged = tmp_path / "staged" / "zekam"
    package.mkdir(parents=True)
    staged.mkdir(parents=True)
    _prepare_excluded_sources(repository)
    (package / "module.py").write_bytes(b"module\r\n")
    (package / "PACKAGE_RELEASE_MANIFEST.json").write_bytes(b"recursive")
    for source_name, target_name in (
        ("config", "_config"),
        ("schemas", "schemas"),
        ("modeller", "modeller"),
    ):
        source = repository / source_name
        source.mkdir()
        (source / "resource.bin").write_bytes(source_name.encode())
        target = staged / target_name
        target.mkdir()
        (target / "resource.bin").write_bytes(source_name.encode())
    (repository / "AKTIF_GOREV.md").write_bytes(b"task")
    (staged / "module.py").write_bytes(b"module\r\n")
    (staged / "AKTIF_GOREV.md").write_bytes(b"task")
    (staged / "PACKAGE_RELEASE_MANIFEST.json").write_bytes(b"different-recursive-body")

    source_digest = _package_source_bundle(package, repository)
    assert source_digest == _package_source_bundle(staged)

    (staged / "module.py").write_bytes(b"tampered")
    assert _package_source_bundle(staged) != source_digest
    (staged / "module.py").unlink()
    assert _package_source_bundle(staged) != source_digest
    (staged / "unexpected.txt").write_bytes(b"extra")
    assert _package_source_bundle(staged) != source_digest


def test_source_bundle_rejects_symlink_and_case_collision(tmp_path: Path) -> None:
    package = tmp_path / "zekam"
    package.mkdir()
    (package / "module.py").write_bytes(b"ok")
    (package / "alias.py").symlink_to(package / "module.py")
    with pytest.raises(ValidationFailed, match="symlink"):
        _package_source_bundle(package)

    repository = tmp_path / "repository"
    _prepare_excluded_sources(repository)
    package = repository / "src" / "zekam"
    bundled_config = package / "_config"
    bundled_config.mkdir()
    (bundled_config / "NAME.yaml").write_bytes(b"first")
    config = repository / "config"
    config.mkdir(parents=True)
    (config / "name.yaml").write_bytes(b"collision")
    with pytest.raises(ValidationFailed, match="case-collision"):
        _package_source_bundle(package, repository)


def _manifest_for_source_entries(entries: dict[str, bytes]) -> dict[str, object]:
    document = load_package_manifest(ROOT / "src" / "zekam").body()
    document["package_source_bundle_digest"] = digest(
        [
            {"path": path, "content_digest": "sha256:" + hashlib.sha256(body).hexdigest()}
            for path, body in sorted(entries.items())
        ]
    )
    return document


def test_wheel_manifest_binds_exact_package_bytes_and_rejects_adversarial_entries(
    tmp_path: Path,
) -> None:
    source = {"module.py": b"exact-bytes", "data/value.bin": b"\x00\xff"}
    manifest = _manifest_for_source_entries(source)
    wheel = tmp_path / "valid.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, body in source.items():
            archive.writestr(f"zekam/{path}", body)
        archive.writestr("zekam/PACKAGE_RELEASE_MANIFEST.json", json.dumps(manifest))
    assert (
        _wheel_manifest(wheel).package_source_bundle_digest
        == manifest["package_source_bundle_digest"]
    )

    tampered = tmp_path / "tampered.whl"
    with zipfile.ZipFile(tampered, "w") as archive:
        archive.writestr("zekam/module.py", b"tampered")
        archive.writestr("zekam/data/value.bin", b"\x00\xff")
        archive.writestr("zekam/PACKAGE_RELEASE_MANIFEST.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="source bundle"):
        _wheel_manifest(tampered)

    duplicate = tmp_path / "duplicate.whl"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(duplicate, "w") as archive,
    ):
        archive.writestr("zekam/module.py", b"first")
        archive.writestr("zekam/module.py", b"second")
        archive.writestr("zekam/PACKAGE_RELEASE_MANIFEST.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="duplicate"):
        _wheel_manifest(duplicate)

    linked = tmp_path / "linked.whl"
    link = zipfile.ZipInfo("zekam/alias.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(link, "module.py")
        archive.writestr("zekam/PACKAGE_RELEASE_MANIFEST.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="symlink"):
        _wheel_manifest(linked)


def test_sdist_manifest_binds_virtual_wheel_tree(tmp_path: Path) -> None:
    archive_path = tmp_path / "valid.tar.gz"
    source = {
        "module.py": b"module",
        "_config/default.yaml": b"config",
        "schemas/schema.json": b"{}",
        "modeller/catalog.yaml": b"models",
        "AKTIF_GOREV.md": b"task",
    }
    manifest = _manifest_for_source_entries(source)
    archive_entries = {
        "pkg/pyproject.toml": _exclusion_policy(),
        "pkg/src/zekam/module.py": source["module.py"],
        "pkg/config/default.yaml": source["_config/default.yaml"],
        "pkg/schemas/schema.json": source["schemas/schema.json"],
        "pkg/modeller/catalog.yaml": source["modeller/catalog.yaml"],
        "pkg/AKTIF_GOREV.md": source["AKTIF_GOREV.md"],
        "pkg/src/zekam/PACKAGE_RELEASE_MANIFEST.json": json.dumps(manifest).encode(),
    }
    archive_entries.update(
        {f"pkg/src/zekam/{relative}": b"archive-only\n" for relative in ARCHIVE_ONLY_WHEEL_PATHS}
    )
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, body in archive_entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    assert (
        _sdist_manifest(archive_path).package_source_bundle_digest
        == manifest["package_source_bundle_digest"]
    )

    tampered = tmp_path / "tampered.tar.gz"
    archive_entries["pkg/src/zekam/module.py"] = b"changed"
    with tarfile.open(tampered, "w:gz") as archive:
        for name, body in archive_entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="source bundle"):
        _sdist_manifest(tampered)


@pytest.mark.parametrize(
    "payload",
    (
        _exclusion_policy(None).replace(b"exclude = [", b"exclude = 7 # [", 1),
        _exclusion_policy(["src/zekam/application/execution.py"]),
        _exclusion_policy(["/src/zekam/../execution.py"]),
        _exclusion_policy(["/src/zekam/application\\execution.py"]),
        _exclusion_policy(
            [
                *[f"/src/zekam/{path}" for path in sorted(ARCHIVE_ONLY_WHEEL_PATHS)],
                "/src/zekam/Application/execution.py",
            ]
        ),
        _exclusion_policy([]),
    ),
)
def test_wheel_exclusion_policy_rejects_wrong_type_path_case_and_drift(payload: bytes) -> None:
    with pytest.raises(ValidationFailed, match="Hatch wheel exclusion"):
        _parse_wheel_exclusion_policy(payload)


def test_wheel_exclusion_policy_and_targets_reject_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _prepare_excluded_sources(repository)
    policy = repository / "pyproject.toml"
    real_policy = repository / "real.toml"
    policy.replace(real_policy)
    policy.symlink_to(real_policy)
    with pytest.raises(ValidationFailed, match="regular-file"):
        _wheel_exclusion_paths(repository)

    policy.unlink()
    real_policy.replace(policy)
    relative = next(iter(ARCHIVE_ONLY_WHEEL_PATHS))
    target = repository / "src" / "zekam" / relative
    target.unlink()
    target.symlink_to(policy)
    with pytest.raises(ValidationFailed, match="hedefi regular-file"):
        _wheel_exclusion_paths(repository)


def test_wheel_rejects_archive_only_file_even_with_matching_digest(tmp_path: Path) -> None:
    relative = next(iter(ARCHIVE_ONLY_WHEEL_PATHS))
    source = {relative: b"legacy"}
    manifest = _manifest_for_source_entries(source)
    wheel = tmp_path / "legacy.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"zekam/{relative}", source[relative])
        archive.writestr("zekam/PACKAGE_RELEASE_MANIFEST.json", json.dumps(manifest))

    with pytest.raises(ValueError, match="archive-only"):
        _wheel_manifest(wheel)


def test_sdist_rejects_policy_drift_and_missing_excluded_source(tmp_path: Path) -> None:
    source = {"module.py": b"module"}
    manifest = _manifest_for_source_entries(source)
    base = {
        "pkg/pyproject.toml": _exclusion_policy(),
        "pkg/src/zekam/module.py": source["module.py"],
        "pkg/src/zekam/PACKAGE_RELEASE_MANIFEST.json": json.dumps(manifest).encode(),
        **{f"pkg/src/zekam/{relative}": b"archive-only" for relative in ARCHIVE_ONLY_WHEEL_PATHS},
    }
    drift = tmp_path / "drift.tar.gz"
    drift_entries = dict(base)
    drift_entries["pkg/pyproject.toml"] = _exclusion_policy([])
    with tarfile.open(drift, "w:gz") as archive:
        for name, body in drift_entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="policy gecersiz"):
        _sdist_manifest(drift)

    missing = tmp_path / "missing.tar.gz"
    missing_entries = dict(base)
    missing_entries.pop(f"pkg/src/zekam/{next(iter(ARCHIVE_ONLY_WHEEL_PATHS))}")
    with tarfile.open(missing, "w:gz") as archive:
        for name, body in missing_entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="hedeflerinin tumunu"):
        _sdist_manifest(missing)


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
