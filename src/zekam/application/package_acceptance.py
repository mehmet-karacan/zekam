"""Build and verify package manifest v2 from shipped resources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from zekam import __version__
from zekam.application.config import package_root
from zekam.application.opencode_agent_bootstrap import opencode_template_bundle
from zekam.domain.app_server_protocol import schema_bundle_digest
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.package_acceptance import PackageManifestV2

PACKAGE_MANIFEST_NAME = "PACKAGE_RELEASE_MANIFEST.json"


def _file_bundle(root: Path, pattern: str = "*") -> str:
    if not root.is_dir():
        raise ValidationFailed(f"Package bundle bulunamadi: {root.name}")
    entries = []
    for path in sorted(item for item in root.rglob(pattern) if item.is_file()):
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


def build_package_manifest(root: Path | None = None) -> PackageManifestV2:
    """Derive the v2 manifest from the actual package resource root."""

    resource_root = (root or package_root()).resolve()
    repository_root = resource_root.parents[1] if resource_root.name == "zekam" else None
    migration_root = resource_root / "migrations"
    config_root = resource_root / "_config"
    model_root = resource_root / "modeller"
    if repository_root is not None and (repository_root / "pyproject.toml").is_file():
        migration_root = repository_root / "migrations"
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
        "manifest_generator": "zekam-package-manifest/v2",
    }
    return PackageManifestV2(
        version=__version__,
        entrypoints=("zekam",),
        python=requires_python,
        included_migrations_digest=_file_bundle(migration_root, "*.sql"),
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


def load_package_manifest(root: Path | None = None) -> PackageManifestV2:
    path = manifest_path(root)
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Shipped package manifest okunamadi") from exc
    return PackageManifestV2.parse(document)


def verify_package_manifest(root: Path | None = None) -> PackageManifestV2:
    shipped = load_package_manifest(root)
    current = build_package_manifest(root)
    if shipped.body() != current.body():
        raise ValidationFailed("Shipped package manifest component drift")
    return shipped
