"""Artifact-only clean-environment acceptance for a shipped Zekam wheel.

The harness never imports from the source tree.  It creates an external temporary
home and virtual environment, strips user/Python/shell/proxy injection variables,
installs the exact wheel, runs the public CLI, and emits digest-only evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from zekam.application.package_acceptance import (
    ARCHIVE_ONLY_WHEEL_PATHS,
    _parse_wheel_exclusion_policy,
    _strict_json_document,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.package_acceptance import (
    AcceptanceStatus,
    PackageAcceptanceResult,
    PackageAcceptanceRun,
    PackageManifestV3,
    PackageVerifierProvenance,
)

_STRIP = {
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
}
_IGNORED_PACKAGE_PARTS = frozenset({"__pycache__"})
_IGNORED_PACKAGE_NAMES = frozenset({".DS_Store"})


def _safe_archive_path(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValueError("Package archive path gecersiz")
    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts.pop()
    if (
        not raw_parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw_parts[0].endswith(":")
    ):
        raise ValueError("Package archive traversal girdisi reddedildi")
    return tuple(raw_parts)


def _add_archive_entry(entries: dict[str, str], path: str, payload: bytes) -> None:
    if (
        any(part in _IGNORED_PACKAGE_PARTS for part in Path(path).parts)
        or Path(path).name in _IGNORED_PACKAGE_NAMES
        or Path(path).suffix in {".pyc", ".pyo"}
    ):
        return
    folded = path.casefold()
    if path in entries or any(existing.casefold() == folded for existing in entries):
        raise ValueError(f"Package source duplicate/case-collision: {path}")
    entries[path] = digest_of_bytes(payload)


def _entry_bundle_digest(entries: dict[str, str]) -> str:
    if not entries:
        raise ValueError("Package source bundle bos")
    return digest(
        [
            {"path": path, "content_digest": content_digest}
            for path, content_digest in sorted(entries.items())
        ]
    )


def _wheel_manifest(wheel: Path) -> PackageManifestV3:
    entries: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        names: set[str] = set()
        folded_names: set[str] = set()
        manifest_payload: bytes | None = None
        for member in archive.infolist():
            parts = _safe_archive_path(member.filename)
            normalized = "/".join(parts)
            folded = normalized.casefold()
            if normalized in names or folded in folded_names:
                raise ValueError(f"Wheel duplicate/case-collision entry: {normalized}")
            names.add(normalized)
            folded_names.add(folded)
            mode = (member.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise ValueError("Wheel symlink girdisi reddedildi")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("Wheel special-file girdisi reddedildi")
            if member.is_dir():
                continue
            payload = archive.read(member)
            if normalized == "zekam/PACKAGE_RELEASE_MANIFEST.json":
                manifest_payload = payload
            elif len(parts) > 1 and parts[0] == "zekam":
                target = "/".join(parts[1:])
                if target in ARCHIVE_ONLY_WHEEL_PATHS:
                    raise ValueError(f"Wheel archive-only source sevk ediyor: {target}")
                _add_archive_entry(entries, target, payload)
        if manifest_payload is None:
            raise ValueError("Wheel shipped package manifest icermiyor")
    manifest = PackageManifestV3.parse(_strict_json_document(manifest_payload))
    if manifest.package_source_bundle_digest != _entry_bundle_digest(entries):
        raise ValueError("Wheel package source bundle manifest ile uyusmuyor")
    return manifest


def isolated_environment(root: Path, executable_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() not in _STRIP}
    home = root / "user-home"
    appdata = home / "AppData" / "Roaming"
    xdg = home / ".config"
    for path in (home, appdata, xdg):
        path.mkdir(parents=True, exist_ok=True)
    existing_path = env.get("PATH", "")
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata),
            "XDG_CONFIG_HOME": str(xdg),
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PATH": str(executable_dir) + os.pathsep + existing_path,
        }
    )
    return env


def _portable_command(argv: Sequence[str], root: Path) -> list[str]:
    portable: list[str] = []
    root_text = str(root)
    for index, value in enumerate(argv):
        rendered = Path(value).name if index == 0 else value
        rendered = rendered.replace(root_text, "<acceptance-root>")
        portable.append(rendered)
    return portable


def _run_check(
    check_id: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    accepted: frozenset[int] = frozenset({0}),
) -> PackageAcceptanceResult:
    started = time.monotonic()
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        shell=False,
        check=False,
    )
    duration_ms = round((time.monotonic() - started) * 1000)
    return PackageAcceptanceResult(
        check_id=check_id,
        status=(
            AcceptanceStatus.PASSED if completed.returncode in accepted else AcceptanceStatus.FAILED
        ),
        command_digest=digest(
            {
                "argv": _portable_command(argv, cwd.parent),
                "accepted_exit_codes": sorted(accepted),
            }
        ),
        stdout_digest=digest_of_bytes(completed.stdout),
        stderr_digest=digest_of_bytes(completed.stderr),
        duration_ms=duration_ms,
        detail=None if completed.returncode in accepted else f"exit-{completed.returncode}",
    )


def _provenance(source_digest: str) -> PackageVerifierProvenance:
    """Create receipt identities to be materialized before ledger persistence."""

    return PackageVerifierProvenance(
        builder_assignment_id=uuid4(),
        builder_invocation_id=uuid4(),
        builder_execution_identity=f"package-builder-{uuid4()}",
        builder_envelope_digest=digest("package-builder-result"),
        verifier_assignment_id=uuid4(),
        verifier_invocation_id=uuid4(),
        verifier_execution_identity=f"package-verifier-{uuid4()}",
        verifier_envelope_digest=digest("package-verifier-result"),
        verifier_source_digest=source_digest,
    )


def accept_wheel(
    wheel: Path,
    *,
    wheelhouse: Path | None,
    allow_index: bool,
    source_revision: str,
    builder_identity: str,
    verifier_identity: str,
    verifier_provenance_digest: str,
) -> PackageAcceptanceRun:
    wheel = wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        raise ValueError("--wheel exact .whl artifact istemeli")
    if wheelhouse is None and not allow_index:
        raise ValueError("Network-free acceptance icin --wheelhouse veya --allow-index gerekli")
    packaged_manifest = _wheel_manifest(wheel)
    started_at = dt.datetime.now(dt.UTC)
    with tempfile.TemporaryDirectory(prefix="zekam-package-acceptance-") as temporary:
        root = Path(temporary).resolve()
        work = root / "work"
        venv = root / "venv"
        zekam_home = root / "zekam-home"
        work.mkdir()
        subprocess.run(
            [sys.executable, "-I", "-m", "venv", str(venv)],
            cwd=work,
            stdin=subprocess.DEVNULL,
            check=True,
        )
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        zekam = scripts / ("zekam.exe" if os.name == "nt" else "zekam")
        env = isolated_environment(root, scripts)
        pip_argv = [str(python), "-I", "-m", "pip", "--isolated", "install"]
        if wheelhouse is not None:
            pip_argv += ["--no-index", "--find-links", str(wheelhouse.resolve(strict=True))]
        pip_argv.append(str(wheel))
        results = [
            _run_check("install.wheel", pip_argv, cwd=work, env=env),
        ]
        if results[-1].status is AcceptanceStatus.PASSED:
            import_check = (
                "import json,pathlib,zekam;"
                "from zekam.application.package_acceptance import verify_package_manifest;"
                "p=pathlib.Path(zekam.__file__).resolve();"
                "m=verify_package_manifest();"
                "print(json.dumps({'installed':'.zip' in str(p) or 'site-packages' in str(p),"
                "'manifest_digest':m.manifest_digest,'path_name':p.name},sort_keys=True));"
                "raise SystemExit(0 if ('site-packages' in str(p) or '.zip' in str(p)) else 9)"
            )
            commands: tuple[tuple[str, Sequence[str], frozenset[int]], ...] = (
                ("artifact.resources", [str(python), "-I", "-c", import_check], frozenset({0})),
                ("cli.version", [str(zekam), "--version"], frozenset({0})),
                ("cli.help", [str(zekam), "--help"], frozenset({0})),
                (
                    "init.sqlite",
                    [
                        str(zekam),
                        "init",
                        "--home",
                        str(zekam_home),
                        "--persistence",
                        "sqlite",
                    ],
                    frozenset({0}),
                ),
                (
                    "cli.doctor",
                    [str(zekam), "doctor", "--json", "--home", str(zekam_home)],
                    frozenset({0, 1}),
                ),
                (
                    "cli.init-dry-run",
                    [str(zekam), "init", "--dry-run", "--home", str(zekam_home)],
                    frozenset({0}),
                ),
                (
                    "cli.db-status",
                    [str(zekam), "db", "status", "--json", "--home", str(zekam_home)],
                    frozenset({0}),
                ),
                (
                    "cli.protocol-digest",
                    [str(zekam), "protocol", "digest", "--json"],
                    frozenset({0}),
                ),
                (
                    "cli.permission-profile-list",
                    [str(zekam), "permission", "profile", "list", "--json"],
                    frozenset({0}),
                ),
                (
                    "cli.work-resume-empty",
                    [str(zekam), "work", "resume", "--json", "--home", str(zekam_home)],
                    frozenset({0}),
                ),
                (
                    "opencode.bootstrap-plan",
                    [str(zekam), "init", "--dry-run", "--home", str(zekam_home)],
                    frozenset({0}),
                ),
            )
            results.extend(
                _run_check(check_id, argv, cwd=work, env=env, accepted=accepted)
                for check_id, argv, accepted in commands
            )
        suite_digest = digest(
            {
                "schema": "zekam-package-acceptance-suite/v1",
                "checks": [
                    item.check_id for item in sorted(results, key=lambda item: item.check_id)
                ],
                "environment_stripped": sorted(_STRIP),
            }
        )
        completed_at = dt.datetime.now(dt.UTC)
        return PackageAcceptanceRun(
            id=uuid4(),
            manifest_digest=packaged_manifest.manifest_digest,
            artifact_digest=digest_of_bytes(wheel.read_bytes()),
            artifact_kind="wheel",
            source_revision=source_revision,
            suite_digest=suite_digest,
            platform=f"{platform.system().lower()}-{platform.machine().lower()}",
            python_version=platform.python_version(),
            builder_identity=builder_identity,
            verifier_identity=verifier_identity,
            verifier_provenance=_provenance(verifier_provenance_digest),
            started_at=started_at,
            completed_at=completed_at,
            results=tuple(sorted(results, key=lambda item: item.check_id)),
        )


def _sdist_manifest(sdist: Path) -> PackageManifestV3:
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        names: set[str] = set()
        folded_names: set[str] = set()
        for member in members:
            parts = _safe_archive_path(member.name)
            normalized = "/".join(parts)
            folded = normalized.casefold()
            if normalized in names or folded in folded_names:
                raise ValueError(f"Sdist duplicate/case-collision entry: {normalized}")
            names.add(normalized)
            folded_names.add(folded)
            if member.issym() or member.islnk():
                raise ValueError("Sdist traversal/link girdisi reddedildi")
            if not (member.isdir() or member.isfile()):
                raise ValueError("Sdist special-file girdisi reddedildi")
        matches = [
            member
            for member in members
            if member.name.endswith("/src/zekam/PACKAGE_RELEASE_MANIFEST.json")
        ]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError("Sdist exact package manifest icermiyor")
        stream = archive.extractfile(matches[0])
        if stream is None:
            raise ValueError("Sdist package manifest okunamadi")
        manifest = PackageManifestV3.parse(_strict_json_document(stream.read()))
        manifest_parts = _safe_archive_path(matches[0].name)
        source_prefix = manifest_parts[:-3]
        pyproject_matches = [
            member
            for member in members
            if _safe_archive_path(member.name) == (*source_prefix, "pyproject.toml")
        ]
        if len(pyproject_matches) != 1 or not pyproject_matches[0].isfile():
            raise ValueError("Sdist exact pyproject exclusion policy icermiyor")
        policy_stream = archive.extractfile(pyproject_matches[0])
        if policy_stream is None:
            raise ValueError("Sdist pyproject exclusion policy okunamadi")
        try:
            exclusions = _parse_wheel_exclusion_policy(policy_stream.read())
        except ValidationFailed as exc:
            raise ValueError("Sdist wheel exclusion policy gecersiz") from exc
        entries: dict[str, str] = {}
        excluded_seen: set[str] = set()
        for member in members:
            if not member.isfile() or member is matches[0]:
                continue
            parts = _safe_archive_path(member.name)
            target: str | None = None
            if parts[: len(source_prefix) + 2] == (*source_prefix, "src", "zekam"):
                relative = parts[len(source_prefix) + 2 :]
                if relative:
                    target = "/".join(relative)
            elif parts[: len(source_prefix) + 1] == (*source_prefix, "config"):
                relative = parts[len(source_prefix) + 1 :]
                if relative:
                    target = "/".join(("_config", *relative))
            elif parts[: len(source_prefix) + 1] == (*source_prefix, "schemas"):
                relative = parts[len(source_prefix) + 1 :]
                if relative:
                    target = "/".join(("schemas", *relative))
            elif parts[: len(source_prefix) + 1] == (*source_prefix, "modeller"):
                relative = parts[len(source_prefix) + 1 :]
                if relative:
                    target = "/".join(("modeller", *relative))
            elif parts == (*source_prefix, "AKTIF_GOREV.md"):
                target = "AKTIF_GOREV.md"
            if target is not None:
                if target in exclusions:
                    excluded_seen.add(target)
                    continue
                payload = archive.extractfile(member)
                if payload is None:
                    raise ValueError("Sdist package source girdisi okunamadi")
                _add_archive_entry(entries, target, payload.read())
        if excluded_seen != set(exclusions):
            raise ValueError("Sdist reviewed wheel exclusion hedeflerinin tumunu icermiyor")
        if manifest.package_source_bundle_digest != _entry_bundle_digest(entries):
            raise ValueError("Sdist package source bundle manifest ile uyusmuyor")
        return manifest


def accept_sdist(
    sdist: Path,
    *,
    wheelhouse: Path | None,
    allow_index: bool,
    source_revision: str,
    builder_identity: str,
    verifier_identity: str,
    verifier_provenance_digest: str,
) -> PackageAcceptanceRun:
    """Build a wheel from the shipped sdist, then execute the same artifact-only suite."""

    sdist = sdist.resolve(strict=True)
    if not sdist.name.endswith(".tar.gz"):
        raise ValueError("--sdist exact .tar.gz artifact istemeli")
    if wheelhouse is None and not allow_index:
        raise ValueError("Network-free acceptance icin --wheelhouse veya --allow-index gerekli")
    packaged_manifest = _sdist_manifest(sdist)
    started_at = dt.datetime.now(dt.UTC)
    with tempfile.TemporaryDirectory(prefix="zekam-sdist-acceptance-") as temporary:
        root = Path(temporary).resolve()
        wheel_dir = root / "wheel"
        wheel_dir.mkdir()
        env = isolated_environment(root, Path(sys.executable).resolve().parent)
        build_argv = [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ]
        if wheelhouse is not None:
            build_argv += ["--no-index", "--find-links", str(wheelhouse.resolve(strict=True))]
        build_argv.append(str(sdist))
        build_result = _run_check(
            "sdist.build-wheel",
            build_argv,
            cwd=root,
            env=env,
        )
        wheels = tuple(wheel_dir.glob("*.whl"))
        if build_result.status is AcceptanceStatus.FAILED or len(wheels) != 1:
            results: tuple[PackageAcceptanceResult, ...] = (build_result,)
            manifest_digest = packaged_manifest.manifest_digest
        else:
            wheel_run = accept_wheel(
                wheels[0],
                wheelhouse=wheelhouse,
                allow_index=allow_index,
                source_revision=source_revision,
                builder_identity=builder_identity,
                verifier_identity=verifier_identity,
                verifier_provenance_digest=verifier_provenance_digest,
            )
            if wheel_run.manifest_digest != packaged_manifest.manifest_digest:
                raise ValueError("Sdist ile uretilen wheel manifest digesti drift etti")
            results = tuple(
                sorted((build_result, *wheel_run.results), key=lambda item: item.check_id)
            )
            manifest_digest = wheel_run.manifest_digest
        completed_at = dt.datetime.now(dt.UTC)
        suite_digest = digest(
            {
                "schema": "zekam-package-acceptance-suite/v1",
                "checks": [item.check_id for item in results],
                "environment_stripped": sorted(_STRIP),
            }
        )
        return PackageAcceptanceRun(
            id=uuid4(),
            manifest_digest=manifest_digest,
            artifact_digest=digest_of_bytes(sdist.read_bytes()),
            artifact_kind="sdist",
            source_revision=source_revision,
            suite_digest=suite_digest,
            platform=f"{platform.system().lower()}-{platform.machine().lower()}",
            python_version=platform.python_version(),
            builder_identity=builder_identity,
            verifier_identity=verifier_identity,
            verifier_provenance=_provenance(verifier_provenance_digest),
            started_at=started_at,
            completed_at=completed_at,
            results=results,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    artifacts = parser.add_mutually_exclusive_group(required=True)
    artifacts.add_argument("--wheel", type=Path)
    artifacts.add_argument("--sdist", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--allow-index", action="store_true")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--builder-identity", required=True)
    parser.add_argument("--verifier-identity", required=True)
    parser.add_argument("--verifier-provenance-digest", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.wheel is not None:
        run = accept_wheel(
            args.wheel,
            wheelhouse=args.wheelhouse,
            allow_index=args.allow_index,
            source_revision=args.source_revision,
            builder_identity=args.builder_identity,
            verifier_identity=args.verifier_identity,
            verifier_provenance_digest=args.verifier_provenance_digest,
        )
    else:
        assert args.sdist is not None
        run = accept_sdist(
            args.sdist,
            wheelhouse=args.wheelhouse,
            allow_index=args.allow_index,
            source_revision=args.source_revision,
            builder_identity=args.builder_identity,
            verifier_identity=args.verifier_identity,
            verifier_provenance_digest=args.verifier_provenance_digest,
        )
    rendered = json.dumps(run.body() | {"run_digest": run.run_digest}, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    else:
        print(rendered)
    return 0 if run.status is AcceptanceStatus.PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
