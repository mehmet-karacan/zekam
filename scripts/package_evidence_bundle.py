"""Build a deterministic, digest-only index for package acceptance artifacts."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest, digest_of_bytes

REQUIRED_GROUPS: dict[str, Callable[[Path], bool]] = {
    "distribution": lambda path: path.name == "SHA256SUMS",
    "wheel-linux": lambda path: (
        "ubuntu" in path.as_posix() and path.name == "package-acceptance.json"
    ),
    "wheel-windows": lambda path: (
        "windows" in path.as_posix() and path.name == "package-acceptance.json"
    ),
    "wheel-macos": lambda path: (
        "macos" in path.as_posix() and path.name == "package-acceptance.json"
    ),
    "sdist": lambda path: path.name == "sdist-acceptance.json",
    "container": lambda path: path.name == "container-acceptance.json",
    "migration": lambda path: path.name == "migration-rehearsal.json",
    "restore": lambda path: path.name == "restore-rehearsal.json",
    "sbom": lambda path: path.name == "sbom.cdx.json",
}
_SHA_LINE = re.compile(r"^([0-9a-f]{64})  ([^/\\]+)$")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON evidence: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Evidence root must be an object: {path.name}")
    return value


def _exact_group_files(root: Path, files: tuple[Path, ...]) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for group, predicate in REQUIRED_GROUPS.items():
        matches = [path for path in files if predicate(path.relative_to(root))]
        if len(matches) != 1:
            raise RuntimeError(f"Package evidence group {group} expected 1, found {len(matches)}")
        selected[group] = matches[0]
    return selected


def _validate_distribution(root: Path, checksum_file: Path) -> dict[str, str]:
    declared: dict[str, str] = {}
    for line in checksum_file.read_text(encoding="ascii").splitlines():
        match = _SHA_LINE.fullmatch(line)
        if match is None or match.group(2) in declared:
            raise RuntimeError("Distribution SHA256SUMS syntax/uniqueness drift")
        declared[match.group(2)] = f"sha256:{match.group(1)}"
    artifacts = tuple(
        sorted(
            (
                path
                for path in checksum_file.parent.iterdir()
                if path.is_file() and path != checksum_file
            ),
            key=lambda path: path.name,
        )
    )
    if set(declared) != {path.name for path in artifacts}:
        raise RuntimeError("Distribution SHA256SUMS file set drift")
    for path in artifacts:
        if declared[path.name] != digest_of_bytes(path.read_bytes()):
            raise RuntimeError(f"Distribution checksum drift: {path.name}")
    if (
        len([name for name in declared if name.endswith(".whl")]) != 1
        or len([name for name in declared if name.endswith(".tar.gz")]) != 1
    ):
        raise RuntimeError("Distribution must contain exact wheel and sdist")
    return declared


def _validate_acceptance(
    path: Path,
    *,
    kind: str,
    source_revision: str,
    artifact_digest: str,
) -> None:
    document = _json(path)
    run_digest = document.pop("run_digest", None)
    if (
        document.get("schema") != "zekam-package-acceptance-run/v1"
        or document.get("artifact_kind") != kind
        or document.get("artifact_digest") != artifact_digest
        or document.get("source_revision") != source_revision
        or document.get("status") != "passed"
        or document.get("isolated_environment") is not True
        or document.get("grants_authority") is not False
        or run_digest != digest(document)
    ):
        raise RuntimeError(f"Package acceptance semantic/digest drift: {path.name}")
    provenance = document.get("verifier_provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("schema") != "zekam-package-verifier-provenance/v1"
        or document.get("verifier_provenance_digest") != digest(provenance)
        or document.get("builder_identity") == document.get("verifier_identity")
        or provenance.get("builder_execution_identity")
        == provenance.get("verifier_execution_identity")
    ):
        raise RuntimeError(f"Package verifier provenance drift: {path.name}")
    results = document.get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError(f"Package acceptance results missing: {path.name}")
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError(f"Package result schema drift: {path.name}")
        supplied = result.get("result_digest")
        body = {key: value for key, value in result.items() if key != "result_digest"}
        if result.get("status") != "passed" or supplied != digest(body):
            raise RuntimeError(f"Package result failed/digest drift: {path.name}")


def _validate_container(path: Path, source_revision: str) -> None:
    document = _json(path)
    supplied = document.pop("receipt_digest", None)
    if (
        document.get("schema") != "zekam-container-acceptance/v1"
        or document.get("source_revision") != source_revision
        or document.get("user") != "zekam"
        or document.get("installed_wheel_only") is not True
        or document.get("healthz") != "passed"
        or document.get("readyz_without_database") != "fail-closed-503"
        or document.get("grants_authority") is not False
        or supplied != digest(document)
    ):
        raise RuntimeError("Container acceptance semantic/digest drift")


def _validate_database(migration_path: Path, restore_path: Path) -> None:
    documents = (_json(migration_path), _json(restore_path))
    states: list[dict[str, Any]] = []
    for document in documents:
        state = document.get("state")
        if (
            document.get("schema") != "zekam-package-database-rehearsal/v1"
            or document.get("artifact_install") != "wheel"
            or document.get("grants_authority") is not False
            or not isinstance(state, dict)
            or state.get("is_current") is not True
            or not isinstance(state.get("head"), int)
            or state.get("head") != state.get("applied_count")
        ):
            raise RuntimeError("Database rehearsal semantic drift")
        states.append(state)
    if states[0] != states[1] or documents[0].get("reapply_count") != 1:
        raise RuntimeError("Migration and restored database evidence drift")


def _validate_evidence(root: Path, files: tuple[Path, ...], source_revision: str) -> None:
    groups = _exact_group_files(root, files)
    declared = _validate_distribution(root, groups["distribution"])
    wheel_name = next(name for name in declared if name.endswith(".whl"))
    sdist_name = next(name for name in declared if name.endswith(".tar.gz"))
    for key in ("wheel-linux", "wheel-windows", "wheel-macos"):
        _validate_acceptance(
            groups[key],
            kind="wheel",
            source_revision=source_revision,
            artifact_digest=declared[wheel_name],
        )
    _validate_acceptance(
        groups["sdist"],
        kind="sdist",
        source_revision=source_revision,
        artifact_digest=declared[sdist_name],
    )
    _validate_container(groups["container"], source_revision)
    _validate_database(groups["migration"], groups["restore"])
    sbom = _json(groups["sbom"])
    if sbom.get("bomFormat") != "CycloneDX" or not isinstance(sbom.get("components"), list):
        raise RuntimeError("CycloneDX SBOM semantic drift")


def build_bundle(root: Path, source_revision: str, output: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    output = output.resolve()
    files = tuple(
        sorted(
            (path for path in root.rglob("*") if path.is_file() and path.resolve() != output),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )
    _validate_evidence(root, files, source_revision)
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "digest": digest_of_bytes(path.read_bytes()),
        }
        for path in files
    ]
    body: dict[str, object] = {
        "schema": "zekam-package-evidence-bundle/v1",
        "source_revision": source_revision,
        "groups": sorted(REQUIRED_GROUPS),
        "files": entries,
        "grants_authority": False,
    }
    return body | {"bundle_digest": digest(body)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    document = build_bundle(args.root, args.source_revision, args.output)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
