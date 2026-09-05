"""Build and verify the fail-closed manifest for shipped package resources."""

from __future__ import annotations

import json
import stat
import tomllib
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from zekam import __version__
from zekam.application.config import package_root
from zekam.application.opencode_agent_bootstrap import opencode_template_bundle
from zekam.domain.app_server_protocol import schema_bundle_digest
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.package_acceptance import PackageManifestV3

PACKAGE_MANIFEST_NAME = "PACKAGE_RELEASE_MANIFEST.json"
_IGNORED_PACKAGE_PARTS = frozenset({"__pycache__"})
_IGNORED_PACKAGE_NAMES = frozenset({".DS_Store"})
ARCHIVE_ONLY_WHEEL_PATHS = frozenset(
    {
        "application/client_lifecycle_composition.py",
        "application/client_lifecycle_continuity.py",
        "application/client_runtime_bootstrap.py",
        "application/diagnostic_trace_composition.py",
        "application/doctor_repair_runtime.py",
        "application/execution.py",
        "application/legacy_repository_provider.py",
        "application/lifecycle_runtime_template_prepare.py",
        "application/lifecycle_template_recovery.py",
        "application/measured_loop_runtime.py",
        "application/measured_loop_worker.py",
        "application/project_integration.py",
        "application/projection_close_runtime.py",
        "application/provider_contract_runner.py",
        "application/recovery_reconciliation.py",
        "application/resume_apply_service.py",
        "application/run_reconciliation.py",
        "application/work_graph.py",
        "application/worker.py",
    }
)


def _parse_wheel_exclusion_policy(payload: bytes) -> frozenset[str]:
    """Parse the one reviewed exact Hatch wheel exclusion set."""

    try:
        document = tomllib.loads(payload.decode("utf-8", errors="strict"))
        raw = document["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
    except (
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise ValidationFailed("Hatch wheel exclusion policy okunamadi") from exc
    if type(raw) is not list or any(type(value) is not str for value in raw):
        raise ValidationFailed("Hatch wheel exclusion policy metin listesi olmali")
    found: set[str] = set()
    folded: set[str] = set()
    prefix = "/src/zekam/"
    for value in raw:
        if (
            not value.startswith(prefix)
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value[len(prefix) :].split("/"))
        ):
            raise ValidationFailed("Hatch wheel exclusion path absolute ve canonical olmali")
        relative = PurePosixPath(value[len(prefix) :]).as_posix()
        if relative.casefold() in folded:
            raise ValidationFailed("Hatch wheel exclusion duplicate/case-collision")
        found.add(relative)
        folded.add(relative.casefold())
    if found != ARCHIVE_ONLY_WHEEL_PATHS:
        raise ValidationFailed("Hatch wheel exclusion policy reviewed exact set ile uyusmuyor")
    return frozenset(found)


def _wheel_exclusion_paths(repository_root: Path) -> frozenset[str]:
    policy = repository_root / "pyproject.toml"
    if policy.is_symlink() or not policy.is_file():
        raise ValidationFailed("Hatch wheel exclusion policy regular-file olmali")
    try:
        exclusions = _parse_wheel_exclusion_policy(policy.read_bytes())
    except OSError as exc:
        raise ValidationFailed("Hatch wheel exclusion policy okunamadi") from exc
    package_root = repository_root / "src" / "zekam"
    for relative in exclusions:
        target = package_root / relative
        if target.is_symlink() or not target.is_file():
            raise ValidationFailed(f"Hatch wheel exclusion hedefi regular-file olmali: {relative}")
    return exclusions


def _strict_json_document(payload: str | bytes) -> dict[str, Any]:
    """Decode one JSON object while rejecting duplicate keys and non-objects."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValidationFailed(f"Package manifest yinelenen JSON anahtari: {key}")
            document[key] = value
        return document

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Shipped package manifest okunamadi") from exc
    if not isinstance(value, dict):
        raise ValidationFailed("Shipped package manifest JSON object olmali")
    return value


def _file_bundle(root: Path, pattern: str = "*") -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValidationFailed(f"Package bundle bulunamadi: {root.name}")
    entries = []
    for path in sorted(root.rglob(pattern)):
        if path.is_symlink():
            raise ValidationFailed(f"Package bundle symlink iceremez: {path.name}")
        mode = path.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValidationFailed(f"Package bundle regular-file olmali: {path.name}")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content_digest": digest_of_bytes(
                    path.read_text(encoding="utf-8")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .encode("utf-8")
                ),
            }
        )
    if not entries:
        raise ValidationFailed(f"Package bundle bos: {root.name}")
    return digest(entries)


def _mapping_bundle(value: Mapping[str, str]) -> str:
    return digest(
        [
            {"path": path, "content_digest": digest_of_bytes(body.encode("utf-8"))}
            for path, body in sorted(value.items())
        ]
    )


def _add_package_tree(
    entries: dict[str, str],
    *,
    root: Path,
    destination: Path = Path(),
    exclude_manifest: bool = False,
    excluded: frozenset[str] = frozenset(),
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValidationFailed(f"Package source root bulunamadi: {root.name}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValidationFailed(f"Package source bundle symlink iceremez: {relative.as_posix()}")
        mode = path.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValidationFailed(
                f"Package source bundle regular-file olmali: {relative.as_posix()}"
            )
        target = destination / relative
        if target.as_posix() in excluded:
            continue
        if (
            any(part in _IGNORED_PACKAGE_PARTS for part in target.parts)
            or target.name in _IGNORED_PACKAGE_NAMES
            or target.suffix in {".pyc", ".pyo"}
            or (exclude_manifest and target == Path(PACKAGE_MANIFEST_NAME))
        ):
            continue
        portable = target.as_posix()
        folded = portable.casefold()
        if portable in entries or any(existing.casefold() == folded for existing in entries):
            raise ValidationFailed(f"Package source hedefi duplicate/case-collision: {portable}")
        entries[portable] = digest_of_bytes(path.read_bytes())


def _add_package_file(entries: dict[str, str], *, source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValidationFailed(f"Package source file bulunamadi: {source.name}")
    portable = destination.as_posix()
    if portable in entries or any(
        existing.casefold() == portable.casefold() for existing in entries
    ):
        raise ValidationFailed(f"Package source hedefi duplicate/case-collision: {portable}")
    entries[portable] = digest_of_bytes(source.read_bytes())


def _package_source_entries(
    root: Path, repository_root: Path | None = None
) -> list[dict[str, str]]:
    """Return the exact virtual wheel package tree, without its recursive manifest."""

    entries: dict[str, str] = {}
    if repository_root is not None:
        expected_root = (repository_root / "src" / "zekam").resolve()
        if root.resolve() != expected_root:
            raise ValidationFailed("Package source root repository binding ile uyusmuyor")
    exclusions = (
        _wheel_exclusion_paths(repository_root) if repository_root is not None else frozenset()
    )
    _add_package_tree(entries, root=root, exclude_manifest=True, excluded=exclusions)
    if repository_root is not None:
        _add_package_tree(entries, root=repository_root / "config", destination=Path("_config"))
        _add_package_tree(entries, root=repository_root / "schemas", destination=Path("schemas"))
        _add_package_tree(entries, root=repository_root / "modeller", destination=Path("modeller"))
        _add_package_file(
            entries,
            source=repository_root / "AKTIF_GOREV.md",
            destination=Path("AKTIF_GOREV.md"),
        )
    if not entries:
        raise ValidationFailed("Package source bundle bos")
    return [
        {"path": path, "content_digest": content_digest}
        for path, content_digest in sorted(entries.items())
    ]


def _package_source_bundle(root: Path, repository_root: Path | None = None) -> str:
    return digest(_package_source_entries(root, repository_root))


def build_package_manifest(root: Path | None = None) -> PackageManifestV3:
    """Derive the v3 manifest from the exact package resource tree."""

    resource_root = (root or package_root()).resolve()
    candidate_root = resource_root.parents[1] if resource_root.name == "zekam" else None
    repository_root = (
        candidate_root
        if candidate_root is not None and (candidate_root / "pyproject.toml").is_file()
        else None
    )
    schema_root = resource_root / "infrastructure" / "sqlite"
    config_root = resource_root / "_config"
    model_root = resource_root / "modeller"
    if repository_root is not None:
        schema_root = repository_root / "src" / "zekam" / "infrastructure" / "sqlite"
        config_root = repository_root / "config"
        model_root = repository_root / "modeller"
    try:
        project_metadata = metadata.metadata("zekam")
        requires_python = project_metadata.get("Requires-Python") or ">=3.12"
    except metadata.PackageNotFoundError:
        requires_python = ">=3.12"
    provenance = {
        "schema": "zekam-package-build-provenance/v1",
        "builder": "hatchling",
        "package": "zekam",
        "version": __version__,
        "python": requires_python,
        "manifest_generator": "zekam-package-manifest/v3",
    }
    return PackageManifestV3(
        version=__version__,
        entrypoints=("zekam",),
        python=requires_python,
        local_schema_bundle_digest=_file_bundle(schema_root, "*.py"),
        package_source_bundle_digest=_package_source_bundle(resource_root, repository_root),
        config_bundle_digest=digest(
            {
                "config": _file_bundle(config_root, "*"),
                "models": _file_bundle(model_root, "*"),
            }
        ),
        protocol_schema_digest=schema_bundle_digest(),
        agent_template_digest=_mapping_bundle(opencode_template_bundle()),
        build_provenance_digest=digest(provenance),
    )


def manifest_path(root: Path | None = None) -> Path:
    return (root or package_root()) / PACKAGE_MANIFEST_NAME


def load_package_manifest(root: Path | None = None) -> PackageManifestV3:
    path = manifest_path(root)
    try:
        document = _strict_json_document(path.read_bytes())
    except OSError as exc:
        raise ValidationFailed("Shipped package manifest okunamadi") from exc
    return PackageManifestV3.parse(document)


def verify_package_manifest(root: Path | None = None) -> PackageManifestV3:
    shipped = load_package_manifest(root)
    current = build_package_manifest(root)
    if shipped.body() != current.body():
        raise ValidationFailed("Shipped package manifest component drift")
    return shipped
